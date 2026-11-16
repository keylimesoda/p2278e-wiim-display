from concurrent.futures import wait
from unittest.mock import patch

import pytest

from p2278e_display import wiim
from p2278e_display.wiim import _parse_didl, _seconds, discover_wiim


DESCRIPTION = b"""<root><device>
<friendlyName>Example WiiM Renderer</friendlyName>
<manufacturer>Linkplay Technology Inc.</manufacturer>
<modelName>WiiM Pro Receiver</modelName>
<serviceList><service>
<serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
</service></serviceList>
</device></root>"""


class ChunkResponse:
    def __init__(self, body):
        self.body = body
        self.offset = 0
        self.headers = {}
        self.read1_sizes = []

    def read1(self, size):
        self.read1_sizes.append(size)
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class SocketStub:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


class RawStub:
    def __init__(self, sock):
        self._sock = sock


class FileStub:
    def __init__(self, sock):
        self.raw = RawStub(sock)


class SocketResponse(ChunkResponse):
    def __init__(self, body, sock):
        super().__init__(body)
        self.fp = FileStub(sock)


def test_read_response_limited_rejects_oversized_body():
    response = ChunkResponse(b'x' * 11)
    with pytest.raises(ValueError, match='response byte limit'):
        wiim._read_response_limited(
            response, max_bytes=10, deadline=wiim.time.monotonic() + 10.0
        )
    assert sum(response.read1_sizes) <= 11


def test_read_response_limited_enforces_one_total_deadline(monkeypatch):
    clock = [0.0]
    sock = SocketStub()

    class TricklingResponse(SocketResponse):
        def read1(self, size):
            self.read1_sizes.append(size)
            clock[0] += 0.6
            return b'x'

    response = TricklingResponse(b'', sock)
    monkeypatch.setattr(wiim.time, 'monotonic', lambda: clock[0])
    with pytest.raises(TimeoutError, match='deadline'):
        wiim._read_response_limited(response, max_bytes=10, deadline=1.0)
    assert sock.timeouts == pytest.approx([1.0, 0.4])


def test_soap_uses_configured_response_limit(monkeypatch):
    monkeypatch.setattr(wiim, 'MAX_SOAP_RESPONSE_BYTES', 8, raising=False)
    with patch('p2278e_display.wiim._read_url_response', side_effect=ValueError('response byte limit exceeded')) as read_response:
        with pytest.raises(ValueError, match='response byte limit'):
            wiim._soap('http://192.0.2.7', 'Action', '<u:Action/>')
    assert read_response.call_args.kwargs['max_bytes'] == 8


def test_discovery_uses_configured_description_limit(monkeypatch):
    monkeypatch.setattr(wiim, 'MAX_DESCRIPTION_BYTES', 8, raising=False)
    with patch('p2278e_display.wiim._ssdp_locations', return_value=['http://192.0.2.7/desc']), patch(
        'p2278e_display.wiim._read_url_response', side_effect=ValueError('response byte limit exceeded')
    ) as read_response:
        with pytest.raises(LookupError):
            discover_wiim()
    assert read_response.call_args.kwargs['max_bytes'] == 8


