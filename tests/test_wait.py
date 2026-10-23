import json
import unittest

from muse_cli.wait import (EXIT_FAILED, EXIT_HUMAN, EXIT_OK, EXIT_TIMEOUT, Waiter,
                           clean_text, describe_approval, exit_code, replies_after, user_ack)

ROOT = "20acb600"


def ts(status, **kw):
    p = {"status": status, "agent_id": ROOT, "root_agent_id": ROOT, "is_thread": False,
         "session_id": "main"}
    p.update(kw)
    return {"event": "task.status", "payload": p, "seq": 1, "type": "event"}


APPROVAL = {"approval_id": "a1", "request_expires_at_ms": 5,
            "display": {"summary_title": "Share info with github.com",
                        "short_explanation": "clone repo",
                        "permission_question": {"kind": "network_access", "scope": "github.com",
                                                "text": "Allow {assistant} to share information with github.com?"},
                        "detail_rows": [{"label": "Host", "value": "github.com"}]},
            "payload": {"type": "network"}}


class WaiterTest(unittest.TestCase):
    def test_done_and_failed(self):
        w = Waiter()
        self.assertIsNone(w.feed(ts("running")))
        self.assertEqual(w.feed(json.dumps(ts("completed"))), ("done", "completed"))
        self.assertEqual(Waiter().feed(ts("failed")), ("failed", "failed"))

    def test_require_running_skips_stale_snapshot(self):
        w = Waiter(require_running=True)
        self.assertIsNone(w.feed(ts("completed")))
        w.feed(ts("running"))
        self.assertEqual(w.feed(ts("idle"))[0], "done")

    def test_ignores_subagents_and_other_threads(self):
        w = Waiter()
        self.assertIsNone(w.feed(ts("completed", agent_id="sub")))
        self.assertIsNone(w.feed(ts("completed", is_thread=True)))
        t = Waiter(thread="T1")
        self.assertIsNone(t.feed(ts("completed", session_id="other", is_thread=True)))
        self.assertEqual(t.feed(ts("completed", session_id="T1", is_thread=True))[0], "done")

    def test_approval(self):
        w = Waiter()
        self.assertIsNone(w.feed({"event": "approvals.snapshot", "payload": {"pending_approvals": []}}))
        kind, det = w.feed({"event": "approvals.snapshot", "payload": {"pending_approvals": [APPROVAL]}})
        self.assertEqual(kind, "approval")
        self.assertEqual(det[0]["scope"], "github.com")
        self.assertIn("Muse", det[0]["question"])

    def test_garbage_line(self):
        self.assertIsNone(Waiter().feed(b"not json"))


class HistoryTest(unittest.TestCase):
    def ev(self, seq, name, text, widgets=None):
        p = {"display_text": text, "message_id": f"m{seq}"}
        if widgets:
            p["widgets"] = widgets
        return {"seq": seq, "event_name": name, "payload": p}

    def test_replies_and_widgets(self):
        w = [{"widget_id": "w1", "kind": "option", "display_text": "A / B",
              "data": {"options": ["A", "B"]}, "state": {"version": 0}}]
        evs = [self.ev(5, "message.assistant", "old"), self.ev(6, "message.user", "hi"),
               self.ev(8, "message.assistant", "pick\n\n[[hatch_widget:w1]]", w),
               self.ev(7, "message.assistant", "first")]
        r = replies_after(evs, 6)
        self.assertEqual([x["seq"] for x in r], [7, 8])
        self.assertEqual(r[1]["text"], "pick")
        self.assertEqual(r[1]["widgets"][0]["options"], ["A", "B"])
        self.assertEqual(exit_code("done", r), EXIT_HUMAN)
        self.assertEqual(exit_code("done", r[:1]), EXIT_OK)
        self.assertEqual(user_ack(evs, 5, "hi"), 6)
        self.assertIsNone(user_ack(evs, 6, "hi"))

    def test_exit_codes(self):
        self.assertEqual(exit_code("failed"), EXIT_FAILED)
        self.assertEqual(exit_code("approval"), EXIT_HUMAN)
        self.assertEqual(exit_code("timeout"), EXIT_TIMEOUT)
        self.assertEqual(clean_text("x [[hatch_widget:a]]"), "x")
        self.assertEqual(describe_approval({})["approval_id"], None)


if __name__ == "__main__":
    unittest.main()
