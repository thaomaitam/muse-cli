import unittest

from muse_cli.cli import _human_tokens, summarize_usage
from muse_cli.gateway import parse_rsc_result

RSC = ('0:{"a":"$@1","f":"","q":"","i":false}\n'
       '1:{"success":true,"subscription":{"tier":{"name":"Muse Free","tierCode":"HATCH_FREE",'
       '"isPaid":false},"usage":{"state":"METERED","percentUsed":87,"resetsAt":1792821457,'
       '"quotaStatus":"SUFFICIENT"},"usageRowLabel":"Free plan","topupBalance":12000000000,'
       '"topupTotal":12500000000,"topupRowLabel":"Additional tokens",'
       '"entryPointCtaLabel":"Upgrade"}}\n')


class UsageTest(unittest.TestCase):
    def test_parse_rsc(self):
        res = parse_rsc_result(RSC)
        self.assertTrue(res["success"])
        self.assertEqual(res["subscription"]["usage"]["percentUsed"], 87)

    def test_summary(self):
        u = summarize_usage(parse_rsc_result(RSC)["subscription"])
        self.assertEqual(u["used_percent"], 87)
        self.assertEqual(u["bonus_tokens"]["used_percent"], 4)
        self.assertEqual(u["bonus_tokens"]["remaining_tokens"], 12000000000)
        self.assertTrue(u["resets_at"].startswith("2026-10-2"))

    def test_no_topup(self):
        u = summarize_usage({"usage": {"state": "UNLIMITED"}, "topupTotal": 0})
        self.assertIsNone(u["bonus_tokens"])
        self.assertIsNone(u["used_percent"])

    def test_human_tokens(self):
        self.assertEqual(_human_tokens(12_000_000_000), "12B")
        self.assertEqual(_human_tokens(1_500_000), "1.5M")


if __name__ == "__main__":
    unittest.main()
