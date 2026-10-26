"""`muse-cli telegram`: operate your Muse from a Telegram bot.

One long-polling process (getUpdates, no webhook, no extra dependency) that
bridges a Telegram bot to the active muse-cli profile:

- plain messages go to Muse in the chat's current conversation and the reply
  streams into one Telegram message (edited about once a second);
- questions/widgets become inline buttons; approvals are auto-approved with
  Muse's forever choice (allow_always) unless /autoapprove off;
- photos/documents are uploaded to System Files (telegram-inbox/);
- slash commands cover chats, history, approvals, files, fs, feed, goals,
  ideas, usage, profiles, wake, update, stop and raw;
- a background monitor forwards Muse messages that arrive outside a turn the
  bot started, new approvals, and a one-time "cookies expired" notice.

Only Telegram user ids in the allowlist get any answer; everything else is
dropped silently. The bot token lives in telegram.json (mode 600) in the
profile folder and is never logged.
"""
import collections
import getpass
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from . import __version__
from . import cli
from .gateway import AuthError, GatewayError
from .tgapi import (CHUNK, UPLOAD_LIMIT, TelegramAPI, TelegramError, button, doc_name, esc,
                    file_kind, keyboard, md_to_html, needs_document, split_text)
from .wait import FOREVER, describe_approval, event_name, offers_forever, pending_approvals

log = logging.getLogger("muse_cli.telegram")

CONFIG_NAME = "telegram.json"
STATE_NAME = "telegram-state.json"
INBOX = "telegram-inbox"
EDIT_INTERVAL = 1.0          # seconds between edits of a streaming reply
LONG_TURN = 60               # turns longer than this get a separate "done" ping
TURN_TIMEOUT = 6 * 3600      # how long one turn is followed before handing off
POLL_FALLBACK = 3.0          # history poll when no live deltas arrive (side chats)
WATCH_FLUSH = 3.0            # /watch batches events at most this often
CHATS_PAGE = 8
MAX_FILES_PER_TURN = 6
MAIN = "main"                # state key for the main chat (session_id None)

COMMANDS = [
    ("help", "what the bot can do"),
    ("status", "what Muse is doing, identity, VM"),
    ("stop", "stop Muse's current turn"),
    ("chats", "open another conversation"),
    ("main", "switch to the main chat"),
    ("new", "start a side chat: /new [title]"),
    ("history", "last messages: /history [N]"),
    ("unread", "unread counts"),
    ("approvals", "pending approvals with buttons"),
    ("autoapprove", "on | off | status (default on)"),
    ("watch", "on | off: forward live tool/progress events"),
    ("files", "recent files Muse made, as buttons"),
    ("fs", "System Files: ls tree cat get find stat mkdir mv rm"),
    ("feed", "your feed"),
    ("goals", "goals; /goal <id> for one"),
    ("goal", "one goal: /goal <id>"),
    ("ideas", "idea cards with Execute buttons"),
    ("usage", "plan usage and reset time"),
    ("rename", "rename this side chat: /rename <title>"),
    ("pin", "pin this side chat"),
    ("unpin", "unpin this side chat"),
    ("archive", "archive this side chat"),
    ("unarchive", "unarchive this side chat"),
    ("delete", "delete this side chat (asks first)"),
    ("profile", "show / switch muse-cli profile"),
    ("wake", "wake the VM"),
    ("login", "replace the muse.ai cookies (owner)"),
    ("update", "upgrade muse-cli and restart (owner)"),
    ("raw", "raw gateway call: /raw <method> [json] (owner)"),
]

HELP = """<b>Muse on Telegram</b>
Type anything to talk to Muse in the current conversation. Send a photo or file to put it in Muse's System Files (<code>telegram-inbox/</code>) and tell Muse about it.

<b>Conversation</b>
/chats open another chat · /main · /new [title]
/history [N] · /unread · /stop
/rename &lt;title&gt; · /pin · /unpin · /archive · /unarchive · /delete

<b>Muse</b>
/status · /approvals · /autoapprove on|off · /watch on|off
/files · /feed · /goals · /goal &lt;id&gt; · /ideas · /usage · /wake

<b>System Files</b>
/fs ls [path] · /fs tree [path] [depth] · /fs cat &lt;path&gt; · /fs get &lt;path&gt;
/fs find &lt;name&gt; · /fs stat &lt;path&gt; · /fs mkdir &lt;path&gt; · /fs mv &lt;src&gt; &lt;dst&gt; · /fs rm [-r] &lt;path&gt;

<b>Admin</b>
/profile · /login (new cookies) · /update · /raw &lt;method&gt; [json]"""


