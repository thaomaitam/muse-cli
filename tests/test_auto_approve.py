import argparse
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from muse_cli import cli
from muse_cli.gateway import join_chunks
from muse_cli.wait import (EXIT_HUMAN, EXIT_OK, FOREVER, ONCE, Waiter, choose_auto_decision,
                           describe_approval, offers_forever)

ROOT = "r1"
OPTIONS = [{"kind": "allow_once", "label_text": "Allow once"},
           {"kind": "allow_always", "label_text": "Always allow this site"},
           {"kind": "deny", "label_text": "Deny"}]


def appr(aid, host="uploads.github.com", options=OPTIONS):
    a = {"approval_id": aid,
         "display": {"summary_title": f"Share info with {host}",
                     "permission_question": {"kind": "network_access", "scope": host,
                                             "text": "Allow {assistant}?"}}}
    if options is not None:
        a["decision_options"] = options
    return a


def snap(*items):
    return json.dumps({"event": "approvals.snapshot", "payload": {"pending_approvals": list(items)}})


def ts(status):
    return json.dumps({"event": "task.status", "payload": {
        "status": status, "agent_id": ROOT, "root_agent_id": ROOT, "is_thread": False}})


def reply(seq, text):
    return {"seq": seq, "event_name": "message.assistant",
            "payload": {"display_text": text, "message_id": f"m{seq}"}}


class FakeGateway:
    """Plays `lines` into chat.subscribe and records approval decisions."""
    def __init__(self, world):
        self.w = world

    def call_json(self, method, path_params=None, body=None, **_kw):
        if method == "chat.history":
            return {"chat_events": self.w["history"]}
        if method == "egress.approval.decide":
            if path_params["approval_id"] in self.w.get("fail", ()):
                raise cli.GatewayError(500, "boom")
            self.w["decided"].append((path_params["approval_id"], body["decision"]))
            return {"ok": True}
        if method == "egress.approvals":
            return {"pending_approvals": self.w["pending"]}
        raise AssertionError(method)

    def subscribe_raw(self, method, on_record=None, **_kw):
        for line in self.w["lines"]:
            if on_record(line):
                return

    def close(self):
        pass


def world(lines, history=None, **kw):
    return {"lines": lines, "history": history or [], "decided": [], "pending": [], **kw}


class WaiterApproverTest(unittest.TestCase):
    def test_repeated_approvals_keep_waiting(self):
        seen = []
        w = Waiter(approver=lambda items: seen.extend(i["approval_id"] for i in items)
                   or [i["approval_id"] for i in items])
        self.assertIsNone(w.feed(ts("running")))
        self.assertIsNone(w.feed(snap(appr("a1"))))
        self.assertIsNone(w.feed(snap(appr("a1"))))  # same id again: not re-offered
        self.assertIsNone(w.feed(snap(appr("a1"), appr("a2"))))
        self.assertEqual(w.feed(ts("completed")), ("done", "completed"))
        self.assertEqual(seen, ["a1", "a2"])

    def test_unhandled_approval_still_stops(self):
        w = Waiter(approver=lambda items: [])
        kind, det = w.feed(snap(appr("a1")))
        self.assertEqual(kind, "approval")
        self.assertEqual(det[0]["approval_id"], "a1")

    def test_without_approver_unchanged(self):
        self.assertEqual(Waiter().feed(snap(appr("a1")))[0], "approval")


class ForeverTest(unittest.TestCase):
    def test_always_is_muse_forever_option(self):
        d = describe_approval(appr("a1"))
        self.assertEqual(d["options"], ["allow_once", "allow_always", "deny"])
        self.assertEqual(FOREVER, "allow_always")
        self.assertTrue(offers_forever(d))
        args = argparse.Namespace(once=False, session=False, today=False, always=True)
        self.assertEqual(cli._approve_decision(args), FOREVER)

    def test_no_forever_option(self):
        d = describe_approval(appr("a1", options=OPTIONS[:1]))
        self.assertFalse(offers_forever(d))
        self.assertTrue(offers_forever(describe_approval(appr("a2", options=None))))


class AllowOnceSettingTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("MUSE_AUTO_ALLOW_ONCE", None)

    def tearDown(self):
        self.env.stop()

    def test_default_off(self):
        self.assertFalse(cli.auto_allow_once_setting({})[0])
        self.assertFalse(cli.auto_allow_once_setting({"auto_allow_once": False})[0])

    def test_config_forms(self):
        self.assertTrue(cli.auto_allow_once_setting({"auto_allow_once": True})[0])
        self.assertTrue(cli.auto_allow_once_setting({"auto_allow_once": "on"})[0])
        self.assertFalse(cli.auto_allow_once_setting({"auto_allow_once": "off"})[0])

    def test_config_env_flag_precedence(self):
        cfg = {"auto_allow_once": True}
        self.assertEqual(cli.auto_allow_once_setting(cfg)[0], True)
        os.environ["MUSE_AUTO_ALLOW_ONCE"] = "off"
        self.assertEqual(cli.auto_allow_once_setting(cfg),
                         (False, "env MUSE_AUTO_ALLOW_ONCE"))
        self.assertEqual(cli.auto_allow_once_setting(cfg, True), (True, "flag"))
        os.environ["MUSE_AUTO_ALLOW_ONCE"] = "on"
        self.assertTrue(cli.auto_allow_once_setting({})[0])
        self.assertEqual(cli.auto_allow_once_setting({}, False), (False, "flag"))
        os.environ["MUSE_AUTO_ALLOW_ONCE"] = "maybe"
        with self.assertRaises(ValueError):
            cli.auto_allow_once_setting({})

    def test_once_command_persists_and_status_shows_both(self):
        d = tempfile.mkdtemp()
        f = os.path.join(d, "config.json")
        with open(f, "w") as fh:
            json.dump({"node_id": "n", "cookies_file": "c"}, fh)
        with mock.patch.object(cli, "CONFIG_DIR", d), mock.patch.object(cli, "CONFIG_FILE", f):
            for state, once_state, want in (("once", "on", True), ("status", None, True),
                                            ("once", "off", False)):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    cli.cmd_auto_approve(argparse.Namespace(state=state, once_state=once_state))
                got = json.loads(buf.getvalue())
                self.assertEqual(got["auto_allow_once"], want, (state, once_state))
                self.assertIn("auto_approve", got)
            with open(f) as fh:
                saved = json.load(fh)
        self.assertEqual(saved["auto_allow_once"], False)
        self.assertEqual(saved["node_id"], "n")

    def test_allow_once_flags_parse(self):
        p = argparse.ArgumentParser()
        cli._auto_allow_once_flags(p)
        self.assertIsNone(p.parse_args([]).auto_allow_once)
        self.assertTrue(p.parse_args(["--auto-allow-once"]).auto_allow_once)
        self.assertFalse(p.parse_args(["--no-auto-allow-once"]).auto_allow_once)


class SettingTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("MUSE_AUTO_APPROVE", None)

    def tearDown(self):
        self.env.stop()

    def test_default_off(self):
        self.assertFalse(cli.auto_approve_setting({})[0])

    def test_config_env_flag_precedence(self):
        cfg = {"auto_approve": "always"}
        self.assertEqual(cli.auto_approve_setting(cfg)[0], True)
        os.environ["MUSE_AUTO_APPROVE"] = "off"
        self.assertEqual(cli.auto_approve_setting(cfg), (False, "env MUSE_AUTO_APPROVE"))
        self.assertEqual(cli.auto_approve_setting(cfg, True), (True, "flag"))
        os.environ["MUSE_AUTO_APPROVE"] = "always"
        self.assertTrue(cli.auto_approve_setting({})[0])
        self.assertEqual(cli.auto_approve_setting({}, False), (False, "flag"))
        os.environ["MUSE_AUTO_APPROVE"] = "maybe"
        with self.assertRaises(ValueError):
            cli.auto_approve_setting({})

    def test_command_persists(self):
        d = tempfile.mkdtemp()
        f = os.path.join(d, "config.json")
        with open(f, "w") as fh:
            json.dump({"node_id": "n", "cookies_file": "c"}, fh)
        with mock.patch.object(cli, "CONFIG_DIR", d), mock.patch.object(cli, "CONFIG_FILE", f):
            for state, want in (("status", "off"), ("on", "always"), ("status", "always"),
                                ("off", "off")):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    cli.cmd_auto_approve(argparse.Namespace(state=state))
                self.assertEqual(json.loads(buf.getvalue())["auto_approve"], want, state)
            with open(f) as fh:
                saved = json.load(fh)
        self.assertEqual(saved["auto_approve"], "off")
        self.assertEqual(saved["node_id"], "n")  # other settings kept


