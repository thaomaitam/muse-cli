import unittest

from muse_cli.cli import summarize_invite


class InviteTest(unittest.TestCase):
    def test_with_code(self):
        s = summarize_invite({"invite_state": "ACTIVE", "code": "R4O8PL", "max_uses": 30,
                              "use_count": 12, "uses_remaining": None,
                              "reward": {"title": "You earned 12B tokens",
                                         "body": "12 friends redeemed"}})
        self.assertEqual(s["code"], "R4O8PL")
        self.assertEqual(s["uses_remaining"], 18)
        self.assertEqual(s["reward"]["body"], "12 friends redeemed")

    def test_server_remaining_wins(self):
        s = summarize_invite({"code": "ABCDEF", "max_uses": 30, "use_count": 12,
                              "uses_remaining": 5})
        self.assertEqual(s["uses_remaining"], 5)

    def test_no_code_yet(self):
        s = summarize_invite({"invite_state": "ELIGIBLE", "code": None, "reward": None})
        self.assertIsNone(s["code"])
        self.assertIsNone(s["uses_remaining"])
        self.assertIsNone(s["reward"])


if __name__ == "__main__":
    unittest.main()
