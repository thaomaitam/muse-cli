"""Muse gateway client: HTTPS auth + WebSocket/Noise-XX transport + protobuf envelopes.

Derived from the muse.ai web app's own protocol (route table + framing observed
in its client bundle). Talks to the user's personal VM gateway directly.
No browser needed after the initial cookie export.
"""
import json
import os
import re
import struct
import threading
import time
import urllib.parse
import uuid
import queue as queue_mod

from curl_cffi import requests as rq
from curl_cffi.requests import WebSocket
from curl_cffi.const import CurlInfo
from noise.connection import NoiseConnection, Keypair
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

_HERE = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")
GATEWAY_HOST = "hatch.metaaivm.com"

_pool = descriptor_pool.DescriptorPool()
for _i in (0, 1):
    _fd = descriptor_pb2.FileDescriptorProto()
    with open(os.path.join(_HERE, f"desc{_i}.bin"), "rb") as fh:
        _fd.ParseFromString(fh.read())
    _pool.Add(_fd)


def _msg(name):
    return message_factory.GetMessageClass(_pool.FindMessageTypeByName(name))


NoiseTransportFrame = _msg("ingress_rev_proxy.NoiseTransportFrame")
ServiceRequest = _msg("hatch.noise.ServiceRequest")
ServiceResponse = _msg("hatch.noise.ServiceResponse")
ServiceFrame = _msg("hatch.noise.ServiceFrame")
ApplicationRequest = _msg("hatch.noise.ApplicationRequest")

with open(os.path.join(_HERE, "routes.json")) as fh:
    ROUTES = {e["method"]: e for e in json.load(fh)}

SERVICE_IDS = {"daemon": 0, "sentinel": 1, "vault": 2, "authd": 3}


class AuthError(RuntimeError):
    pass


def media_path(ref):
    """Map a sandbox path / media URL / resource dict to a /media/raw/... path."""
    if isinstance(ref, dict):
        ref = (ref.get("variants") or {}).get("original") or ref.get("path") or ref.get("url")
        if not ref:
            raise ValueError("resource has no path or variants.original")
    ref = str(ref)
    if ref.startswith("sandbox://"):
        rel = ref[len("sandbox://"):]
    elif ref.startswith(("https://", "http://")):
        rel = urllib.parse.urlsplit(ref).path
    else:
        rel = ref
    rel = rel.lstrip("/")
    if rel.startswith("media/raw/"):
        rel = rel[len("media/raw/"):]
    rel = urllib.parse.unquote(rel)
    if not rel or ".." in rel.split("/"):
        raise ValueError(f"bad sandbox path: {ref!r}")
    return "/media/raw/" + urllib.parse.quote(rel, safe="/")


# Max payload bytes per outgoing transport frame: 65535 Noise limit minus the
# 16-byte AEAD tag and the NoiseTransportFrame field overhead, with margin.
SEND_PIECE = 60000


def join_chunks(partial, ntf):
    """Feed one NoiseTransportFrame; return the full payload once complete."""
    total = ntf.total_chunks or 1
    if total <= 1:
        return bytes(ntf.payload)
    parts = partial.setdefault(ntf.chunk_id, {})
    parts[ntf.chunk_index] = bytes(ntf.payload)
    if len(parts) < total:
        return None
    del partial[ntf.chunk_id]
    return b"".join(parts[i] for i in sorted(parts))


class ConnectionDown(ConnectionError):
    """The connection was already down when a request was made: nothing was
    sent, so the request is safe to repeat on a new connection."""


class GatewayError(RuntimeError):
    def __init__(self, status, payload):
        super().__init__(f"gateway status={status} payload={payload!r}")
        self.status = status
        self.payload = payload


def _hatch_headers(cookies, access_token=None):
    h = {
        "User-Agent": UA,
        "Referer": "https://muse.ai/",
        "Origin": "https://muse.ai",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Cookie": cookies,
    }
    if access_token:
        h["Authorization"] = f"Bearer {access_token}"
    return h


