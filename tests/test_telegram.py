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
        for item in script:
            if callable(item):
                item()
                continue
            if on_record(item.encode() if isinstance(item, str) else item):
                break
            if should_stop and should_stop():
                break
        return []

    def download_file(self, ref, timeout=300):
        return b"FILE:" + str(ref.get("path") if isinstance(ref, dict) else ref).encode()

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
    assert list(env.bot.queues[OWNER]) == [("text", "second")]


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
