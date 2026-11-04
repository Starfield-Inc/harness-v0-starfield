import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from harness import slack_notify, socket_mode
from harness.checks import FAIL, FLAG, PASS, Check
from harness.daily import Lock
from harness.news_pr import listen, review, slack
from harness.news_pr.model import (Branch, Ci, Click, KeywordRow, Marker, Outcome, PrFacts, PrState, Verb)

APPROVER = "U0TESTAPPROVER"
TOKEN = "xapp-1-A0CANARY-1234-abcdef"
HOOK = "https://hooks.slack.com/services/T0TEST/B0TEST/HOOKSECRET1"
RESPONSE = "https://hooks.slack.com/actions/T0TEST/1234/RESPONSESECRET2"
PR_URL = "https://github.com/example-org/example-site/pull/41"
KEY = "qwen/qwen3-5"
A, R, C = Verb.APPROVE, Verb.REJECT, Verb.CHECK
NOW = datetime.now(timezone.utc)


def facts(**over) -> PrFacts:
    f = dict(number=41, url=PR_URL, title="News: Qwen 3.5 open weights", state=PrState.OPEN, draft=True, merged_at=None,
             not_ours=None, marker=Marker("qwen-3-5-open-weights", KEY, True, "proposed", True), head_sha="abc123",
             ci=Ci.PENDING, branch=Branch(False, True), rejected_recorded=False)
    f.update(over)
    return PrFacts(**f)


def click(verb=A, pr=41, at=None, id="E0001") -> Click:
    return Click(id, verb, pr, APPROVER, at or NOW, RESPONSE)


def payload(action_id="news_approve", value="41", user=APPROVER, url=RESPONSE, **over) -> dict:
    p = {"type": "block_actions", "user": {"id": user}, "response_url": url, "trigger_id": "T1",
         "actions": [{"action_id": action_id, "value": value, "action_ts": str(NOW.timestamp())}]}
    p.update(over)
    return p


def texts(value):
    """Every "text" string anywhere in a payload."""
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in ([v] if k == "text" and isinstance(v, str) else texts(v))]
    if isinstance(value, list):
        return [s for v in value for s in texts(v)]
    return []


def action_ids(c: slack.Card):
    return [e["action_id"] for b in c.blocks if b["type"] == "actions" for e in b["elements"]]


class Offered:
    """A stand in for review.offered, which unit B builds."""

    def __init__(self, verbs=(A, R), reason=None):
        self.verbs, self.reason = verbs, reason

    def __call__(self, f):
        return self.verbs, self.reason


class Posts:
    """Records slack_notify.post. fail_urls answer with HTTP 404."""

    def __init__(self, fail_urls=()):
        self.calls, self.fail_urls = [], set(fail_urls)

    def __call__(self, url, body):
        self.calls.append((url, body))
        if url in self.fail_urls:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return 200

    def to(self, url):
        return [b for u, b in self.calls if u == url]