class RunWaitTest(unittest.TestCase):
    def run_wait(self, w, auto, allow_once=None):
        err = io.StringIO()
        with mock.patch.object(cli, "connect", lambda cfg: FakeGateway(w)), redirect_stderr(err):
            code, res = cli.run_wait({}, after_seq=10, timeout=5, auto_approve=auto,
                                     auto_allow_once=allow_once)
        return code, res, err.getvalue()

    def test_auto_approves_forever_and_finishes(self):
        w = world([ts("running"), snap(appr("a1")), snap(appr("a1")),
                   snap(appr("a2", host="api.github.com")), ts("completed")],
                  history=[reply(11, "Released v1.3.10.")])
        code, res, err = self.run_wait(w, True)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(res["outcome"], "done")
        self.assertEqual(w["decided"], [("a1", "allow_always"), ("a2", "allow_always")])
        self.assertEqual([a["approval_id"] for a in res["auto_approved"]], ["a1", "a2"])
        self.assertEqual(res["auto_approved"][1]["scope"], "api.github.com")
        self.assertEqual(res["auto_approved"][0]["kind"], "network_access")
        self.assertIn("auto-approved a1 (allow_always)", err)

    def test_question_still_exits_4(self):
        w = world([ts("running"), snap(appr("a1")), ts("completed")],
                  history=[reply(11, "Do you want me to publish it too?")])
        code, res, _ = self.run_wait(w, True)
        self.assertEqual(code, EXIT_HUMAN)
        self.assertEqual(res["outcome"], "needs_input")
        self.assertEqual(len(res["auto_approved"]), 1)

    def test_off_exits_4_and_decides_nothing(self):
        w = world([ts("running"), snap(appr("a1")), ts("completed")])
        code, res, _ = self.run_wait(w, False)
        self.assertEqual((code, res["outcome"]), (EXIT_HUMAN, "needs_approval"))
        self.assertEqual(w["decided"], [])
        self.assertNotIn("auto_approved", res)

    def test_failed_decide_falls_back_to_exit_4(self):
        w = world([ts("running"), snap(appr("a1")), ts("completed")], fail={"a1"})
        code, res, _ = self.run_wait(w, True)
        self.assertEqual((code, res["outcome"]), (EXIT_HUMAN, "needs_approval"))
        self.assertEqual(res["auto_approve_errors"][0]["approval_id"], "a1")

    def test_no_forever_toggle_off_uses_no_weaker_choice(self):
        w = world([ts("running"), snap(appr("a1", options=OPTIONS[:1])), ts("completed")])
        code, res, err = self.run_wait(w, True)
        self.assertEqual(code, EXIT_HUMAN)
        self.assertEqual(w["decided"], [])
        self.assertIn("no forever option", err)

    def test_no_forever_toggle_on_decides_allow_once(self):
        w = world([ts("running"), snap(appr("a1", options=OPTIONS[:1])), ts("completed")],
                  history=[reply(11, "Released v1.3.10.")])
        code, res, err = self.run_wait(w, True, allow_once=True)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(res["outcome"], "done")
        self.assertEqual(w["decided"], [("a1", ONCE)])
        self.assertEqual(res["auto_approved"][0]["decision"], ONCE)
        self.assertEqual(res["auto_approved"][0]["kind"], "network_access")
        self.assertEqual(res["auto_approved"][0]["scope"], "uploads.github.com")
        self.assertIn("auto-approved a1 (allow_once)", err)
        self.assertNotIn("allow_always", err)

    def test_no_forever_toggle_on_via_env(self):
        w = world([ts("running"), snap(appr("a1", options=OPTIONS[:1])), ts("completed")],
                  history=[reply(11, "Released v1.3.10.")])
        with mock.patch.dict(os.environ, {"MUSE_AUTO_ALLOW_ONCE": "on"}):
            code, res, _ = self.run_wait(w, True)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(w["decided"], [("a1", ONCE)])

    def test_supplied_approver_ignores_bad_allow_once_env(self):
        # A caller-supplied approver embodies its own policy: an invalid
        # MUSE_AUTO_ALLOW_ONCE must not raise before the wait starts.
        w = world([ts("running"), snap(appr("a1")), ts("completed")],
                  history=[reply(11, "Released v1.3.10.")])
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"MUSE_AUTO_ALLOW_ONCE": "maybe"}), \
                mock.patch.object(cli, "connect", lambda cfg: FakeGateway(w)), \
                redirect_stderr(err):
            code, res = cli.run_wait(
                {}, after_seq=10, timeout=5, auto_approve=True,
                approver=lambda items: [i["approval_id"] for i in items])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(res["outcome"], "done")

    def test_toggle_on_still_prefers_forever(self):
        w = world([ts("running"), snap(appr("a1")), ts("completed")],
                  history=[reply(11, "Released v1.3.10.")])
        code, res, err = self.run_wait(w, True, allow_once=True)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(w["decided"], [("a1", "allow_always")])
        self.assertIn("auto-approved a1 (allow_always)", err)

    def test_session_or_deny_only_never_decides_even_with_toggle_on(self):
        for options in ([{"kind": "allow_session"}, {"kind": "deny"}],
                        [{"kind": "deny"}]):
            w = world([ts("running"), snap(appr("a1", options=options)), ts("completed")])
            code, res, _ = self.run_wait(w, True, allow_once=True)
            self.assertEqual(code, EXIT_HUMAN)
            self.assertEqual(w["decided"], [])

    def test_allow_once_applies_to_any_kind(self):
        a = appr("a1", options=OPTIONS[:1])
        a["display"]["permission_question"]["kind"] = "file_access"
        w = world([ts("running"), snap(a), ts("completed")], history=[reply(11, "done")])
        code, _, _ = self.run_wait(w, True, allow_once=True)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(w["decided"], [("a1", ONCE)])

    def test_skip_warning_not_repeated_within_one_approver(self):
        w = world([])
        approved, errors = [], []
        approver = cli.make_auto_approver({}, approved, errors,
                                          connector=lambda cfg: FakeGateway(w), allow_once=False)
        a = describe_approval(appr("a1", options=OPTIONS[:1]))
        err1 = io.StringIO()
        with redirect_stderr(err1):
            self.assertEqual(approver([a]), [])
        err2 = io.StringIO()
        with redirect_stderr(err2):
            self.assertEqual(approver([a]), [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(err1.getvalue().count("SKIPPED"), 1)
        self.assertEqual(err2.getvalue().count("SKIPPED"), 0)
        self.assertIn("network_access", err1.getvalue())
        self.assertIn("uploads.github.com", err1.getvalue())


class ApproveAllTest(unittest.TestCase):
    def test_approve_all_always(self):
        w = world([], pending=[appr("a1"), appr("a2"), appr("a3", options=OPTIONS[:1])])
        args = argparse.Namespace(id=None, all=True, once=False, session=False, today=False,
                                  always=True)
        buf, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "connect", lambda cfg: FakeGateway(w)), \
                mock.patch.object(cli, "load_config", lambda: {}), \
                redirect_stdout(buf), redirect_stderr(err):
            cli.cmd_decide(cli._approve_decision)(args)
        self.assertEqual(w["decided"], [("a1", "allow_always"), ("a2", "allow_always")])
        self.assertEqual(len(json.loads(buf.getvalue())["approved"]), 2)

    def test_needs_id_or_all(self):
        args = argparse.Namespace(id=None, all=False, once=False, session=False, today=False,
                                  always=True)
        with self.assertRaises(ValueError):
            cli.cmd_decide(cli._approve_decision)(args)

    def test_single_id_unchanged(self):
        w = world([])
        args = argparse.Namespace(id="a9", all=False, once=False, session=False, today=False,
                                  always=True)
        with mock.patch.object(cli, "connect", lambda cfg: FakeGateway(w)), \
                mock.patch.object(cli, "load_config", lambda: {}), redirect_stdout(io.StringIO()):
            cli.cmd_decide(cli._approve_decision)(args)
        self.assertEqual(w["decided"], [("a9", "allow_always")])


class ChunkTest(unittest.TestCase):
    def test_join_chunks(self):
        F = lambda cid, i, n, p: mock.Mock(chunk_id=cid, chunk_index=i, total_chunks=n, payload=p)
        part = {}
        self.assertEqual(join_chunks(part, F(1, 0, 1, b"x")), b"x")
        self.assertIsNone(join_chunks(part, F(2, 1, 2, b"B")))
        self.assertEqual(join_chunks(part, F(2, 0, 2, b"A")), b"AB")
        self.assertEqual(part, {})


if __name__ == "__main__":
    unittest.main()