def fetch_access_token(cookies):
    r = rq.post("https://muse.ai/api/auth/check", headers=_hatch_headers(cookies),
                data=b"", impersonate="chrome", timeout=20)
    if r.status_code != 200:
        raise AuthError(f"auth/check -> {r.status_code} {r.text[:120]} (cookies expired? re-export)")
    return r.json()["access_token"]


def fetch_hatch_token(cookies, access_token, vm_id):
    r = rq.post(
        "https://muse.ai/api/hatch/token",
        headers=_hatch_headers(cookies, access_token),
        json={"vmAddress": f"wss://{vm_id}.metaaivm.com/", "vmName": vm_id},
        impersonate="chrome", timeout=20,
    )
    if r.status_code != 200:
        raise AuthError(f"hatch/token -> {r.status_code} {r.text[:160]}")
    return r.json()["token"]


def load_cookies(path):
    """Accept a curl-style 'a=b; c=d' file or {"cookies": {...}} JSON."""
    raw = open(path).read().strip()
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
            if isinstance(d, dict) and "cookies" in d and isinstance(d["cookies"], dict):
                return "; ".join(f"{k}={v}" for k, v in d["cookies"].items())
            return "; ".join(f"{k}={v}" for k, v in d.items())
        except json.JSONDecodeError:
            pass
    if "hatch_sess=" in raw or "datr=" in raw:
        for line in raw.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line and ";" in line:
                return line
        return " ".join(raw.split())
    # Netscape cookie-jar format
    parts = []
    for line in raw.splitlines():
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) >= 7:
            parts.append(f"{cols[5]}={cols[6]}")
    if not parts:
        raise AuthError(f"could not parse cookies file {path}")
    return "; ".join(parts)


def fetch_session_info(cookies):
    """Discover the user's assigned personal VM (id + gateway URL)."""
    r = rq.get("https://muse.ai/api/session", headers=_hatch_headers(cookies),
               impersonate="chrome", timeout=20)
    if r.status_code != 200:
        raise AuthError(f"api/session -> {r.status_code} (cookies expired? re-export)")
    try:
        info = r.json()
    except ValueError:
        raise AuthError(f"api/session returned non-JSON ({r.text[:80]!r})")
    if "vm_id" not in info:
        # The VM is down/restarting: {"status":"unavailable",
        # "vm_resolution_issue":{"kind":"retryable"}}. Not an auth problem,
        # so say so: retry, or wake a known VM id directly.
        raise GatewayError(-1, f"VM unavailable ({info!r}); retry shortly or "
                               "`MUSE_VM_ID=<id> muse-cli wake`")
    return info


# -- plan usage (Settings > Usage) ---------------------------------------------
# The settings page reads usage through a Next.js server action
# (fetchSubscriptionAction), not a REST route or a VM gateway method. A server
# action is a POST to any app page with a `Next-Action: <id>` header; the id
# is a hash that changes when the web app is redeployed, so it is looked up
# again from the JS bundles when the known one stops working.
SUBSCRIPTION_ACTION_NAME = "fetchSubscriptionAction"
SUBSCRIPTION_ACTION_ID = "4029ac5c4bd03136487d91ebde5de39c64b35229b4"
_ACTION_RE = re.compile(
    r'createServerReference\)\("([0-9a-f]{20,})",[^)]{0,120}"' + SUBSCRIPTION_ACTION_NAME + '"')


def parse_rsc_result(text):
    """Return the action's return value from a text/x-component (RSC) body."""
    rows = {}
    for line in text.splitlines():
        key, sep, val = line.partition(":")
        if sep and val.startswith(("{", "[")):
            try:
                rows[key] = json.loads(val)
            except ValueError:
                pass
    head = rows.get("0")
    ref = head.get("a") if isinstance(head, dict) else None
    if isinstance(ref, str) and ref.startswith("$@"):
        return rows.get(ref[2:])
    return rows.get("1", head)