class CardTest(unittest.TestCase):
    def card(self, verbs=(A, R), reason=None, f=None, note=None):
        with mock.patch.object(review, "offered", Offered(verbs, reason)):
            return slack.card(f or facts(), note)

    def test_buttons_follow_offered_in_a_fixed_order(self):
        for verbs, want in [((A, R), ["news_approve", "news_reject"]), ((R, C), ["news_reject", "news_check"]),
                            ((C, R), ["news_reject", "news_check"]), ((R,), ["news_reject"]),
                            ((C,), ["news_check"]), ((), [])]:
            with self.subTest(verbs=verbs):
                c = self.card(verbs)
                self.assertEqual(action_ids(c), want)
                buttons = [e for b in c.blocks if b["type"] == "actions" for e in b["elements"]]
                self.assertTrue(all(b["value"] == "41" for b in buttons))
                has_mac_line = any(b["type"] == "context" for b in c.blocks)
                self.assertEqual(has_mac_line, bool(want))

    def test_reason_line_only_when_approved_is_not_offered(self):
        c = self.card((R, C), "the keyword row is not on this branch.")
        self.assertIn("Approved is not offered: the keyword row is not on this branch.", texts(list(c.blocks)))
        self.assertFalse([t for t in texts(list(self.card((A, R)).blocks)) if "not offered" in t])

    def test_not_approved_has_a_confirm_dialog_naming_the_key(self):
        c = self.card((A, R, C))
        buttons = {e["action_id"]: e for b in c.blocks if b["type"] == "actions" for e in b["elements"]}
        self.assertEqual(buttons["news_reject"]["confirm"]["text"]["text"],
                         f"Close PR #41 and never suggest {KEY} again?")
        self.assertEqual(buttons["news_reject"]["style"], "danger")
        self.assertNotIn("confirm", buttons["news_approve"])
        self.assertNotIn("confirm", buttons["news_check"])
        no_key = facts(marker=Marker("qwen-3-5-open-weights", None, True, "proposed", True))
        with mock.patch.object(review, "offered", Offered((R,))):
            dialog = slack.card(no_key, None).blocks[-2]["elements"][0]["confirm"]["text"]["text"]
        self.assertIn("nothing is recorded", dialog)

    def test_status_line_link_and_note(self):
        c = self.card(note="Approved by <@U0TESTAPPROVER> at 14:09 EAT. <!channel> ready")
        head, note = c.blocks[0]["text"]["text"], c.blocks[1]["text"]["text"]
        self.assertTrue(head.startswith(f"*News: Qwen 3.5 open weights*\n<{PR_URL}|PR #41>\n"))
        self.assertIn("Checks: all passed. Keyword row: on this branch. GitHub checks: running.", head)
        self.assertIn("<@U0TESTAPPROVER>", note)
        self.assertIn("&lt;!channel&gt;", note)
        merged = self.card((), f=facts(state=PrState.MERGED, merged_at=NOW))
        self.assertIn("Merged at ", merged.blocks[0]["text"]["text"])
        failed = self.card((R, C), f=facts(marker=Marker("s", KEY, False, "none", False), branch=Branch(False, False)))
        self.assertIn("a check failed when the PR was opened. Keyword row: not on this branch.",
                      failed.blocks[0]["text"]["text"])


class ScrubTest(unittest.TestCase):
    env = {"SLACK_WEBHOOK_URL_TEST": HOOK, "SLACK_APP_TOKEN": TOKEN}
    planted = f"see {HOOK} and {TOKEN} and xapp-9-OTHER-1 now"

    def assert_clean(self, body):
        dump = json.dumps(body)
        for secret in ("HOOKSECRET1", "RESPONSESECRET2", "xapp-", "A0CANARY"):
            self.assertNotIn(secret, dump)

    def test_every_text_of_the_first_post_and_of_a_reply_is_scrubbed(self):
        posts = Posts()
        outcome = Outcome("qwen-3-5-open-weights", self.planted, (Check("site", FAIL, self.planted),), (),
                          pr_url=PR_URL)
        with mock.patch.object(slack_notify, "post", posts), \
                mock.patch.object(review, "offered", Offered((R, C), self.planted)), \
                mock.patch.object(review, "facts_from_outcome", lambda o: facts(title=o.title)):
            self.assertTrue(slack.notify(outcome, self.env, io.StringIO()))
            c = slack.card(facts(title=self.planted), self.planted)
            slack.reply(click(), c, self.env, io.StringIO())
            slack.reply(click(), self.planted, self.env, io.StringIO(), ephemeral=True)
        self.assertEqual(len(posts.calls), 3)
        for _, body in posts.calls:
            self.assert_clean(body)
            self.assertTrue(any("<redacted>" in t for t in texts(body)))
        self.assertIn(PR_URL, json.dumps(posts.calls[0][1]))

    def test_send_scrubs_text_nested_in_blocks(self):
        posts = Posts()
        blocks = [{"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text",
                                                                               "text": self.planted}}]}]
        with mock.patch.object(slack_notify, "post", posts):
            slack_notify.send(HOOK, "fallback", self.env, io.StringIO(), blocks=blocks)
        self.assert_clean(posts.calls[0][1])

    def test_a_member_mention_survives_the_scrub(self):
        from harness.scrub import scrub
        self.assertEqual(scrub("Approved by <@U0TESTAPPROVER>.", {}), "Approved by <@U0TESTAPPROVER>.")
        self.assertEqual(scrub("mail reviewer-login@example.org", {}), "mail <redacted>")


