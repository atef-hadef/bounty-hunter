"""Tests hors ligne (aucun appel réseau) : python -m unittest discover -s tests -v"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import hunter  # noqa: E402
import jobs  # noqa: E402
import startups  # noqa: E402
import status  # noqa: E402


def bot(login, body):
    return {"user": {"login": login, "type": "Bot"}, "body": body}


def human(login, body, association="NONE"):
    return {"user": {"login": login, "type": "User"}, "body": body, "author_association": association}


class TempStateMixin:
    """Redirige les fichiers d'état vers un dossier temporaire."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patches = [mock.patch.object(hunter, "QUOTA_FILE", self.tmp / "quota.json"),
                   mock.patch.object(status, "STATE_FILE", self.tmp / "status.json")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class MoneyFilters(unittest.TestCase):
    def test_no_money_detects_zero_bounties(self):
        for text in ["Bounty: $0", "reward 0 USD", "This pays no money", "credit only"]:
            self.assertTrue(hunter.NO_MONEY_RE.search(text), text)

    def test_no_money_keeps_real_amounts(self):
        for text in ["$0.50 per test", "$100 bounty", "$05", "$1,000"]:
            self.assertFalse(hunter.NO_MONEY_RE.search(text), text)

    def test_bot_trap(self):
        self.assertTrue(hunter.BOT_TRAP_RE.search("Pull requests from human engineers are not reviewed"))


class CommentAnalysis(unittest.TestCase):
    def analyze(self, comments):
        with mock.patch.object(hunter, "gh_get", return_value=comments):
            return hunter.analyze_comments(None, {"comments_url": "x"})

    def test_algora_funding_company_and_attempts(self):
        info = self.analyze([
            bot(hunter.ALGORA_BOT, "## 💎 **$250** bounty [• Onyx (YC W24)](https://algora.io/onyx)"),
            human("alice", "/attempt #12"),
            human("bob", "nice issue"),
        ])
        self.assertEqual((info["platform"], info["amount"], info["company"]), ("Algora", 250.0, "Onyx (YC W24)"))
        self.assertEqual(info["attempters"], {"alice"})
        self.assertFalse(info["claimed"])

    def test_opire_rewards_are_summed(self):
        info = self.analyze([
            bot(hunter.OPIRE_BOT, "@x created a $100.00 reward using [Opire]"),
            bot(hunter.OPIRE_BOT, "@y created a $50 reward using [Opire]"),
        ])
        self.assertEqual((info["platform"], info["amount"]), ("Opire", 150.0))

    def test_unfunded_and_claimed(self):
        info = self.analyze([bot(hunter.OPIRE_BOT, "Ups... this issue does not have any reward yet!")])
        self.assertIsNone(info["platform"])
        info = self.analyze([bot(hunter.ALGORA_BOT, "💎 $80 bounty"),
                             bot(hunter.ALGORA_BOT, "@z submitted a pull request that claims the bounty")])
        self.assertTrue(info["claimed"])


class DailyQuota(TempStateMixin, unittest.TestCase):
    def test_quota_counts_and_caps(self):
        self.assertEqual(hunter.quota_left("jobs", 3), 3)
        hunter.quota_use("jobs", 2)
        self.assertEqual(hunter.quota_left("jobs", 3), 1)
        hunter.quota_use("jobs", 5)
        self.assertEqual(hunter.quota_left("jobs", 3), 0)
        self.assertEqual(hunter.quota_left("startups", 2), 2)

    def test_new_day_resets_and_keeps_history(self):
        hunter.QUOTA_FILE.write_text(json.dumps({"date": "2000-01-01", "counts": {"jobs": 3}}))
        self.assertEqual(hunter.quota_left("jobs", 3), 3)
        self.assertEqual(hunter.sent_on("2000-01-01"), {"jobs": 3})
        hunter.quota_use("jobs", 1)
        self.assertEqual(hunter.sent_on("2000-01-01"), {"jobs": 3})


class Notifications(unittest.TestCase):
    def test_each_feed_uses_its_own_channel(self):
        env = {"DISCORD_JOBS_WEBHOOK_URL": "https://jobs.example", "DISCORD_WEBHOOK_URL": "https://bounties.example"}
        with mock.patch.dict("os.environ", env, clear=True), \
                mock.patch.object(hunter, "discord_post") as post, mock.patch.object(hunter.time, "sleep"):
            sent = hunter.send_notifications([{"url": "u"}], lambda i: {}, lambda i: "", "DISCORD_JOBS_WEBHOOK_URL")
        post.assert_called_once_with("https://jobs.example", {})
        self.assertEqual(len(sent), 1)

    def test_missing_channel_sends_nothing(self):
        with mock.patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "https://bounties.example"}, clear=True), \
                mock.patch.object(hunter, "discord_post") as post:
            sent = hunter.send_notifications([{"url": "u"}], lambda i: {}, lambda i: "", "DISCORD_STARTUPS_WEBHOOK_URL")
        post.assert_not_called()
        self.assertEqual(sent, [])


