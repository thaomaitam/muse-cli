"""Telegram bridge (`muse-cli telegram`): fake Telegram API + fake Muse gateway."""
import base64
import io
import json
import os
import stat
import types

import pytest

from muse_cli import cli, tgapi, tgbot
from muse_cli.tgapi import TelegramAPI, TelegramError

OWNER = 1001
STRANGER = 666
TOKEN = "123456:" + "A" * 35


# -- fakes ------------------------------------------------------------------------------
class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.messages = {}
        self.docs = []
        self.files = {}
        self.next_id = 1
        self.html_fails = 0          # make the next N HTML sends/edits fail to parse

    def call(self, method, _http_timeout=None, _retries=None, **params):
        self.calls.append((method, params))
        if method in ("sendMessage", "editMessageText") and params.get("parse_mode") == "HTML" \
                and self.html_fails:
            self.html_fails -= 1
            raise TelegramError(method, 400, "Bad Request: can't parse entities: unclosed tag")
        if method == "sendMessage":
            mid = self.next_id
            self.next_id += 1
            self.messages[mid] = dict(params)
            return {"message_id": mid}
        if method == "editMessageText":
            self.messages.setdefault(params["message_id"], {}).update(params)
            return True
        if method == "getMe":
            return {"id": 1, "username": "muse_test_bot", "first_name": "Muse"}
        if method == "getUpdates":
            return []
        return True

    def send_file(self, chat_id, filename, data, caption=None, kind="document", **params):
        self.docs.append({"chat_id": chat_id, "name": filename, "data": data, "caption": caption,
                          "kind": kind, **params})
        mid = self.next_id
        self.next_id += 1
        return {"message_id": mid}

    def download(self, file_id, limit=None):
        return self.files[file_id], "x/" + file_id

    # helpers
    def sent(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method]

    def texts(self):
        return [p.get("text") for m, p in self.calls if m in ("sendMessage", "editMessageText")]

    def buttons(self, markup=None):
        if markup is None:
            for m, p in reversed(self.calls):
                if p.get("reply_markup") and p["reply_markup"].get("inline_keyboard"):
                    markup = p["reply_markup"]
                    break
            else:
                for d in reversed(self.docs):
                    if d.get("reply_markup"):
                        markup = d["reply_markup"]
                        break
        return {b["text"]: b["callback_data"] for row in (markup or {}).get("inline_keyboard", [])
                for b in row}


def ev(seq, name, text, status="completed", **payload):
    p = {"display_text": text, "message_id": f"m{seq}", **payload}
    if status:
        p["status"] = status
    return {"seq": seq, "event_name": name, "payload": p}


def ts(status, **kw):
    p = {"status": status, "agent_id": "r", "root_agent_id": "r", "is_thread": False}
    p.update(kw)
    return json.dumps({"event": "task.status", "payload": p})


class Server:
    """A tiny in-memory Muse: history per session, approvals, fs, sessions."""

    def __init__(self):
        self.seq = 100
        self.events = {None: [ev(100, "message.assistant", "hello earlier")]}
        self.sessions = [{"session_id": "main", "title": "Main", "is_thread": False, "updated_at_ms": 1}]
        self.approvals = []
        self.decided = []
        self.cancels = []
        self.fs = {}
        self.calls = []
        self.sent_texts = []
        self.reply = "pong"
        self.reply_extra = {}
        self.script = None           # callable(session_id) -> list of lines / callables
        self.idea_execs = []
        self.ops = []
        self.subscribes = 0
        self.downloads = []
        self.download_error = False
        self.idle_cap = 30           # how long an "IDLE" stream stays open at most

    def next_seq(self):
        self.seq += 1
        return self.seq

    def add(self, sid, name, text, **kw):
        e = ev(self.next_seq(), name, text, **kw)
        self.events.setdefault(sid, []).append(e)
        return e

    def default_script(self, sid):
        def finish():
            self.add(sid, "message.assistant", self.reply, **self.reply_extra)
        return [ts("completed"), ts("running"),
                json.dumps({"event": "delta.text_append", "payload": {"message_id": "x", "text": "po"}}),
                json.dumps({"event": "delta.text_append", "payload": {"message_id": "x", "text": "ng"}}),
                finish, ts("completed")]

    def connect(self, cfg):
        return FakeGateway(self)


class FakeGateway:
    vm_id = "vm-test"

    def __init__(self, server):
        self.s = server

    def call_json(self, method, path_params=None, body=None, query=None, timeout=30):
        s = self.s
        s.calls.append((method, body, path_params))
        body = body or {}
        if method == "chat.history":
            evs = s.events.setdefault(body.get("session_id"), [])
            return {"chat_events": evs[-int(body.get("limit", 10)):]}
        if method == "sessions.list":
            return {"sessions": s.sessions}
        if method == "chat.unread_count":
            return {"total": 2}
        if method == "identity":
            return {"name": "Muse"}
        if method == "session.start":
            sid = f"T{len(s.sessions)}"
            s.sessions.append({"session_id": sid, "title": body["params"].get("title"), "is_thread": True,
                               "updated_at_ms": 5})
            return {"session_id": sid}
        if method.startswith("session."):
            s.ops.append((method, body))
            return {"ok": True}
        if method == "egress.approvals":
            return {"pending_approvals": list(s.approvals)}
        if method == "egress.approval.decide":
            s.decided.append((path_params["approval_id"], body["decision"]))
            s.approvals = [a for a in s.approvals if a["approval_id"] != path_params["approval_id"]]
            return {"ok": True}
        if method == "chat.cancel":
            s.cancels.append(body)
            return {"cancelled": True}
        if method == "fs.write":
            s.fs[body["path"]] = base64.b64decode(body["data_base64"])
            return {}
        if method == "fs.delete":
            s.fs.pop(body["path"], None)
            return {}
        if method == "fs.read":
            data = s.fs.get(body["path"], b"")
            return {"data_base64": base64.b64encode(data).decode(), "eof": True}
        if method == "fs.list":
            return {"entries": [{"name": p, "type": "file", "size": len(d)} for p, d in s.fs.items()]}
        if method == "api.idea-cards.list":
            return {"sections": [{"title": "Today", "cards": [{"id": "i1", "title": "Plan a trip"}]}]}
        if method == "api.idea-cards.execute":
            s.idea_execs.append(body)
            return {"ok": True}
        if method == "goals.list":
            return {"goals": [{"id": "g1", "title": "Run 5k", "status": "active"}]}
        if method == "feed.list":
            return {"days": [{"local_date": "2026-10-26", "editions": [{"kind": "am", "units": [
                {"unit_id": "u1", "title": "News <b>"}]}]}]}
        raise AssertionError(f"unexpected method {method}")

    def request(self, method, path_params=None, body=None, query=None, timeout=30):
        return json.dumps(self.call_json(method, path_params, body)).encode()

    def _open(self, method, body=None, **_kw):
        assert method == "chat.stream"
        sid = body.get("session_id")
        text = body["items"][0]["text"]
        self.s.sent_texts.append((sid, text))
        self.s.add(sid, "message.user", text)
        return 7

    def subscribe_raw(self, method, body=None, on_record=None, should_stop=None, **_kw):
        sid = (body or {}).get("session_id")
        script = (self.s.script or self.s.default_script)(sid)
        self.s.subscribes += 1
        for item in script:
            if item == "IDLE":     # the stream stays open but goes quiet
                import time as _t
                end = _t.time() + self.s.idle_cap
                while _t.time() < end and not (should_stop and should_stop()):
                    _t.sleep(0.01)
                break
            if callable(item):
                item()
                continue
            if on_record(item.encode() if isinstance(item, str) else item):
                break
            if should_stop and should_stop():
                break
        return []

    def download_file(self, ref, timeout=300):
        path = str(ref.get("path") if isinstance(ref, dict) else ref)
        self.s.downloads.append(path)
        if self.s.download_error:
            from muse_cli.gateway import GatewayError
            raise GatewayError(404, "media.raw -> 404")
        return b"FILE:" + path.encode()

    def close(self):
        pass


# -- fixtures -------------------------------------------------------------------------
@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(cli.PROFILE_ENV, raising=False)
    monkeypatch.delenv("MUSE_AUTO_APPROVE", raising=False)
    for attr in ("PROFILE", "CONFIG_DIR", "CONFIG_FILE", "COOKIES_FILE"):
        monkeypatch.setattr(cli, attr, getattr(cli, attr))
    cli.activate_profile(cli.DEFAULT_PROFILE)
    cli.ensure_config_dir()
    tgbot.write_private_json(tgbot.config_path(), {"token": TOKEN, "owner_id": OWNER,
                                                   "allowed_user_ids": [OWNER]})
    monkeypatch.setattr(cli, "POLL", 0)
    monkeypatch.setattr(cli, "RESUB_PAUSE", 0, raising=False)
    monkeypatch.setattr(tgbot, "QUEUE_RECHECK", 0.3, raising=False)
    return tmp_path