def find_action_id(cookies, name=SUBSCRIPTION_ACTION_NAME, page="https://muse.ai/settings"):
    """Scan the web app's JS chunks for a server action's id (slow: ~1k files)."""
    from concurrent.futures import ThreadPoolExecutor
    h = {"User-Agent": UA, "Cookie": cookies}
    html = rq.get(page, headers=h, impersonate="chrome", timeout=30).text
    pat = re.compile(r'createServerReference\)\("([0-9a-f]{20,})",[^)]{0,120}"' + re.escape(name) + '"')
    todo, seen = set(re.findall(r"/_next/static/chunks/[A-Za-z0-9_.-]+\.js", html)), set()

    def get(path):
        try:
            return rq.get("https://muse.ai" + path, impersonate="chrome", timeout=30).text
        except Exception:
            return ""
    with ThreadPoolExecutor(16) as ex:
        while todo and len(seen) < 3000:
            batch = list(todo - seen)
            todo = set()
            seen.update(batch)
            for text in ex.map(get, batch):
                m = pat.search(text)
                if m:
                    return m.group(1)
                todo.update("/_next/" + c for c in
                            re.findall(r"static/chunks/[A-Za-z0-9_.-]+\.js", text))
    return None


def _call_action(cookies, action_id, args, page="https://muse.ai/settings"):
    h = _hatch_headers(cookies)
    h.update({"Accept": "text/x-component", "Content-Type": "text/plain;charset=UTF-8",
              "Next-Action": action_id})
    r = rq.post(page, headers=h, data=json.dumps(args), impersonate="chrome", timeout=30)
    if r.status_code in (401, 403):
        raise AuthError(f"server action -> {r.status_code} (cookies expired? re-export)")
    if r.status_code != 200 or "x-component" not in r.headers.get("content-type", ""):
        return None  # most likely a stale action id
    return parse_rsc_result(r.text)


def fetch_subscription(cookies, action_id=None, include_agreement=False):
    """Plan + usage as the Settings page shows it.

    Returns (subscription_dict, action_id_used). Keys include tier{name,
    tierCode,isPaid}, usage{state,percentUsed,resetsAt(unix s),quotaStatus},
    statusSubtitle, usageRowLabel, topupBalance/topupTotal (tokens),
    topupRowLabel, entryPointCtaLabel.
    """
    args = [{"includeAgreement": include_agreement}]
    tried = []
    for aid in (action_id, SUBSCRIPTION_ACTION_ID):
        if aid and aid not in tried:
            tried.append(aid)
            res = _call_action(cookies, aid, args)
            if res is not None:
                break
    else:
        aid = find_action_id(cookies)
        if not aid:
            raise GatewayError(-1, f"could not find {SUBSCRIPTION_ACTION_NAME} in the web app bundle")
        res = _call_action(cookies, aid, args)
    if not isinstance(res, dict) or not res.get("success") or not res.get("subscription"):
        err = res.get("error") if isinstance(res, dict) else res
        raise GatewayError(-1, f"{SUBSCRIPTION_ACTION_NAME} failed: {err!r}")
    return res["subscription"], aid


def fetch_invite(cookies):
    """Invite-friends sheet data: GET https://muse.ai/api/hatch/invite.

    Read-only. Keys include invite_state (ELIGIBLE / NOT_ELIGIBLE / ...),
    code, invite_link, max_uses, use_count, uses_remaining, share_text,
    reward{title, body} (earned stats as display text), subtitle,
    has_redeemed_invite_code. `code` stays null until the user opens the
    sheet once: the web app then POSTs the same URL to create the code. That
    POST changes account state, so this client never sends it.
    """
    r = rq.get("https://muse.ai/api/hatch/invite", headers=_hatch_headers(cookies),
               impersonate="chrome", timeout=20)
    if r.status_code in (401, 403):
        raise AuthError(f"api/hatch/invite -> {r.status_code} (cookies expired? re-export)")
    if r.status_code != 200:
        raise GatewayError(r.status_code, r.text[:200])
    try:
        d = r.json()
    except ValueError:
        raise GatewayError(-1, f"api/hatch/invite returned non-JSON ({r.text[:80]!r})")
    if not isinstance(d, dict):
        raise GatewayError(-1, f"api/hatch/invite returned {type(d).__name__}")
    return d