# -- files ------------------------------------------------------------------------
def write_private_json(path, data):
    """Atomically write JSON readable only by the owner (mode 600)."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def read_json(path):
    try:
        with open(path) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def config_path(home=None):
    return os.path.join(home or cli.CONFIG_DIR, CONFIG_NAME)


def state_path(home=None):
    return os.path.join(home or cli.CONFIG_DIR, STATE_NAME)


# -- Muse access ------------------------------------------------------------------
class Muse:
    """The Muse calls the bot makes, on the active muse-cli profile.

    Every call opens its own gateway connection (safe from several threads).
    `connector(cfg)` defaults to `cli.connect`; tests pass a fake gateway.
    """

    def __init__(self, connector=None, on_auth_error=None, on_auth_ok=None):
        self.connector = connector
        self.cfg = None
        self.on_auth_error = on_auth_error
        self.on_auth_ok = on_auth_ok

    def reload(self):
        self.cfg = cli.load_config()
        return self.cfg

    def config(self):
        return self.cfg if self.cfg is not None else self.reload()

    def connect(self, cfg=None):
        try:
            gw = (self.connector or cli.connect)(cfg or self.config())
        except AuthError as e:
            if self.on_auth_error:
                self.on_auth_error(e)
            raise
        if self.on_auth_ok:
            self.on_auth_ok()
        return gw

    @contextmanager
    def gw(self):
        g = self.connect()
        try:
            yield g
        finally:
            g.close()

    def call(self, method, **kw):
        with self.gw() as g:
            return g.call_json(method, **kw)

    def history(self, session_id=None, limit=20):
        body = {"limit": limit}
        if session_id:
            body["session_id"] = session_id
        evs = self.call("chat.history", body=body).get("chat_events", [])
        return sorted(evs, key=lambda e: e.get("seq") or 0)

    def fs(self, fn):
        from .fs import FS
        with self.gw() as g:
            return fn(FS(g))


# -- small helpers -----------------------------------------------------------------
def _payload(ev):
    p = ev.get("payload")
    return p if isinstance(p, dict) else {}


def ev_text(ev):
    p = _payload(ev)
    return p.get("display_text") or p.get("content") or ev.get("display_text") or ""


def fmt_secs(s):
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def option_label(o):
    if isinstance(o, dict):
        return str(o.get("label") or o.get("text") or o.get("title") or o.get("value") or "")
    return str(o)


def option_value(o):
    if isinstance(o, dict):
        return str(o.get("value") or o.get("label") or o.get("text") or o.get("title") or "")
    return str(o)


_OPT_RE = re.compile(r"^\s*(?:\(?\d{1,2}[.)]|\(?[A-Ha-h][.)]|[-•*])\s+(.{1,80}?)\s*$")
GENERIC_ANSWERS = [("👍 Yes", "Yes"), ("👎 No", "No"), ("▶️ Go ahead", "Go ahead")]


def question_options(res):
    """Buttons for a turn that ended asking something: widget options first,
    then a short numbered/bulleted list at the end of the reply, else generic."""
    for w in res.get("widgets") or []:
        opts = [o for o in (w.get("options") or []) if option_label(o)]
        if opts:
            return [(option_label(o), option_value(o)) for o in opts[:10]]
    replies = res.get("replies") or []
    text = replies[-1]["text"] if replies else ""
    tail = [ln for ln in text.splitlines() if ln.strip()][-12:]
    found = []
    for ln in tail:
        m = _OPT_RE.match(ln)
        if m:
            found.append(re.sub(r"[*_`]", "", m.group(1)).strip())
        elif found and not ln.strip().endswith("?"):
            found = []  # options must be the last block (before a closing question)
    if 2 <= len(found) <= 8:
        return [(f, f) for f in found]
    return list(GENERIC_ANSWERS)


def _tool_name(p):
    return p.get("tool_name") or p.get("name") or p.get("display_name") or p.get("title")


def watch_line(ev):
    """One short line for /watch, or None for noise (text deltas etc.)."""
    name = event_name(ev) or ""
    p = _payload(ev)
    if name == "delta.tool_start":
        detail = p.get("display_text") or p.get("summary") or p.get("activity_text") or ""
        return f"🔧 {_tool_name(p) or 'tool'}" + (f": {str(detail)[:120]}" if detail else "")
    if name == "agent.status":
        t = p.get("activity_text") or p.get("status_text") or p.get("text")
        return f"⏳ {_activity(t)[:160]}" if t else None
    if name == "task.status":
        if p.get("root_agent_id") and p.get("agent_id") and p["agent_id"] != p["root_agent_id"]:
            return f"   ↳ subagent {p.get('status')}"
        return f"📋 task {p.get('status')}" if p.get("status") else None
    if name == "delta.message_done":
        return "💬 message done"
    if name.startswith("approvals") and (p.get("pending_approvals") or p.get("pending")):
        return "🔐 approval pending"
    return None


def _activity(t):
    """Muse says "is working" / "is responding" about itself: name the subject."""
    t = str(t).strip()
    return "Muse " + t if t.startswith(("is ", "was ")) else t


def status_from_event(ev):
    name = event_name(ev) or ""
    p = _payload(ev)
    if name == "delta.tool_start":
        tool = _tool_name(p)
        return f"🔧 {tool}" if tool else "🔧 using a tool"
    if name == "agent.status":
        t = p.get("activity_text") or p.get("status_text")
        return f"⏳ {_activity(t)[:120]}" if t else None
    return None


def _strip_tags(text):
    import html as _h
    return _h.unescape(re.sub(r"<[^>]+>", "", text))


# -- one Muse turn ---------------------------------------------------------------
class Turn:
    """A message the bot sent to Muse, streamed into one Telegram message."""

    def __init__(self, bot, chat_id, session_id, title):
        self.bot = bot
        self.chat_id = chat_id
        self.session_id = session_id
        self.title = title
        self.msg_id = None
        self.user_seq = None
        self.parts = collections.OrderedDict()   # message id -> text so far
        self.status = "⏳ Muse is working…"
        self.started = bot.clock()
        self.last_edit = 0.0
        self.last_delta = 0.0
        self.shown = None
        self.dirty = False
        self.cancel_requested = False
        self.stop_waiting = False
        self.finished = False
        self.lock = threading.Lock()

    def on_event(self, line):
        """Live chat.subscribe record (subscription thread)."""
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            return
        name = event_name(ev) or ""
        p = _payload(ev)
        with self.lock:
            if name == "delta.text_append":
                key = str(p.get("message_id") or p.get("message_seq") or "m")
                self.parts[key] = self.parts.get(key, "") + str(p.get("text") or "")
                self.last_delta = self.bot.clock()
                self.dirty = True
            elif name == "delta.message_done":
                key = str(p.get("message_id") or p.get("message_seq") or "m")
                full = p.get("content") or p.get("display_text") or p.get("text")
                if isinstance(full, str) and full.strip():
                    self.parts[key] = full
                    self.dirty = True
                self.last_delta = self.bot.clock()
            else:
                st = status_from_event(ev)
                if st and st != self.status:
                    self.status = st
                    self.dirty = True
        self.maybe_edit()

    def set_partial_from_history(self, events):
        """Fallback when no live deltas arrive (side chats): partial replies from history."""
        texts = collections.OrderedDict()
        for e in events:
            if (e.get("seq") or 0) > (self.user_seq or 0) and e.get("event_name") == "message.assistant":
                t = ev_text(e)
                if t:
                    texts[str(_payload(e).get("message_id") or e.get("seq"))] = t
        if texts:
            with self.lock:
                if list(texts.values()) != list(self.parts.values()):
                    self.parts = texts
                    self.dirty = True

    def render(self):
        from .wait import clean_text
        body = clean_text("\n\n".join(t for t in self.parts.values() if t).strip())
        if len(body) > 3300:
            body = "…" + body[-3300:]
        elapsed = fmt_secs(self.bot.clock() - self.started)
        return (body + "\n\n" if body else "") + f"{self.status} · {elapsed}"

    def maybe_edit(self, force=False):
        if self.msg_id is None or self.finished:
            return
        now = self.bot.clock()
        if not force and (not self.dirty or now - self.last_edit < self.bot.edit_interval):
            return
        text = self.render()
        if text == self.shown:
            return
        self.dirty = False
        self.last_edit = now
        self.shown = text
        self.bot.edit_plain(self.chat_id, self.msg_id, text)

    def should_stop(self):
        return self.stop_waiting


# -- the bot ----------------------------------------------------------------------
class Bot:
    def __init__(self, api, muse=None, home=None, sync=False, clock=time.time):
        self.api = api
        self.home = home or cli.CONFIG_DIR       # where telegram.json lives (fixed)
        self.conf = read_json(config_path(self.home))
        self.state = read_json(state_path(self.home))
        self.sync = sync                          # tests: run everything inline
        self.clock = clock
        self.edit_interval = EDIT_INTERVAL
        self.lock = threading.RLock()
        self.muse = muse or Muse()
        self.muse.on_auth_error = self.auth_problem
        self.muse.on_auth_ok = self.auth_ok
        self.callbacks = collections.OrderedDict()
        self.turns = {}            # chat_id -> Turn
        self.queues = collections.defaultdict(collections.deque)
        self.workers = {}
        self.awaiting = {}         # chat_id -> "cookies"
        self.claimed = set()       # approval ids already being decided
        self.notified = set()      # approval ids already announced
        self.auto_log = []         # (time, approval_id, title) decided with allow_always
        self.monitors = {}
        self.watch_buf = []
        self.watch_last = 0.0
        self.pool = None if sync else ThreadPoolExecutor(max_workers=4, thread_name_prefix="tg-cmd")
        self.stopping = False
        self.restart_hook = None
        self.state.setdefault("chats", {})
        self.state.setdefault("watermarks", {})
        self.state.setdefault("auto_approve", True)   # the user's rule: never block Muse
        self.state.setdefault("watch", False)
        prof = self.state.get("profile")
        if prof and prof != cli.PROFILE:
            try:
                cli.activate_profile(cli.validate_profile_name(prof))
            except ValueError:
                pass
        self.state["profile"] = cli.PROFILE

    # -- config / state -------------------------------------------------------
    @property
    def allowed(self):
        ids = set(int(x) for x in self.conf.get("allowed_user_ids") or [])
        if self.conf.get("owner_id"):
            ids.add(int(self.conf["owner_id"]))
        return ids

    @property
    def owner(self):
        o = self.conf.get("owner_id")
        return int(o) if o else None

    @property
    def profile(self):
        return cli.PROFILE

    @property
    def auto_approve(self):
        return bool(self.state.get("auto_approve", True))

    def save_state(self):
        with self.lock:
            write_private_json(state_path(self.home), self.state)

    def chat_state(self, chat_id):
        with self.lock:
            chats = self.state["chats"].setdefault(self.profile, {})
            return chats.setdefault(str(chat_id), {"session_id": None, "title": "main chat"})

    def set_chat(self, chat_id, session_id, title):
        with self.lock:
            self.state["chats"].setdefault(self.profile, {})[str(chat_id)] = {
                "session_id": session_id,
                "title": title or ("side chat" if session_id else "main chat")}
            self.save_state()
        self.ensure_monitors()

    def watermark(self, session_id):
        return self.state["watermarks"].get(self.profile, {}).get(session_id or MAIN)

    def set_watermark(self, session_id, seq):
        if seq is None:
            return
        with self.lock:
            wm = self.state["watermarks"].setdefault(self.profile, {})
            key = session_id or MAIN
            if key not in wm or seq > (wm.get(key) or 0):
                wm[key] = seq
                self.save_state()

    def session_busy(self, session_id):
        return any(t.session_id == session_id and not t.finished for t in list(self.turns.values()))

    # -- telegram output ------------------------------------------------------
    def send(self, chat_id, text, markup=None, html=True, reply_to=None, silent=False):
        """Send text (split when long). html=True: TEXT is HTML; "md": Markdown,
        converted; False: plain. Falls back to plain text on a parse error.
        Returns the last message id."""
        mid = None
        pieces = split_text(text) if len(text) > CHUNK else [text]
        for i, piece in enumerate(pieces):
            last = i == len(pieces) - 1
            mid = self._send_one(chat_id, piece, markup if last else None, html, reply_to, silent)
        return mid

    def _send_one(self, chat_id, text, markup, html, reply_to, silent):
        kw = {"chat_id": chat_id, "reply_markup": markup, "reply_to_message_id": reply_to,
              "disable_web_page_preview": True, "disable_notification": True if silent else None}
        if html:
            body = md_to_html(text) if html == "md" else text
            if len(body) <= 4096:
                try:
                    return self.api.call("sendMessage", text=body, parse_mode="HTML", **kw)["message_id"]
                except TelegramError as e:
                    if not e.parse_error:
                        raise
            if html is True:
                text = _strip_tags(text)
        return self.api.call("sendMessage", text=text[:4096], **kw)["message_id"]

    def send_md(self, chat_id, text, markup=None, **kw):
        return self.send(chat_id, text, markup, html="md", **kw)

    def edit(self, chat_id, msg_id, text, markup=None, html="md"):
        """Edit a message, HTML first with plain fallback; ignores 'not modified'."""
        try:
            if html:
                body = md_to_html(text) if html == "md" else text
                if len(body) <= 4096:
                    try:
                        self.api.call("editMessageText", chat_id=chat_id, message_id=msg_id, text=body,
                                      parse_mode="HTML", reply_markup=markup,
                                      disable_web_page_preview=True)
                        return
                    except TelegramError as e:
                        if not e.parse_error:
                            raise
                if html is True:
                    text = _strip_tags(text)
            self.api.call("editMessageText", chat_id=chat_id, message_id=msg_id, text=text[:4096],
                          reply_markup=markup, disable_web_page_preview=True)
        except TelegramError as e:
            if not e.not_modified:
                raise

    def edit_plain(self, chat_id, msg_id, text):
        try:
            self.edit(chat_id, msg_id, text, html=False)
        except (TelegramError, ConnectionError) as e:
            log.debug("stream edit failed: %s", e)

    def send_doc(self, chat_id, name, data, caption=None, markup=None):
        kind = file_kind(name) if len(data) <= 10 * 1024 * 1024 else "document"
        try:
            return self.api.send_file(chat_id, name, data, caption=caption, kind=kind,
                                      reply_markup=markup)
        except TelegramError:
            if kind == "photo":   # odd image formats: fall back to a document
                return self.api.send_file(chat_id, name, data, caption=caption, kind="document",
                                          reply_markup=markup)
            raise

    def reply_long(self, chat_id, text, stem="output", pre=False, markup=None):
        """Short output as a message (<pre> when pre), long output as a file."""
        if len(text) > CHUNK:
            name = stem if "." in stem else stem + ".txt"
            self.send_doc(chat_id, name, text.encode(), caption=f"{name} ({len(text)} chars)", markup=markup)
        elif pre:
            self.send(chat_id, f"<pre>{esc(text)}</pre>", markup)
        else:
            self.send_md(chat_id, text, markup)

    def cb(self, action, *args):
        """Register a button action; returns its callback_data (<= 64 bytes)."""
        token = secrets.token_urlsafe(6)
        with self.lock:
            self.callbacks[token] = (action, args)
            while len(self.callbacks) > 3000:
                self.callbacks.popitem(last=False)
        return "c:" + token

    # -- notifications ----------------------------------------------------------
    def notify(self, text, markup=None, silent=False, html=True):
        if not self.owner:
            return
        try:
            self.send(self.owner, text, markup, html=html, silent=silent)
        except Exception as e:  # noqa: BLE001
            log.warning("notify failed: %s", e)

    def auth_problem(self, err):
        with self.lock:
            if self.state.get("auth_broken"):
                return
            self.state["auth_broken"] = True
            self.save_state()
        log.warning("muse.ai auth failed: %s", err)
        self.notify(
            "🔑 <b>muse.ai cookies expired</b> (profile <code>%s</code>), so Muse is unreachable.\n"
            "Re-export them on a computer with Chrome: <code>%s</code>, then send the cookies here "
            "with /login (or copy them to <code>%s</code> on this server). I keep retrying and "
            "will tell you when it works again." % (esc(self.profile), esc(cli.cli_cmd("auth export")),
                                                    esc(cli.COOKIES_FILE)))

    def auth_ok(self):
        if not self.state.get("auth_broken"):
            return
        with self.lock:
            if not self.state.get("auth_broken"):
                return
            self.state["auth_broken"] = False
            self.save_state()
        self.notify("✅ muse.ai login works again.", silent=True)

    # -- dispatch ---------------------------------------------------------------
    def handle_update(self, u):
        msg = u.get("message")
        cq = u.get("callback_query")
        user = (msg or cq or {}).get("from") or {}
        uid = user.get("id")
        if uid is None or int(uid) not in self.allowed:
            log.info("ignored update %s from user %s (not allowed)", u.get("update_id"), uid)
            return
        if cq:
            return self.on_callback(cq, int(uid))
        if msg:
            return self.on_message(msg, int(uid))

    def background(self, fn, *a):
        if self.sync:
            return fn(*a)
        self.pool.submit(self._guard, fn, *a)

    def _guard(self, fn, *a):
        try:
            fn(*a)
        except Exception:  # noqa: BLE001
            log.exception("handler failed")

    def on_message(self, msg, uid):
        chat_id = msg["chat"]["id"]
        text = msg.get("text") or ""
        if self.awaiting.get(chat_id) == "cookies" and ((text and not text.startswith("/"))
                                                         or msg.get("document")):
            return self.background(self.take_cookies, chat_id, msg)
        if text.startswith("/"):
            head, _, rest = text.partition(" ")
            name = head[1:].split("@", 1)[0].lower()
            return self.background(self.run_command, chat_id, uid, name, rest.strip(), msg)
        media = self.media_of(msg)
        if media:
            return self.background(self.upload_and_send, chat_id, msg, media)
        if text.strip():
            self.enqueue(chat_id, ("text", text))

    def run_command(self, chat_id, uid, name, arg, msg):
        fn = getattr(self, "cmd_" + name, None) if re.fullmatch(r"[a-z_]+", name) else None
        if fn is None:
            return self.send(chat_id, f"Unknown command /{esc(name)}. /help lists them.")
        if name in ("raw", "update", "login", "profile") and uid != self.owner:
            return self.send(chat_id, "Only the owner can use that command.")
        try:
            fn(chat_id, arg, msg=msg, uid=uid)
        except AuthError:
            self.send(chat_id, "🔑 muse.ai login expired. Use /login to send new cookies.")
        except (GatewayError, TimeoutError, ValueError, ConnectionError, TelegramError) as e:
            self.send(chat_id, f"⚠️ {esc(str(e)[:600])}")

    def on_callback(self, cq, uid):
        data = cq.get("data") or ""
        message = cq.get("message") or {}
        chat_id = message.get("chat", {}).get("id") or uid
        mid = message.get("message_id")
        entry = self.callbacks.get(data[2:]) if data.startswith("c:") else None
        try:
            self.api.call("answerCallbackQuery", callback_query_id=cq["id"],
                          text=None if entry else "This button expired; run the command again.")
        except Exception as e:  # noqa: BLE001
            log.debug("answerCallbackQuery: %s", e)
        if not entry:
            return
        action, args = entry
        if action == "profile" and uid != self.owner:
            return
        self.background(self._run_callback, chat_id, mid, action, args)

    def _run_callback(self, chat_id, mid, action, args):
        try:
            getattr(self, "cb_" + action)(chat_id, mid, *args)
        except AuthError:
            self.send(chat_id, "🔑 muse.ai login expired. Use /login to send new cookies.")
        except (GatewayError, TimeoutError, ValueError, ConnectionError, TelegramError) as e:
            self.send(chat_id, f"⚠️ {esc(str(e)[:600])}")

    # -- turns ------------------------------------------------------------------
    def enqueue(self, chat_id, job):
        """Messages to Muse run one at a time per Telegram chat, in order."""
        with self.lock:
            busy = chat_id in self.turns or bool(self.queues[chat_id]) or chat_id in self.workers
            self.queues[chat_id].append(job)
            start = chat_id not in self.workers
            if start:
                self.workers[chat_id] = None
        if busy and job[0] == "text":
            self.send(chat_id, "⏳ Queued: Muse is still on your previous message. /stop cancels it.",
                      silent=True)
        if not start:
            return
        if self.sync:
            self._drain(chat_id)
            return
        t = threading.Thread(target=self._drain, args=(chat_id,), daemon=True, name=f"tg-turn-{chat_id}")
        with self.lock:
            self.workers[chat_id] = t
        t.start()

    def _drain(self, chat_id):
        while True:
            with self.lock:
                if not self.queues[chat_id]:
                    self.workers.pop(chat_id, None)
                    return
                job = self.queues[chat_id].popleft()
            try:
                if job[0] == "text":
                    self.run_turn(chat_id, job[1])
                elif job[0] == "resume":
                    self.resume_turn(chat_id, *job[1:])
            except Exception:  # noqa: BLE001
                log.exception("turn failed")

    def run_turn(self, chat_id, text):
        cs = dict(self.chat_state(chat_id))
        turn = Turn(self, chat_id, cs["session_id"], cs["title"])
        self.turns[chat_id] = turn
        try:
            turn.msg_id = self.api.call("sendMessage", chat_id=chat_id,
                                        text="⏳ Sending to Muse…")["message_id"]
            cfg = self.muse.config()
            gw = self.muse.connect(cfg)
            ticker = None
            try:
                user_seq, _sid = cli.deliver(gw, cfg, text, turn.session_id)
                if user_seq is None:
                    self.edit(chat_id, turn.msg_id, "⚠️ Muse did not confirm delivery within 30 s. "
                              "Check /history before sending again.", html=False)
                    return
                turn.user_seq = user_seq
                turn.dirty = True
                turn.maybe_edit(force=True)
                if not self.sync:
                    ticker = threading.Thread(target=self._tick_turn, args=(turn, gw), daemon=True)
                    ticker.start()
                self._follow(turn, user_seq, require_running=True)
            finally:
                turn.finished = True
                if ticker:
                    ticker.join(timeout=5)
                gw.close()
        except AuthError:
            self._turn_error(turn, "🔑 muse.ai login expired. Send new cookies with /login.")
        except Exception as e:  # noqa: BLE001
            log.exception("turn error")
            self._turn_error(turn, f"⚠️ {type(e).__name__}: {str(e)[:400]}")
        finally:
            turn.finished = True
            self.turns.pop(chat_id, None)

    def resume_turn(self, chat_id, session_id, title, after_seq):
        """Keep following a turn after a human decided an approval."""
        turn = Turn(self, chat_id, session_id, title)
        turn.user_seq = after_seq
        self.turns[chat_id] = turn
        try:
            turn.msg_id = self.api.call("sendMessage", chat_id=chat_id,
                                        text="⏳ Muse is continuing…")["message_id"]
            self._follow(turn, after_seq, require_running=False)
        except AuthError:
            self._turn_error(turn, "🔑 muse.ai login expired. Send new cookies with /login.")
        except Exception as e:  # noqa: BLE001
            log.exception("resume error")
            self._turn_error(turn, f"⚠️ {type(e).__name__}: {str(e)[:400]}")
        finally:
            turn.finished = True
            self.turns.pop(chat_id, None)

    def _turn_error(self, turn, text):
        turn.finished = True
        try:
            if turn.msg_id:
                self.edit(turn.chat_id, turn.msg_id, text, html=False)
            else:
                self.send(turn.chat_id, text, html=False)
        except Exception:  # noqa: BLE001
            pass

    def _tick_turn(self, turn, gw):
        """Flush throttled edits; poll history when no live deltas arrive.
        Uses the sending connection, which nothing else reads during the turn."""
        last_poll = 0.0
        while not turn.finished:
            time.sleep(0.5)
            now = self.clock()
            if turn.user_seq and now - turn.last_delta > POLL_FALLBACK and now - last_poll > POLL_FALLBACK:
                last_poll = now
                try:
                    body = {"limit": 10}
                    if turn.session_id:
                        body["session_id"] = turn.session_id
                    evs = gw.call_json("chat.history", body=body).get("chat_events", [])
                    turn.set_partial_from_history(sorted(evs, key=lambda e: e.get("seq") or 0))
                except Exception as e:  # noqa: BLE001
                    log.debug("history poll: %s", e)
            if not turn.finished:
                if now - turn.last_edit >= 5:
                    turn.dirty = True   # keep the elapsed timer moving
                turn.maybe_edit()

    def _follow(self, turn, after_seq, require_running):
        cfg = self.muse.config()
        _code, res = cli.run_wait(
            cfg, turn.session_id, after_seq, TURN_TIMEOUT, require_running=require_running,
            on_event=turn.on_event, auto_approve=self.auto_approve, connector=self.muse.connect,
            approver=self.approve_forever, should_stop=turn.should_stop)
        turn.finished = True
        self.finish_turn(turn, res)

    def finish_turn(self, turn, res):
        chat_id = turn.chat_id
        outcome = res.get("outcome")
        text = "\n\n".join(r["text"] for r in res.get("replies") or [] if r.get("text")).strip()
        notes = []
        for t, aid, title in list(self.auto_log):
            if t >= turn.started:
                notes.append(f"✅ auto-approved (always): {title or aid}")
        markup = None
        if outcome == "needs_input":
            opts = question_options(res)
            markup = keyboard([[button(lbl, self.cb("answer", val))] for lbl, val in opts])
            notes.append("❓ Muse is asking. Tap an answer or type your reply.")
        elif outcome == "needs_approval":
            rows = []
            for a in res.get("approvals") or []:
                notes.append(f"🔐 Approval: {a.get('title')}: {a.get('question') or ''}")
                args = (turn.session_id, turn.title, turn.user_seq)
                rows.append([button("✅ Always allow", self.cb("decide", a["approval_id"], FOREVER, *args)),
                             button("❌ Deny", self.cb("decide", a["approval_id"], "deny", *args))])
                self.notified.add(a["approval_id"])
            markup = keyboard(rows)
        elif outcome == "failed":
            notes.append("⏹ Stopped." if turn.cancel_requested
                         else f"⚠️ Muse's turn ended: {res.get('status')}")
        elif outcome == "timeout":
            if turn.cancel_requested:
                notes.append("⏹ Stop requested; Muse did not confirm the stop. Check /status.")
            else:
                notes.append(f"⌛ Still running after {fmt_secs(TURN_TIMEOUT)}; I'll forward "
                             "Muse's replies when they arrive.")
        for e in res.get("auto_approve_errors") or []:
            notes.append(f"⚠️ could not auto-approve {e.get('approval_id')}: {e.get('error')}")
        body = text or ("(no reply text)" if outcome in ("done", "needs_input") else "")
        full = (body + ("\n\n" if body and notes else "") + "\n".join(notes)).strip() or "(done)"
        self.deliver_reply(chat_id, turn.msg_id, full, markup, stem=f"muse-reply-{turn.user_seq}")
        self.after_turn(turn)
        elapsed = self.clock() - turn.started
        if elapsed >= LONG_TURN and outcome in ("done", "needs_input", "needs_approval", "failed"):
            icon = {"done": "✅", "failed": "⚠️"}.get(outcome, "❓")
            try:
                self.api.call("sendMessage", chat_id=chat_id, reply_to_message_id=turn.msg_id,
                              text=f"{icon} Muse finished ({fmt_secs(elapsed)}).")
            except Exception:  # noqa: BLE001
                pass

    def deliver_reply(self, chat_id, msg_id, text, markup=None, stem="muse-reply"):
        if needs_document(text):
            preview = text[:1500].rstrip() + "\n\n… (full reply attached)"
            self.edit(chat_id, msg_id, preview)
            self.send_doc(chat_id, doc_name(stem, text), text.encode(),
                          caption="Muse's full reply", markup=markup)
            return
        pieces = split_text(text)
        self.edit(chat_id, msg_id, pieces[0], markup if len(pieces) == 1 else None)
        for i, piece in enumerate(pieces[1:], 1):
            self.send_md(chat_id, piece, markup if i == len(pieces) - 1 else None)

    def after_turn(self, turn):
        """Send files the turn produced; move the background watermark past it."""
        try:
            evs = self.muse.history(turn.session_id, 30)
        except Exception as e:  # noqa: BLE001
            log.debug("after_turn history: %s", e)
            return
        self.set_watermark(turn.session_id, max((e.get("seq") or 0 for e in evs), default=None))
        seen, sent = set(), 0
        for e in evs:
            if (e.get("seq") or 0) <= (turn.user_seq or 0) or e.get("event_name") != "message.assistant":
                continue
            for r in cli.event_resources(e):
                if r["path"] in seen or sent >= MAX_FILES_PER_TURN:
                    continue
                seen.add(r["path"])
                if (r.get("byte_len") or 0) > UPLOAD_LIMIT:
                    self.send(turn.chat_id, f"📎 <code>{esc(r['path'])}</code> is too big for Telegram; "
                                            "use muse-cli download.")
                    continue
                try:
                    with self.muse.gw() as g:
                        data = g.download_file(r)
                    self.send_doc(turn.chat_id, os.path.basename(r["path"].split("?")[0]) or "file", data)
                    sent += 1
                except Exception as ex:  # noqa: BLE001
                    self.send(turn.chat_id, f"📎 could not fetch <code>{esc(r['path'])}</code>: "
                                            f"{esc(str(ex)[:200])}")

    # -- approvals ----------------------------------------------------------------
    def approve_forever(self, items):
        """Approver for run_wait and the monitor: allow_always each approval
        once (ids are claimed so two threads never decide the same one)."""
        done, todo = [], []
        with self.lock:
            for a in items:
                if a["approval_id"] in self.claimed:
                    done.append(a["approval_id"])
                else:
                    self.claimed.add(a["approval_id"])
                    todo.append(a)
        if not todo:
            return done
        with self.muse.gw() as g:
            for a in todo:
                aid = a["approval_id"]
                if not offers_forever(a):
                    log.warning("approval %s offers no forever option %s", aid, a.get("options"))
                    with self.lock:
                        self.claimed.discard(aid)
                    continue
                try:
                    cli.decide_approval(g, aid, FOREVER)
                    done.append(aid)
                    with self.lock:
                        self.auto_log = self.auto_log[-50:] + [(self.clock(), aid, a.get("title"))]
                    log.info("auto-approved %s (allow_always): %s", aid, a.get("title"))
                except (GatewayError, TimeoutError) as e:
                    log.warning("auto-approve %s failed: %s", aid, e)
                    with self.lock:
                        self.claimed.discard(aid)
        return done

    def on_pending(self, pend):
        """Monitor saw pending approvals (from any turn, ours or not)."""
        described = [describe_approval(a) for a in pend]
        if self.auto_approve:
            new = [d for d in described if d["approval_id"] not in self.claimed]
            if new:
                done = self.approve_forever(new)
                if any(not t.finished for t in list(self.turns.values())):
                    return   # the running turn lists them in its reply
                for d in new:
                    if d["approval_id"] in done:
                        self.notify(f"✅ Auto-approved (always): {esc(d.get('title') or d['approval_id'])}",
                                    silent=True)
            return
        if any(not t.finished for t in list(self.turns.values())):
            return   # the running turn shows its own buttons when it stops
        for d in described:
            if d["approval_id"] in self.notified:
                continue
            self.notified.add(d["approval_id"])
            self.notify(self.approval_text(d), self.approval_markup(d))

    def approval_text(self, d):
        t = f"🔐 <b>{esc(d.get('title') or 'Approval')}</b>\n{esc(d.get('question') or '')}"
        if d.get("why"):
            t += f"\n<i>{esc(d['why'])}</i>"
        return t

    def approval_markup(self, d, resume=(None, None, None)):
        aid = d["approval_id"]
        return keyboard([[button("✅ Always allow", self.cb("decide", aid, FOREVER, *resume)),
                          button("❌ Deny", self.cb("decide", aid, "deny", *resume))]])

    def cb_decide(self, chat_id, mid, aid, decision, session_id, title, after_seq):
        with self.muse.gw() as g:
            cli.decide_approval(g, aid, decision)
        label = "✅ Always allowed" if decision == FOREVER else "❌ Denied"
        self.send(chat_id, f"{label}: <code>{esc(aid)}</code>", silent=True)
        if mid:
            try:
                self.api.call("editMessageReplyMarkup", chat_id=chat_id, message_id=mid,
                              reply_markup=keyboard([]))
            except Exception:  # noqa: BLE001
                pass
        if after_seq is not None and chat_id not in self.turns:
            self.enqueue(chat_id, ("resume", session_id, title, after_seq))

    def cb_approve_all(self, chat_id, mid):
        with self.muse.gw() as g:
            _r, pend = cli._pending(g)
            done = []
            for a in pend:
                d = describe_approval(a)
                if offers_forever(d):
                    cli.decide_approval(g, d["approval_id"], FOREVER)
                    done.append(d.get("title") or d["approval_id"])
        self.send(chat_id, "✅ Always allowed: " + (esc(", ".join(done)) or "nothing pending"))

    def cb_answer(self, chat_id, mid, value):
        if mid:
            try:
                self.api.call("editMessageReplyMarkup", chat_id=chat_id, message_id=mid,
                              reply_markup=keyboard([]))
            except Exception:  # noqa: BLE001
                pass
        self.send(chat_id, f"➡️ {esc(value)}", silent=True)
        self.enqueue(chat_id, ("text", value))

    # -- media in ---------------------------------------------------------------
    @staticmethod
    def media_of(msg):
        if msg.get("photo"):
            ph = msg["photo"][-1]
            return {"file_id": ph["file_id"], "name": f"photo-{msg.get('message_id')}.jpg",
                    "mime": "image/jpeg", "size": ph.get("file_size")}
        for key in ("document", "video", "audio", "voice", "video_note", "animation"):
            m = msg.get(key)
            if m:
                ext = {"voice": ".ogg", "video": ".mp4", "video_note": ".mp4", "audio": ".mp3",
                       "animation": ".mp4"}.get(key, "")
                return {"file_id": m["file_id"],
                        "name": m.get("file_name") or f"{key}-{msg.get('message_id')}{ext}",
                        "mime": m.get("mime_type"), "size": m.get("file_size")}
        return None

    def upload_and_send(self, chat_id, msg, media):
        try:
            data, _fp = self.api.download(media["file_id"])
        except (ValueError, TelegramError, ConnectionError) as e:
            return self.send(chat_id, f"⚠️ Could not fetch that file from Telegram: {esc(str(e))}")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", media["name"]).strip("._") or "file"
        path = f"{INBOX}/{time.strftime('%Y%m%d-%H%M%S')}-{safe}"
        try:
            self.muse.fs(lambda fs: fs.write_file(path, data, overwrite=False))
        except AuthError:
            return self.send(chat_id, "🔑 muse.ai login expired. Use /login.")
        except (GatewayError, TimeoutError) as e:
            return self.send(chat_id, f"⚠️ Upload to System Files failed: {esc(str(e)[:300])}")
        caption = (msg.get("caption") or "").strip()
        note = (f"[Telegram attachment saved in your System Files at {path} "
                f"({len(data)} bytes{', ' + media['mime'] if media.get('mime') else ''})]")
        self.send(chat_id, f"📥 Saved to <code>{esc(path)}</code>", silent=True)
        self.enqueue(chat_id, ("text", f"{caption}\n\n{note}" if caption
                               else f"I sent you a file from Telegram. {note}"))

    # -- commands ---------------------------------------------------------------
    def cmd_start(self, chat_id, arg, **_):
        self.send(chat_id, f"👋 Connected to Muse (profile <code>{esc(self.profile)}</code>). "
                           "Type a message to talk to Muse; /help lists commands.")

    def cmd_help(self, chat_id, arg, **_):
        self.send(chat_id, HELP)

    def cmd_stop(self, chat_id, arg, **_):
        cs = self.chat_state(chat_id)
        turn = self.turns.get(chat_id)
        sid = turn.session_id if turn else cs["session_id"]
        with self.muse.gw() as g:
            r = cli.cancel_turn(g, sid)
        cancelled = bool(r.get("cancelled")) if isinstance(r, dict) else False
        with self.lock:
            dropped = len(self.queues[chat_id])
            self.queues[chat_id].clear()
        if turn:
            turn.cancel_requested = True
            turn.status = "⏹ stopping…"
            turn.dirty = True
            # The turn ends on Muse's cancelled status; stop following it after
            # a grace period anyway so the chat never stays stuck.
            if not self.sync:
                timer = threading.Timer(20, lambda: setattr(turn, "stop_waiting", True))
                timer.daemon = True
                timer.start()
        text = "⏹ Stopped Muse's turn." if cancelled else "⏹ Stop sent; Muse reported nothing running."
        if dropped:
            text += f" Dropped {dropped} queued message(s)."
        self.send(chat_id, text)

    def cmd_status(self, chat_id, arg, **_):
        cs = self.chat_state(chat_id)
        with self.muse.gw() as g:
            st = cli.status_data(g)
        ident = st.get("identity") if isinstance(st.get("identity"), dict) else {}
        name = ident.get("name") or ident.get("display_name") or ident.get("assistant_name")
        turn = self.turns.get(chat_id)
        here = esc(cs["title"]) + (f" (<code>{esc(cs['session_id'])}</code>)" if cs["session_id"] else "")
        now = (f"{esc(turn.status)} · {fmt_secs(self.clock() - turn.started)}" if turn
               else "idle (no turn from this chat)")
        lines = [f"<b>Muse</b>{' · ' + esc(name) if name else ''}",
                 f"VM: <code>{esc(st.get('vm_id'))}</code> · chats: {st.get('sessions')}",
                 f"Unread: <code>{esc(json.dumps(st.get('unread'), ensure_ascii=False)[:200])}</code>",
                 f"Here: {here}",
                 f"Now: {now}",
                 f"Profile: <code>{esc(self.profile)}</code> · auto-approve: "
                 f"{'on (always)' if self.auto_approve else 'off'} · watch: "
                 f"{'on' if self.state.get('watch') else 'off'}",
                 f"muse-cli {esc(__version__)}"]
        if ident.get("tagline") or ident.get("vibe"):
            lines.insert(1, f"<i>{esc(str(ident.get('tagline') or ident.get('vibe'))[:300])}</i>")
        self.send(chat_id, "\n".join(lines))

    def cmd_main(self, chat_id, arg, **_):
        self.set_chat(chat_id, None, "main chat")
        self.send(chat_id, "💬 Now in: <b>main chat</b>")

    def cmd_new(self, chat_id, arg, **_):
        with self.muse.gw() as g:
            d = cli.session_start(g, arg or None)
        sid = d.get("session_id") or d.get("id") or (d.get("session") or {}).get("session_id")
        if not sid:
            return self.send(chat_id, f"⚠️ Muse returned no session id: <code>{esc(json.dumps(d)[:300])}</code>")
        self.set_chat(chat_id, sid, arg or "new chat")
        self.send(chat_id, f"🆕 Now in a new side chat: <b>{esc(arg or 'new chat')}</b>")

    def _sessions(self):
        with self.muse.gw() as g:
            rows = cli.session_rows(g.call_json("sessions.list"))
        threads = sorted((r for r in rows if r["thread"]), key=lambda r: r["updated"] or "", reverse=True)
        threads.sort(key=lambda r: not r["pinned"])
        return threads

    def cmd_chats(self, chat_id, arg, **_):
        self.show_chats(chat_id, None, 0)

    def show_chats(self, chat_id, mid, page):
        rows = self._sessions()
        cur = self.chat_state(chat_id)["session_id"]
        pages = max(1, (len(rows) + CHATS_PAGE - 1) // CHATS_PAGE)
        page = max(0, min(page, pages - 1))
        btns = [[button("💬 main chat" + (" ✓" if not cur else ""), self.cb("open", None, "main chat"))]]
        for r in rows[page * CHATS_PAGE:(page + 1) * CHATS_PAGE]:
            label = ("📌 " if r["pinned"] else "") + (r["title"] or "untitled")
            if r["session_id"] == cur:
                label += " ✓"
            btns.append([button(label, self.cb("open", r["session_id"], r["title"] or "untitled"))])
        nav = []
        if page > 0:
            nav.append(button("◀ Prev", self.cb("chats_page", page - 1)))
        if page < pages - 1:
            nav.append(button("Next ▶", self.cb("chats_page", page + 1)))
        btns.append(nav)
        text = f"Your chats ({len(rows)} side chats, page {page + 1}/{pages}). Tap one to open it."
        if mid:
            self.edit(chat_id, mid, text, keyboard(btns), html=False)
        else:
            self.send(chat_id, text, keyboard(btns), html=False)

    def cb_chats_page(self, chat_id, mid, page):
        self.show_chats(chat_id, mid, page)

    def cb_open(self, chat_id, mid, session_id, title):
        self.set_chat(chat_id, session_id, title)
        self.send(chat_id, f"💬 Now in: <b>{esc(title)}</b>")
        self.cmd_history(chat_id, "3")

    def cmd_history(self, chat_id, arg, **_):
        from .wait import clean_text
        n = max(1, min(int(arg) if arg.strip().isdigit() else 10, 50))
        cs = self.chat_state(chat_id)
        msgs = []
        for e in self.muse.history(cs["session_id"], n * 3):
            name, t = e.get("event_name"), ev_text(e)
            if t and name in ("message.user", "message.assistant"):
                msgs.append(("🧑 You" if name == "message.user" else "🤖 Muse", clean_text(t)))
        msgs = msgs[-n:]
        if not msgs:
            return self.send(chat_id, "No messages here yet.")
        text = "\n\n".join(f"{who}: {t}" for who, t in msgs)
        self.reply_long(chat_id, f"📜 {cs['title']}\n\n{text}", stem="history.txt")

    def cmd_unread(self, chat_id, arg, **_):
        d = self.muse.call("chat.unread_count")
        self.send(chat_id, f"<pre>{esc(json.dumps(d, indent=1, ensure_ascii=False)[:3500])}</pre>")

    def _current_thread(self, chat_id):
        cs = self.chat_state(chat_id)
        if not cs["session_id"]:
            self.send(chat_id, "That works on side chats. Open one with /chats or make one with /new.")
            return None
        return cs

    def _session_op(self, chat_id, kind, title=None):
        cs = self._current_thread(chat_id)
        if cs:
            with self.muse.gw() as g:
                cli.session_op(g, kind, cs["session_id"], title)
        return cs

    def cmd_rename(self, chat_id, arg, **_):
        if not arg:
            return self.send(chat_id, "Usage: /rename &lt;new title&gt;")
        cs = self._session_op(chat_id, "rename", arg)
        if cs:
            self.set_chat(chat_id, cs["session_id"], arg)
            self.send(chat_id, f"✏️ Renamed to <b>{esc(arg)}</b>")

    def cmd_pin(self, chat_id, arg, **_):
        if self._session_op(chat_id, "pin"):
            self.send(chat_id, "📌 Pinned.")

    def cmd_unpin(self, chat_id, arg, **_):
        if self._session_op(chat_id, "unpin"):
            self.send(chat_id, "Unpinned.")

    def cmd_unarchive(self, chat_id, arg, **_):
        if self._session_op(chat_id, "unarchive"):
            self.send(chat_id, "📤 Unarchived.")

    def cmd_archive(self, chat_id, arg, **_):
        if self._session_op(chat_id, "archive"):
            self.set_chat(chat_id, None, "main chat")
            self.send(chat_id, "🗄 Archived. Back in the main chat.")

    def cmd_delete(self, chat_id, arg, **_):
        cs = self._current_thread(chat_id)
        if cs:
            self.send(chat_id, f"Delete side chat <b>{esc(cs['title'])}</b> for good?",
                      keyboard([[button("🗑 Yes, delete", self.cb("delete", cs["session_id"])),
                                 button("Cancel", self.cb("noop"))]]))

    def cb_delete(self, chat_id, mid, session_id):
        with self.muse.gw() as g:
            cli.session_op(g, "delete", session_id)
        if self.chat_state(chat_id)["session_id"] == session_id:
            self.set_chat(chat_id, None, "main chat")
        self.edit(chat_id, mid, "🗑 Deleted. Back in the main chat.", html=False)

    def cb_noop(self, chat_id, mid, *a):
        if mid:
            self.edit(chat_id, mid, "Cancelled.", html=False)

    def cmd_approvals(self, chat_id, arg, **_):
        with self.muse.gw() as g:
            _r, pend = cli._pending(g)
        if not pend:
            return self.send(chat_id, "No pending approvals. Auto-approve is "
                             + ("on (always)." if self.auto_approve else "off."))
        for a in pend:
            d = describe_approval(a)
            self.send(chat_id, self.approval_text(d), self.approval_markup(d))
        if len(pend) > 1:
            self.send(chat_id, "Or decide them all:",
                      keyboard([[button("✅ Always allow all", self.cb("approve_all"))]]))

    def cmd_autoapprove(self, chat_id, arg, **_):
        a = arg.strip().lower()
        if a in ("on", "always", "off"):
            on = a != "off"
            with self.lock:
                self.state["auto_approve"] = on
                self.save_state()
            cfg = cli.load_config()     # keep the CLI's setting for this profile in line
            cfg["auto_approve"] = "always" if on else "off"
            cli.save_config(cfg)
            self.muse.reload()
        elif a not in ("", "status"):
            return self.send(chat_id, "Usage: /autoapprove on|off|status")
        if self.auto_approve:
            self.send(chat_id, "✅ Auto-approve is <b>on</b>: every approval gets Muse's forever "
                               "choice (allow_always), so Muse never blocks.")
        else:
            self.send(chat_id, "Auto-approve is <b>off</b>: approvals come here with buttons.")

    def cmd_watch(self, chat_id, arg, **_):
        a = arg.strip().lower()
        if a in ("on", "off"):
            with self.lock:
                self.state["watch"] = a == "on"
                self.save_state()
        on = self.state.get("watch")
        self.send(chat_id, "👀 Watch is " + ("<b>on</b>: live tool/progress events are forwarded, "
                                             "batched every few seconds." if on else "<b>off</b>."))

    def cmd_files(self, chat_id, arg, **_):
        cs = self.chat_state(chat_id)
        rows, seen = [], set()
        for r in reversed(cli.file_rows(self.muse.history(cs["session_id"], 40))):
            if r["path"] not in seen:
                seen.add(r["path"])
                rows.append(r)
        if not rows:
            return self.send(chat_id, "No files in recent messages here.")
        btns = [[button("📥 " + os.path.basename(r["path"].split("?")[0])[:50], self.cb("dl", r))]
                for r in rows[:12]]
        self.send(chat_id, "Recent files (newest first). Tap one to get it:", keyboard(btns), html=False)

    def cb_dl(self, chat_id, mid, r):
        if (r.get("byte_len") or 0) > UPLOAD_LIMIT:
            return self.send(chat_id, "Too big for Telegram (50 MB); use muse-cli download.")
        with self.muse.gw() as g:
            data = g.download_file(r)
        self.send_doc(chat_id, os.path.basename(r["path"].split("?")[0]) or "file", data)

    # /fs
    def cmd_fs(self, chat_id, arg, **_):
        from .fs import fmt_size, norm
        try:
            parts = shlex.split(arg)
        except ValueError as e:
            return self.send(chat_id, f"⚠️ {esc(e)}")
        usage = ("Usage: /fs ls [path] · tree [path] [depth] · cat|get|stat &lt;path&gt; · "
                 "find &lt;name&gt; · mkdir &lt;path&gt; · mv &lt;src&gt; &lt;dst&gt; [-f] · rm [-r] &lt;path&gt;")
        if not parts:
            return self.send(chat_id, usage)
        op, rest = parts[0].lower(), parts[1:]
        flags = {x for x in rest if x.startswith("-")}
        rest = [x for x in rest if not x.startswith("-")]
        path = rest[0] if rest else ""
        if op == "ls":
            ents = self.muse.fs(lambda fs: fs.list_dir(path))
            ents = [e for e in ents if "-a" in flags or not e["name"].startswith(".")]
            lines = [("📁 " if e.get("type") == "directory" else "📄 ") + e["name"]
                     + ("" if e.get("type") == "directory" else f"  {fmt_size(e.get('size'))}")
                     for e in ents]
            return self.reply_long(chat_id, f"/{norm(path)}\n" + ("\n".join(lines) or "(empty)"),
                                   stem="ls.txt", pre=True)
        if op == "tree":
            depth = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else 2
            rows = self.muse.fs(lambda fs: list(fs.walk(path, depth)))
            lines = ["  " * (lvl + 1) + e["name"] + ("/" if e.get("type") == "directory" else "")
                     for r, e, lvl in rows if not any(x.startswith(".") for x in r.split("/"))]
            return self.reply_long(chat_id, "/" + norm(path) + "\n" + "\n".join(lines),
                                   stem="tree.txt", pre=True)
        if op == "stat" and path:
            st = self.muse.fs(lambda fs: fs.stat(path))
            return self.reply_long(chat_id, json.dumps(st, indent=1, ensure_ascii=False), pre=True)
        if op == "find" and path:
            ents = self.muse.fs(lambda fs: fs.search(path, 50))
            lines = [e.get("path") or e.get("name") or json.dumps(e) for e in ents]
            return self.reply_long(chat_id, "\n".join(lines) or "nothing found", stem="find.txt", pre=True)
        if op == "cat" and path:
            data = self.muse.fs(lambda fs: fs.read_file(path, max_bytes=5 * 1024 * 1024))
            name = os.path.basename(norm(path)) or "file"
            if b"\0" in data[:8192]:
                return self.send_doc(chat_id, name, data, caption=f"/{norm(path)} (binary)")
            text = data.decode("utf-8", "replace")
            if len(text) > CHUNK:
                return self.send_doc(chat_id, name, data, caption=f"/{norm(path)}")
            return self.send(chat_id, f"<b>/{esc(norm(path))}</b>\n<pre>{esc(text) or '(empty)'}</pre>")
        if op == "get" and path:
            st = self.muse.fs(lambda fs: fs.stat(path))
            if st.get("kind") in ("dir", "directory"):
                return self.send(chat_id, "That is a folder; /fs ls it and get the files one by one.")
            data = self.muse.fs(lambda fs: fs.read_file(path))
            if len(data) > UPLOAD_LIMIT:
                return self.send(chat_id, "Too big for Telegram (50 MB).")
            return self.send_doc(chat_id, os.path.basename(norm(path)) or "file", data,
                                 caption=f"/{norm(path)}")
        if op == "mkdir" and path:
            self.muse.fs(lambda fs: fs.mkdir(path, parents=True))
            return self.send(chat_id, f"📁 Created <code>/{esc(norm(path))}</code>")
        if op == "mv" and len(rest) == 2:
            self.muse.fs(lambda fs: fs.move(rest[0], rest[1], overwrite="-f" in flags))
            return self.send(chat_id, f"Moved <code>/{esc(norm(rest[0]))}</code> → "
                                      f"<code>/{esc(norm(rest[1]))}</code>")
        if op == "rm" and path:
            rec = bool(flags & {"-r", "-rf", "-R", "-fr"})
            what = " and everything in it" if rec else ""
            return self.send(chat_id, f"Delete <code>/{esc(norm(path))}</code>{what}?",
                             keyboard([[button("🗑 Yes, delete", self.cb("fs_rm", norm(path), rec)),
                                        button("Cancel", self.cb("noop"))]]))
        self.send(chat_id, usage)

    def cb_fs_rm(self, chat_id, mid, path, recursive):
        self.muse.fs(lambda fs: fs.delete(path, recursive=recursive))
        self.edit(chat_id, mid, f"🗑 Deleted /{path}", html=False)

    def cmd_feed(self, chat_id, arg, **_):
        n = int(arg) if arg.strip().isdigit() else 10
        rows = cli.feed_rows(self.muse.call("feed.list", query={"limit": n}), n)
        if not rows:
            return self.send(chat_id, "Your feed is empty.")
        self.send(chat_id, "📰 <b>Feed</b>\n" + "\n".join(
            f"• {esc(r['title'] or '(untitled)')} <i>{esc(r['date'] or '')}</i>" for r in rows))

    def cmd_goals(self, chat_id, arg, **_):
        rows = cli.goal_rows(self.muse.call("goals.list"))
        if not rows:
            return self.send(chat_id, "No goals.")
        self.send(chat_id, "🎯 <b>Goals</b>\n" + "\n".join(
            f"• {esc(r['title'] or '(untitled)')} [{esc(r['status'] or '?')}] <code>{esc(r['id'])}</code>"
            for r in rows) + "\n\n/goal &lt;id&gt; for details")

    def cmd_goal(self, chat_id, arg, **_):
        if not arg:
            return self.send(chat_id, "Usage: /goal &lt;id&gt; (ids from /goals)")
        d = self.muse.call("goals.get", path_params={"id": arg.strip()})
        g = d.get("goal", d) if isinstance(d, dict) else {}
        head = ""
        if isinstance(g, dict):
            head = f"🎯 <b>{esc(g.get('title') or '')}</b> [{esc(g.get('status') or '')}]\n"
            if g.get("subtitle") or g.get("description"):
                head += esc(g.get("subtitle") or g.get("description")) + "\n"
        body = json.dumps(d, indent=1, ensure_ascii=False)
        if len(body) > 3000:
            self.send(chat_id, head or "Goal:")
            return self.send_doc(chat_id, "goal.json", body.encode())
        self.send(chat_id, f"{head}<pre>{esc(body)}</pre>")

    def cmd_ideas(self, chat_id, arg, **_):
        rows = cli.idea_rows(self.muse.call("api.idea-cards.list"))
        if not rows:
            return self.send(chat_id, "No idea cards right now.")
        for r in rows[:15]:
            self.send(chat_id, f"💡 <b>{esc(r['title'] or '(untitled)')}</b>"
                               + (f"\n<i>{esc(r['section'])}</i>" if r.get("section") else ""),
                      keyboard([[button("▶️ Execute", self.cb("idea_exec", r["id"], r["title"]))]]),
                      silent=True)

    def cb_idea_exec(self, chat_id, mid, idea_id, title):
        cs = self.chat_state(chat_id)
        with self.muse.gw() as g:
            cli.idea_execute(g, idea_id, cs["session_id"])
        self.send(chat_id, f"▶️ Muse is working on: <b>{esc(title or idea_id)}</b>. "
                           "Replies arrive here when they land.")

    def cmd_usage(self, chat_id, arg, **_):
        try:
            u = cli.summarize_usage(cli.fetch_usage(self.muse.config()))
        except AuthError as e:
            self.auth_problem(e)
            raise
        self.send(chat_id, "📊 " + esc("\n".join(cli.usage_lines(u))))

    def cmd_wake(self, chat_id, arg, **_):
        r = cli.wake_vm(self.muse.config())
        self.send(chat_id, f"⏰ Wake requested: HTTP {r.get('status')}"
                           f"<pre>{esc(json.dumps(r.get('body'), ensure_ascii=False)[:800])}</pre>")

    def cmd_profile(self, chat_id, arg, **_):
        rows = cli.list_profiles()
        btns = [[button(("✓ " if r["active"] else "") + r["name"] + ("" if r["cookies"] else " (no cookies)"),
                        self.cb("profile", r["name"]))] for r in rows]
        self.send(chat_id, f"Active profile: <b>{esc(self.profile)}</b>. Tap one to switch:", keyboard(btns))

    def cb_profile(self, chat_id, mid, name):
        if name == self.profile:
            return self.send(chat_id, f"Already on <b>{esc(name)}</b>.")
        self.switch_profile(name)
        self.send(chat_id, f"🔀 Switched to profile <b>{esc(name)}</b>. "
                           f"Here: {esc(self.chat_state(chat_id)['title'])}")

    def switch_profile(self, name):
        cli.validate_profile_name(name)
        self.stop_monitors()
        with self.lock:
            cli.activate_profile(name)
            self.state["profile"] = name
            self.state.pop("auth_broken", None)
            self.save_state()
        self.muse.reload()
        self.ensure_monitors()

    def cmd_login(self, chat_id, arg, **_):
        self.awaiting[chat_id] = "cookies"
        self.send(chat_id, "🔑 Send the muse.ai cookies now: the <code>cookies.txt</code> file from "
                           "<code>muse-cli auth export</code>, or one line like "
                           "<code>hatch_sess=…; other=…</code>. I delete your message after reading it. "
                           f"They replace <code>{esc(cli.COOKIES_FILE)}</code> (old one kept as .bak).")

    def take_cookies(self, chat_id, msg):
        self.awaiting.pop(chat_id, None)
        if msg.get("document"):
            data, _ = self.api.download(msg["document"]["file_id"], limit=1024 * 1024)
            text = data.decode("utf-8", "replace")
        else:
            text = msg.get("text") or ""
        try:
            self.api.call("deleteMessage", chat_id=chat_id, message_id=msg["message_id"])
        except Exception:  # noqa: BLE001
            pass
        if "hatch_sess" not in text:
            return self.send(chat_id, "⚠️ No hatch_sess cookie in that, so nothing changed. /login to retry.")
        cli.ensure_config_dir()
        path = cli.COOKIES_FILE
        if os.path.exists(path):
            os.replace(path, path + ".bak")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text.strip() + "\n")
        os.chmod(path, 0o600)
        try:
            with self.muse.gw() as g:
                st = cli.status_data(g)
            self.send(chat_id, f"✅ Cookies saved and working (VM <code>{esc(st.get('vm_id'))}</code>).")
            self.ensure_monitors(restart=True)
        except Exception as e:  # noqa: BLE001
            self.send(chat_id, f"⚠️ Saved, but Muse still refuses: {esc(str(e)[:300])}")

    def cmd_update(self, chat_id, arg, msg=None, **_):
        from .update import upgrade_argv
        argv = upgrade_argv()
        self.send(chat_id, f"⬆️ Running <code>{esc(' '.join(argv))}</code>…")
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=900)
            output, rc = (r.stdout + r.stderr)[-3000:], r.returncode
        except (OSError, subprocess.TimeoutExpired) as e:
            output, rc = str(e), 1
        self.send(chat_id, f"<pre>{esc(output.strip() or '(no output)')}</pre>\nexit {rc}")
        if rc != 0:
            return self.send(chat_id, "⚠️ Upgrade failed; still running the old version.")
        self.send(chat_id, "♻️ Restarting the bot…")
        self.restart()

    def restart(self):
        """Re-exec this process with the upgraded code (same systemd unit)."""
        if self.restart_hook:
            return self.restart_hook()
        try:   # confirm the offset so /update is not replayed after the restart
            if self.state.get("offset"):
                self.api.call("getUpdates", offset=self.state["offset"], timeout=0, _http_timeout=10)
        except Exception:  # noqa: BLE001
            pass
        self.stopping = True
        self.stop_monitors()
        argv = [sys.executable, "-m", "muse_cli"] + sys.argv[1:]
        log.info("restarting: %s", " ".join(argv))
        os.execv(sys.executable, argv)

    def cmd_raw(self, chat_id, arg, **_):
        from .gateway import ROUTES
        method, _, body = arg.strip().partition(" ")
        if not method:
            return self.send(chat_id, "Usage: /raw &lt;method&gt; [json body]; path params go in "
                                      "<code>{\"_params\": {...}}</code>")
        if method not in ROUTES:
            return self.send(chat_id, f"Unknown method <code>{esc(method)}</code> ({len(ROUTES)} known).")
        try:
            payload = json.loads(body) if body.strip() else None
        except ValueError as e:
            return self.send(chat_id, f"⚠️ invalid JSON: {esc(e)}")
        pp = None
        if isinstance(payload, dict) and isinstance(payload.get("_params"), dict):
            pp = payload.pop("_params")
        with self.muse.gw() as g:
            res = cli.raw_call(g, method, payload, pp)
        text = res if isinstance(res, str) else json.dumps(res, indent=1, ensure_ascii=False)
        self.reply_long(chat_id, text, stem=f"{method}.json", pre=True)

    # -- background monitor --------------------------------------------------------
    def monitored_sessions(self):
        chats = self.state["chats"].get(self.profile, {})
        return {None} | {c.get("session_id") for c in chats.values() if c.get("session_id")}

    def ensure_monitors(self, restart=False):
        if self.sync or self.stopping:
            return
        if restart:
            self.stop_monitors()
        want = self.monitored_sessions()
        with self.lock:
            for sid in list(self.monitors):
                if sid not in want:
                    self.monitors.pop(sid).stop()
            for sid in want:
                m = self.monitors.get(sid)
                if m is None or not m.is_alive():
                    m = Monitor(self, sid)
                    self.monitors[sid] = m
                    m.start()

    def stop_monitors(self):
        with self.lock:
            for m in self.monitors.values():
                m.stop()
            self.monitors.clear()

    def check_background(self, session_id):
        """Forward complete assistant messages that no bot turn delivered."""
        from .wait import clean_text
        if self.session_busy(session_id):
            return
        evs = self.muse.history(session_id, 15)
        if self.session_busy(session_id):
            return
        top = max((e.get("seq") or 0 for e in evs), default=0)
        wm = self.watermark(session_id)
        if wm is None:              # first look at this chat: start from now
            self.set_watermark(session_id, top)
            return
        mine = [e for e in evs if (e.get("seq") or 0) > wm and e.get("event_name") == "message.assistant"
                and ev_text(e)]
        writing = [e for e in mine if not _payload(e).get("status")]
        new = [e for e in mine if _payload(e).get("status")]
        if writing:                 # still being written: pick it up once complete
            top = min(e.get("seq") or 0 for e in writing) - 1
            new = [e for e in new if (e.get("seq") or 0) <= top]
        self.set_watermark(session_id, top)
        if not new:
            return
        chats = self.state["chats"].get(self.profile, {})
        targets = [int(c) for c, st in chats.items() if st.get("session_id") == session_id]
        if not targets and self.owner:
            targets = [self.owner]
        title = "main chat"
        if session_id:
            title = next((st.get("title") for st in chats.values()
                          if st.get("session_id") == session_id and st.get("title")), "side chat")
        for e in new:
            text = clean_text(ev_text(e))
            for chat_id in targets:
                here = self.chat_state(chat_id)["session_id"] == session_id
                markup = None if here else keyboard([[button("Open this chat",
                                                             self.cb("open", session_id, title))]])
                self.send_md(chat_id, f"💬 Muse · {title}:\n\n{text}", markup)

    def watch_event(self, ev):
        if not self.state.get("watch"):
            return
        line = watch_line(ev)
        if line:
            with self.lock:
                self.watch_buf.append(line)

    def flush_watch(self, force=False):
        with self.lock:
            if not self.watch_buf or (not force and self.clock() - self.watch_last < WATCH_FLUSH):
                return
            lines, self.watch_buf = self.watch_buf, []
            self.watch_last = self.clock()
        if len(lines) > 20:
            lines = lines[:20] + [f"(+{len(lines) - 20} more)"]
        self.notify("\n".join(lines), silent=True, html=False)

    # -- main loop -------------------------------------------------------------
    def register_commands(self):
        try:
            self.api.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
        except Exception as e:  # noqa: BLE001
            log.warning("setMyCommands failed: %s", e)

    def poll_once(self, offset, sleep=time.sleep):
        """One getUpdates round; returns (new_offset, backoff_hint). Never raises."""
        try:
            ups = self.api.call("getUpdates", offset=offset, timeout=25,
                                allowed_updates=["message", "callback_query"], _http_timeout=40)
        except TelegramError as e:
            if e.code in (401, 404):
                log.error("Telegram rejected the bot token (%s); fix it with `%s`", e.code,
                          cli.cli_cmd("telegram setup"))
                return offset, 60
            if e.code == 409:
                log.error("another process is polling this bot (409 Conflict)")
                return offset, 10
            log.warning("getUpdates: %s", e)
            return offset, -1
        except Exception as e:  # noqa: BLE001 - network: back off and retry
            log.warning("getUpdates failed: %s", e)
            return offset, -1
        for u in ups or []:
            offset = u["update_id"] + 1
            with self.lock:
                self.state["offset"] = offset
                self.save_state()
            try:
                self.handle_update(u)
            except Exception:  # noqa: BLE001 - one bad update never stops the loop
                log.exception("update %s failed", u.get("update_id"))
        return offset, 0

    def run(self):
        me = self.api.call("getMe")
        log.info("bot @%s up (muse-cli %s, profile %s, owner %s, %d allowed)",
                 me.get("username"), __version__, self.profile, self.owner, len(self.allowed))
        self.register_commands()
        self.ensure_monitors()
        offset = self.state.get("offset")
        backoff = 1
        while not self.stopping:
            offset, hint = self.poll_once(offset)
            if hint == 0:
                backoff = 1
            elif hint > 0:
                time.sleep(hint)
            else:
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)


class Monitor(threading.Thread):
    """Long-lived chat.subscribe on one conversation: background replies,
    approvals and /watch events. Reconnects with backoff."""

    def __init__(self, bot, session_id):
        super().__init__(daemon=True, name=f"tg-monitor-{session_id or MAIN}")
        self.bot = bot
        self.session_id = session_id
        self.stop_ev = threading.Event()
        self.dirty_at = 0.0       # check once at start (sets the watermark)

    def stop(self):
        self.stop_ev.set()

    def run(self):
        backoff = 2
        while not self.stop_ev.is_set():
            try:
                gw = self.bot.muse.connect()
            except AuthError:
                self.stop_ev.wait(300)
                continue
            except Exception as e:  # noqa: BLE001
                log.warning("monitor %s: connect failed: %s", self.session_id or MAIN, e)
                self.stop_ev.wait(backoff)
                backoff = min(backoff * 2, 300)
                continue
            backoff = 2
            try:
                body = {"capabilities": ["chat_cancel", "delta_stream"]}
                if self.session_id:
                    body["session_id"] = self.session_id
                gw.subscribe_raw("chat.subscribe", body=body, max_records=10**9, idle_timeout=900,
                                 overall_timeout=1800, on_record=self.on_record, should_stop=self.tick)
            except Exception as e:  # noqa: BLE001
                log.warning("monitor %s: %s", self.session_id or MAIN, e)
                self.stop_ev.wait(backoff)
            finally:
                gw.close()

    def on_record(self, line):
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            return self.stop_ev.is_set()
        name = event_name(ev) or ""
        pend = pending_approvals(ev)
        if pend:
            try:
                self.bot.on_pending(pend)
            except Exception as e:  # noqa: BLE001
                log.warning("approvals: %s", e)
        status = str(_payload(ev).get("status") or "").lower()
        if (name.startswith("message.") or name in ("delta.message_done", "sessions.updated")
                or (name == "task.status" and status and status != "running")):
            self.dirty_at = self.dirty_at or time.time()
        self.bot.watch_event(ev)
        return self.stop_ev.is_set()

    def tick(self):
        if self.stop_ev.is_set():
            return True
        if self.dirty_at is not None and time.time() - self.dirty_at > 1.5:
            self.dirty_at = None
            try:
                self.bot.check_background(self.session_id)
            except AuthError:
                pass
            except Exception as e:  # noqa: BLE001
                log.warning("background check: %s", e)
        try:
            self.bot.flush_watch()
        except Exception:  # noqa: BLE001
            pass
        return False


# -- CLI: muse-cli telegram ... --------------------------------------------------------
def read_token(stdin=None, prompt=getpass.getpass):
    """The bot token from stdin (piped) or a hidden prompt; never from argv."""
    stdin = stdin or sys.stdin
    if stdin.isatty():
        return prompt("Telegram bot token from @BotFather (hidden): ").strip()
    return stdin.readline().strip()


def pair(api, code, timeout=600, out=print, sleep=time.sleep):
    """Wait for '/start CODE' sent to the bot; return the sender's user id or None."""
    offset = None
    try:   # skip anything sent before the code was printed
        old = api.call("getUpdates", offset=-1, timeout=0, _http_timeout=15)
        if old:
            offset = old[-1]["update_id"] + 1
    except Exception:  # noqa: BLE001
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ups = api.call("getUpdates", offset=offset, timeout=20, allowed_updates=["message"],
                           _http_timeout=30)
        except (TelegramError, ConnectionError) as e:
            out(f"(telegram: {e}; retrying)")
            sleep(3)
            continue
        for u in ups or []:
            offset = u["update_id"] + 1
            m = u.get("message") or {}
            words = (m.get("text") or "").split()
            if len(words) == 2 and words[0].split("@")[0] == "/start" and words[1] == code:
                uid = m["from"]["id"]
                try:
                    api.call("sendMessage", chat_id=m["chat"]["id"],
                             text="✅ Paired with muse-cli. Only you can use this bot now.")
                    api.call("getUpdates", offset=offset, timeout=0, _http_timeout=15)
                except Exception:  # noqa: BLE001
                    pass
                return uid
    return None


