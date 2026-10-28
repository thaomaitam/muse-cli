import json
import unittest

from muse_cli.wait import (EXIT_FAILED, EXIT_HUMAN, EXIT_OK, EXIT_TIMEOUT, ONCE, Waiter,
                           choose_auto_decision, clean_text, describe_approval, exit_code,
                           offers_once, replies_after, user_ack)

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


class ChooseAutoDecisionTest(unittest.TestCase):
    def described(self, options, kind="network_access"):
        a = {"approval_id": "a1",
             "display": {"summary_title": "Share info",
                         "permission_question": {"kind": kind, "scope": "example.com"}},
             "decision_options": None if options is None else [{"kind": o} for o in options]}
        return describe_approval(a)

    def test_forever_preferred_when_offered(self):
        d = self.described(["allow_once", "allow_always", "deny"])
        self.assertEqual(choose_auto_decision(d, True, True), "allow_always")
        self.assertEqual(choose_auto_decision(d, True, False), "allow_always")
        self.assertTrue(offers_once(d))

    def test_once_fallback_only_when_toggle_on(self):
        d = self.described(["allow_once", "deny"])
        self.assertEqual(choose_auto_decision(d, True, True), ONCE)
        self.assertIsNone(choose_auto_decision(d, True, False))
        self.assertIsNone(choose_auto_decision(d, False, True))

    def test_no_kind_restriction(self):
        d = self.described(["allow_once", "deny"], kind="file_access")
        self.assertEqual(choose_auto_decision(d, True, True), ONCE)

    def test_deny_only_and_session_only_never_decide(self):
        self.assertIsNone(choose_auto_decision(self.described(["deny"]), True, True))
        self.assertIsNone(choose_auto_decision(self.described(["allow_session", "deny"]), True, True))
        self.assertIsNone(choose_auto_decision(self.described(["allow_today", "deny"]), True, True))

    def test_no_options_keeps_forever_assumption(self):
        d = self.described(None)
        self.assertEqual(choose_auto_decision(d, True, True), "allow_always")
        self.assertEqual(choose_auto_decision(d, True, False), "allow_always")
        self.assertFalse(offers_once(d))

    def test_empty_options_keeps_forever_assumption(self):
        self.assertEqual(choose_auto_decision(self.described([]), True, True), "allow_always")


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


T3127 = ("Preflight: shape=bounded; policy questions=none; authority=Boss duyệt dọn các mục heal FAIL "
         "của hệ thống cũ (tin này).\n\nTôi phải dừng lại trước khi xoá hay sửa gì: file tôi vừa đọc để "
         "lập kế hoạch dọn (heal.sh) không qua kiểm tra an toàn của hệ thống, nên theo cảnh báo đó tôi "
         "không được tiếp tục các thao tác xoá/sửa dựa trên nó.\n\nHiện tại tôi chưa xoá hay sửa gì, và "
         "chưa chạy lại heal check hay heal status.\n\nBoss muốn tôi tiếp tục dọn theo lệnh đã duyệt bất "
         "chấp cảnh báo này, hay Boss muốn tự xem file trước rồi mới quyết? Tôi chờ Boss trả lời rồi mới làm tiếp.")


class NeedsInputTest(unittest.TestCase):
    def test_seq_3127_is_needs_input(self):
        from muse_cli.wait import outcome_of, question_of
        r = [{"seq": 3127, "text": T3127}]
        self.assertEqual(outcome_of("done", r), "needs_input")
        self.assertEqual(exit_code("done", r), EXIT_HUMAN)
        self.assertIn("Boss muốn", question_of(T3127))

    def test_plain_ok_is_done(self):
        from muse_cli.wait import outcome_of
        self.assertEqual(outcome_of("done", [{"seq": 1, "text": "OK"}]), "done")
        self.assertEqual(outcome_of("done", [{"seq": 1, "text": "Đã xanh. heal result=OK failed=0"}]), "done")
        self.assertEqual(exit_code("done", [{"seq": 1, "text": "OK"}]), EXIT_OK)

    def test_other_triggers(self):
        from muse_cli.wait import needs_input_reason, outcome_of
        self.assertTrue(needs_input_reason("Done. Should I also push it?"))
        self.assertTrue(needs_input_reason("Preflight: policy questions=delete files\nWorking."))
        self.assertIsNone(needs_input_reason("Preflight: policy questions=none\nAll done."))
        self.assertTrue(needs_input_reason("I stopped because the safety check flagged heal.sh."))
        self.assertEqual(outcome_of("approval"), "needs_approval")
        self.assertEqual(outcome_of("timeout"), "timeout")
