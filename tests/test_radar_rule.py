import unittest
from datetime import date, datetime, timezone

from harness.radar import config, names, rule, sources
from harness.radar.model import Item, Outcome, RouterModel, Signal, Snapshot

DAY = date(2026, 11, 4)
CONFIG = config.parse({
    "settings": {"cap": 3},
    "orgs": [
        {"name": "Qwen", "hf_author": "Qwen", "openrouter_prefixes": ["qwen"]},
        {"name": "Moonshot AI", "hf_author": "moonshotai", "openrouter_prefixes": ["moonshotai"]},
        {"name": "DeepSeek", "hf_author": "deepseek-ai", "openrouter_prefixes": ["deepseek"]},
        {"name": "NVIDIA", "hf_author": "nvidia", "openrouter_prefixes": ["nvidia"]},
    ],
})
ORG_OF = {"Qwen": "Qwen", "moonshotai": "Moonshot AI", "deepseek-ai": "DeepSeek", "nvidia": "NVIDIA"}


def release(repo, created="2026-11-03T10:00:00.000Z", tags=("safetensors",), pipeline="text-generation", **raw):
    data = {"id": repo, "createdAt": created, "tags": list(tags), "pipeline_tag": pipeline, **raw}
    return sources.parse_release(data, ORG_OF[repo.split("/")[0]])


def router(model_id, day="2026-11-03", hf=None):
    when = datetime.fromisoformat(day).replace(hour=12, tzinfo=timezone.utc)
    return RouterModel(model_id, model_id, when, hf)


def post(title, day="2026-11-03", source="https://lab.test/feed"):
    return Item(title, f"https://lab.test/{len(title)}", date.fromisoformat(day), source)


def snapshot(releases, routers=(), lab=None, press=(), trending=frozenset()):
    return Snapshot(tuple(releases), trending, tuple(routers), lab or {}, tuple(press), (), 1)


def run(snap, day=DAY, seen=None, rejected=frozenset(), history=False):
    return rule.radar_day(CONFIG, snap, day, seen or {}, set(rejected), history=history)


def ids(cands):
    return [c.release.id for c in cands]


class DerivativeTest(unittest.TestCase):
    def test_finetune_of_another_orgs_model_is_excluded(self):
        tuned = release("nvidia/Llama-4-Scout-Tuned", tags=("safetensors", "base_model:finetune:meta-llama/Llama-4-Scout"))
        report = run(snapshot([tuned], [router("nvidia/llama-4-scout-tuned")]))
        self.assertEqual(ids(report.candidates), [])
        self.assertEqual([v.release.id for v in report.rejected], ["nvidia/Llama-4-Scout-Tuned"])

    def test_same_repo_without_the_base_tag_passes(self):
        plain = release("nvidia/Llama-4-Scout-Tuned")
        self.assertEqual(ids(run(snapshot([plain], [router("nvidia/llama-4-scout-tuned")])).candidates),
                         ["nvidia/Llama-4-Scout-Tuned"])

    def test_instruct_model_of_the_orgs_own_base_is_not_a_derivative(self):
        instruct = release("Qwen/Qwen3.5-35B-A3B", tags=("safetensors", "base_model:finetune:Qwen/Qwen3.5-35B-A3B-Base"))
        self.assertEqual(ids(run(snapshot([instruct], [router("qwen/qwen3.5-35b-a3b")])).candidates),
                         ["Qwen/Qwen3.5-35B-A3B"])

    def test_quantized_copy_is_excluded_and_linked_on_the_base(self):
        base = release("Qwen/Qwen3.5-397B-A17B")
        fp8 = release("Qwen/Qwen3.5-397B-A17B-FP8", tags=("safetensors", "base_model:quantized:Qwen/Qwen3.5-397B-A17B"))
        report = run(snapshot([base, fp8], [router("qwen/qwen3.5-397b-a17b")]))
        self.assertEqual(ids(report.candidates), ["Qwen/Qwen3.5-397B-A17B"])
        self.assertEqual(report.candidates[0].copy_links, ("https://huggingface.co/Qwen/Qwen3.5-397B-A17B-FP8",))

    def test_name_marker_alone_makes_a_copy(self):
        gguf = release("Qwen/Qwen3.5-9B-GGUF", tags=("gguf",))
        self.assertEqual(ids(run(snapshot([gguf], [router("qwen/qwen3.5-9b")])).candidates), [])