def cmd_tg_setup(args, stdin=None, api_factory=TelegramAPI, pair_fn=pair):
    cli.ensure_config_dir()
    path = config_path()
    conf = read_json(path)
    token = read_token(stdin)
    if not token:
        if not conf.get("token"):
            print("no token given (paste it at the prompt or pipe it on stdin)", file=sys.stderr)
            sys.exit(2)
        token = conf["token"]
        print("keeping the saved token", file=sys.stderr)
    try:
        api = api_factory(token)
        me = api.call("getMe")
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)
    except TelegramError as e:
        print(f"Telegram rejected the token: {e.description}", file=sys.stderr)
        sys.exit(2)
    print(f"Bot: @{me.get('username')} ({me.get('first_name')})")
    same_bot = conf.get("token") == token
    allowed = set(int(x) for x in (conf.get("allowed_user_ids") or [])) if same_bot else set()
    allowed |= set(args.allow or [])
    owner = conf.get("owner_id") if same_bot else None
    if args.allow and not owner:
        owner = args.allow[0]
    if not args.no_pair and not (args.allow and owner):
        code = f"{secrets.randbelow(10**6):06d}"
        print()
        print("Pair your Telegram account: open the bot and send this message to it:")
        print(f"  https://t.me/{me.get('username')}")
        print(f"  /start {code}")
        print(f"(waiting up to {args.pair_timeout}s; Ctrl+C to stop)", flush=True)
        uid = pair_fn(api, code, timeout=args.pair_timeout)
        if uid is None:
            print("no pairing message arrived; run setup again (or pass --allow YOUR_USER_ID)",
                  file=sys.stderr)
            sys.exit(1)
        print(f"paired: Telegram user {uid}")
        allowed.add(int(uid))
        owner = owner or int(uid)
    if not owner:
        print("no owner: pair (drop --no-pair) or pass --allow USER_ID", file=sys.stderr)
        sys.exit(2)
    conf.update(token=token, owner_id=int(owner), allowed_user_ids=sorted(allowed | {int(owner)}),
                bot_username=me.get("username"))
    write_private_json(path, conf)
    try:
        api.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
    except Exception:  # noqa: BLE001
        pass
    print(f"saved {path} (mode 600)")
    print("Next:")
    print(f"  {cli.cli_cmd('telegram install-service')}   # run it as a systemd service")
    print(f"  {cli.cli_cmd('telegram run')}               # or run it in this terminal")