def test_http_request_preserves_soap_post_and_headers():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    observed = {}

    class SoapHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            observed['method'] = self.command
            observed['content_type'] = self.headers.get('Content-Type')
            observed['soap_action'] = self.headers.get('SOAPACTION')
            observed['body'] = self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'ok')

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), SoapHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request_body = b'<Envelope>soap</Envelope>'
    request = wiim.Request(
        f'http://127.0.0.1:{server.server_port}/control',
        data=request_body,
        headers={'Content-Type': 'text/xml', 'SOAPACTION': 'urn:test#Play'},
    )
    try:
        assert wiim._read_url_response(request, timeout=1, max_bytes=10) == b'ok'
        assert observed == {
            'method': 'POST',
            'content_type': 'text/xml',
            'soap_action': 'urn:test#Play',
            'body': request_body,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_real_http_body_trickle_obeys_total_deadline():
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class SlowBodyHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Length', '100')
            self.end_headers()
            for _ in range(100):
                try:
                    self.wfile.write(b'x')
                    self.wfile.flush()
                except OSError:
                    break
                time.sleep(0.02)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), SlowBodyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            wiim._read_url_response(
                f'http://127.0.0.1:{server.server_port}/',
                timeout=0.12,
                max_bytes=100,
            )
        assert time.monotonic() - started < 0.5
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_real_http_header_trickle_obeys_total_deadline():
    import socket
    import threading
    import time

    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)

    def slow_headers():
        conn, _ = listener.accept()
        try:
            conn.recv(4096)
            reply = b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'
            for byte in reply:
                try:
                    conn.sendall(bytes([byte]))
                except OSError:
                    break
                time.sleep(0.01)
        finally:
            conn.close()
            listener.close()

    thread = threading.Thread(target=slow_headers, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            wiim._read_url_response(
                f'http://127.0.0.1:{listener.getsockname()[1]}/',
                timeout=0.12,
                max_bytes=100,
            )
        assert time.monotonic() - started < 0.4
    finally:
        thread.join(timeout=1)
        listener.close()


def test_ssdp_response_locations_are_count_bounded(monkeypatch):
    class FloodingSocket:
        def __init__(self):
            self.recv_count = 0
            self.closed = False

        def setsockopt(self, *args):
            pass

        def settimeout(self, _):
            pass

        def sendto(self, *_):
            pass

        def recvfrom(self, _):
            self.recv_count += 1
            payload = (
                f'HTTP/1.1 200 OK{chr(10)}'
                f'LOCATION: http://192.0.2.7/{self.recv_count}{chr(10)}{chr(10)}'
            ).encode()
            return payload, ('192.0.2.7', 1900)

        def close(self):
            self.closed = True

    sock = FloodingSocket()
    monkeypatch.setattr(wiim.socket, 'socket', lambda *_: sock)
    monkeypatch.setattr(wiim, 'MAX_SSDP_LOCATIONS', 3)

    locations = wiim._ssdp_locations(timeout=1.0)

    assert len(locations) == 3
    assert sock.recv_count == 3
    assert sock.closed


def test_seconds():
    assert _seconds("01:02:03") == 3723
    assert _seconds("NOT_IMPLEMENTED") is None


def test_parse_didl_unescapes_metadata():
    metadata = "&lt;DIDL-Lite&gt;&lt;item&gt;&lt;title&gt;Song&lt;/title&gt;&lt;artist&gt;Artist&lt;/artist&gt;&lt;/item&gt;&lt;/DIDL-Lite&gt;"
    assert _parse_didl(metadata)["title"] == "Song"
    assert _parse_didl(metadata)["artist"] == "Artist"


def test_slow_unrelated_description_does_not_starve_wiim(monkeypatch):
    import time

    slow_location = 'http://192.0.2.8/description.xml'
    wiim_location = 'http://192.0.2.7:49152/description.xml'
    monkeypatch.setattr(wiim, '_ssdp_locations', lambda timeout, max_locations=None: [slow_location, wiim_location])

    def fetch(location, **kwargs):
        if location == slow_location:
            time.sleep(kwargs['timeout'] + 0.02)
            raise TimeoutError('slow unrelated renderer')
        return DESCRIPTION

    monkeypatch.setattr(wiim, '_read_url_response', fetch)
    assert discover_wiim('Example WiiM Renderer', timeout=0.2) == 'http://192.0.2.7:49152'


def test_discovery_workers_remain_bounded_across_retries(monkeypatch):
    import threading

    locations = [f'http://192.0.2.{host}/description.xml' for host in range(1, 17)]
    monkeypatch.setattr(wiim, '_ssdp_locations', lambda timeout, max_locations=None: locations)
    release = threading.Event()
    lock = threading.Lock()
    active = [0]
    maximum = [0]

    def blocked_fetch(*args, **kwargs):
        with lock:
            active[0] += 1
            maximum[0] = max(maximum[0], active[0])
        release.wait(1.0)
        with lock:
            active[0] -= 1
        raise TimeoutError('simulated stalled name resolution')

    monkeypatch.setattr(wiim, '_read_url_response', blocked_fetch)
    try:
        for _ in range(2):
            with pytest.raises(LookupError):
                discover_wiim(timeout=0.05)
        assert 0 < maximum[0] <= wiim.MAX_SSDP_LOCATIONS
    finally:
        release.set()
        wait(tuple(wiim._DISCOVERY_FUTURES), timeout=1.0)
        assert all(future.done() for future in wiim._DISCOVERY_FUTURES)


def test_discovery_reserves_deadline_for_description_fetch(monkeypatch):
    clock = [100.0]
    location = 'http://192.0.2.7:49152/description.xml'

    def monotonic():
        return clock[0]

    def consume_ssdp_window(timeout, max_locations=None):
        clock[0] += timeout
        return [location]

    def completed_fetch(_location, deadline):
        assert deadline > clock[0]
        future = wiim.Future()
        future.set_result(DESCRIPTION)
        return future

    monkeypatch.setattr(wiim.time, 'monotonic', monotonic)
    monkeypatch.setattr(wiim, '_ssdp_locations', consume_ssdp_window)
    monkeypatch.setattr(wiim, '_submit_description_fetch', completed_fetch)

    assert discover_wiim('Example WiiM Renderer', timeout=0.2) == 'http://192.0.2.7:49152'


def test_discover_wiim_by_friendly_name():
    with patch("p2278e_display.wiim._ssdp_locations", return_value=["http://192.0.2.7:49152/description.xml"]), patch(
        "p2278e_display.wiim._read_url_response", return_value=DESCRIPTION
    ):
        assert discover_wiim("Example WiiM Renderer") == "http://192.0.2.7:49152"


def test_discover_wiim_fails_closed_when_absent():
    with patch("p2278e_display.wiim._ssdp_locations", return_value=[]):
        with pytest.raises(LookupError):
            discover_wiim("Example WiiM Renderer")
