from __future__ import annotations

from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)
from dataclasses import asdict, dataclass
import base64
from html import unescape
import http.client
import os
import socket
import threading
import time
from typing import Optional
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import Request
import re
import xml.etree.ElementTree as ET

AVTRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
CONTROL = "/upnp/control/rendertransport1"
SSDP_ADDRESS = ("239.255.255.250", 1900)
MAX_SSDP_LOCATIONS = 16
MAX_DESCRIPTION_BYTES = 128 * 1024
MAX_SOAP_RESPONSE_BYTES = 256 * 1024
MAX_METADATA_FETCH_SECONDS = 8.0
READ_CHUNK_BYTES = 64 * 1024
DISCOVERY_DESCRIPTION_RESERVE_SECONDS = 1.0
_DISCOVERY_POOL_LOCK = threading.Lock()
_DISCOVERY_POOL: ThreadPoolExecutor | None = None
_DISCOVERY_FUTURES: set[Future[bytes]] = set()

@dataclass
class WiiMState:
    state: str = "UNKNOWN"
    title: str = "Idle"
    artist: str = "WiiM"
    album: str = ""
    source: str = "WiiM"
    track_uri: str = ""
    album_art_uri: str = ""
    rel_time: str = "00:00:00"
    duration: str = "00:00:00"
    progress: float = 0.0

    def to_dict(self):
        return asdict(self)


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _find_text_by_local(root: ET.Element, name: str, default: str = "") -> str:
    for el in root.iter():
        if _local(el.tag) == name:
            return el.text or default
    return default


def _seconds(hms: str) -> Optional[int]:
    if not hms or hms == "NOT_IMPLEMENTED":
        return None
    if hms == "00:00:00":
        return 0
    m = re.match(r"^(\d+):(\d\d):(\d\d)$", hms)
    if not m:
        return None
    h, mnt, sec = map(int, m.groups())
    return h * 3600 + mnt * 60 + sec


def _response_socket(response):
    current = response
    for _ in range(4):
        sock = getattr(current, "sock", None)
        if sock is not None:
            return sock
        raw = getattr(current, "raw", None)
        sock = getattr(raw, "_sock", None)
        if sock is not None:
            return sock
        current = getattr(current, "fp", None)
        if current is None:
            break
    return None


def _response_read1(response):
    current = response
    for _ in range(4):
        reader = getattr(current, "read1", None)
        if reader is not None:
            return reader
        current = getattr(current, "fp", None)
        if current is None:
            break
    return response.read


def _read_response_limited(response, *, max_bytes: int, deadline: float) -> bytes:
    """Read one HTTP body with a total deadline and a hard byte ceiling."""
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    headers = getattr(response, "headers", None)
    content_length = headers.get("Content-Length") if headers is not None else None
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except (TypeError, ValueError):
            declared_length = None
        if declared_length is not None and declared_length > max_bytes:
            raise ValueError("response byte limit exceeded")

    output = bytearray(max_bytes)
    output_view = memoryview(output)
    written = 0
    reader = _response_read1(response)
    while True:
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            raise TimeoutError("HTTP response deadline exceeded")
        sock = _response_socket(response)
        if sock is not None:
            sock.settimeout(remaining_time)
        chunk = reader(min(READ_CHUNK_BYTES, max_bytes - written + 1))
        if time.monotonic() > deadline:
            raise TimeoutError("HTTP response deadline exceeded")
        if not chunk:
            break
        if len(chunk) > max_bytes - written:
            raise ValueError("response byte limit exceeded")
        output_view[written : written + len(chunk)] = chunk
        written += len(chunk)
    return bytes(output_view[:written])


