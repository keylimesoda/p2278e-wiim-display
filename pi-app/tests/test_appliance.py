"""Focused unit tests for the P2278E appliance domain core.

Deterministic: monotonic time is a plain float we advance by hand.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from p2278e_display.appliance import (  # noqa: E402
    ApplianceState,
    AsyncResult,
    Observation,
    Transport,
    UserState,
    UNKNOWN_DURATION,
    STALE_AFTER_S,
    FAULT_SEARCHING_AFTER_S,
    STARTUP_CONNECT_TIMEOUT_S,
    apply_async_result,
    reduce,
    result_is_current,
    should_sleep_no_info,
    track_id,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def playing(at, title="Song", artist="Band", album="Alb", dur=200, art="http://a/x.jpg"):
    return Observation(
        at=at,
        ok=True,
        transport=Transport.PLAYING,
        title=title,
        artist=artist,
        album=album,
        duration_s=dur,
        art_uri=art,
    )


def paused(at, **kw):
    o = playing(at, **kw)
    return Observation(**{**o.__dict__, "transport": Transport.PAUSED})


def stopped(at):
    return Observation(at=at, ok=True, transport=Transport.STOPPED)


def no_media(at):
    return Observation(at=at, ok=True, transport=Transport.NO_MEDIA)


def fail(at):
    return Observation.failure(at)


# --------------------------------------------------------------------------- #
# track_id / identity
# --------------------------------------------------------------------------- #
def test_track_id_deterministic():
    a = track_id("Hey Jude", "Beatles", "1", 431)
    b = track_id("Hey Jude", "Beatles", "1", 431)
    assert a == b and a != ""


def test_track_id_normalizes_whitespace_and_case():
    assert track_id("  Hey  Jude ", "BEATLES", "1", 431) == track_id(
        "hey jude", "beatles", "1", 431
    )


def test_track_id_empty_metadata_is_empty():
    assert track_id("", "", "", UNKNOWN_DURATION) == ""


def test_url_token_change_same_identity():
    a = playing(1.0, art="http://cdn/x.jpg?token=AAA&exp=1")
    b = playing(2.0, art="http://cdn/x.jpg?token=BBB&exp=2")
    assert a.identity == b.identity  # art token must not affect identity


def test_different_track_different_identity():
    assert playing(1.0, title="A").identity != playing(1.0, title="B").identity


# --------------------------------------------------------------------------- #
# STARTING entry/exit
# --------------------------------------------------------------------------- #
def test_initial_state_is_starting():
    s = ApplianceState.initial(0.0)
    assert s.state is UserState.STARTING


def test_starting_first_playing_goes_now_playing():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0))
    assert s.state is UserState.NOW_PLAYING
    assert s.content.title == "Song"


def test_starting_first_stopped_needs_two_for_idle():
    s = ApplianceState.initial(0.0)
    s = reduce(s, stopped(1.0))
    assert s.state is UserState.STARTING  # single stop does not confirm idle
    s = reduce(s, stopped(2.0))
    assert s.state is UserState.IDLE


def test_starting_no_connection_18s_goes_searching():
    s = ApplianceState.initial(0.0)
    s = reduce(s, fail(5.0))
    assert s.state is UserState.STARTING
    s = reduce(s, fail(STARTUP_CONNECT_TIMEOUT_S + 0.1))
    assert s.state is UserState.SEARCHING


def test_starting_stays_before_18s_on_failure():
    s = ApplianceState.initial(0.0)
    s = reduce(s, fail(17.9))
    assert s.state is UserState.STARTING


def test_starting_unknown_transport_is_bounded():
    s = ApplianceState.initial(0.0)
    s = reduce(s, Observation(at=17.9, transport=Transport.UNKNOWN))
    assert s.state is UserState.STARTING
    s = reduce(s, Observation(at=18.1, transport=Transport.UNKNOWN))
    assert s.state is UserState.SEARCHING


# --------------------------------------------------------------------------- #
# NOW_PLAYING + pause modifier
# --------------------------------------------------------------------------- #
def test_pause_is_modifier_not_state():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0))
    s = reduce(s, paused(2.0))
    assert s.state is UserState.NOW_PLAYING
    assert s.paused is True


def test_resume_clears_pause():
    s = ApplianceState.initial(0.0)
    s = reduce(s, paused(1.0))
    assert s.paused is True
    s = reduce(s, playing(2.0))
    assert s.paused is False and s.state is UserState.NOW_PLAYING


# --------------------------------------------------------------------------- #
# IDLE two-poll requirement + immediate exit
# --------------------------------------------------------------------------- #
def test_two_consecutive_stops_enter_idle():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0))
    s = reduce(s, stopped(2.0))
    assert s.state is UserState.NOW_PLAYING  # still showing prior track
    s = reduce(s, stopped(3.0))
    assert s.state is UserState.IDLE
    assert s.content.track_id == ""


def test_mixed_no_media_and_stopped_counts():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0))
    s = reduce(s, stopped(2.0))
    s = reduce(s, no_media(3.0))
    assert s.state is UserState.IDLE


def test_stop_then_play_resets_idle_counter():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0))
    s = reduce(s, stopped(2.0))
    s = reduce(s, playing(3.0))  # playback resumes
    assert s.state is UserState.NOW_PLAYING
    assert s.stopped_polls == 0
    s = reduce(s, stopped(4.0))
    assert s.state is UserState.NOW_PLAYING  # counter was reset; needs two again


def test_idle_exits_immediately_on_playing():
    s = ApplianceState.initial(0.0)
    s = reduce(s, stopped(1.0))
    s = reduce(s, stopped(2.0))
    assert s.state is UserState.IDLE
    s = reduce(s, playing(3.0))
    assert s.state is UserState.NOW_PLAYING


def test_loading_exits_idle():
    s = ApplianceState.initial(0.0)
    s = reduce(s, stopped(1.0))
    s = reduce(s, stopped(2.0))
    load = Observation(at=3.0, transport=Transport.LOADING)
    s = reduce(s, load)
    assert s.state is UserState.NOW_PLAYING


# --------------------------------------------------------------------------- #
# Track transitions / generation / rapid skips
# --------------------------------------------------------------------------- #
def test_track_change_bumps_generation_and_transitions():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0, title="A"))
    g0 = s.generation
    s = reduce(s, playing(2.0, title="B"))
    assert s.generation == g0 + 1
    assert s.transitioning is True
    assert s.use_fallback_art is True


def test_first_track_gets_generation_owner():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0, title="A"))
    assert s.generation == 1


def test_repeated_transitioning_poll_bumps_once():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0, title="A"))
    s = reduce(s, Observation(at=2.0, transport=Transport.TRANSITIONING))
    first_generation = s.generation
    s = reduce(s, Observation(at=3.0, transport=Transport.TRANSITIONING))
    assert s.generation == first_generation


def test_same_track_after_idle_gets_new_generation():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0, title="A"))
    first_generation = s.generation
    s = reduce(s, stopped(2.0))
    s = reduce(s, stopped(3.0))
    s = reduce(s, playing(4.0, title="A"))
    assert s.generation == first_generation + 1


def test_same_track_settles_transitioning():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0, title="A"))
    s = reduce(s, playing(2.0, title="B"))
    assert s.transitioning is True
    s = reduce(s, playing(3.0, title="B"))  # same B continues
    assert s.transitioning is False


def test_explicit_transitioning_holds_content():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0, title="A"))
    s = reduce(s, Observation(at=2.0, transport=Transport.TRANSITIONING))
    assert s.transitioning is True
    assert s.use_fallback_art is True
    assert s.content.title == "A"  # prior content held during transition


def test_rapid_skips_supersede_generation():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0, title="A"))
    s = reduce(s, playing(1.5, title="B"))
    s = reduce(s, playing(1.7, title="C"))
    s = reduce(s, playing(1.9, title="D"))
    assert s.generation == 4  # first owner plus A->B->C->D changes
    assert s.content.title == "D"


def test_no_generation_bump_on_same_identity_url_change():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0, art="http://a?token=1"))
    g0 = s.generation
    s = reduce(s, playing(2.0, art="http://a?token=2"))
    assert s.generation == g0
    assert s.transitioning is False


# --------------------------------------------------------------------------- #
# Fault path: 12s stale / 45s searching / recovery
# --------------------------------------------------------------------------- #
def test_fetch_failure_preserves_content_short():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0, title="Keep"))
    s = reduce(s, fail(2.0))
    assert s.state is UserState.NOW_PLAYING
    assert s.content.title == "Keep"
    assert s.stale is False


def test_stale_after_12s():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0))
    s = reduce(s, fail(2.0))                       # streak starts at 2.0
    s = reduce(s, fail(2.0 + STALE_AFTER_S - 0.1))
    assert s.stale is False
    s = reduce(s, fail(2.0 + STALE_AFTER_S + 0.1))
    assert s.stale is True
    assert s.state is UserState.NOW_PLAYING


def test_searching_after_45s_continuous_failure():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0))
    s = reduce(s, fail(2.0))
    s = reduce(s, fail(2.0 + FAULT_SEARCHING_AFTER_S - 0.1))
    assert s.state is UserState.NOW_PLAYING
    s = reduce(s, fail(2.0 + FAULT_SEARCHING_AFTER_S + 0.1))
    assert s.state is UserState.SEARCHING
    assert s.content.track_id == ""


def test_recovery_clears_stale_and_returns_to_observation():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0, title="Old"))
    s = reduce(s, fail(2.0))
    s = reduce(s, fail(20.0))
    assert s.stale is True
    s = reduce(s, playing(21.0, title="Fresh"))
    assert s.stale is False
    assert s.state is UserState.NOW_PLAYING
    assert s.content.title == "Fresh"


def test_recovery_from_searching():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0))
    s = reduce(s, fail(2.0))
    s = reduce(s, fail(60.0))
    assert s.state is UserState.SEARCHING
    s = reduce(s, playing(61.0, title="Back"))
    assert s.state is UserState.NOW_PLAYING
    assert s.content.title == "Back"


def test_failure_streak_resets_on_success():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0))
    s = reduce(s, fail(2.0))
    s = reduce(s, fail(20.0))
    assert s.stale is True
    s = reduce(s, playing(21.0))
    assert s.last_fail_at == -1.0
    s = reduce(s, fail(22.0))          # new streak begins here
    assert s.stale is False            # only ~0s into new streak


# --------------------------------------------------------------------------- #
# Async ownership
# --------------------------------------------------------------------------- #
def test_async_result_applies_when_current():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0))
    r = AsyncResult(generation=s.generation, track_id=s.content.track_id,
                    kind="art", ok=False)
    assert result_is_current(s, r)
    s2 = apply_async_result(s, r)
    assert s2.art_degraded is True


def test_async_result_ignored_on_generation_mismatch():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0, title="A"))
    stale_r = AsyncResult(generation=s.generation, track_id=s.content.track_id,
                          kind="analysis", ok=False)
    s = reduce(s, playing(2.0, title="B"))  # generation bumped
    assert not result_is_current(s, stale_r)
    s2 = apply_async_result(s, stale_r)
    assert s2.analysis_degraded is False    # ignored


def test_async_result_ignored_on_trackid_mismatch():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0))
    r = AsyncResult(generation=s.generation, track_id="deadbeef",
                    kind="art", ok=False)
    assert not result_is_current(s, r)
    assert apply_async_result(s, r) is s


def test_async_success_clears_fallback_art():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0, title="A"))
    s = reduce(s, playing(2.0, title="B"))  # fallback art on
    assert s.use_fallback_art is True
    r = AsyncResult(generation=s.generation, track_id=s.content.track_id,
                    kind="art", ok=True)
    s = apply_async_result(s, r)
    assert s.use_fallback_art is False
    assert s.art_degraded is False


# --------------------------------------------------------------------------- #
# Unknown duration
# --------------------------------------------------------------------------- #
def test_unknown_duration_explicit():
    o = playing(1.0, dur=UNKNOWN_DURATION)
    s = ApplianceState.initial(0.0)
    s = reduce(s, o)
    assert s.content.duration_known is False
    assert s.content.duration_s == UNKNOWN_DURATION


def test_known_duration_flag():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0, dur=180))
    assert s.content.duration_known is True
    assert s.duration_known is True


def test_unknown_duration_identity_stable():
    a = track_id("T", "A", "Al", UNKNOWN_DURATION)
    b = track_id("T", "A", "Al", UNKNOWN_DURATION)
    assert a == b and a != ""


# --------------------------------------------------------------------------- #
# No-information sleep (successful polls, not track changes)
# --------------------------------------------------------------------------- #
def test_long_song_with_successful_polls_does_not_sleep():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0, title="Twenty Minute Song", dur=1200))
    for at in (300.0, 600.0, 900.0, 1199.0):
        s = reduce(s, playing(at, title="Twenty Minute Song", dur=1200))
        assert not should_sleep_no_info(s, at, 900.0)


def test_continuous_wiim_silence_sleeps_at_timeout():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(10.0))
    assert not should_sleep_no_info(s, 909.9, 900.0)
    assert should_sleep_no_info(s, 910.0, 900.0)


def test_first_successful_response_wakes_immediately():
    s = ApplianceState.initial(0.0)
    s = reduce(s, playing(1.0))
    assert should_sleep_no_info(s, 901.0, 900.0)
    s = reduce(s, playing(901.1))
    assert not should_sleep_no_info(s, 901.1, 900.0)


def test_no_info_sleep_can_be_disabled():
    s = ApplianceState.initial(0.0)
    assert not should_sleep_no_info(s, 99999.0, 0.0)