class RuleTest(unittest.TestCase):
    def test_minor_dated_update_needs_a_lab_post(self):
        update = release("moonshotai/Kimi-K2-Instruct-0905")
        signals = [router("moonshotai/kimi-k2-0905")]
        report = run(snapshot([update], signals))
        self.assertEqual(ids(report.candidates), [])
        self.assertIn("minor dated update without a lab post", report.rejected[0].reasons)
        with_post = run(snapshot([update], signals, lab={"Moonshot AI": (post("Kimi K2 0905 is here"),)}))
        self.assertEqual(ids(with_post.candidates), ["moonshotai/Kimi-K2-Instruct-0905"])

    def test_changelog_that_calls_it_an_update_makes_it_minor(self):
        repo = release("deepseek-ai/DeepSeek-V3.2")
        lab = {"DeepSeek": (post("DeepSeek-V3.2 update"),)}
        report = run(snapshot([repo], [router("deepseek/deepseek-v3.2")], lab=lab))
        self.assertEqual(ids(report.candidates), [])
        self.assertEqual(ids(run(snapshot([repo], [router("deepseek/deepseek-v3.2")])).candidates),
                         ["deepseek-ai/DeepSeek-V3.2"])

    def test_private_repo_is_excluded(self):
        repo = release("Qwen/Qwen3.5-9B", private=True)
        report = run(snapshot([repo], [router("qwen/qwen3.5-9b")]))
        self.assertEqual(ids(report.candidates), [])
        self.assertIn("private repository", report.rejected[0].reasons)

    def test_repo_without_weight_files_is_excluded(self):
        report = run(snapshot([release("Qwen/Qwen3.5-9B", tags=())], [router("qwen/qwen3.5-9b")]))
        self.assertEqual(ids(report.candidates), [])

    def test_no_signal_no_candidate(self):
        report = run(snapshot([release("Qwen/Qwen3.5-9B")]))
        self.assertEqual(ids(report.candidates), [])
        self.assertEqual(report.rejected[0].reasons, ("no signal",))

    def test_other_kind_is_listed_only_with_a_lab_post(self):
        tts = release("Qwen/Qwen3.5-TTS", pipeline="text-to-speech")
        bare = run(snapshot([tts], [router("qwen/qwen3.5-tts")]))
        self.assertEqual((ids(bare.candidates), ids(bare.other)), ([], []))
        posted = run(snapshot([tts], lab={"Qwen": (post("Qwen3.5 TTS"),)}))
        self.assertEqual((ids(posted.candidates), ids(posted.other)), ([], ["Qwen/Qwen3.5-TTS"]))

    def test_api_only_model_goes_to_watch_never_to_candidates(self):
        hf = release("Qwen/Qwen3.5-397B-A17B")
        routers = [router("qwen/qwen3.5-397b-a17b"), router("qwen/qwen-max-2"), router("qwen/qwen3.5-plus"),
                   router("qwen/qwen3-coder-next", hf="Qwen/Qwen3-Coder-Next"), router("other/model-9")]
        report = run(snapshot([hf], routers))
        self.assertEqual([m.id for m in report.watch], ["qwen/qwen-max-2"])
        self.assertEqual(ids(report.candidates), ["Qwen/Qwen3.5-397B-A17B"])

    def test_old_api_model_is_not_watched(self):
        self.assertEqual(run(snapshot([], [router("qwen/qwen-max-2", day="2026-10-20")])).watch, ())

    def test_press_needs_two_outlets(self):
        repo = release("Qwen/Qwen3.5-9B")
        one = [post("Qwen3.5 lands", source="a"), post("Alibaba ships Qwen3.5", source="a")]
        two = [post("Qwen3.5 lands", source="a"), post("Alibaba ships Qwen3.5", source="b")]
        self.assertEqual(ids(run(snapshot([repo], press=one)).candidates), [])
        self.assertEqual(run(snapshot([repo], press=two)).candidates[0].signals, frozenset({Signal.PRESS_2PLUS}))

    def test_trending_counts_live(self):
        repo = release("Qwen/Qwen3.5-9B")
        report = run(snapshot([repo], trending=frozenset({"qwen/qwen3.5-9b"})))
        self.assertEqual(report.candidates[0].signals, frozenset({Signal.HF_TRENDING}))

    def test_signals_after_the_day_are_not_seen(self):
        repo = release("Qwen/Qwen3.5-9B", created="2026-10-30T10:00:00Z")
        snap = snapshot([repo], [router("qwen/qwen3.5-9b", day="2026-11-02")])
        self.assertEqual(ids(run(snap, day=date(2026, 11, 1)).candidates), [])
        self.assertEqual(ids(run(snap, day=date(2026, 11, 2)).candidates), ["Qwen/Qwen3.5-9B"])

    def test_cap_and_ranking(self):
        releases = [release("Qwen/Qwen4-9B", created="2026-11-01T00:00:00Z"),
                    release("moonshotai/Kimi-K3", created="2026-11-02T00:00:00Z"),
                    release("deepseek-ai/DeepSeek-V4", created="2026-11-03T00:00:00Z"),
                    release("nvidia/Nemotron-5-Nano", created="2026-11-03T06:00:00Z"),
                    release("Qwen/QwQ-2-32B", created="2026-11-03T08:00:00Z")]
        routers = [router("qwen/qwen4-9b"), router("moonshotai/kimi-k3"), router("deepseek/deepseek-v4"),
                   router("nvidia/nemotron-5-nano"), router("qwen/qwq-2-32b")]
        lab = {"Qwen": (post("Qwen4 is here"),), "Moonshot AI": (post("Kimi K3"),)}
        report = run(snapshot(releases, routers, lab=lab))
        self.assertEqual(ids(report.candidates), ["moonshotai/Kimi-K3", "Qwen/Qwen4-9B", "Qwen/QwQ-2-32B"])
        self.assertEqual(ids(report.over_cap), ["nvidia/Nemotron-5-Nano", "deepseek-ai/DeepSeek-V4"])

    def test_one_candidate_per_family(self):
        big, small = release("Qwen/Qwen3.5-397B-A17B"), release("Qwen/Qwen3.5-35B-A3B")
        report = run(snapshot([big, small], [router("qwen/qwen3.5-397b-a17b")]))
        self.assertEqual(len(report.candidates), 1)
        self.assertEqual(len(report.candidates[0].family_links), 1)

    def test_seen_on_an_earlier_day_is_not_suggested_again(self):
        snap = snapshot([release("Qwen/Qwen3.5-9B")], [router("qwen/qwen3.5-9b")])
        key = names.family_key("Qwen/Qwen3.5-9B")
        earlier = run(snap, seen={key: "2026-11-03"})
        self.assertEqual((ids(earlier.candidates), earlier.already_suggested), ([], (key,)))
        today = run(snap, seen={key: DAY.isoformat()})
        self.assertEqual(ids(today.candidates), ["Qwen/Qwen3.5-9B"])

    def test_rejected_key_is_never_suggested(self):
        snap = snapshot([release("Qwen/Qwen3.5-9B")], [router("qwen/qwen3.5-9b")])
        self.assertEqual(ids(run(snap, rejected={names.family_key("Qwen/Qwen3.5-9B")}).candidates), [])

    def test_undated_headline_counts_live_but_not_in_the_backtest(self):
        repo = release("Qwen/Qwen3.5-9B")
        undated = {"Qwen": (Item("Qwen3.5", "https://lab.test/q", None, "page"),)}
        self.assertEqual(ids(run(snapshot([repo], lab=undated)).candidates), ["Qwen/Qwen3.5-9B"])
        self.assertEqual(ids(run(snapshot([repo], lab=undated), history=True).candidates), [])

    def test_verdict_says_why_it_passed(self):
        repo = release("Qwen/Qwen3.5-9B")
        verdict = rule.judge(repo, rule.evidence(repo, CONFIG.orgs[0], snapshot([repo], [router("qwen/qwen3.5-9b")]),
                                                 DAY, CONFIG.settings, False), official=True)
        self.assertEqual(verdict.outcome, Outcome.CANDIDATE)
        self.assertIn("qwen/qwen3.5-9b", verdict.reasons[0])


