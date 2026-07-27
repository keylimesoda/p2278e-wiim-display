from __future__ import annotations

from dataclasses import asdict, dataclass
from html import unescape
import socket
import time
from typing import Optional
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
import re
import xml.etree.ElementTree as ET

AVTRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
CONTROL = "/upnp/control/rendertransport1"
SSDP_ADDRESS = ("239.255.255.250", 1900)

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


def _soap(base_url: str, action: str, inner: str, timeout: float = 2.0) -> ET.Element:
    envelope = (f'''<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>{inner}</s:Body>
</s:Envelope>''').encode()
    req = Request(urljoin(base_url, CONTROL), data=envelope, headers={
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPACTION": f'"{AVTRANSPORT}#{action}"',
    })
    with urlopen(req, timeout=timeout) as r:
        return ET.fromstring(r.read())


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


def _ssdp_locations(timeout: float) -> list[str]:
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
        while time.monotonic() < deadline:
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
                    if location and location not in locations:
                        locations.append(location)
                    break
    finally:
        sock.close()
    return locations


def discover_wiim(friendly_name: str = "", timeout: float = 3.0) -> str:
    """Return the base URL of a Linkplay/WiiM renderer discovered over SSDP."""
    matches: list[tuple[str, str]] = []
    for location in _ssdp_locations(timeout):
        try:
            with urlopen(location, timeout=1.0) as response:
                root = ET.fromstring(response.read())
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
        matches.append((name, f"{parts.scheme}://{parts.netloc}"))
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


def get_now_playing(base_url: str, timeout: float = 2.0) -> WiiMState:
    transport = _soap(base_url, "GetTransportInfo", '<u:GetTransportInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID></u:GetTransportInfo>', timeout=timeout)
    position = _soap(base_url, "GetPositionInfo", '<u:GetPositionInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID></u:GetPositionInfo>', timeout=timeout)
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
