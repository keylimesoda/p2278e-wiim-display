"""Minimal appliance domain core for the P2278E WiiM display.

A tiny deterministic state machine. Pure reducer, explicit monotonic time,
no external dependencies, no I/O. The live orchestrator feeds it observations
and reads back a presentation snapshot to render.

Design principles
-----------------
* User-facing states are a *small* closed enum: STARTING, NOW_PLAYING, IDLE,
  SEARCHING. Everything else (paused, stale, transitioning, degraded art or
  analysis) is a boolean modifier so the state enum never explodes.
* Track identity is derived from stable metadata (title/artist/album/duration),
  never from an album-art or media URI that may carry expiring query tokens.
* Async work (art fetch, spectrum analysis) is owned by a (generation, track_id)
  pair; results are applied only when both still match the current track.
* Fetch failures preserve the last honest state/content. Content goes ``stale``
  after 12s of continuous failure and only falls back to SEARCHING after 45s.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Optional
import hashlib
import re

# --------------------------------------------------------------------------- #
# Timing thresholds (seconds). Explicit and testable.
# --------------------------------------------------------------------------- #
STARTUP_CONNECT_TIMEOUT_S = 18.0   # no first observation by here -> SEARCHING
STALE_AFTER_S = 12.0               # continuous fetch failure -> content stale
FAULT_SEARCHING_AFTER_S = 45.0     # continuous fetch failure -> SEARCHING
IDLE_CONFIRM_POLLS = 2             # consecutive stopped polls to enter IDLE

UNKNOWN_DURATION = -1              # explicit sentinel: duration is not known


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class UserState(Enum):
    STARTING = "STARTING"
    NOW_PLAYING = "NOW_PLAYING"
    IDLE = "IDLE"
    SEARCHING = "SEARCHING"


class Transport(Enum):
    """Normalized transport reported by an observation."""

    PLAYING = "PLAYING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    NO_MEDIA = "NO_MEDIA"
    TRANSITIONING = "TRANSITIONING"
    LOADING = "LOADING"
    UNKNOWN = "UNKNOWN"


# --------------------------------------------------------------------------- #
# Identity helpers
# --------------------------------------------------------------------------- #
def _norm(text: Optional[str]) -> str:
    """Normalize a metadata field for identity: lowercase, collapse spaces."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip().lower()


def track_id(
    title: Optional[str],
    artist: Optional[str],
    album: Optional[str],
    duration_s: int,
) -> str:
    """Deterministic stable identity from normalized metadata only.

    URIs (which may embed expiring auth/query tokens) are intentionally never
    part of the identity. Empty metadata yields the empty-identity sentinel.
    """
    if not any((_norm(title), _norm(artist), _norm(album))) and duration_s < 0:
        return ""
    dur = "?" if duration_s == UNKNOWN_DURATION else str(int(duration_s))
    basis = "\x1f".join((_norm(title), _norm(artist), _norm(album), dur))
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Observation: the single normalized input to the reducer
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Observation:
    """A normalized poll result at monotonic time ``at``.

    ``ok`` False means the poll/fetch itself failed (network/transport error);
    the reducer then preserves prior honest content rather than blanking.
    """

    at: float
    ok: bool = True
    transport: Transport = Transport.UNKNOWN
    title: str = ""
    artist: str = ""
    album: str = ""
    duration_s: int = UNKNOWN_DURATION
    art_uri: str = ""

    @staticmethod
    def failure(at: float) -> "Observation":
        return Observation(at=at, ok=False)

    @property
    def is_playing_like(self) -> bool:
        return self.transport in (
            Transport.PLAYING,
            Transport.PAUSED,
            Transport.LOADING,
            Transport.TRANSITIONING,
        )

    @property
    def is_stopped_like(self) -> bool:
        return self.transport in (Transport.STOPPED, Transport.NO_MEDIA)

    @property
    def identity(self) -> str:
        return track_id(self.title, self.artist, self.album, self.duration_s)