class NamesTest(unittest.TestCase):
    def test_positive_matches(self):
        cases = [("Introducing Qwen3.5: native agents", "Qwen/Qwen3.5-397B-A17B"),
                 ("qwen3.5-plus", "Qwen/Qwen3.5-397B-A17B"),
                 ("DeepSeek-V3.2-Exp released", "deepseek-ai/DeepSeek-V3.2-Exp"),
                 ("Kimi K2 0905 update", "moonshotai/Kimi-K2-Instruct-0905"),
                 ("NVIDIA Nemotron Nano 2 is out", "nvidia/NVIDIA-Nemotron-Nano-9B-v2"),
                 ("OpenAI releases gpt-oss", "openai/gpt-oss-120b")]
        for text, repo in cases:
            self.assertTrue(names.mentions(text, repo), (text, repo))

    def test_near_misses_do_not_match(self):
        cases = [("Qwen3.5 is here", "Qwen/Qwen3-235B-A22B-Instruct-2507"),
                 ("Qwen3 recap", "Qwen/Qwen3.5-9B"),
                 ("DeepSeek-V3.1 notes", "deepseek-ai/DeepSeek-V3.2"),
                 ("MegaQwen3.5 fork", "Qwen/Qwen3.5-9B")]
        for text, repo in cases:
            self.assertFalse(names.mentions(text, repo), (text, repo))

    def test_dated_suffix(self):
        self.assertTrue(names.dated_suffix("moonshotai/Kimi-K2-Instruct-0905"))
        self.assertTrue(names.dated_suffix("Qwen/Qwen3-4B-Thinking-2507"))
        self.assertFalse(names.dated_suffix("Qwen/Qwen3-4B-9999"))
        self.assertFalse(names.dated_suffix("Qwen/Qwen3.5-9B"))

    def test_variants_share_a_family_key(self):
        self.assertEqual(names.family_key("Qwen/Qwen3-4B-Instruct-2507"), names.family_key("Qwen/Qwen3-4B-Thinking-2507"))
        self.assertNotEqual(names.family_key("Qwen/Qwen3-Next-80B-A3B"), names.family_key("Qwen/Qwen3-8B"))


if __name__ == "__main__":
    unittest.main()