@pytest.fixture
def env(home):
    server = Server()
    tg = FakeTelegram()
    bot = tgbot.Bot(tg, muse=tgbot.Muse(connector=server.connect), sync=True)
    bot.edit_interval = 0
    return types.SimpleNamespace(bot=bot, tg=tg, s=server, home=home)


_uid = iter(range(1, 10**6))


def msg(text=None, user=OWNER, **extra):
    m = {"message_id": next(_uid), "from": {"id": user}, "chat": {"id": user}}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return {"update_id": next(_uid), "message": m}


def press(env, label, user=OWNER):
    data = env.tg.buttons()[label]
    env.bot.handle_update({"update_id": next(_uid), "callback_query": {
        "id": "cq", "from": {"id": user}, "data": data,
        "message": {"message_id": 1, "chat": {"id": user}}}})


# -- security ---------------------------------------------------------------------------
def test_strangers_get_no_reply_and_reach_nothing(env):
    env.bot.handle_update(msg("hello", user=STRANGER))
    env.bot.handle_update(msg("/status", user=STRANGER))
    env.bot.handle_update({"update_id": 1, "callback_query": {"id": "q", "from": {"id": STRANGER},
                                                              "data": "c:x", "message": {}}})
    assert env.tg.calls == []
    assert env.s.calls == []


def test_owner_gets_help(env):
    env.bot.handle_update(msg("/help"))
    assert "Muse on Telegram" in env.tg.sent()[-1]["text"]
    assert env.tg.sent()[-1]["parse_mode"] == "HTML"


def test_owner_only_commands(env):
    env.bot.conf["allowed_user_ids"] = [OWNER, 2002]
    env.bot.handle_update(msg("/raw identity", user=2002))
    assert "Only the owner" in env.tg.sent()[-1]["text"]
    env.bot.handle_update(msg("/raw identity"))
    assert "Muse" in env.tg.sent()[-1]["text"]


def test_token_never_in_errors():
    class Boom:
        def post(self, url, **kw):
            raise OSError(f"connect failed for {url}")
    api = TelegramAPI(TOKEN, session=Boom())
    with pytest.raises(ConnectionError) as e:
        api.call("getMe")
    assert TOKEN not in str(e.value) and "<token>" in str(e.value)
    assert TOKEN not in repr(api)


def test_bad_token_rejected():
    with pytest.raises(ValueError):
        TelegramAPI("not-a-token")


# -- message -> Muse -> streamed reply ------------------------------------------------------
def test_message_streams_and_finishes(env):
    env.bot.handle_update(msg("reply with pong"))
    assert env.s.sent_texts == [(None, "reply with pong")]
    texts = env.tg.texts()
    assert texts[0] == "⏳ Sending to Muse…"
    # a plain-text streaming edit showed the partial reply, then the final HTML edit
    partial = [t for t in texts if t.startswith("po\n") or t.startswith("pong\n")]
    assert partial, texts
    final = env.tg.sent("editMessageText")[-1]
    assert final["text"] == "pong" and final["parse_mode"] == "HTML"
    assert not env.bot.turns
    # the watermark moved past the turn: the monitor will not re-forward it
    assert env.bot.watermark(None) == env.s.seq


def test_html_parse_error_falls_back_to_plain(env):
    env.s.reply = "**bold** reply"
    env.tg.html_fails = 1
    env.bot.handle_update(msg("hi"))
    last = env.tg.sent("editMessageText")[-1]
    assert "parse_mode" not in last and last["text"] == "**bold** reply"


def test_long_reply_is_split(env):
    env.s.reply = "\n\n".join(f"paragraph {i} " + "x" * 300 for i in range(25))   # ~7.8k chars
    env.bot.handle_update(msg("long please"))
    edits, sends = env.tg.sent("editMessageText"), env.tg.sent("sendMessage")
    assert all(len(p["text"]) <= 4096 for p in edits + sends)
    assert "paragraph 24" in sends[-1]["text"]
    assert not env.tg.docs


def test_huge_reply_becomes_document(env):
    env.s.reply = "start\n```python\n" + "print(1)\n" * 600 + "```"
    env.bot.handle_update(msg("code please"))
    assert env.tg.docs and env.tg.docs[-1]["name"].endswith(".md")
    assert b"print(1)" in env.tg.docs[-1]["data"]
    assert "full reply attached" in env.tg.sent("editMessageText")[-1]["text"]


def test_split_text_limits_and_fences():
    text = "```\n" + "line\n" * 2000 + "```"
    parts = tgapi.split_text(text, 3800)
    assert len(parts) > 1 and all(len(p) <= 3800 + 10 for p in parts)
    assert all(p.count("```") % 2 == 0 for p in parts)
    assert tgapi.split_text("short") == ["short"]


def test_md_to_html_escapes():
    h = tgapi.md_to_html("a <b> & **bold** `x<y` [l](https://e.com?a=1&b=2)\n```py\nif a<b:\n```")
    assert "&lt;b&gt;" in h and "<b>bold</b>" in h and "<code>x&lt;y</code>" in h
    assert '<a href="https://e.com?a=1&amp;b=2">l</a>' in h
    assert '<pre><code class="language-py">if a&lt;b:</code></pre>' in h


def test_files_from_turn_are_sent(env):
    env.s.reply_extra = {"data": {"images": [{"path": "sandbox://workspace/cat.png", "byte_len": 10}]}}
    env.bot.handle_update(msg("draw a cat"))
    assert env.tg.docs[-1]["name"] == "cat.png" and env.tg.docs[-1]["kind"] == "photo"


# -- questions and buttons -------------------------------------------------------------------
def test_widget_options_become_buttons_and_answer_goes_back(env):
    env.s.reply = "Which one?\n\n[[hatch_widget:w1]]"
    env.s.reply_extra = {"widgets": [{"widget_id": "w1", "kind": "option", "data": {"options": ["Alpha", "Beta"]},
                                      "state": {}}]}
    env.bot.handle_update(msg("pick"))
    assert set(env.tg.buttons()) == {"Alpha", "Beta"}
    env.s.reply, env.s.reply_extra = "ok, Beta", {}
    press(env, "Beta")
    assert env.s.sent_texts[-1] == (None, "Beta")
    assert env.tg.sent("editMessageText")[-1]["text"] == "ok, Beta"


def test_numbered_question_buttons():
    res = {"replies": [{"text": "Pick a plan:\n1. Fast\n2. Cheap\n3. Both\nWhich do you prefer?"}]}
    assert [v for _, v in tgbot.question_options(res)] == ["Fast", "Cheap", "Both"]
    res = {"replies": [{"text": "Should I deploy it now?"}]}
    assert tgbot.question_options(res) == tgbot.GENERIC_ANSWERS


def test_plain_question_gets_generic_buttons_and_free_text_reply(env):
    env.s.reply = "Should I continue?"
    env.bot.handle_update(msg("do it"))
    assert "👍 Yes" in env.tg.buttons()
    env.s.reply = "done"
    env.bot.handle_update(msg("yes, continue"))
    assert env.s.sent_texts[-1] == (None, "yes, continue")


def test_expired_button(env):
    env.bot.handle_update({"update_id": 5, "callback_query": {"id": "q", "from": {"id": OWNER},
                                                              "data": "c:nope", "message": {}}})
    assert "expired" in env.tg.sent("answerCallbackQuery")[-1]["text"]


# -- chats -------------------------------------------------------------------------------
def test_chats_paginate_and_switch(env):
    for i in range(10):
        env.s.sessions.append({"session_id": f"S{i}", "title": f"chat {i}", "is_thread": True,
                               "pinned": i == 3, "updated_at_ms": 10**12 + i * 3600000})
    env.bot.handle_update(msg("/chats"))
    b = env.tg.buttons()
    assert "💬 main chat ✓" in b and "Next ▶" in b
    assert list(b)[1] == "📌 chat 3"           # pinned first
    press(env, "Next ▶")
    assert "◀ Prev" in env.tg.buttons()
    env.bot.handle_update(msg("/chats"))
    press(env, "chat 9")
    assert env.bot.chat_state(OWNER)["session_id"] == "S9"
    env.bot.handle_update(msg("hello side chat"))
    assert env.s.sent_texts[-1] == ("S9", "hello side chat")
    env.bot.handle_update(msg("/main"))
    env.bot.handle_update(msg("back"))
    assert env.s.sent_texts[-1] == (None, "back")
    # persisted across restarts
    bot2 = tgbot.Bot(FakeTelegram(), muse=tgbot.Muse(connector=env.s.connect), sync=True)
    assert bot2.chat_state(OWNER)["session_id"] is None


