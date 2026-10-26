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