class JobFilters(unittest.TestCase):
    def test_open_worldwide_python_job_matches(self):
        text = "Acme | Python Engineer | REMOTE (Worldwide) | Full-time\nWe use Django. jobs@acme.io"
        self.assertTrue(jobs.matches(text))
        self.assertEqual(jobs.how_to_apply(text), "Email : jobs@acme.io")

    def test_us_only_and_onsite_are_rejected(self):
        self.assertFalse(jobs.matches("Acme | Python Engineer | REMOTE (US only) | Full-time\nDjango"))
        self.assertFalse(jobs.matches("Acme | Python Engineer | REMOTE\nMust be a US citizen. Django"))
        self.assertFalse(jobs.matches("Location: Tunis\nRemote: yes, python"))

    def test_leadership_roles_are_rejected(self):
        self.assertFalse(jobs.matches("Acme | Principal Python Engineer | REMOTE (Global)\nDjango"))

    def test_apply_link_prefers_careers_pages(self):
        text = "Acme | Python | REMOTE (global)\nDemo https://youtube.com/x apply https://acme.io/careers/1"
        self.assertEqual(jobs.how_to_apply(text), "Lien : https://acme.io/careers/1")


class StartupReadiness(unittest.TestCase):
    ISSUE = {"repo": "a/b", "number": 1, "body": "x" * 200, "comments": 1, "good_label": "good first issue"}
    EXTERNAL = [{"merged_at": "t", "author_association": "CONTRIBUTOR", "user": {"type": "User"}}] * 3

    def check(self, comments, prs=EXTERNAL, linked=False, **overrides):
        def fake_get(session, path, params=None):
            return comments if path.endswith("/comments") else prs
        with mock.patch.object(startups, "gh_get", fake_get), \
                mock.patch.object(hunter, "has_linked_pr", return_value=linked):
            return startups.ready_check(None, dict(self.ISSUE, **overrides), {})

    def test_ready_issue_passes(self):
        self.assertIsNone(self.check([human("c", "Same bug on macOS")]))

    def test_rejections(self):
        self.assertEqual(self.check([human("c", "Hi, can I work on this?")]), "déjà réclamée par quelqu'un")
        self.assertEqual(self.check([human("m", "I'm already working on this", "MEMBER")]), "l'équipe s'en occupe")
        self.assertEqual(self.check([], linked=True), "PR déjà liée")
        self.assertEqual(self.check([], prs=self.EXTERNAL[:1]), "dépôt qui fusionne peu de PR externes")
        self.assertEqual(self.check([], body="too short"), "description trop vague")


class HealthReport(TempStateMixin, unittest.TestCase):
    NOW = datetime(2026, 11, 27, 9, 0, tzinfo=timezone.utc)

    def run_status(self, outcomes, now=NOW):
        state = status.load_state()
        with mock.patch.object(status, "post", return_value=True) as post:
            status.handle_outcomes(state, status.parse_outcomes(outcomes), "run", now, False)
            status.daily_report(state, now, False)
        status.save_state(state)
        return [c.args[0]["title"] for c in post.call_args_list]

    def test_failure_alert_is_throttled_then_recovery_is_announced(self):
        titles = self.run_status("bounties=failure,jobs=success,startups=skipped")
        self.assertTrue(any("Bounties : échec" in t for t in titles))
        later = self.run_status("bounties=failure", self.NOW + timedelta(minutes=15))
        self.assertFalse(any("échec" in t for t in later))
        back = self.run_status("bounties=success", self.NOW + timedelta(minutes=30))
        self.assertTrue(any("rétabli" in t for t in back))

    def test_daily_report_once_per_day(self):
        hunter.QUOTA_FILE.write_text(json.dumps({"date": "2026-11-26", "counts": {"startups": 2}}))
        first = self.run_status("startups=success")
        self.assertTrue(any("Bilan du 2026-11-26" in t for t in first))
        again = self.run_status("startups=success", self.NOW + timedelta(hours=1))
        self.assertFalse(any("Bilan" in t for t in again))

    def test_no_report_before_the_hour(self):
        early = self.run_status("startups=success", self.NOW.replace(hour=3))
        self.assertEqual(early, [])


if __name__ == "__main__":
    unittest.main()