# --------------------------------------------------------------------------- #
# Content: the honest "what track is this" payload we preserve across faults
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TrackContent:
    track_id: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    duration_s: int = UNKNOWN_DURATION
    art_uri: str = ""

    @property
    def duration_known(self) -> bool:
        return self.duration_s != UNKNOWN_DURATION

    @staticmethod
    def from_obs(obs: Observation) -> "TrackContent":
        return TrackContent(
            track_id=obs.identity,
            title=obs.title,
            artist=obs.artist,
            album=obs.album,
            duration_s=obs.duration_s,
            art_uri=obs.art_uri,
        )


# --------------------------------------------------------------------------- #
# ApplianceState: full immutable machine state + modifiers
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ApplianceState:
    state: UserState = UserState.STARTING
    content: TrackContent = field(default_factory=TrackContent)

    # modifiers (never expand the user-state enum)
    paused: bool = False
    stale: bool = False
    transitioning: bool = False
    art_degraded: bool = False
    analysis_degraded: bool = False
    use_fallback_art: bool = False

    # async ownership token
    generation: int = 0

    # bookkeeping (monotonic seconds)
    started_at: float = 0.0
    last_ok_at: float = -1.0        # last successful observation time
    last_fail_at: float = -1.0      # start of current failure streak
    stopped_polls: int = 0          # consecutive stopped-like observations

    @staticmethod
    def initial(now: float) -> "ApplianceState":
        return ApplianceState(state=UserState.STARTING, started_at=now)

    # convenience predicates -------------------------------------------------
    @property
    def is_now_playing(self) -> bool:
        return self.state is UserState.NOW_PLAYING

    @property
    def duration_known(self) -> bool:
        return self.content.duration_known


# --------------------------------------------------------------------------- #
# Async result ownership
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AsyncResult:
    """A completed async job (art or spectrum) tagged with its owner token."""

    generation: int
    track_id: str
    kind: str            # "art" | "analysis"
    ok: bool


def result_is_current(state: ApplianceState, result: AsyncResult) -> bool:
    """An async result applies only when both generation and track_id match."""
    return (
        result.generation == state.generation
        and result.track_id == state.content.track_id
        and result.track_id != ""
    )


def advance_pending_fetch_failure(
    state: ApplianceState, now: float, started_at: float, timeout_s: float
) -> tuple[ApplianceState, bool]:
    """Age content as failed once a metadata request exceeds its deadline."""
    if now - started_at < timeout_s:
        return state, False
    return reduce(state, Observation.failure(now)), True


def should_sleep_no_info(
    state: ApplianceState, now: float, timeout_s: float
) -> bool:
    """True only after continuous absence of successful WiiM observations.

    Track changes are irrelevant: every successful observation refreshes
    ``last_ok_at``. The initial start time is used until the first response.
    """
    if timeout_s <= 0:
        return False
    last_info_at = state.last_ok_at if state.last_ok_at >= 0 else state.started_at
    return (now - last_info_at) >= timeout_s


# --------------------------------------------------------------------------- #
# Reducer
# --------------------------------------------------------------------------- #
def reduce(state: ApplianceState, obs: Observation) -> ApplianceState:
    """Pure transition: (state, observation) -> new state.

    Never mutates ``state``; always returns a fresh ApplianceState.
    """
    if obs.ok:
        return _reduce_ok(state, obs)
    return _reduce_fail(state, obs)


def _reduce_ok(state: ApplianceState, obs: Observation) -> ApplianceState:
    # A fresh, successful observation clears fault flags.
    base = replace(
        state,
        stale=False,
        last_ok_at=obs.at,
        last_fail_at=-1.0,
    )

    if obs.is_playing_like:
        return _to_playing(base, obs)

    if obs.is_stopped_like:
        return _to_stopped(base, obs)

    # Unknown transport is reachable but not useful. During STARTING it still
    # obeys the bounded connection budget rather than hanging forever.
    if (
        state.state is UserState.STARTING
        and (obs.at - state.started_at) >= STARTUP_CONNECT_TIMEOUT_S
    ):
        return replace(base, state=UserState.SEARCHING)
    return base