class TimeTest(unittest.TestCase):
    def setUp(self):
        old = os.environ.get("TZ")
        os.environ["TZ"] = "Africa/Nairobi"
        time.tzset()

        def restore():
            os.environ.pop("TZ") if old is None else os.environ.__setitem__("TZ", old)
            time.tzset()
        self.addCleanup(restore)

    def test_local_time_names_the_zone_and_the_day_when_not_today(self):
        dt = datetime(2026, 11, 4, 11, 5, tzinfo=timezone.utc)
        self.assertEqual(slack.local_time(dt, now=datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc)), "14:05 EAT")
        self.assertEqual(slack.local_time(dt, now=datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)), "4 Nov 14:05 EAT")

    def test_working_note(self):
        c = click(at=datetime(2026, 11, 4, 11, 5, tzinfo=timezone.utc))
        self.assertIn("Working on Approved for PR #41, clicked ", slack.working_note(c))
        self.assertIn("14:05 EAT", slack.working_note(c))
        self.assertIn("A second click is safe.", slack.working_note(c))


class ParseClickTest(unittest.TestCase):
    def parse(self, p, **kw):
        return listen.parse_click(p, APPROVER, NOW, envelope_id="E1", **kw)

    def test_the_approver_gets_a_click(self):
        got = self.parse(payload("news_reject"))
        self.assertEqual((got.id, got.verb, got.pr, got.user, got.response_url), ("E1", R, 41, APPROVER, RESPONSE))
        self.assertLess(abs((got.at - NOW).total_seconds()), 1)
        self.assertNotIn("RESPONSESECRET2", repr(got))
        self.assertEqual(listen.parse_click(payload(), APPROVER, NOW).id, "T1")

    def test_another_user_gets_only_the_refusal(self):
        got = self.parse(payload(user="U0SOMEONEELSE"))
        self.assertIsInstance(got, listen.NotApprover)
        self.assertNotIn("RESPONSESECRET2", repr(got))
        self.assertIsInstance(listen.parse_click(payload(), "", NOW, envelope_id="E1"), listen.NotApprover)

    def test_unknown_action_and_bad_values_are_ignored(self):
        for p in [payload("news_merge"), payload(value="41; rm"), payload(value="0"), payload(value="４１"),
                  payload(value=41), payload(type="view_submission")]:
            with self.subTest(p=p):
                self.assertIsNone(self.parse(p))

    def test_response_url_must_be_slack_https(self):
        for url in ["http://hooks.slack.com/actions/x", "https://hooks.slack.com.example.org/x",
                    "https://evil@hooks.slack.com/x", "https://example.org/x", "https://hooks.slack.com:8443/x",
                    "http://127.0.0.1:9/respond/1", None]:
            with self.subTest(url=url):
                self.assertIsNone(self.parse(payload(url=url)))
        self.assertEqual(self.parse(payload(url="http://127.0.0.1:9/respond/1"), loopback=True).pr, 41)

    def test_malformed_payloads(self):
        for p in [None, "text", {}, {"type": "block_actions"}, payload(actions=[]), payload(actions="x"),
                  payload(actions=["x"]), {**payload(), "user": None}, {**payload(), "user": "U0TESTAPPROVER"}]:
            with self.subTest(p=p):
                self.assertIsNone(self.parse(p))
        self.assertIsNone(listen.parse_click(payload(trigger_id=None), APPROVER, NOW))


