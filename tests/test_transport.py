"""Outgoing gateway frames must fit the Noise 65535-byte message limit."""
import base64
import json

from muse_cli import fs as fsmod
from muse_cli.gateway import SEND_PIECE, join_chunks
from muse_cli import gateway
from muse_cli.gateway import NoiseTransportFrame, ServiceRequest

NOISE_MAX = 65535


class FakeNoise:
    def encrypt(self, pt):
        assert len(pt) + 16 <= NOISE_MAX, len(pt)
        return pt + b"\0" * 16


class FakeWS:
    def __init__(self):
        self.out = []

    def send_bytes(self, b):
        assert len(b) <= NOISE_MAX
        self.out.append(b)


def make():
    import threading
    g = object.__new__(gateway.Gateway)
    g.noise, g.ws, g._send_lock = FakeNoise(), FakeWS(), threading.Lock()
    return g


def roundtrip(g):
    partial, payload = {}, None
    for b in g.ws.out:
        ntf = NoiseTransportFrame()
        ntf.ParseFromString(b[:-16])
        payload = join_chunks(partial, ntf)
    sr = ServiceRequest()
    sr.ParseFromString(payload)
    return sr


def test_small_frame_single_piece():
    g = make()
    g._send_envelope(3, b"hello")
    assert len(g.ws.out) == 1 and roundtrip(g).payload == b"hello"


def test_big_frame_is_split_and_reassembles():
    g = make()
    body = json.dumps({"data_base64": base64.b64encode(bytes(range(256)) * 6000).decode()}).encode()
    g._send_envelope(3, body)
    assert len(g.ws.out) > 1
    sr = roundtrip(g)
    assert sr.service == 3 and sr.payload == body


def test_write_chunk_stays_under_gateway_body_limit():
    # 4 MiB raw chunks were rejected with 413; keep base64 body well under that
    assert fsmod.WRITE_CHUNK * 4 // 3 < 2 * 1024 * 1024
    assert SEND_PIECE + 64 + 16 <= NOISE_MAX


# -- cli.connect: VM id and tokens cached between connections ---------------------------------
def test_connect_reuses_cached_tokens_and_falls_back(tmp_path, monkeypatch):
    from muse_cli import cli
    made = []

    class FakeGW:
        def __init__(self, cookies, vm_id=None, access_token=None, hatch_token=None):
            made.append((vm_id, access_token, hatch_token))
            if hatch_token == "stale":
                raise RuntimeError("ws upgrade -> 401")
            self.vm_id = vm_id or "vm-1"
            self.access_token = access_token or "acc-1"
            self.hatch_token = hatch_token or f"hatch-{len(made)}"
    monkeypatch.setattr(cli, "Gateway", FakeGW)
    monkeypatch.setattr(cli, "_TOKENS", {})
    ck = tmp_path / "cookies.txt"
    ck.write_text("hatch_sess=abc\n")
    cfg = {"cookies_file": str(ck)}
    cli.connect(cfg)
    cli.connect(cfg)
    assert made == [(None, None, None), ("vm-1", "acc-1", "hatch-1")]   # 2nd: no token fetches
    next(iter(cli._TOKENS.values()))["hatch_token"] = "stale"
    made.clear()
    gw = cli.connect(cfg)                                                # stale: fresh fetch
    assert made == [("vm-1", "acc-1", "stale"), (None, None, None)] and gw.hatch_token == "hatch-2"


# -- one multiplexed connection: frames go to their own stream ---------------------------------
def _mux_gateway(inbox):
    """A Gateway without a socket: sent requests are recorded, the reader takes
    frames from INBOX (a Queue of ServiceFrame or an exception to raise)."""
    import queue
    import threading
    import time
    g = object.__new__(gateway.Gateway)
    g.stream, g._streams, g._streams_lock = 1, {}, threading.Lock()
    g.closed, g.dead, g.last_used = False, None, time.time()
    g.sent = queue.Queue()

    def send_envelope(service, data):
        sf = gateway.ServiceFrame()
        sf.ParseFromString(data)
        g.sent.put(sf)

    def read_frame():
        item = inbox.get()
        if isinstance(item, Exception):
            raise item
        return item
    g._send_envelope, g._read_frame = send_envelope, read_frame
    g._reader = threading.Thread(target=g._read_loop, daemon=True)
    g._reader.start()
    return g


def _response(sid, body):
    sf = gateway.ServiceFrame(stream_id=sid)
    sf.response.status = 200
    sf.response.body = json.dumps(body).encode()
    sf.response.end_body = True
    return sf


def test_mux_routes_interleaved_responses_by_stream_id():
    import queue
    import threading
    inbox = queue.Queue()
    g = _mux_gateway(inbox)
    out = {}

    def ask(name):
        out[name] = g.call_json("chat.history", body={"limit": 1, "session_id": name})
    ts = [threading.Thread(target=ask, args=(n,)) for n in ("a", "b", "c")]
    for t in ts:
        t.start()
    reqs = [g.sent.get(timeout=2) for _ in ts]
    import urllib.parse
    by_session = {urllib.parse.parse_qs(urllib.parse.urlparse(r.request.path).query)["session_id"][0]:
                  r.stream_id for r in reqs}
    for name in ("c", "a", "b"):                  # answers arrive in another order
        inbox.put(_response(by_session[name], {"who": name}))
    for t in ts:
        t.join(timeout=2)
    assert out == {"a": {"who": "a"}, "b": {"who": "b"}, "c": {"who": "c"}}
    assert len(set(by_session.values())) == 3 and not g._streams      # all streams released


def test_mux_drop_fails_pending_calls_and_marks_connection_dead():
    import queue
    import threading
    inbox = queue.Queue()
    g = _mux_gateway(inbox)
    err = []

    def ask():
        try:
            g.call_json("chat.history", body={"limit": 1})
        except ConnectionError as e:
            err.append(e)
    t = threading.Thread(target=ask)
    t.start()
    g.sent.get(timeout=2)
    inbox.put(ConnectionError("websocket closed"))
    t.join(timeout=2)
    assert err and not g.alive
    import pytest
    with pytest.raises(ConnectionError):
        g._open("chat.history", body={"limit": 1})