def _to_playing(state: ApplianceState, obs: Observation) -> ApplianceState:
    new_id = obs.identity
    paused = obs.transport is Transport.PAUSED
    explicit_transition = obs.transport is Transport.TRANSITIONING

    identity_changed = new_id != "" and new_id != state.content.track_id
    # Every newly-owned track gets a fresh generation, including the first track
    # after STARTING/IDLE. Repeated TRANSITIONING polls without new metadata bump
    # only once, when entering the transition modifier.
    begins_transition = explicit_transition and not state.transitioning
    acquires_owner = identity_changed or begins_transition
    has_prior_track = state.content.track_id != ""

    generation = state.generation
    transitioning = state.transitioning if explicit_transition else False
    use_fallback_art = state.use_fallback_art
    art_degraded = state.art_degraded
    analysis_degraded = state.analysis_degraded

    if acquires_owner:
        # Every ownership change gets a unique token, even when the same song
        # returns after IDLE. Only replacement/loading paths expose the brief\n        # transition modifier; a complete first track can render immediately.
        generation = state.generation + 1
        transitioning = begins_transition or (identity_changed and has_prior_track)
        use_fallback_art = True
        art_degraded = False
        analysis_degraded = False

    if explicit_transition and new_id == "":
        # Pure TRANSITIONING with no metadata yet: hold content, show fallback.
        content = state.content
    else:
        content = TrackContent.from_obs(obs)
        if not explicit_transition and not identity_changed and content.track_id == state.content.track_id:
            # Same stable track settles a prior transition.
            transitioning = False
            use_fallback_art = state.use_fallback_art

    return replace(
        state,
        state=UserState.NOW_PLAYING,
        content=content,
        paused=paused,
        transitioning=transitioning,
        use_fallback_art=use_fallback_art,
        art_degraded=art_degraded,
        analysis_degraded=analysis_degraded,
        generation=generation,
        stopped_polls=0,
    )


def _to_stopped(state: ApplianceState, obs: Observation) -> ApplianceState:
    polls = state.stopped_polls + 1
    if polls >= IDLE_CONFIRM_POLLS:
        # Confirmed idle: clear track content and playback modifiers.
        return replace(
            state,
            state=UserState.IDLE,
            content=TrackContent(),
            paused=False,
            transitioning=False,
            use_fallback_art=False,
            art_degraded=False,
            analysis_degraded=False,
            stopped_polls=polls,
        )
    # First stopped poll: not yet idle. Preserve prior visible state/content;
    # do not flash a blank screen on a single stop.
    return replace(state, stopped_polls=polls)


def _reduce_fail(state: ApplianceState, obs: Observation) -> ApplianceState:
    fail_start = state.last_fail_at if state.last_fail_at >= 0 else obs.at
    fail_dur = obs.at - fail_start

    # STARTING never connected: fall to SEARCHING after the connect timeout.
    if state.state is UserState.STARTING:
        if (obs.at - state.started_at) >= STARTUP_CONNECT_TIMEOUT_S:
            return replace(
                state,
                state=UserState.SEARCHING,
                last_fail_at=fail_start,
                stale=False,
            )
        return replace(state, last_fail_at=fail_start)

    # Long continuous failure -> SEARCHING (lost the source).
    if fail_dur >= FAULT_SEARCHING_AFTER_S:
        return replace(
            state,
            state=UserState.SEARCHING,
            content=TrackContent(),
            paused=False,
            transitioning=False,
            use_fallback_art=False,
            stale=False,
            last_fail_at=fail_start,
            stopped_polls=0,
        )

    # Shorter failure: preserve honest content; mark stale past the threshold.
    stale = fail_dur >= STALE_AFTER_S
    return replace(state, stale=stale, last_fail_at=fail_start)


# --------------------------------------------------------------------------- #
# Async application helpers
# --------------------------------------------------------------------------- #
def apply_async_result(
    state: ApplianceState, result: AsyncResult
) -> ApplianceState:
    """Apply an async art/analysis result iff it is current; else ignore."""
    if not result_is_current(state, result):
        return state

    if result.kind == "art":
        return replace(
            state,
            use_fallback_art=not result.ok,
            art_degraded=not result.ok,
        )
    if result.kind == "analysis":
        return replace(state, analysis_degraded=not result.ok)
    return state