def service_name(profile):
    return "muse-telegram.service" if profile == cli.DEFAULT_PROFILE else f"muse-telegram-{profile}.service"


def service_unit(exe, profile, user_mode, home, path_env, user=None):
    """systemd unit text for `muse-cli [--profile X] telegram run`, Restart=always."""
    flag = "" if profile == cli.DEFAULT_PROFILE else f" --profile {profile}"
    service = ["[Service]", "Type=simple"]
    if not user_mode and user:
        service.append(f"User={user}")
    service += [f"ExecStart={exe}{flag} telegram run",
                "Restart=always",
                "RestartSec=5",
                f"Environment=HOME={home}",
                f"Environment=PATH={path_env}",
                "Environment=PYTHONUNBUFFERED=1",
                "Environment=MUSE_NO_UPDATE_CHECK=1"]
    lines = ["[Unit]",
             f"Description=muse-cli Telegram bridge (profile {profile})",
             "After=network-online.target",
             "Wants=network-online.target",
             "StartLimitIntervalSec=0",
             "", *service, "",
             "[Install]",
             f"WantedBy={'default.target' if user_mode else 'multi-user.target'}", ""]
    return "\n".join(lines)


def find_exe():
    exe = shutil.which("muse-cli")
    return os.path.abspath(exe or sys.argv[0])