class Env(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.inbox = root / "news" / "clicks"
        self.env = {"SLACK_APP_TOKEN": TOKEN, "SLACK_APPROVER_ID": APPROVER, "SLACK_WEBHOOK_URL_TEST": HOOK,
                    "HARNESS_NEWS_DIR": str(root / "news"), "HARNESS_DAILY_LOCK_DIR": str(root / "locks"),
                    "HARNESS_SITE_REPO_URL": str(root / "site.git"), "HARNESS_SITE_WORKDIR": str(root / "work"),
                    "HARNESS_GH_CONFIG_DIR": str(root / "gh"), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        self.posts = Posts()
        self.spawned = []
        self.settled = []
        for target, name, value in [(slack_notify, "post", self.posts),
                                    (listen, "_spawn_drain", lambda environ: self.spawned.append(environ)),
                                    (review, "offered", Offered((A, R))), (review, "settle", self.settle)]:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def settle(self, c, env, runner):
        self.settled.append(c)
        return review.Settled(facts(), f"Approved by <@{c.user}>. The PR is ready to merge.")

    def run_main(self, *argv, serve=None):
        out = io.StringIO()
        with mock.patch.object(socket_mode, "serve", serve or (lambda *a, **k: None)):
            code = listen.main(list(argv), environ=self.env, out=out)
        return code, out.getvalue()


class InboxTest(Env):
    def test_put_replace_done_and_unreadable(self):
        listen._inbox_put(self.inbox, click(id="E1"))
        self.assertEqual(oct((self.inbox / "pr-41.json").stat().st_mode & 0o777), "0o600")
        listen._inbox_put(self.inbox, click(verb=R, id="E2"))
        self.assertEqual([(c.id, c.verb) for c in listen._inbox_all(self.inbox)], [("E2", R)])
        listen._inbox_done(self.inbox, click(id="E1"))
        self.assertTrue((self.inbox / "pr-41.json").exists())
        listen._inbox_done(self.inbox, click(id="E2"))
        self.assertFalse((self.inbox / "pr-41.json").exists())
        listen._inbox_put(self.inbox, click(pr=7, at=NOW, id="E3"))
        listen._inbox_put(self.inbox, click(pr=8, at=NOW - timedelta(minutes=5), id="E4"))
        (self.inbox / "pr-9.json").write_text("{not json")
        logs = []
        self.assertEqual([c.pr for c in listen._inbox_all(self.inbox, log=logs.append)], [8, 7])
        self.assertFalse((self.inbox / "pr-9.json").exists())
        self.assertEqual(logs, ["removed an unreadable click file (JSONDecodeError)"])


class DaemonTest(Env):
    def envelope(self, p=None, id="E0001", kind="interactive"):
        return {"type": kind, "envelope_id": id, "payload": payload() if p is None else p}

    def serving(self, *envelopes, order=None):
        order = [] if order is None else order

        def serve(token, on_envelope, tick, stop, api_base, idle, log):
            self.assertEqual(token, TOKEN)
            for e in envelopes:
                acked = []
                on_envelope(e, lambda: (acked.append(1), order.append(("ack", (self.inbox / "pr-41.json").exists()))))
                order.append(("returned", bool(acked)))
        return serve

    def test_the_click_file_is_written_before_the_ack(self):
        order = []
        real_put = listen._inbox_put
        with mock.patch.object(listen, "_inbox_put", lambda f, c: (order.append(("file", c.id)), real_put(f, c))):
            code, out = self.run_main(serve=self.serving(self.envelope(), order=order))
        self.assertEqual(code, 0)
        self.assertEqual(order, [("file", "E0001"), ("ack", True), ("returned", True)])
        self.assertEqual(len(self.spawned), 1)
        note = self.posts.to(RESPONSE)[0]
        self.assertEqual((note["response_type"], note["replace_original"]), ("ephemeral", False))
        self.assertIn("Working on Approved for PR #41", note["text"])
        self.assertIn("envelope interactive: PR 41 news_approve: saved", out)

    def test_a_failed_file_write_gets_no_ack(self):
        order = []

        def broken(folder, c):
            raise PermissionError("no")
        with mock.patch.object(listen, "_inbox_put", broken):
            serve = self.serving(self.envelope(), order=order)

            def guarded(*a, **k):
                with self.assertRaises(RuntimeError):
                    serve(*a, **k)
            self.run_main(serve=guarded)
        self.assertEqual(order, [])
        self.assertEqual(self.posts.calls, [])

    def test_another_user_is_refused_and_nothing_is_saved(self):
        order = []
        code, out = self.run_main(serve=self.serving(self.envelope(payload(user="U0SOMEONEELSE")), order=order))
        self.assertEqual(order, [("ack", False), ("returned", True)])
        self.assertEqual(self.posts.to(RESPONSE)[0]["text"], listen.NOT_APPROVER)
        self.assertEqual(self.posts.to(RESPONSE)[0]["response_type"], "ephemeral")
        self.assertFalse(self.inbox.exists() and list(self.inbox.glob("pr-*.json")))
        self.assertEqual(self.spawned, [])
        self.assertIn("not the approver, refused", out)

    def test_unknown_actions_and_other_envelopes_are_ignored(self):
        order = []
        code, out = self.run_main(serve=self.serving(self.envelope(payload("news_merge")),
                                                     self.envelope(kind="events_api"),
                                                     self.envelope(p="not json"), order=order))
        self.assertEqual(order, [("returned", False)] * 3)
        self.assertEqual(self.posts.calls, [])
        self.assertEqual(self.spawned, [])
        self.assertIn("envelope events_api: ignored", out)
        self.assertEqual(out.count("not a news click, ignored"), 2)

    def test_a_crash_after_the_ack_leaves_a_file_that_the_drain_runs(self):
        self.run_main(serve=self.serving(self.envelope()))
        self.assertTrue((self.inbox / "pr-41.json").exists())
        code, out = self.run_main("--drain")
        self.assertEqual(code, 0)
        self.assertEqual([(c.id, c.verb, c.pr) for c in self.settled], [("E0001", A, 41)])
        final = self.posts.to(RESPONSE)[-1]
        self.assertTrue(final["replace_original"])
        self.assertIn("ready to merge", json.dumps(final["blocks"]))
        self.assertFalse((self.inbox / "pr-41.json").exists())
        self.assertEqual(self.run_main("--drain")[0], 0)
        self.assertEqual(len(self.settled), 1)

    def test_the_daemon_spawns_a_drain_at_start_when_a_file_waits(self):
        listen._inbox_put(self.inbox, click())
        self.run_main()
        self.assertEqual(len(self.spawned), 1)

    def test_a_second_daemon_is_refused_by_the_lock(self):
        served = []
        with Lock(listen.REPO, self.env, name="harness-news-listener"):
            code, out = self.run_main(serve=lambda *a, **k: served.append(1))
        self.assertEqual(code, 1)
        self.assertEqual(served, [])
        self.assertIn("another listener is running", out)
        self.assertEqual(self.run_main(serve=lambda *a, **k: served.append(1))[0], 0)
        self.assertEqual(served, [1])

    def test_the_token_never_reaches_a_log_line(self):
        def serve(token, on_envelope, tick, stop, api_base, idle, log):
            log(f"connect failed with {token} on {HOOK}")
            raise ValueError(f"bad token {token}")
        code, out = self.run_main(serve=serve)
        self.assertEqual(code, 1)
        self.assertIn("the listener stopped with an error: ValueError", out)
        for secret in ("A0CANARY", "xapp-", "HOOKSECRET1", "RESPONSESECRET2"):
            self.assertNotIn(secret, out)
        self.env["SLACK_APP_TOKEN"] = "xoxb-WRONG-KIND-SECRET"
        code, out = self.run_main()
        self.assertEqual(code, 2)
        self.assertNotIn("WRONG-KIND-SECRET", out)

    def test_config_errors_exit_2(self):
        for key, value in [("SLACK_APPROVER_ID", "reviewer-login"), ("SLACK_APP_TOKEN", ""),
                           ("HARNESS_SITE_WORKDIR", ""), ("HARNESS_SLACK_API_URL", "https://slack.example.org/api")]:
            with self.subTest(key=key):
                saved = self.env[key] if key in self.env else None
                self.env[key] = value
                code, out = self.run_main()
                self.assertEqual(code, 2)
                self.assertEqual(len(out.strip().splitlines()), 1)
                self.env.pop(key) if saved is None else self.env.__setitem__(key, saved)
        self.assertEqual(self.run_main("--bogus")[0], 2)


class DrainTest(Env):
    def test_clicks_older_than_24_hours_are_dropped_with_a_note(self):
        listen._inbox_put(self.inbox, click(at=NOW - timedelta(hours=25)))
        code, out = self.run_main("--drain")
        self.assertEqual(code, 0)
        self.assertEqual(self.settled, [])
        self.assertFalse((self.inbox / "pr-41.json").exists())
        self.assertIn("more than 24 hours old", self.posts.to(RESPONSE)[0]["text"])
        self.assertIn("older than 24 hours, dropped", out)

    def test_oldest_first_one_at_a_time(self):
        listen._inbox_put(self.inbox, click(pr=7, id="E7"))
        listen._inbox_put(self.inbox, click(pr=8, at=NOW - timedelta(minutes=3), id="E8"))
        self.run_main("--drain")
        self.assertEqual([c.pr for c in self.settled], [8, 7])

    def test_a_click_replaced_during_a_settle_is_settled_again(self):
        listen._inbox_put(self.inbox, click(id="E1"))

        def settle(c, env, runner):
            self.settled.append(c)
            if c.id == "E1":
                listen._inbox_put(self.inbox, click(verb=R, id="E2"))
            return review.Settled(facts(), "done")
        with mock.patch.object(review, "settle", settle):
            self.run_main("--drain")
        self.assertEqual([c.id for c in self.settled], ["E1", "E2"])
        self.assertFalse((self.inbox / "pr-41.json").exists())

    def test_the_drain_waits_while_the_pr_job_holds_its_lock(self):
        listen._inbox_put(self.inbox, click())
        with Lock(listen.REPO, self.env, name="harness-news-pr"):
            code, out = self.run_main("--drain")
        self.assertEqual(code, 0)
        self.assertEqual(self.settled, [])
        self.assertTrue((self.inbox / "pr-41.json").exists())
        self.assertIn("lock is held", out)

    def test_an_expired_response_url_falls_back_to_the_webhook(self):
        self.posts.fail_urls.add(RESPONSE)
        listen._inbox_put(self.inbox, click())
        code, out = self.run_main("--drain")
        body = self.posts.to(HOOK)[0]
        self.assertTrue(body["text"].startswith("Update on PR #41:"))
        self.assertEqual(body["blocks"][0]["text"]["text"], "Update on PR #41:")
        self.assertIn("Slack reply failed: HTTPError", out)
        self.assertNotIn("RESPONSESECRET2", out)

    def test_a_settle_error_is_reported_and_not_retried(self):
        listen._inbox_put(self.inbox, click())

        def boom(c, env, runner):
            raise review.StepFailed(review.Step.CLOSE, "gh could not reach GitHub", False)
        with mock.patch.object(review, "settle", boom):
            code, out = self.run_main("--drain")
        self.assertEqual(code, 0)
        self.assertIn("stopped at close: gh could not reach GitHub", self.posts.to(RESPONSE)[0]["text"])
        self.assertFalse((self.inbox / "pr-41.json").exists())


    def test_a_read_failure_with_no_step_is_reported_and_does_not_crash_the_drain(self):
        listen._inbox_put(self.inbox, click())

        def boom(c, env, runner):
            raise review.StepFailed(None, "GitHub could not be read", False)
        with mock.patch.object(review, "settle", boom):
            code, out = self.run_main("--drain")
        self.assertEqual(code, 0)
        self.assertIn("stopped at read: GitHub could not be read", self.posts.to(RESPONSE)[0]["text"])
        self.assertFalse((self.inbox / "pr-41.json").exists())


class NotifyTest(unittest.TestCase):
    def test_first_post_is_the_card_through_the_radar_switch(self):
        posts = Posts()
        main_hook = "https://hooks.slack.com/services/T0/B0/MAINSECRET3"
        outcome = Outcome("qwen-3-5-open-weights", "Qwen 3.5", (Check("keyword row", FLAG, "no row"),
                                                                Check("site build", FAIL, "broke"),
                                                                Check("site tests", PASS, "ok")), (),
                          pr_url=PR_URL, keyword=KeywordRow(True, "proposed"))
        seen = []
        with mock.patch.object(slack_notify, "post", posts), \
                mock.patch.object(review, "offered", Offered((R, C), "a check failed.")), \
                mock.patch.object(review, "facts_from_outcome", lambda o: (seen.append(o), facts())[1]):
            slack.notify(outcome, {"SLACK_WEBHOOK_URL_TEST": HOOK}, io.StringIO())
            slack.notify(outcome, {"SLACK_WEBHOOK_URL_TEST": HOOK, "SLACK_WEBHOOK_URL_MAIN": main_hook,
                                   "RADAR_SLACK_TARGET": "main", "SLACK_TARGET": "test"}, io.StringIO())
        self.assertEqual([u for u, _ in posts.calls], [HOOK, main_hook])
        self.assertEqual(seen, [outcome, outcome])
        body = posts.calls[0][1]
        self.assertTrue(body["text"].startswith("PR #41, News: Qwen 3.5 open weights"))
        detail = body["blocks"][1]["text"]["text"]
        self.assertLess(detail.index("Failed (1):\n• site build: broke"), detail.index("Flagged for a look (1):"))
        self.assertNotIn("site tests", detail)
        self.assertEqual(action_ids(slack.Card("", tuple(body["blocks"]))), ["news_reject", "news_check"])
        self.assertNotIn("here>", json.dumps(body))

    def test_notify_never_raises(self):
        out = io.StringIO()
        with mock.patch.object(review, "facts_from_outcome", mock.Mock(side_effect=KeyError("x"))):
            self.assertFalse(slack.notify(Outcome("s", "t", (), ()), {}, out))
        self.assertIn("Slack message failed: KeyError", out.getvalue())


if __name__ == "__main__":
    unittest.main()