def _abort_socket(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _request_parts(url_or_request):
    if isinstance(url_or_request, Request):
        return (
            url_or_request.full_url,
            url_or_request.get_method(),
            url_or_request.data,
            {**url_or_request.headers, **url_or_request.unredirected_hdrs},
        )
    return str(url_or_request), "GET", None, {}


def _read_url_response(url_or_request, *, timeout: float, max_bytes: int, deadline: float | None = None) -> bytes:
    """Read one HTTP response under a hard total deadline and byte ceiling."""
    started = time.monotonic()
    request_deadline = min(deadline, started + timeout) if deadline is not None else started + timeout
    current_url, method, body, headers = _request_parts(url_or_request)
    redirects = 0

    while True:
        remaining = request_deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("HTTP request deadline exceeded")
        parts = urlsplit(current_url)
        scheme = parts.scheme.lower()
        if scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("unsupported or invalid HTTP URL")
        port = parts.port
        connection_type = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        conn = connection_type(parts.hostname, port, timeout=remaining)
        abort_socket = None
        timer = None
        response = None
        try:
            conn.connect()
            remaining = request_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("HTTP request deadline exceeded")
            conn.sock.settimeout(remaining)
            # Keep a duplicate descriptor so the absolute-deadline watchdog can
            # interrupt reads even if http.client detaches its socket on EOF.
            abort_socket = socket.socket(
                conn.sock.family,
                conn.sock.type,
                conn.sock.proto,
                fileno=os.dup(conn.sock.fileno()),
            )
            timer = threading.Timer(remaining, _abort_socket, args=(abort_socket,))
            timer.daemon = True
            timer.start()

            target = parts.path or "/"
            if parts.query:
                target += "?" + parts.query
            request_headers = dict(headers)
            if parts.username is not None and not any(
                key.lower() == "authorization" for key in request_headers
            ):
                credentials = f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode()
                request_headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode("ascii")
            conn.request(method, target, body=body, headers=request_headers)
            response = conn.getresponse()

            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if location and redirects < 5:
                    next_url = urljoin(current_url, location)
                    old_parts, new_parts = urlsplit(current_url), urlsplit(next_url)
                    if (old_parts.scheme, old_parts.netloc) != (new_parts.scheme, new_parts.netloc):
                        headers = {
                            key: value for key, value in headers.items()
                            if key.lower() not in {"authorization", "proxy-authorization"}
                        }
                    if response.status == 303 or (response.status in {301, 302} and method.upper() == "POST"):
                        method, body = "GET", None
                        headers = {
                            key: value for key, value in headers.items()
                            if key.lower() not in {"content-length", "content-type", "soapaction"}
                        }
                    current_url = next_url
                    redirects += 1
                    continue
            if response.status < 200 or response.status >= 300:
                raise OSError(f"HTTP request failed with status {response.status}")
            return _read_response_limited(
                response, max_bytes=max_bytes, deadline=request_deadline
            )
        finally:
            if timer is not None:
                timer.cancel()
            if abort_socket is not None:
                abort_socket.close()
            if response is not None:
                response.close()
            conn.close()


def _submit_description_fetch(location: str, deadline: float) -> Future[bytes] | None:
    """Submit a description fetch without exceeding the global worker cap."""
    global _DISCOVERY_POOL
    with _DISCOVERY_POOL_LOCK:
        completed = {future for future in _DISCOVERY_FUTURES if future.done()}
        _DISCOVERY_FUTURES.difference_update(completed)
        if len(_DISCOVERY_FUTURES) >= MAX_SSDP_LOCATIONS:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        if _DISCOVERY_POOL is None:
            _DISCOVERY_POOL = ThreadPoolExecutor(
                max_workers=MAX_SSDP_LOCATIONS,
                thread_name_prefix="wiim-discovery",
            )
        future = _DISCOVERY_POOL.submit(
            _read_url_response,
            location,
            timeout=min(1.0, remaining),
            max_bytes=MAX_DESCRIPTION_BYTES,
            deadline=deadline,
        )
        _DISCOVERY_FUTURES.add(future)
        return future


def _soap(base_url: str, action: str, inner: str, timeout: float = 2.0, deadline: float | None = None) -> ET.Element:
    envelope = (f'''<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>{inner}</s:Body>
</s:Envelope>''').encode()
    req = Request(urljoin(base_url, CONTROL), data=envelope, headers={
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPACTION": f'"{AVTRANSPORT}#{action}"',
    })
    body = _read_url_response(
        req, timeout=timeout, max_bytes=MAX_SOAP_RESPONSE_BYTES, deadline=deadline
    )
    return ET.fromstring(body)


def _parse_didl(metadata: str) -> dict[str, str]:
    if not metadata:
        return {}
    metadata = unescape(metadata).strip()
    if not metadata:
        return {}
    try:
        root = ET.fromstring(metadata)
    except ET.ParseError:
        return {}
    return {
        "title": _find_text_by_local(root, "title"),
        "artist": _find_text_by_local(root, "artist") or _find_text_by_local(root, "creator"),
        "album": _find_text_by_local(root, "album"),
        "album_art_uri": _find_text_by_local(root, "albumArtURI"),
    }


def _ssdp_locations(timeout: float, max_locations: int | None = None) -> list[str]:
    if max_locations is None:
        max_locations = MAX_SSDP_LOCATIONS
    request = "\r\n".join((
        "M-SEARCH * HTTP/1.1",
        f"HOST: {SSDP_ADDRESS[0]}:{SSDP_ADDRESS[1]}",
        'MAN: "ssdp:discover"',
        "MX: 2",
        "ST: upnp:rootdevice",
        "",
        "",
    )).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.2)
    locations: list[str] = []
    started = time.monotonic()
    deadline = started + timeout
    next_send = started
    try:
        while time.monotonic() < deadline and len(locations) < max_locations:
            remaining = deadline - time.monotonic()
            sock.settimeout(min(0.2, remaining))
            if time.monotonic() >= next_send:
                sock.sendto(request, SSDP_ADDRESS)
                next_send += 1.0
            try:
                payload, _ = sock.recvfrom(65535)
            except socket.timeout:
                continue
            for line in payload.decode("utf-8", "replace").splitlines()[1:]:
                if line.lower().startswith("location:"):
                    location = line.split(":", 1)[1].strip()
                    if location and len(location) <= 2048 and location not in locations:
                        locations.append(location)
                    break
    finally:
        sock.close()
    return locations