def cmd_tg_install_service(args):
    if not os.path.exists(config_path()):
        print(f"run `{cli.cli_cmd('telegram setup')}` first", file=sys.stderr)
        sys.exit(2)
    user_mode = args.user
    if not user_mode and os.geteuid() != 0 and not args.dry_run:
        print("a system unit needs root: run this as root, or add --user", file=sys.stderr)
        sys.exit(2)
    import pwd
    exe = find_exe()
    home = os.path.expanduser("~")
    path_env = ":".join(dict.fromkeys([os.path.dirname(exe), os.path.join(home, ".local", "bin"),
                                       "/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin",
                                       "/sbin", "/bin"]))
    name = service_name(cli.PROFILE)
    text = service_unit(exe, cli.PROFILE, user_mode, home, path_env,
                        user=pwd.getpwuid(os.getuid()).pw_name)
    unit_dir = os.path.join(home, ".config", "systemd", "user") if user_mode else "/etc/systemd/system"
    unit_path = os.path.join(unit_dir, name)
    if args.dry_run:
        print(f"# {unit_path}")
        print(text)
        return
    if shutil.which("systemctl") is None:
        print(f"systemctl not found; run `{exe} telegram run` under your own supervisor", file=sys.stderr)
        sys.exit(1)
    os.makedirs(unit_dir, exist_ok=True)
    with open(unit_path, "w") as fh:
        fh.write(text)
    os.chmod(unit_path, 0o644)
    print(f"wrote {unit_path}")
    sc = ["systemctl"] + (["--user"] if user_mode else [])
    for cmd in (sc + ["daemon-reload"], sc + ["enable", name], sc + ["restart", name]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        print("$ " + " ".join(cmd) + ("" if r.returncode == 0 else
                                      f"  -> exit {r.returncode}: {r.stderr.strip()}"))
        if r.returncode != 0:
            sys.exit(1)
    if user_mode:
        print(f"tip: `loginctl enable-linger {pwd.getpwuid(os.getuid()).pw_name}` keeps it running "
              "after you log out")
    print(f"status: systemctl {'--user ' if user_mode else ''}status {name}")
    print(f"logs:   journalctl {'--user ' if user_mode else ''}-u {name} -f")


def cmd_tg_status(args):
    path = config_path()
    conf = read_json(path)
    st = read_json(state_path())
    info = {"config": path, "configured": bool(conf.get("token")), "profile": cli.PROFILE}
    if os.path.exists(path):
        info["config_mode"] = oct(os.stat(path).st_mode & 0o777)
    info.update(owner_id=conf.get("owner_id"), allowed_user_ids=conf.get("allowed_user_ids"),
                bot=conf.get("bot_username"), auto_approve=st.get("auto_approve", True),
                watch=st.get("watch", False), muse_profile=st.get("profile") or cli.PROFILE,
                chats=(st.get("chats") or {}).get(st.get("profile") or cli.PROFILE))
    if conf.get("token") and not args.offline:
        try:
            info["bot"] = "@" + TelegramAPI(conf["token"]).call("getMe", _http_timeout=15)["username"]
            info["token_ok"] = True
        except Exception as e:  # noqa: BLE001
            info["token_ok"] = False
            info["token_error"] = str(e)
    name = service_name(cli.PROFILE)
    info["service"] = {"unit": name, "state": "not installed"}
    if shutil.which("systemctl"):
        for mode in ([], ["--user"]):
            r = subprocess.run(["systemctl", *mode, "is-active", name], capture_output=True, text=True)
            state = r.stdout.strip()
            if r.returncode == 0 or state in ("activating", "failed", "deactivating"):
                info["service"] = {"unit": name, "scope": "user" if mode else "system", "state": state}
                break
    cli.out(info)


def cmd_tg_run(args):
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("curl_cffi", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    conf = read_json(config_path())
    if not conf.get("token"):
        print(f"not set up: run `{cli.cli_cmd('telegram setup')}`", file=sys.stderr)
        sys.exit(2)
    bot = Bot(TelegramAPI(conf["token"]))
    try:
        bot.run()
    except KeyboardInterrupt:
        pass
    finally:
        bot.stopping = True
        bot.stop_monitors()


def add_telegram_parser(sub):
    p = sub.add_parser("telegram", help="Telegram bot that operates your Muse (setup, run, install-service)")
    t = p.add_subparsers(dest="op", required=True)
    q = t.add_parser("setup", help="save the bot token (stdin or hidden prompt) and pair your account",
                     description="Reads the @BotFather token from stdin or a hidden prompt (never an "
                                 "argument), checks it, then prints a pairing code: send `/start CODE` "
                                 "to the bot to become its owner. Writes telegram.json (mode 600) in "
                                 "the active profile's folder.")
    q.add_argument("--allow", type=int, action="append", metavar="USER_ID",
                   help="also allow this Telegram user id (repeatable); the first becomes owner "
                        "when there is none, which skips pairing")
    q.add_argument("--no-pair", action="store_true", help="skip the /start pairing step")
    q.add_argument("--pair-timeout", type=int, default=600, help="seconds to wait for /start CODE")
    q.set_defaults(fn=cmd_tg_setup)
    q = t.add_parser("run", help="run the bot in the foreground (what the service runs)")
    q.add_argument("-v", "--verbose", action="store_true")
    q.set_defaults(fn=cmd_tg_run)
    q = t.add_parser("install-service", help="write and enable a systemd unit (Restart=always)")
    q.add_argument("--user", action="store_true", help="a systemd --user unit instead of a system unit")
    q.add_argument("--dry-run", action="store_true", help="print the unit, change nothing")
    q.set_defaults(fn=cmd_tg_install_service)
    q = t.add_parser("status", help="config, allowed users, token check, service state")
    q.add_argument("--offline", action="store_true", help="skip the Telegram token check")
    q.set_defaults(fn=cmd_tg_status)
