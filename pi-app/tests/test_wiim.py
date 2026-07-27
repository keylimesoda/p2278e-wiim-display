from unittest.mock import patch

import pytest

from p2278e_display.wiim import _parse_didl, _seconds, discover_wiim


DESCRIPTION = b"""<root><device>
<friendlyName>Example WiiM Renderer</friendlyName>
<manufacturer>Linkplay Technology Inc.</manufacturer>
<modelName>WiiM Pro Receiver</modelName>
<serviceList><service>
<serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
</service></serviceList>
</device></root>"""


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return DESCRIPTION


def test_seconds():
    assert _seconds("01:02:03") == 3723
    assert _seconds("NOT_IMPLEMENTED") is None


def test_parse_didl_unescapes_metadata():
    metadata = "&lt;DIDL-Lite&gt;&lt;item&gt;&lt;title&gt;Song&lt;/title&gt;&lt;artist&gt;Artist&lt;/artist&gt;&lt;/item&gt;&lt;/DIDL-Lite&gt;"
    assert _parse_didl(metadata)["title"] == "Song"
    assert _parse_didl(metadata)["artist"] == "Artist"


def test_discover_wiim_by_friendly_name():
    with patch("p2278e_display.wiim._ssdp_locations", return_value=["http://192.0.2.7:49152/description.xml"]), patch("p2278e_display.wiim.urlopen", return_value=Response()):
        assert discover_wiim("Example WiiM Renderer") == "http://192.0.2.7:49152"


def test_discover_wiim_fails_closed_when_absent():
    with patch("p2278e_display.wiim._ssdp_locations", return_value=[]):
        with pytest.raises(LookupError):
            discover_wiim("Example WiiM Renderer")