def test_new_rename_archive_delete(env):
    env.bot.handle_update(msg("/new trip planning"))
    sid = env.bot.chat_state(OWNER)["session_id"]
    assert sid and env.s.sessions[-1]["title"] == "trip planning"
    env.bot.handle_update(msg("/rename Japan trip"))
    assert ("session.rename", {"session_id": sid, "title": "Japan trip"}) in env.s.ops
    assert env.bot.chat_state(OWNER)["title"] == "Japan trip"
    env.bot.handle_update(msg("/pin"))
    assert env.s.ops[-1][0] == "session.pin"
    env.bot.handle_update(msg("/delete"))
    assert "🗑 Yes, delete" in env.tg.buttons()
    press(env, "🗑 Yes, delete")
    assert env.s.ops[-1] == ("session.delete", {"method": "/api/session/delete", "session_id": sid})
    assert env.bot.chat_state(OWNER)["session_id"] is None
    env.bot.handle_update(msg("/archive"))
    assert "side chats" in env.tg.sent()[-1]["text"]


def test_history(env):
    env.s.add(None, "message.user", "hi there")
    env.s.add(None, "message.assistant", "hello <you>")
    env.bot.handle_update(msg("/history 2"))
    t = env.tg.sent()[-1]["text"]
    assert "🧑 You: hi there" in t and "&lt;you&gt;" in t


# -- approvals ---------------------------------------------------------------------------
APPROVAL = {"approval_id": "a1",
            "display": {"summary_title": "Share info with github.com",
                        "permission_question": {"kind": "network_access", "scope": "github.com",
                                                "text": "Allow {assistant}?"}},
            "decision_options": [{"kind": "allow_once"}, {"kind": "allow_always"}, {"kind": "deny"}]}


def snapshot(*items):
    return json.dumps({"event": "approvals.snapshot", "payload": {"pending_approvals": list(items)}})


def test_approvals_command_and_buttons(env):
    env.s.approvals = [APPROVAL]
    env.bot.handle_update(msg("/approvals"))
    assert "Share info with github.com" in env.tg.sent()[-1]["text"]
    press(env, "✅ Always allow")
    assert env.s.decided == [("a1", "allow_always")]


def test_turn_auto_approves_forever_by_default(env):
    assert env.bot.auto_approve is True
    env.s.approvals = [APPROVAL]

    def script(sid):
        return [ts("running"), snapshot(APPROVAL), lambda: env.s.add(sid, "message.assistant", "cloned"),
                ts("completed")]
    env.s.script = script
    env.bot.handle_update(msg("clone the repo"))
    assert env.s.decided == [("a1", "allow_always")]
    final = env.tg.sent("editMessageText")[-1]["text"]
    assert "cloned" in final and "auto-approved (always): Share info with github.com" in final


def test_turn_with_autoapprove_off_shows_buttons_and_resumes(env):
    env.bot.handle_update(msg("/autoapprove off"))
    assert env.bot.auto_approve is False
    assert json.load(open(cli.CONFIG_FILE))["auto_approve"] == "off"
    env.s.approvals = [APPROVAL]
    env.s.script = lambda sid: [ts("running"), snapshot(APPROVAL)]
    env.bot.handle_update(msg("clone the repo"))
    assert "🔐 Approval" in env.tg.sent("editMessageText")[-1]["text"]
    assert env.s.decided == []
    env.s.script = lambda sid: [ts("running"), lambda: env.s.add(sid, "message.assistant", "cloned"),
                                ts("completed")]
    press(env, "✅ Always allow")
    assert env.s.decided == [("a1", "allow_always")]
    assert env.tg.sent("editMessageText")[-1]["text"] == "cloned"     # the resumed turn finished
    env.bot.handle_update(msg("/autoapprove on"))
    assert env.bot.auto_approve and json.load(open(cli.CONFIG_FILE))["auto_approve"] == "always"


def test_background_approvals(env):
    env.bot.on_pending([APPROVAL])                          # auto-approve on
    assert env.s.decided == [("a1", "allow_always")]
    env.bot.on_pending([APPROVAL])                          # claimed: decided once only
    assert len(env.s.decided) == 1
    env.bot.state["auto_approve"] = False
    a2 = dict(APPROVAL, approval_id="a2")
    env.bot.on_pending([a2])
    env.bot.on_pending([a2])                                # announced once, not spammed
    assert sum("🔐" in (t or "") for t in env.tg.texts()) == 1
    assert "✅ Always allow" in env.tg.buttons()


# -- files in ------------------------------------------------------------------------------
def test_photo_upload_goes_to_system_files_and_muse(env):
    env.tg.files["F1"] = b"\x89PNGdata"
    env.bot.handle_update(msg(caption="what is this?", photo=[{"file_id": "small"},
                                                              {"file_id": "F1", "file_size": 8}]))
    (path, data), = env.s.fs.items()
    assert path.startswith("telegram-inbox/") and path.endswith(".jpg") and data == b"\x89PNGdata"
    sid, text = env.s.sent_texts[-1]
    assert text.startswith("what is this?") and path in text


def test_document_upload_without_caption(env):
    env.tg.files["D1"] = b"hello"
    env.bot.handle_update(msg(document={"file_id": "D1", "file_name": "my notes.txt", "mime_type": "text/plain"}))
    path = next(iter(env.s.fs))
    assert path.endswith("-my_notes.txt")
    assert path in env.s.sent_texts[-1][1]


# -- /stop ------------------------------------------------------------------------------
def test_stop_during_turn(env):
    def script(sid):
        return [ts("running"),
                json.dumps({"event": "delta.text_append", "payload": {"message_id": "x", "text": "counting 1 2"}}),
                lambda: env.bot.handle_update(msg("/stop")),
                ts("cancelled")]
    env.s.script = script
    env.bot.handle_update(msg("count to 100"))
    assert env.s.cancels == [{}]
    assert any("Stopped Muse's turn" in (t or "") for t in env.tg.texts())
    assert "⏹ Stopped." in env.tg.sent("editMessageText")[-1]["text"]


def test_stop_in_side_chat_and_idle(env):
    env.bot.set_chat(OWNER, "T7", "side")
    env.bot.handle_update(msg("/stop"))
    assert env.s.cancels == [{"session_id": "T7"}]


def test_queue_while_busy(env):
    env.bot.turns[OWNER] = tgbot.Turn(env.bot, OWNER, None, "main chat")
    env.bot.workers[OWNER] = object()
    env.bot.handle_update(msg("second"))
    assert "Queued" in env.tg.sent()[-1]["text"]
    assert [j[:2] for j in env.bot.queues[OWNER]] == [("text", "second")]


# -- profiles --------------------------------------------------------------------------------
def test_profile_switch(env):
    work = cli.profile_paths("work")
    os.makedirs(work["dir"], mode=0o700)
    open(work["cookies"], "w").write("hatch_sess=x")
    env.bot.handle_update(msg("/profile"))
    assert "✓ default (no cookies)" in env.tg.buttons() and "work" in env.tg.buttons()
    press(env, "work")
    assert cli.PROFILE == "work" and env.bot.state["profile"] == "work"
    assert env.bot.chat_state(OWNER)["session_id"] is None
    # telegram.json stays in the profile it was set up in
    assert os.path.exists(tgbot.config_path(env.bot.home)) and env.bot.home != cli.CONFIG_DIR
    saved = json.load(open(tgbot.state_path(env.bot.home)))
    assert saved["profile"] == "work"


# -- misc commands ------------------------------------------------------------------------------
def test_ideas_execute_button(env):
    env.bot.handle_update(msg("/ideas"))
    press(env, "▶️ Execute")
    assert env.s.idea_execs == [{"ideaCardId": "i1", "mode": "full"}]


def test_fs_commands(env):
    env.s.fs["notes.md"] = b"# hi\n" + b"x" * 5000
    env.bot.handle_update(msg("/fs ls"))
    assert "notes.md" in env.tg.sent()[-1]["text"]
    env.bot.handle_update(msg("/fs cat notes.md"))
    assert env.tg.docs[-1]["name"] == "notes.md"            # long -> document
    env.bot.handle_update(msg("/fs rm notes.md"))
    assert "notes.md" in env.s.fs                          # nothing deleted before confirming
    press(env, "🗑 Yes, delete")
    assert "notes.md" not in env.s.fs


def test_feed_goals_status_unread(env):
    env.bot.handle_update(msg("/feed"))
    assert "News &lt;b&gt;" in env.tg.sent()[-1]["text"]
    env.bot.handle_update(msg("/goals"))
    assert "Run 5k" in env.tg.sent()[-1]["text"]
    env.bot.handle_update(msg("/status"))
    t = env.tg.sent()[-1]["text"]
    assert "vm-test" in t and "auto-approve: on (always)" in t
    env.bot.handle_update(msg("/nosuch"))
    assert "Unknown command" in env.tg.sent()[-1]["text"]


def test_files_command_downloads(env):
    env.s.add(None, "message.assistant", "here", data={"videos": [{"path": "sandbox://workspace/v.mp4"}]})
    env.bot.handle_update(msg("/files"))
    press(env, "📥 v.mp4")
    assert env.tg.docs[-1]["name"] == "v.mp4" and env.tg.docs[-1]["data"].startswith(b"FILE:")