def create_invite(cookies):
    """Create this account's invite code (WRITES; what opening Invite friends does).

    POST https://muse.ai/api/hatch/invite, no body. Returns {code, max_uses,
    use_count, invite_link, share_text}. Only needed while GET returns
    code: null.
    """
    r = rq.post("https://muse.ai/api/hatch/invite", headers=_hatch_headers(cookies),
                data=b"", impersonate="chrome", timeout=30)
    if r.status_code in (401, 403):
        raise AuthError(f"api/hatch/invite POST -> {r.status_code} (cookies expired? re-export)")
    if r.status_code != 200:
        raise GatewayError(r.status_code, r.text[:200])
    return r.json()


def redeem_invite_code(cookies, code):
    """Redeem a friend's invite code (WRITES account state; one-shot).

    POST https://muse.ai/api/hatch/invite-code/redeem {code,
    supportsRedemptionStatus: true}. Returns (http_status, body) where body
    is {"success": true, "redemptionStatus": "redeemed"|...} or {"success":
    false, "reason": "window_expired"|"invalid"|"already_redeemed"|...,
    "detail": "..."}. Only valid within 48 h of the account joining.
    """
    code = code.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{6,16}", code):
        raise ValueError(f"not an invite code: {code!r}")
    r = rq.post("https://muse.ai/api/hatch/invite-code/redeem", headers=_hatch_headers(cookies),
                data=json.dumps({"code": code, "supportsRedemptionStatus": True}),
                impersonate="chrome", timeout=30)
    if r.status_code in (401, 403):
        raise AuthError(f"invite-code/redeem -> {r.status_code} (cookies expired? re-export)")
    try:
        body = r.json()
    except ValueError:
        body = {"success": False, "reason": "unknown", "detail": r.text[:200]}
    return r.status_code, body


