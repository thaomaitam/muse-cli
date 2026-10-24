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
from muse_cli.wait import EXIT_HUMAN, EXIT_OK, FOREVER, Waiter, describe_approval, offers_forever

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
    def run_wait(self, w, auto):
        err = io.StringIO()
        with mock.patch.object(cli, "connect", lambda cfg: FakeGateway(w)), redirect_stderr(err):
            code, res = cli.run_wait({}, after_seq=10, timeout=5, auto_approve=auto)
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

    def test_never_uses_weaker_choice(self):
        w = world([ts("running"), snap(appr("a1", options=OPTIONS[:1])), ts("completed")])
        code, res, err = self.run_wait(w, True)
        self.assertEqual(code, EXIT_HUMAN)
        self.assertEqual(w["decided"], [])
        self.assertIn("no forever option", err)


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