def test_update_runs_upgrade_then_restarts(env, monkeypatch):
    ran = []
    monkeypatch.setattr(tgbot.subprocess, "run",
                        lambda argv, **kw: ran.append(argv) or types.SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    restarted = []
    env.bot.restart_hook = lambda: restarted.append(True)
    env.bot.handle_update(msg("/update"))
    assert ran and restarted


# -- background notifications -------------------------------------------------------------------
def test_background_messages_forwarded_once(env):
    env.bot.check_background(None)                       # first look: sets the watermark only
    assert not env.tg.sent()
    env.s.add(None, "message.assistant", "scheduled report ready")
    env.s.add(None, "message.assistant", "still writing", status=None)
    env.bot.check_background(None)
    assert "scheduled report ready" in env.tg.sent()[-1]["text"]
    env.bot.check_background(None)
    assert len(env.tg.sent()) == 1
    env.s.events[None][-1]["payload"]["status"] = "completed"
    env.bot.check_background(None)
    assert "still writing" in env.tg.sent()[-1]["text"] and len(env.tg.sent()) == 2


def test_background_skips_busy_session(env):
    env.bot.check_background(None)
    env.s.add(None, "message.assistant", "turn reply")
    env.bot.turns[OWNER] = tgbot.Turn(env.bot, OWNER, None, "main chat")
    env.bot.check_background(None)
    assert not env.tg.sent()


def test_auth_expiry_notified_once(env):
    from muse_cli.gateway import AuthError

    def broken(cfg):
        raise AuthError("auth/check -> 401 (cookies expired? re-export)")
    env.bot.muse.connector = broken
    env.bot.handle_update(msg("/status"))
    env.bot.handle_update(msg("/unread"))
    texts = env.tg.texts()
    assert sum("cookies expired" in t for t in texts) == 1
    env.bot.muse.connector = env.s.connect
    env.bot.handle_update(msg("/unread"))
    assert any("works again" in t for t in env.tg.texts())


def test_cookies_via_login(env):
    env.bot.handle_update(msg("/login"))
    env.bot.handle_update(msg("hatch_sess=abc; other=1"))
    assert open(cli.COOKIES_FILE).read().strip() == "hatch_sess=abc; other=1"
    assert stat.S_IMODE(os.stat(cli.COOKIES_FILE).st_mode) == 0o600
    assert env.tg.sent("deleteMessage")
    assert "working" in env.tg.sent()[-1]["text"]


def test_poll_loop_survives_bad_update(env, monkeypatch):
    ups = [{"update_id": 10, "message": {"message_id": 1, "from": {"id": OWNER}, "chat": {"id": OWNER},
                                         "text": "/help"}},
           {"update_id": 11, "message": {"from": {"id": OWNER}}},            # malformed
           {"update_id": 12, "message": {"message_id": 2, "from": {"id": OWNER}, "chat": {"id": OWNER},
                                         "text": "/help"}}]
    real = env.tg.call
    env.tg.call = lambda method, **p: ups if method == "getUpdates" else real(method, **p)
    off, hint = env.bot.poll_once(None)
    assert off == 13 and hint == 0
    assert len(env.tg.sent()) == 2
    assert json.load(open(tgbot.state_path()))["offset"] == 13

    def down(method, **p):
        raise ConnectionError("network down")
    env.tg.call = down
    assert env.bot.poll_once(13) == (13, -1)


# -- setup and service ----------------------------------------------------------------------
class TTYless(io.StringIO):
    def isatty(self):
        return False


def test_setup_writes_private_config_and_pairs(home, capsys):
    os.remove(tgbot.config_path())
    tg = FakeTelegram()
    args = types.SimpleNamespace(allow=None, no_pair=False, pair_timeout=5)
    tgbot.cmd_tg_setup(args, stdin=TTYless(TOKEN + "\n"), api_factory=lambda t: tg,
                       pair_fn=lambda api, code, timeout: 4242)
    path = tgbot.config_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    conf = json.load(open(path))
    assert conf["token"] == TOKEN and conf["owner_id"] == 4242 and conf["allowed_user_ids"] == [4242]
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err
    assert "/start " in out.out
    assert any(m == "setMyCommands" for m, _ in tg.calls)


def test_setup_allow_skips_pairing(home):
    tg = FakeTelegram()
    args = types.SimpleNamespace(allow=[7, 8], no_pair=False, pair_timeout=5)

    def no_pair(*a, **k):
        raise AssertionError("should not pair")
    tgbot.cmd_tg_setup(args, stdin=TTYless("999:" + "B" * 35 + "\n"), api_factory=lambda t: tg, pair_fn=no_pair)
    conf = json.load(open(tgbot.config_path()))
    assert conf["owner_id"] == 7 and conf["allowed_user_ids"] == [7, 8]


def test_token_is_not_a_cli_argument():
    with pytest.raises(SystemExit):
        cli.main(["telegram", "setup", "--token", TOKEN])


def test_pairing_matches_code_only():
    tg = FakeTelegram()
    batches = [[{"update_id": 1, "message": {"text": "/start 000000", "from": {"id": 5}, "chat": {"id": 5}}},
                {"update_id": 2, "message": {"text": "/start 123456", "from": {"id": 9}, "chat": {"id": 9}}}]]

    def call(method, **p):
        tg.calls.append((method, p))
        if method == "getUpdates":
            return batches.pop(0) if p.get("offset") != -1 and batches else []
        return {"message_id": 1}
    tg.call = call
    assert tgbot.pair(tg, "123456", timeout=5) == 9


def test_systemd_unit_text():
    u = tgbot.service_unit("/root/.local/bin/muse-cli", "no.0", False, "/root", "/root/.local/bin:/usr/bin",
                           user="root")
    assert "ExecStart=/root/.local/bin/muse-cli --profile no.0 telegram run" in u
    assert "Restart=always" in u and "User=root" in u and "WantedBy=multi-user.target" in u
    assert "Environment=HOME=/root" in u
    d = tgbot.service_unit("/x/muse-cli", "default", True, "/home/u", "/usr/bin")
    assert "ExecStart=/x/muse-cli telegram run" in d and "WantedBy=default.target" in d and "User=" not in d
    assert tgbot.service_name("default") == "muse-telegram.service"
    assert tgbot.service_name("no.0") == "muse-telegram-no.0.service"


def test_install_service_dry_run(home, capsys):
    cli.main(["telegram", "install-service", "--dry-run"])
    out = capsys.readouterr().out
    assert "telegram run" in out and "Restart=always" in out


def test_watch_lines_batched(env):
    env.bot.state["watch"] = True
    env.bot.watch_event({"event": "delta.tool_start", "payload": {"tool_name": "browser"}})
    env.bot.watch_event({"event": "delta.text_append", "payload": {"text": "noise"}})
    env.bot.flush_watch(force=True)
    assert env.tg.sent()[-1]["text"] == "🔧 browser"
    env.bot.flush_watch(force=True)
    assert len(env.tg.sent()) == 1


def test_threaded_turn_and_commands(home):
    """The real (non-sync) path: worker thread per chat, command pool, ticker."""
    import time as _t
    server, tg = Server(), FakeTelegram()
    bot = tgbot.Bot(tg, muse=tgbot.Muse(connector=server.connect))
    bot.edit_interval = 0
    bot.ensure_monitors = lambda restart=False: None      # no live subscriptions in tests
    bot.handle_update(msg("ping"))
    bot.handle_update(msg("/help"))
    deadline = _t.time() + 10
    while _t.time() < deadline and (bot.turns or bot.workers or bot.queues[OWNER]):
        _t.sleep(0.05)
    bot.pool.shutdown(wait=True)
    assert server.sent_texts == [(None, "ping")]
    assert tg.sent("editMessageText")[-1]["text"] == "pong"
    assert any("Muse on Telegram" in (t or "") for t in tg.texts())


def test_cli_stop_calls_chat_cancel(home, monkeypatch, capsys):
    server = Server()
    monkeypatch.setattr(cli, "connect", server.connect)
    cli.main(["stop", "--thread", "T1"])
    assert server.cancels == [{"session_id": "T1"}]
    assert json.loads(capsys.readouterr().out) == {"cancelled": True}


def _pair_with(msgs, code="530029"):
    tg = FakeTelegram()
    batches = [[{"update_id": i + 1, "message": m} for i, m in enumerate(msgs)]]

    def call(method, **p):
        tg.calls.append((method, p))
        if method == "getUpdates":
            return batches.pop(0) if p.get("offset") != -1 and batches else []
        return {"message_id": 1}
    tg.call = call
    return tgbot.pair(tg, code, timeout=1, sleep=lambda s: None)


def test_pairing_accepts_pasted_link_and_bare_code():
    pasted = {"text": "https://t.me/musecli_bot\n/start 530029", "from": {"id": 7},
              "chat": {"id": 7, "type": "private"}}
    assert _pair_with([pasted]) == 7
    bare = {"text": "530029", "from": {"id": 8}, "chat": {"id": 8, "type": "private"}}
    assert _pair_with([bare]) == 8


def test_pairing_rejects_groups_and_partial_codes():
    group = {"text": "/start 530029", "from": {"id": 7}, "chat": {"id": -1, "type": "group"}}
    partial = {"text": "/start 5300291", "from": {"id": 8}, "chat": {"id": 8, "type": "private"}}
    assert _pair_with([group, partial]) is None


# -- turn-end detection (regressions: 0.5.1 "Queued" forever) ---------------------------------
def _wait_env(home):
    server = Server()
    assert server.add(None, "message.user", "hi")["seq"] == 101
    return server


def test_run_wait_fast_turn_finished_before_subscribe(home):
    """The reply landed and the task completed before we subscribed: the replay
    shows only "completed". 0.5.1 ignored it (never saw running) and hung."""
    import time as _t
    server = _wait_env(home)
    server.add(None, "message.assistant", "Hi Boss. What are we working on next?")
    server.script = lambda sid: [ts("completed"), "IDLE"]
    t0 = _t.time()
    code, res = cli.run_wait({}, None, 101, 30, require_running=True, connector=server.connect,
                             auto_approve=False, recheck_after=60)
    assert _t.time() - t0 < 2
    assert res["outcome"] == "needs_input" and res["final_text"].startswith("Hi Boss")


def test_run_wait_stale_completed_without_reply_keeps_waiting(home):
    server = _wait_env(home)

    def script(sid):
        return [ts("completed"), ts("running"), lambda: server.add(None, "message.assistant", "done!"),
                ts("completed")]
    server.script = script
    code, res = cli.run_wait({}, None, 101, 30, require_running=True, connector=server.connect,
                             auto_approve=False)
    assert res["outcome"] == "done" and res["final_text"] == "done!" and server.subscribes == 1


def test_run_wait_watchdog_rechecks_a_silent_stream(home):
    """The completion event never arrives on the open subscription: after
    recheck_after seconds of silence the server state is re-read."""
    import time as _t
    server = _wait_env(home)
    calls = []

    def script(sid):
        calls.append(1)
        if len(calls) == 1:
            return [ts("running"), lambda: server.add(None, "message.assistant", "pong"), "IDLE"]
        return [ts("completed")]
    server.script = script
    t0 = _t.time()
    code, res = cli.run_wait({}, None, 101, 30, require_running=True, connector=server.connect,
                             auto_approve=False, recheck_after=0.2)
    assert res["outcome"] == "done" and res["final_text"] == "pong"
    assert len(calls) == 2 and _t.time() - t0 < 3


def test_run_wait_resubscribes_when_stream_ends(home):
    server = _wait_env(home)
    calls = []

    def script(sid):
        calls.append(1)
        if len(calls) == 1:
            return [ts("running")]           # stream closed by the server mid-turn
        return [ts("running"), lambda: server.add(None, "message.assistant", "ok"), ts("completed")]
    server.script = script
    code, res = cli.run_wait({}, None, 101, 30, require_running=True, connector=server.connect,
                             auto_approve=False)
    assert res["outcome"] == "done" and len(calls) == 2


def test_run_wait_survives_subscription_errors(home):
    server = _wait_env(home)
    calls = []

    def script(sid):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("socket closed")
        return [ts("running"), lambda: server.add(None, "message.assistant", "ok"), ts("completed")]
    server.script = script
    code, res = cli.run_wait({}, None, 101, 30, require_running=True, connector=server.connect,
                             auto_approve=False)
    assert res["outcome"] == "done"


def test_run_wait_recheck_hook(home):
    """recheck() (the bot's queue self-heal) re-reads the server immediately."""
    server = _wait_env(home)
    calls, asked = [], [True]

    def script(sid):
        calls.append(1)
        if len(calls) == 1:
            return [ts("running"), lambda: server.add(None, "message.assistant", "x"), "IDLE"]
        return [ts("completed")]
    server.script = script

    def recheck():
        if asked and calls:
            asked.pop()
            return True
        return False
    code, res = cli.run_wait({}, None, 101, 30, require_running=True, connector=server.connect,
                             auto_approve=False, recheck=recheck, recheck_after=60)
    assert res["outcome"] == "done" and len(calls) == 2


def test_bot_fast_reply_turn_ends(env):
    """Muse answered before the bot subscribed (main chat, short reply)."""
    def script(sid):
        if not any(e["event_name"] == "message.assistant" and e["seq"] > 101
                   for e in env.s.events[sid]):
            env.s.add(sid, "message.assistant", "Hi Boss. What are we working on next?")
        return [ts("completed"), "IDLE"]
    env.s.script = script
    import time as _t
    t0 = _t.time()
    env.bot.handle_update(msg("hi"))
    assert _t.time() - t0 < 3
    final = env.tg.sent("editMessageText")[-1]["text"]
    assert final.startswith("Hi Boss") and "online" not in final and "Still running" not in final
    assert not env.bot.turns and not env.bot.workers


def test_question_turn_then_followup_is_delivered(env):
    """A turn that ends with a question is over: the next message is sent,
    not queued, and its own turn ends."""
    env.s.reply = "Boss muốn test foreground trước hay cài service luôn?"
    env.bot.handle_update(msg("set it up"))
    assert "Muse is asking" in env.tg.sent("editMessageText")[-1]["text"]
    assert env.tg.buttons()
    def fast(sid):    # the follow-up is answered before the bot subscribes
        if env.s.events[sid][-1]["event_name"] == "message.user":
            env.s.add(sid, "message.assistant", "Hi Boss.")
        return [ts("completed"), "IDLE"]
    env.s.script = fast
    import time as _t
    t0 = _t.time()
    env.bot.handle_update(msg("hi"))
    assert _t.time() - t0 < 3
    assert [t for _s, t in env.s.sent_texts] == ["set it up", "hi"]
    assert not any("Queued" in (t or "") for t in env.tg.texts())
    assert env.tg.sent("editMessageText")[-1]["text"] == "Hi Boss."
    assert not env.bot.turns


def test_finished_turn_still_cleaning_up_does_not_say_queued(env):
    """0.5.1 said "Queued" while the worker was still sending files of a turn
    that had already ended."""
    t = tgbot.Turn(env.bot, OWNER, None, "main chat")
    t.finished = True
    env.bot.turns[OWNER] = t
    env.bot.workers[OWNER] = object()
    env.bot.handle_update(msg("next"))
    assert not any("Queued" in (x or "") for x in env.tg.texts())
    assert [j[:2] for j in env.bot.queues[OWNER]] == [("text", "next")]


def test_queue_self_heals_when_server_says_idle(env):
    """A turn the bot still thinks is running is asked to re-check the server;
    when it ends, the new message goes out with no "Queued" notice."""
    import threading
    t = tgbot.Turn(env.bot, OWNER, None, "main chat")
    env.bot.turns[OWNER] = t
    env.bot.workers[OWNER] = object()

    def fake_follow():                     # stands in for run_wait polling take_recheck()
        while not t.take_recheck():
            pass
        t.finished = True
    th = threading.Thread(target=fake_follow)
    th.start()
    env.bot.handle_update(msg("Bạn là models gì"))
    th.join(2)
    assert t.finished
    assert not any("Queued" in (x or "") for x in env.tg.texts())


def test_queue_notice_when_muse_really_busy(env):
    t = tgbot.Turn(env.bot, OWNER, None, "main chat")
    env.bot.turns[OWNER] = t
    env.bot.workers[OWNER] = object()
    env.bot.handle_update(msg("second"))
    assert t.recheck_requested                   # it asked for a server re-check first
    assert "Queued" in env.tg.sent()[-1]["text"]


def test_threaded_stuck_turn_is_rescued_by_next_message(home, monkeypatch):
    """End to end on the real threaded path: the live completion is missed, the
    watchdog is far away, and the user's next message triggers the re-check;
    both messages reach Muse and both turns end."""
    import time as _t
    monkeypatch.setattr(tgbot, "WATCHDOG", 120)
    monkeypatch.setattr(tgbot, "QUEUE_RECHECK", 5)
    server, tg = Server(), FakeTelegram()
    first = []

    def script(sid):
        if not first:
            first.append(1)
            return [ts("running"), lambda: server.add(sid, "message.assistant", "Hi Boss."), "IDLE"]
        if len(first) == 1 and server.sent_texts[-1][1] == "hi":
            first.append(2)
            return [ts("completed")]                 # the re-check: Muse is idle
        return [ts("running"), lambda: server.add(sid, "message.assistant", "I am Muse."),
                ts("completed")]
    server.script = script
    bot = tgbot.Bot(tg, muse=tgbot.Muse(connector=server.connect))
    bot.edit_interval = 0
    bot.ensure_monitors = lambda restart=False: None
    bot.handle_update(msg("hi"))
    deadline = _t.time() + 3
    while _t.time() < deadline and not server.sent_texts:
        _t.sleep(0.02)
    _t.sleep(0.3)
    bot.handle_update(msg("Bạn là models gì"))
    deadline = _t.time() + 10
    while _t.time() < deadline and (bot.turns or bot.workers or bot.queues[OWNER]):
        _t.sleep(0.05)
    bot.pool.shutdown(wait=True)
    assert [t for _s, t in server.sent_texts] == ["hi", "Bạn là models gì"]
    finals = [p["text"] for p in tg.sent("editMessageText") if "⏳" not in p["text"]]
    assert "Hi Boss." in finals and "I am Muse." in finals
    assert not any("Queued" in (x or "") for x in tg.texts())


def test_turn_logging_is_useful_and_short(env, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="muse_cli.telegram")
    secret = "a very long private message that should not be logged in full " * 3
    env.bot.handle_update(msg(secret))
    logs = caplog.text
    assert "turn start" in logs and "turn end" in logs and "outcome=done" in logs
    assert "queue chat=" in logs and "dequeue chat=" in logs
    assert secret not in logs and secret[:24] in logs
    assert TOKEN not in logs


def test_status_shows_server_task_state(env):
    env.s.script = lambda sid: [ts("completed")]
    env.bot.handle_update(msg("/status"))
    assert "completed" in env.tg.sent()[-1]["text"]


def test_resubscribed_stream_does_not_duplicate_text(env):
    t = tgbot.Turn(env.bot, OWNER, None, "main")
    for _ in range(2):   # the same message streamed again after a re-subscription
        t.on_event(json.dumps({"event": "delta.message_start", "payload": {"message_id": "a"}}))
        t.on_event(json.dumps({"event": "delta.text_append", "payload": {"message_id": "a", "text": "Hi"}}))
    assert t.parts["a"] == "Hi"


def _online():
    return json.dumps({"event": "agent.status", "payload": {"activity_code": "online",
                                                             "activity_text": "online", "is_thread": False}})


def test_run_wait_online_without_turn_end_rechecks_fast(home, monkeypatch):
    """Muse said "online" (idle) but the stream never carried the completed status
    (live case: the bot showed "⏳ online · 2m 41s")."""
    import time as _t
    monkeypatch.setattr(cli, "ONLINE_GRACE", 0.1)
    server = _wait_env(home)
    calls = []

    def script(sid):
        calls.append(1)
        if len(calls) == 1:
            return [ts("running"), lambda: server.add(None, "message.assistant", "OK"), _online(), "IDLE"]
        return [_online(), ts("completed")]
    server.script = script
    t0 = _t.time()
    code, res = cli.run_wait({}, None, 101, 30, require_running=True, connector=server.connect,
                             auto_approve=False, recheck_after=60)
    assert res["outcome"] == "done" and res["final_text"] == "OK"
    assert len(calls) == 2 and _t.time() - t0 < 3


def test_keepalives_do_not_feed_the_watchdog(home):
    import time as _t
    server = _wait_env(home)
    calls = []
    ka = json.dumps({"event": "keepalive", "payload": {}})

    def script(sid):
        calls.append(1)
        if len(calls) == 1:
            return [ts("running"), lambda: server.add(None, "message.assistant", "x")] + \
                [lambda: _t.sleep(0.02), ka] * 100 + ["IDLE"]
        return [ts("completed")]
    server.script = script
    t0 = _t.time()
    code, res = cli.run_wait({}, None, 101, 30, require_running=True, connector=server.connect,
                             auto_approve=False, recheck_after=0.3)
    assert res["outcome"] == "done" and len(calls) == 2
    assert _t.time() - t0 < 1.5        # 100 keep-alives (2 s) did not hold off the watchdog


def test_online_status_label():
    ev_ = json.loads(_online())
    assert tgbot.status_from_event(ev_) == "⏳ Muse is wrapping up…"


def test_upload_failure_is_reported_not_silent(env):
    def boom(fn):
        raise ValueError("Data must be bytes and less or equal 65535 bytes in length")
    env.bot.muse.fs = boom
    env.tg.files["F9"] = b"x" * 70000
    env.bot.handle_update(msg(document={"file_id": "F9", "file_name": "big.bin"}))
    assert "Upload to System Files failed" in env.tg.sent()[-1]["text"]
    assert not env.s.sent_texts


# -- media Muse shows (delta.presentation), e.g. a generated image ------------------------------
DUCK = "sandbox://workspace/imagine_media/media-generation-duck-0-abc.webp"


def presentation(server, sid, path=DUCK, label="Ảnh vịt con", mime="image/webp", kind="image",
                 byte_len=549408):
    """Add a delta.presentation like the real seq 4647 one."""
    seq = server.next_seq()
    key = {"image": "images", "video": "videos", "audio": "audios"}.get(kind, "files")
    e = {"seq": seq, "event_name": "delta.presentation", "payload": {
        "kind": kind, "display_text": label, "image_path": path.replace("sandbox://", ""),
        "fallback_text": f"![{label}]({path})\n",
        "data": {"fallback_text": f"![{label}]({path})\n", key: [{
            "byte_len": byte_len, "label": label, "mime": mime, "missing": False, "path": path,
            "variants": {"original": "https://vm.metaaivm.com/media/raw/" + path.replace("sandbox://", "")}}]}}}
    server.events.setdefault(sid, []).append(e)
    return e


def image_turn_script(env, before="Xong rồi, ảnh vịt con đây.", after="Một chú vịt con."):
    def script(sid):
        def finish():
            env.s.add(sid, "message.assistant", before)
            presentation(env.s, sid)
            env.s.add(sid, "message.assistant", after)
        return [ts("completed"), ts("running"), finish, ts("completed")]
    return script


def test_turn_with_image_presentation_sends_photo_once_in_order(env):
    env.s.script = image_turn_script(env)
    env.bot.handle_update(msg("Tạo ảnh con vịt bằng muse"))
    photos = [d for d in env.tg.docs if d["kind"] == "photo"]
    assert len(photos) == 1
    p = photos[0]
    assert p["name"].endswith(".webp") and p["caption"] == "Ảnh vịt con"
    assert p["data"] == b"FILE:" + DUCK.encode()
    # order: text before the image (the streamed message), the photo, then the text after it
    edits = env.tg.sent("editMessageText")
    assert edits[-1]["text"] == "Xong rồi, ảnh vịt con đây."
    after = [c for c in env.tg.calls if c[0] == "sendMessage" and "Một chú vịt con" in c[1]["text"]]
    assert after and env.tg.calls.index(after[0]) > 0
    # nothing is sent again: the monitor sees the same events later
    env.bot.check_background(env.bot.chat_state(OWNER)["session_id"])
    env.bot.check_background(None)
    assert len([d for d in env.tg.docs if d["kind"] == "photo"]) == 1
    assert env.s.downloads == [DUCK]


def test_photo_sent_between_texts_order(env):
    order = []
    orig_send_file = env.tg.send_file

    def send_file(*a, **kw):
        order.append("photo")
        return orig_send_file(*a, **kw)
    orig_call = env.tg.call

    def call(method, **p):
        if method == "sendMessage" and "Một chú" in (p.get("text") or ""):
            order.append("after")
        if method == "editMessageText" and p.get("text") == "Xong rồi, ảnh vịt con đây.":
            order.append("before")
        return orig_call(method, **p)
    env.tg.send_file, env.tg.call = send_file, call
    env.s.script = image_turn_script(env)
    env.bot.handle_update(msg("duck"))
    assert order == ["before", "photo", "after"]


def test_image_only_turn_and_question_buttons_after_it(env):
    def script(sid):
        def finish():
            presentation(env.s, sid)
            env.s.add(sid, "message.assistant", "Bạn có muốn thêm một ảnh nữa không?")
        return [ts("completed"), ts("running"), finish, ts("completed")]
    env.s.script = script
    env.bot.handle_update(msg("duck"))
    assert len(env.tg.docs) == 1
    assert env.tg.texts()[-1].startswith("Bạn có muốn") and "Muse is asking" in env.tg.texts()[-1]
    assert env.tg.buttons()


def test_monitor_forwards_background_image_once(env):
    env.bot.check_background(None)                       # first look: watermark only
    env.s.add(None, "message.assistant", "Here is your daily picture")
    presentation(env.s, None, path="sandbox://workspace/imagine_media/daily.webp", label="Daily")
    env.bot.check_background(None)
    assert "daily picture" in env.tg.sent()[-1]["text"]
    assert [d["name"] for d in env.tg.docs] == ["daily.webp"] and env.tg.docs[0]["kind"] == "photo"
    env.bot.check_background(None)
    assert len(env.tg.docs) == 1 and len(env.s.downloads) == 1


def test_media_download_failure_sends_a_notice(env):
    env.s.download_error = True
    env.s.script = image_turn_script(env)
    env.bot.handle_update(msg("duck"))
    assert not env.tg.docs
    notice = [t for t in env.tg.texts() if t and "could not fetch" in t]
    assert len(notice) == 1 and "Ảnh vịt con" in notice[0]
    assert "Một chú vịt con" in env.tg.texts()[-1]          # the turn still finishes


def test_monitor_download_failure_notice_once(env):
    env.s.download_error = True
    env.bot.check_background(None)
    presentation(env.s, None, label="Report chart", path="sandbox://workspace/c.png", mime="image/png")
    env.bot.check_background(None)
    env.bot.check_background(None)
    assert sum("could not fetch" in (t or "") for t in env.tg.texts()) == 1


def test_media_kinds_and_size_limits(env):
    from muse_cli.tgapi import file_kind
    assert file_kind("a.webp", "image/webp", 500) == "photo"
    assert file_kind("a.webp", "image/webp", 11 * 1024 * 1024) == "document"
    assert file_kind("v.mp4", "video/mp4") == "video"
    assert file_kind("s.mp3", "audio/mpeg") == "audio"
    assert file_kind("x.gif", "image/gif") == "document"
    assert file_kind("r.pdf") == "document"
    env.bot.check_background(None)
    presentation(env.s, None, path="sandbox://workspace/big.mp4", label="Big video", mime="video/mp4",
                 kind="video", byte_len=80 * 1024 * 1024)
    presentation(env.s, None, path="sandbox://workspace/clip.mp4", label="Clip", mime="video/mp4",
                 kind="video", byte_len=1000)
    env.bot.check_background(None)
    assert any("too big for Telegram" in (t or "") and "Big video" in t for t in env.tg.texts())
    assert [(d["name"], d["kind"]) for d in env.tg.docs] == [("clip.mp4", "video")]


def test_rejected_photo_falls_back_to_document(env):
    def send_file(chat_id, filename, data, caption=None, kind="document", **params):
        if kind == "photo":
            raise TelegramError("sendPhoto", 400, "Bad Request: IMAGE_PROCESS_FAILED")
        env.tg.docs.append({"name": filename, "kind": kind, "caption": caption})
        return {"message_id": 1}
    env.tg.send_file = send_file
    env.s.script = image_turn_script(env)
    env.bot.handle_update(msg("duck"))
    assert [(d["name"].endswith(".webp"), d["kind"]) for d in env.tg.docs] == [(True, "document")]


def test_multipart_uses_real_content_type():
    body, _ = tgapi.multipart({"chat_id": 1}, "photo", "duck.webp", b"RIFF")
    assert b"Content-Type: image/webp" in body


# -- live status like the Muse app; no "Muse finished" ping -----------------------------------------
def _agent_status(text, emoji=None, code="working"):
    p = {"activity_code": code, "activity_text": text, "agent_id": "r", "is_thread": False,
         "message_id": "x"}
    if emoji:
        p["activity_emoji"] = emoji
    return json.dumps({"event": "agent.status", "payload": p})


def test_app_status_with_emoji_is_shown_live(env):
    """Real agent.status from the app: activity_emoji 🦆 + activity_text."""
    env.s.script = lambda sid: [
        ts("completed"), ts("running"), _agent_status("is working"),
        _agent_status("Updating plan", "🦆"),
        json.dumps({"event": "delta.tool_start", "payload": {"tool_name": "plan_update"}}),
        _agent_status("Creating image", "🎨"),
        lambda: env.s.add(None, "message.assistant", "pong"), ts("completed")]
    env.bot.handle_update(msg("go"))
    texts = env.tg.texts()
    assert any(t.startswith("⏳ Muse is working · ") for t in texts)
    assert any(t.startswith("🦆 Updating plan · ") for t in texts)
    assert any(t.startswith("🎨 Creating image · ") for t in texts)
    # the app's status wins over our derived tool label
    assert not any(t.startswith("🔧") for t in texts)
    assert env.tg.sent("editMessageText")[-1]["text"] == "pong"


def test_tool_label_is_the_fallback_without_app_status(env):
    env.s.script = lambda sid: [
        ts("completed"), ts("running"),
        json.dumps({"event": "delta.tool_start", "payload": {"tool_name": "web_search"}}),
        lambda: env.s.add(None, "message.assistant", "done"), ts("completed")]
    env.bot.handle_update(msg("go"))
    assert any((t or "").startswith("🔧 web_search") for t in env.tg.texts())


def test_app_status_helper():
    assert tgbot.app_status({"activity_code": "working", "activity_emoji": "🦆",
                             "activity_text": "Planning ducks"}) == "🦆 Planning ducks"
    assert tgbot.app_status({"activity_code": "responding", "activity_text": "is responding"}) \
        == "⏳ Muse is responding"
    assert tgbot.app_status({"activity_code": "online", "activity_text": "online"}) == "⏳ Muse is wrapping up…"
    assert tgbot.app_status({"activity_code": "working"}) is None


def test_no_muse_finished_ping_after_long_turn(env):
    clock = [1000.0]
    env.bot.clock = lambda: clock[0]

    def script(sid):
        def slow():
            clock[0] += 600                    # a 10-minute turn
            env.s.add(sid, "message.assistant", "all done")
        return [ts("completed"), ts("running"), slow, ts("completed")]
    env.s.script = script
    env.bot.handle_update(msg("long job"))
    assert not any("Muse finished" in (t or "") for t in env.tg.texts())
    assert env.tg.sent("editMessageText")[-1]["text"] == "all done"
    assert len(env.tg.sent()) == 1                 # only the bubble that became the reply


def _last_text_of(tg, mid):
    return tg.messages[mid].get("text")


def test_no_stale_status_bubble(env):
    env.s.script = lambda sid: [ts("completed"), ts("running"), _agent_status("Thinking", "🤔"),
                                lambda: env.s.add(None, "message.assistant", "answer"), ts("completed")]
    env.bot.handle_update(msg("q"))
    assert env.tg.sent()[0]["text"] == "⏳ Sending to Muse…"
    assert len(env.tg.messages) == 1               # one bubble, no extra status/ping messages
    (mid,) = env.tg.messages
    assert _last_text_of(env.tg, mid) == "answer"


def test_image_first_turn_deletes_the_status_bubble(env):
    def script(sid):
        def finish():
            presentation(env.s, sid)
        return [ts("completed"), ts("running"), _agent_status("Creating image", "🎨"), finish,
                ts("completed")]
    env.s.script = script
    env.bot.handle_update(msg("duck"))
    deleted = [p for m, p in env.tg.calls if m == "deleteMessage"]
    bubble_id = min(env.tg.messages)          # the "⏳ Sending to Muse…" bubble
    assert len(deleted) == 1 and deleted[0]["message_id"] == bubble_id
    assert [d["kind"] for d in env.tg.docs] == ["photo"]


def test_streaming_edit_cannot_land_after_the_final_reply(env):
    """A ticker edit racing the final edit must not overwrite the reply with a ⏳ status."""
    import threading
    t = tgbot.Turn(env.bot, OWNER, None, "main")
    t.msg_id = 5
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow_edit(chat_id, mid, text):
        calls.append(text)
        entered.set()
        release.wait(2)
    env.bot.edit_plain = slow_edit
    t.dirty = True
    th = threading.Thread(target=t.maybe_edit, kwargs={"force": True})
    th.start()
    entered.wait(2)
    done = []
    fin = threading.Thread(target=lambda: (t.finish(), done.append(1)))
    fin.start()
    fin.join(0.2)
    assert not done                       # finish() waits for the in-flight edit
    release.set()
    th.join(2)
    fin.join(2)
    assert done and t.finished
    t.dirty = True
    t.maybe_edit(force=True)              # nothing after finish
    assert len(calls) == 1


def test_replayed_online_before_the_turn_is_not_shown(env):
    online = json.dumps({"event": "agent.status", "payload": {"activity_code": "online",
                                                              "activity_text": "online"}})
    env.s.script = lambda sid: [online, ts("completed"), ts("running"), _agent_status("Planning ducks", "🦆"),
                                lambda: env.s.add(None, "message.assistant", "ok"), ts("completed")]
    env.bot.handle_update(msg("go"))
    statuses = [t for t in env.tg.texts() if " · " in (t or "")]
    assert statuses and not any("wrapping up" in t for t in statuses[:1])
    assert any(t.startswith("🦆 Planning ducks") for t in statuses)


# -- latency: media sent live mid-turn, shared warm connection ---------------------------------
def live_presentation(path=DUCK, label="Ảnh vịt con"):
    """The live chat.subscribe record (its seq is the stream's own counter)."""
    e = presentation(Server(), None, path=path, label=label)
    return json.dumps({"event": "delta.presentation", "payload": e["payload"], "seq": 3, "type": "event"})


def text_append(text, mseq=50):
    return json.dumps({"event": "delta.text_append", "payload": {"message_seq": mseq, "text": text}})


def live_image_script(env, seen, before="Xong rồi, ảnh vịt con đây.", after="Một chú vịt con."):
    def script(sid):
        def history():
            env.s.add(sid, "message.assistant", before)
            presentation(env.s, sid)
            env.s.add(sid, "message.assistant", after)

        def check():
            seen.append(len([d for d in env.tg.docs if d["kind"] == "photo"]))
        return [ts("running"), text_append(before, 50), live_presentation(), check,
                text_append(after, 52), history, ts("completed")]
    return script


def test_live_image_is_sent_during_the_turn_in_order_once(env):
    seen, order = [], []
    orig_send_file, orig_call = env.tg.send_file, env.tg.call

    def send_file(*a, **kw):
        order.append("photo")
        return orig_send_file(*a, **kw)

    def call(method, **p):
        t = p.get("text") or ""
        if method in ("sendMessage", "editMessageText") and "⏳" not in t and " · " not in t:
            order.append((method, t))
        return orig_call(method, **p)
    env.tg.send_file, env.tg.call = send_file, call
    env.s.script = live_image_script(env, seen)
    env.bot.handle_update(msg("duck"))
    assert seen == [1]                       # the photo was out before the turn ended
    assert order == [("editMessageText", "Xong rồi, ảnh vịt con đây."), "photo",
                     ("editMessageText", "Một chú vịt con.")]
    assert [x for x in order if x == "photo"] == ["photo"]
    final = [x for x in order if x != "photo"][-1]
    assert final[1] == "Một chú vịt con."
    assert env.s.downloads == [DUCK]         # downloaded once, full file
    assert env.tg.docs[0]["data"] == b"FILE:" + DUCK.encode()
    # the monitor and a later look at history do not send it again
    env.bot.check_background(env.bot.chat_state(OWNER)["session_id"])
    assert len(env.tg.docs) == 1 and env.s.downloads == [DUCK]


def test_live_image_first_drops_status_bubble_and_text_follows(env):
    order = []
    orig_send_file, orig_call = env.tg.send_file, env.tg.call

    def send_file(*a, **kw):
        order.append("photo")
        return orig_send_file(*a, **kw)

    def call(method, **p):
        if method == "deleteMessage":
            order.append("delete")
        return orig_call(method, **p)
    env.tg.send_file, env.tg.call = send_file, call

    def script(sid):
        def history():
            presentation(env.s, sid)
            env.s.add(sid, "message.assistant", "Đây là ảnh.")
        return [ts("running"), live_presentation(), text_append("Đây là ảnh.", 51), history,
                ts("completed")]
    env.s.script = script
    env.bot.handle_update(msg("duck"))
    assert order == ["delete", "photo"]          # the status-only bubble goes, then the photo
    assert env.tg.texts()[-1] == "Đây là ảnh."   # the text after it in a bubble below the photo


def test_live_image_threaded_goes_out_before_turn_end(home):
    import time as _t
    server, tg = Server(), FakeTelegram()
    seen = []
    env = types.SimpleNamespace(s=server, tg=tg)

    def script(sid):
        def wait_photo():           # Muse is still working: give the media worker time
            end = _t.time() + 3
            while _t.time() < end and not tg.docs:
                _t.sleep(0.01)
            seen.append(len(tg.docs))
        s = live_image_script(env, [])(sid)
        return s[:3] + [wait_photo] + s[4:]
    server.script = script
    bot = tgbot.Bot(tg, muse=tgbot.Muse(connector=server.connect))
    bot.edit_interval = 0
    bot.ensure_monitors = lambda restart=False: None
    bot.handle_update(msg("duck"))
    end = _t.time() + 10
    while _t.time() < end and (bot.turns or bot.workers or bot.queues[OWNER]):
        _t.sleep(0.02)
    assert seen == [1]
    assert len(tg.docs) == 1 and server.downloads == [DUCK]
    assert tg.texts()[-1] == "Một chú vịt con."


class SharedGW:
    """A gateway that can be shared (lease/alive), like gateway.Gateway."""
    opened = []

    def __init__(self, server):
        self.inner = FakeGateway(server)
        self.dead = None
        self.closed = False
        self.last_used = 0
        self.pings = 0
        SharedGW.opened.append(self)

    @property
    def alive(self):
        return not self.closed and self.dead is None

    def lease(self):
        from muse_cli.gateway import Lease
        return Lease(self)

    def ping(self):
        self.pings += 1
        if self.dead:
            raise ConnectionError("gone")

    def call_json(self, *a, **kw):
        if self.dead:
            raise ConnectionError("connection lost")
        return self.inner.call_json(*a, **kw)

    def download_file(self, ref, timeout=300):
        if self.dead:
            raise ConnectionError("connection lost")
        return self.inner.download_file(ref)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def close(self):
        self.closed = True


def test_muse_reuses_one_warm_connection(home):
    server = Server()
    SharedGW.opened = []
    muse = tgbot.Muse(connector=lambda cfg: SharedGW(server))
    for _ in range(5):
        muse.history(None)
    with muse.gw() as g:
        g.call_json("identity")
    muse.download({"path": DUCK})
    assert muse.connects == 1 and len(SharedGW.opened) == 1
    assert not SharedGW.opened[0].closed          # a lease's close() keeps it up


def test_muse_reconnects_after_a_drop(home):
    server = Server()
    SharedGW.opened = []
    muse = tgbot.Muse(connector=lambda cfg: SharedGW(server))
    first = muse.history(None)
    SharedGW.opened[0].dead = ConnectionError("reset by peer")
    assert muse.history(None) == first            # transparently on a new connection
    assert muse.connects == 2 and SharedGW.opened[0].closed and SharedGW.opened[1].alive


def test_muse_retries_once_when_the_connection_dies_mid_call(home):
    server = Server()
    SharedGW.opened = []
    muse = tgbot.Muse(connector=lambda cfg: SharedGW(server))
    muse.warm()
    g = SharedGW.opened[0]
    orig = g.call_json
    calls = []

    def flaky(*a, **kw):          # alive until the call fails, as when the reader sees the drop
        calls.append(1)
        g.dead = ConnectionError("eof")
        raise ConnectionError("eof")
    g.call_json = flaky
    assert muse.call("identity") == {"name": "Muse"}
    assert calls == [1] and muse.connects == 2
    g.call_json = orig


def test_heartbeat_pings_idle_connection_and_reconnects(home):
    import time as _t
    server = Server()
    SharedGW.opened = []
    muse = tgbot.Muse(connector=lambda cfg: SharedGW(server))
    muse.warm()
    muse.start_heartbeat(interval=0.05)
    end = _t.time() + 3
    while _t.time() < end and not SharedGW.opened[0].pings:
        _t.sleep(0.01)
    assert SharedGW.opened[0].pings >= 1
    SharedGW.opened[0].dead = ConnectionError("gone")
    end = _t.time() + 8
    while _t.time() < end and len(SharedGW.opened) < 2:
        _t.sleep(0.02)
    muse.stop_heartbeat()
    assert len(SharedGW.opened) == 2 and SharedGW.opened[1].alive


def test_profile_reload_without_change_keeps_connection(home):
    server = Server()
    SharedGW.opened = []
    muse = tgbot.Muse(connector=lambda cfg: SharedGW(server))
    muse.reload()
    muse.warm()
    muse.reload()                                 # e.g. /autoapprove: same cookies and VM
    assert SharedGW.opened[0].alive
    muse.reset()                                  # /login, /profile
    assert SharedGW.opened[0].closed


def test_telegram_calls_run_on_warm_pool_threads(monkeypatch):
    import threading as _th
    from muse_cli.tgapi import TelegramAPI
    api = TelegramAPI(TOKEN)
    names = []

    def post_now(method, timeout, **kw):
        names.append((method, _th.current_thread().name))
        return {"ok": True}
    monkeypatch.setattr(api, "_post_now", post_now)
    ts_ = [_th.Thread(target=api._post, args=("sendMessage", 5)) for _ in range(3)]
    for t in ts_:
        t.start()
    for t in ts_:
        t.join()
    api._post("getUpdates", 5)
    assert all(n.startswith("tg-http") for m, n in names if m == "sendMessage")
    assert [n for m, n in names if m == "getUpdates"] == [_th.current_thread().name]


def test_live_image_last_removes_the_empty_trailing_bubble(env):
    def script(sid):
        def history():
            env.s.add(sid, "message.assistant", "Đây là ảnh.")
            presentation(env.s, sid)
        return [ts("running"), text_append("Đây là ảnh.", 50), live_presentation(), history,
                ts("completed")]
    env.s.script = script
    env.bot.handle_update(msg("duck"))
    assert len(env.tg.docs) == 1
    assert len(env.tg.sent("deleteMessage")) == 1     # the status bubble opened after the photo
    finals = [p["text"] for p in env.tg.sent("editMessageText") if " · " not in p["text"]]
    assert finals == ["Đây là ảnh."]