def discover_wiim(
    friendly_name: str = "", timeout: float = 3.0, deadline: float | None = None
) -> str:
    """Return a WiiM base URL within an overall discovery/description deadline."""
    started = time.monotonic()
    overall_deadline = min(deadline, started + timeout) if deadline is not None else started + timeout
    discovery_budget = max(0.0, overall_deadline - time.monotonic())
    description_reserve = min(
        DISCOVERY_DESCRIPTION_RESERVE_SECONDS, discovery_budget / 2
    )
    locations = _ssdp_locations(max(0.0, discovery_budget - description_reserve))
    matches: list[tuple[str, str]] = []
    futures: dict[Future[bytes], str] = {}
    for location in locations:
        future = _submit_description_fetch(location, overall_deadline)
        if future is not None:
            futures[future] = location

    try:
        for future in as_completed(
            futures, timeout=max(0.0, overall_deadline - time.monotonic())
        ):
            location = futures[future]
            try:
                root = ET.fromstring(future.result())
            except Exception:
                continue
            manufacturer = _find_text_by_local(root, "manufacturer").lower()
            model = _find_text_by_local(root, "modelName").lower()
            name = _find_text_by_local(root, "friendlyName")
            services = {
                _find_text_by_local(el, "serviceType")
                for el in root.iter()
                if _local(el.tag) == "service"
            }
            if not ("linkplay" in manufacturer or "wiim" in model) or AVTRANSPORT not in services:
                continue
            parts = urlsplit(location)
            base_url = f"{parts.scheme}://{parts.netloc}"
            matches.append((name, base_url))
            if friendly_name and name.casefold() == friendly_name.casefold():
                for pending in futures:
                    pending.cancel()
                break
    except FuturesTimeoutError:
        pass
    finally:
        for future in futures:
            future.cancel()

    if friendly_name:
        for name, base_url in matches:
            if name.casefold() == friendly_name.casefold():
                return base_url
    if len(matches) == 1:
        return matches[0][1]
    if matches:
        names = ", ".join(name or url for name, url in matches)
        raise LookupError(f"multiple WiiM renderers found; configure friendly_name ({names})")
    raise LookupError("no WiiM/Linkplay renderer found via SSDP")


def get_now_playing(
    base_url: str, timeout: float = 2.0, deadline: float | None = None
) -> WiiMState:
    transport = _soap(base_url, "GetTransportInfo", '<u:GetTransportInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID></u:GetTransportInfo>', timeout=timeout, deadline=deadline)
    position = _soap(base_url, "GetPositionInfo", '<u:GetPositionInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID></u:GetPositionInfo>', timeout=timeout, deadline=deadline)
    state = _find_text_by_local(transport, "CurrentTransportState", "UNKNOWN")
    rel = _find_text_by_local(position, "RelTime", "00:00:00")
    dur = _find_text_by_local(position, "TrackDuration", "00:00:00")
    uri = _find_text_by_local(position, "TrackURI", "")
    meta = _parse_didl(_find_text_by_local(position, "TrackMetaData", ""))
    rel_s = _seconds(rel)
    dur_s = _seconds(dur)
    progress = (rel_s / dur_s) if rel_s is not None and dur_s and dur_s > 0 else 0.0
    title = meta.get("title") or ("Idle" if state in ("NO_MEDIA_PRESENT", "STOPPED") else "Playing")
    artist = meta.get("artist") or state.replace("_", " ").title()
    return WiiMState(
        state=state,
        title=title,
        artist=artist,
        album=meta.get("album", ""),
        track_uri=uri,
        album_art_uri=meta.get("album_art_uri", ""),
        rel_time=rel,
        duration=dur,
        progress=max(0.0, min(1.0, progress)),
    )