class Gateway:
    """One authenticated connection to the personal VM gateway.

    The connection is multiplexed like the web app's: every request or
    subscription is its own stream_id, and one reader thread demultiplexes
    incoming frames into per-stream queues. So any number of threads can
    run requests and subscriptions on one warm connection at once (no
    reconnect + Noise handshake per call). libcurl's handle is never used by
    two threads at once (every ws_send/ws_recv runs under _io_lock; the
    reader waits for data with select() outside the lock), Noise encrypt is
    serialized by _send_lock and only the reader thread decrypts.
    """

    def __init__(self, cookies, vm_id=None, access_token=None, hatch_token=None):
        if vm_id is None:
            vm_id = fetch_session_info(cookies)["vm_id"]
        self.vm_id = vm_id
        access_token = access_token or fetch_access_token(cookies)
        hatch_token = hatch_token or fetch_hatch_token(cookies, access_token, vm_id)
        self.access_token, self.hatch_token = access_token, hatch_token
        url = (f"wss://{GATEWAY_HOST}/v1/noise?vm_id={vm_id}"
               f"&auth_token={urllib.parse.quote(hatch_token, safe='')}")
        self.ws = WebSocket()
        # verify=True is required: curl_cffi's WebSocket.connect defaults to
        # verify=None, which disables TLS certificate verification entirely.
        self.ws.connect(url, impersonate="chrome", timeout=20, verify=True)
        noise = NoiseConnection.from_name(b"Noise_XX_25519_AESGCM_SHA256")
        noise.set_as_initiator()
        noise.set_keypair_from_private_bytes(Keypair.STATIC, os.urandom(32))
        noise.start_handshake()
        self.ws.send_bytes(bytes(noise.write_message(b"")))
        m2, _ = self.ws.recv()
        noise.read_message(bytes(m2))
        self.ws.send_bytes(bytes(noise.write_message(b"")))
        assert noise.handshake_finished
        self.noise = noise
        self.stream = 1
        self._send_lock = threading.Lock()
        self._recv_lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._partial = {}  # chunk_id -> {chunk_index: payload}
        self._streams = {}  # stream_id -> Queue of ServiceFrame (or the fatal exception)
        self._streams_lock = threading.Lock()
        self.closed = False
        self.dead = None    # the exception that killed the reader, if any
        self.last_used = time.time()
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="muse-gw-reader")
        self._reader.start()

    @property
    def alive(self):
        return not self.closed and self.dead is None

    # -- low-level framing -------------------------------------------------
    def _write(self, data):
        if getattr(self, "_io_lock", None) is None:     # bare objects in tests
            self.ws.send_bytes(data)
            return
        from curl_cffi.const import CurlECode, CurlWsFlag
        from curl_cffi.curl import CurlError
        from select import select
        sock = self.ws.curl.getinfo(CurlInfo.ACTIVESOCKET)
        off = 0
        while off < len(data):
            n = None
            with self._io_lock:
                try:
                    n = self.ws.curl.ws_send(data[off:], CurlWsFlag.BINARY)
                except CurlError as e:
                    if e.code != CurlECode.AGAIN:
                        raise ConnectionError(f"gateway send failed: {e}") from None
            if n is None:
                _r, w, _x = select([], [sock], [], 5)
                if not w:
                    raise ConnectionError("gateway send timed out")
                continue
            off += n

    def _recv_message(self):
        """One complete websocket message (reader thread only)."""
        from curl_cffi.const import CurlECode, CurlWsFlag
        from curl_cffi.curl import CurlError
        from select import select
        sock = self.ws.curl.getinfo(CurlInfo.ACTIVESOCKET)
        chunks = []
        while True:
            if self.closed:
                raise ConnectionError("gateway connection closed")
            frame = None
            with self._io_lock:
                try:
                    chunk, frame = self.ws.curl.ws_recv()
                except CurlError as e:
                    if e.code != CurlECode.AGAIN:
                        raise ConnectionError(f"gateway receive failed: {e}") from None
            if frame is None:
                select([sock], [], [], 0.25)
                continue
            if frame.flags & CurlWsFlag.CLOSE:
                raise ConnectionError("gateway closed the websocket")
            if frame.flags & (CurlWsFlag.PING | CurlWsFlag.PONG):
                continue
            chunks.append(chunk)
            if frame.bytesleft == 0 and not frame.flags & CurlWsFlag.CONT:
                return b"".join(chunks)

    def _send_envelope(self, service_id, frame_bytes):
        # A Noise transport message holds at most 65535 bytes (ciphertext,
        # incl. the 16-byte tag), so a big request (e.g. an fs.write of a
        # photo) is split into transport frames sharing a chunk_id, the same
        # framing the server uses for large responses (see join_chunks).
        outer = ServiceRequest(service=service_id, payload=frame_bytes).SerializeToString()
        chunk_id = struct.unpack("<q", os.urandom(8))[0]
        pieces = [outer[i:i + SEND_PIECE] for i in range(0, len(outer), SEND_PIECE)] or [b""]
        with self._send_lock:
            for idx, piece in enumerate(pieces):
                fr = NoiseTransportFrame(chunk_id=chunk_id, chunk_index=idx,
                                         total_chunks=len(pieces), payload=piece)
                self._write(bytes(self.noise.encrypt(fr.SerializeToString())))

    def _read_frame(self):
        # Only the reader thread calls this. A large response (e.g.
        # /approvals with many recent entries) arrives as several transport
        # frames sharing a chunk_id; join them in chunk_index order before
        # parsing the ServiceResponse.
        with self._recv_lock:
            while True:
                data = self._recv_message()
                pt = bytes(self.noise.decrypt(bytes(data)))
                ntf = NoiseTransportFrame()
                ntf.ParseFromString(pt)
                payload = join_chunks(self._partial, ntf)
                if payload is not None:
                    break
        sr = ServiceResponse()
        sr.ParseFromString(payload)
        sf = ServiceFrame()
        sf.ParseFromString(sr.payload)
        return sf

    def _read_loop(self):
        try:
            while not self.closed:
                sf = self._read_frame()
                with self._streams_lock:
                    q = self._streams.get(sf.stream_id)
                if q is not None:
                    q.put(sf)
        except Exception as e:  # noqa: BLE001
            if not self.closed:
                self.dead = e
            with self._streams_lock:
                queues = list(self._streams.values())
            for q in queues:
                q.put(e if isinstance(e, Exception) else ConnectionError(str(e)))

    def _open(self, method, path_params=None, body=None, query=None, route=None, track=True):
        """Start a stream. track=False: nobody reads its frames (they are dropped)."""
        if self.dead is not None or self.closed:
            raise ConnectionDown(f"gateway connection is down ({self.dead or 'closed'})")
        route = route or ROUTES[method]
        path = route["path"]
        for k, v in (path_params or {}).items():
            path = path.replace("{" + k + "}", urllib.parse.quote(str(v), safe=""))
        if query:
            qs = urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
            if qs:
                path = path + ("&" if "?" in path else "?") + qs
        raw = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        req = ApplicationRequest(verb=route["http"], path=path, body=raw, end_body=True)
        h = req.headers.add()
        h.key = "x-request-id"
        h.value = str(uuid.uuid4())
        if raw:
            h2 = req.headers.add()
            h2.key = "content-type"
            h2.value = "application/json"
        with self._streams_lock:
            sid = self.stream
            self.stream += 1
            if track:
                self._streams[sid] = queue_mod.Queue()
        frame = ServiceFrame(stream_id=sid)
        frame.request.CopyFrom(req)
        self.last_used = time.time()
        try:
            self._send_envelope(SERVICE_IDS.get(route.get("service", "daemon"), 0),
                                frame.SerializeToString())
        except BaseException:
            self._forget(sid)
            raise
        return sid

    def _forget(self, sid):
        with self._streams_lock:
            self._streams.pop(sid, None)

    def _cancel(self, sid, service=0):
        """Tell the server we are done with a stream (the web app's encryptReset)."""
        self._forget(sid)
        if not self.alive:
            return
        frame = ServiceFrame(stream_id=sid)
        frame.reset.code = 1   # CANCELLED
        try:
            self._send_envelope(service, frame.SerializeToString())
        except Exception:  # noqa: BLE001
            pass

    def _next(self, sid, timeout):
        with self._streams_lock:
            q = self._streams.get(sid)
        if q is None:
            raise ConnectionError("stream is not open")
        if self.dead is not None and q.empty():
            raise ConnectionError(f"gateway connection is down ({self.dead})")
        sf = q.get(timeout=timeout)
        if isinstance(sf, Exception):
            raise ConnectionError(f"gateway connection lost: {sf}")
        return sf

    # -- unary request/response --------------------------------------------
    def request(self, method, path_params=None, body=None, query=None, timeout=30, route=None):
        route = route or ROUTES[method]
        if route["http"] == "GET" and body is not None and query is None:
            query, body = body, None
        sid = self._open(method, path_params, body, query, route=route)
        status, chunks, deadline = None, [], time.time() + timeout
        ended = False
        try:
            while True:
                left = deadline - time.time()
                if left <= 0:
                    break
                try:
                    sf = self._next(sid, left)
                except queue_mod.Empty:
                    break
                kind = sf.WhichOneof("kind")
                if kind == "response":
                    status = sf.response.status
                    if sf.response.body:
                        chunks.append(bytes(sf.response.body))
                    if sf.response.end_body:
                        ended = True
                        break
                elif kind == "body_chunk":
                    chunks.append(bytes(sf.body_chunk.data))
                    if sf.body_chunk.end_body:
                        ended = True
                        break
                elif kind == "reset":
                    ended = True
                    raise GatewayError(-1, f"stream reset {sf.reset.code}: {sf.reset.reason}")
        finally:
            if ended:
                self._forget(sid)
            else:
                self._cancel(sid, SERVICE_IDS.get(route.get("service", "daemon"), 0))
        if status is None:
            raise TimeoutError(f"no response for {method}")
        payload = b"".join(chunks)
        if status < 200 or status >= 300:
            raise GatewayError(status, payload[:500].decode("utf-8", "replace"))
        return payload

    def ping(self, timeout=10):
        """The web app's heartbeat (connection.ping every 30 s). presence
        "away" so Muse never takes the bot for the user looking at the app."""
        return self.call_json("connection.ping", body={"presence": "away"}, timeout=timeout)

    # -- files the agent produced (sandbox://...) ---------------------------
    def download_file(self, ref, timeout=300):
        """Return the bytes of a file in the agent's sandbox.

        `ref` is a `sandbox://workspace/...` path, a media URL from a
        resource's `variants` (https://<vm>.metaaivm.com/media/raw/...), a
        bare `/media/raw/...` path, or a resource dict with `path`/`variants`.
        The web app loads these from GET /media/raw/<path> on the VM; here
        the same request rides the Noise tunnel (plain TLS to the per-VM
        host is reset at the edge).
        """
        path = media_path(ref)
        route = {"method": "media.raw", "http": "GET", "path": path}
        return self.request("media.raw", timeout=timeout, route=route)

    def call_json(self, method, path_params=None, body=None, query=None, timeout=30):
        raw = self.request(method, path_params, body, query, timeout)
        try:
            d = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise GatewayError(-1, f"non-JSON response: {raw[:200]!r}")
        if isinstance(d, dict) and d.get("ok") is False:
            raise GatewayError(-1, f"api error: {d.get('error')}")
        return d.get("result", d) if isinstance(d, dict) else d

    # -- subscriptions (ndjson event streams) -------------------------------
    def subscribe_raw(self, method, path_params=None, body=None, query=None,
                      max_records=100, idle_timeout=10, overall_timeout=120,
                      on_record=None, should_stop=None, on_open=None):
        """Open a subscription; returns list of raw record bytes.

        ``on_record`` is called with each record the moment it arrives, for
        callers that show events live instead of after the stream ends. If it
        returns a truthy value the subscription stops right there.
        ``should_stop`` is polled about every 0.5 s, even while no records
        arrive; a truthy value ends the subscription. ``on_open`` is called
        once the subscription request is on the wire (e.g. to post a message
        only after the stream that will carry its events exists). A
        subscription we stop is reset on the server (the connection stays up).
        """
        sid = self._open(method, path_params, body, query)
        if on_open is not None:
            on_open()
        buf, records = b"", []
        deadline = time.time() + overall_timeout
        idle = time.time() + idle_timeout
        halted = ended = False
        try:
            while not halted and time.time() < deadline and len(records) < max_records:
                if should_stop is not None and should_stop():
                    halted = True
                    break
                try:
                    sf = self._next(sid, 0.5)
                except queue_mod.Empty:
                    if time.time() > idle:
                        break
                    continue
                idle = time.time() + idle_timeout
                kind = sf.WhichOneof("kind")
                if kind == "response":
                    if sf.response.body:
                        buf += bytes(sf.response.body)
                    if sf.response.end_body:
                        ended = True
                        if not buf:
                            break
                elif kind == "body_chunk":
                    buf += bytes(sf.body_chunk.data)
                    if sf.body_chunk.end_body:
                        ended = True
                elif kind == "reset":
                    ended = True
                    break
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        records.append(line)
                        if on_record and on_record(line):
                            halted = True
                            break
                        if len(records) >= max_records:
                            break
                if ended:
                    break
        finally:
            if ended:
                self._forget(sid)
            else:
                self._cancel(sid)
        if buf.strip() and not halted:
            records.append(buf)
            if on_record:
                on_record(buf)
        return records

    def subscribe_json(self, *a, **kw):
        out = []
        for line in self.subscribe_raw(*a, **kw):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def close(self):
        self.closed = True
        reader = getattr(self, "_reader", None)
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1)     # it notices `closed` within 0.25 s
        try:
            with getattr(self, "_io_lock", threading.Lock()):
                self.ws.close()
        except Exception:  # noqa: BLE001
            pass

    def lease(self):
        """This connection for code that calls close() when done: close() on
        the lease is a no-op, so a shared warm connection stays up."""
        return Lease(self)


class Lease:
    def __init__(self, gw):
        self._gw = gw

    def __getattr__(self, name):
        return getattr(self._gw, name)

    def close(self):
        pass
