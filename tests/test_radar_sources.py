import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from harness.radar import config, covered, sources, state
from harness.radar.model import Derivation, FeedKind, Kind

FIXTURES = Path(__file__).parent / "fixtures" / "radar"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class FeedTest(unittest.TestCase):
    def test_rss_titles_links_dates(self):
        items = sources.parse_feed(fixture("feed.rss.xml"), FeedKind.RSS, "src")
        self.assertEqual([(i.title, i.link, i.published) for i in items],
                         [("Introducing Qwen3.5", "https://lab.test/qwen3-5", date(2026, 11, 2)),
                          ("Team news", "https://lab.test/team", None)])

    def test_atom_titles_links_dates(self):
        items = sources.parse_feed(fixture("feed.atom.xml"), FeedKind.ATOM, "src")
        self.assertEqual([(i.title, i.link, i.published) for i in items],
                         [("v2.0 Kimi K3", "https://lab.test/k3", date(2026, 11, 1)),
                          ("Docs fix", "https://lab.test/docs", date(2026, 10, 20))])

    def test_bad_xml_and_wrong_kind_raise_source_error(self):
        with self.assertRaises(sources.SourceError):
            sources.parse_feed("<rss><channel><item>", FeedKind.RSS, "src")
        with self.assertRaises(sources.SourceError):
            sources.parse_feed(fixture("feed.atom.xml"), FeedKind.RSS, "src")


class PageTest(unittest.TestCase):
    def test_deepseek_changelog(self):
        items = sources.parse_deepseek_changelog(fixture("deepseek_updates.html"), "https://ds.test/updates/")
        self.assertEqual([(i.title, i.published) for i in items],
                         [("DeepSeek-V4 is out", date(2026, 11, 2)), ("DeepSeek-V3.2 update", date(2026, 10, 15))])

    def test_zai_release_notes(self):
        items = sources.parse_zai_release_notes(fixture("zai_release_notes.html"), "https://z.test/notes")
        self.assertEqual([(i.title, i.published) for i in items],
                         [("GLM-5 released", date(2026, 10, 30)), ("GLM-4.7 Air", date(2026, 10, 12))])

    def test_plain_links(self):
        items = sources.parse_plain_links(fixture("links.html"), "https://lab.test/blog/")
        self.assertEqual([(i.title, i.link, i.published) for i in items],
                         [("Kimi K3: open agentic model", "https://lab.test/blog/kimi-k3", None),
                          ("Elsewhere", "https://other.test/x", None)])


class HuggingFaceTest(unittest.TestCase):
    def test_parse_release_fields(self):
        r = sources.parse_release({
            "id": "Qwen/Qwen3.5-9B", "createdAt": "2026-11-03T10:00:00.000Z", "lastModified": "2026-11-03T11:00:00Z",
            "pipeline_tag": "image-text-to-text", "tags": ["safetensors", "license:apache-2.0"], "downloads": 12,
            "gated": "auto", "private": False, "cardData": {"license": "other"}}, "Qwen")
        self.assertEqual((r.kind, r.license_tag, r.gated, r.has_weights, r.is_derivative, r.downloads),
                         (Kind.VISION, "other", True, True, False, 12))

    def test_kinds_and_derivations(self):
        def parse(repo, pipeline, tags=()):
            return sources.parse_release({"id": repo, "createdAt": "2026-11-03T10:00:00Z", "pipeline_tag": pipeline,
                                          "tags": list(tags)}, "x")
        self.assertEqual(parse("meta-llama/Llama-Guard-5", "text-generation").kind, Kind.SAFETY)
        self.assertEqual(parse("google/x-embed", "sentence-similarity").kind, Kind.EMBEDDING)
        self.assertEqual(parse("google/x", "any-to-any").kind, Kind.ANY_TO_ANY)
        self.assertEqual(parse("google/x", None).kind, Kind.OTHER)
        self.assertEqual(parse("nvidia/Qwen3-8B-NVFP4", "text-generation").derivation, Derivation.QUANTIZED)
        adapter = parse("google/x-lora", "text-generation", ["base_model:adapter:google/gemma-4"])
        self.assertEqual((adapter.derivation, adapter.base_model), (Derivation.ADAPTER, "google/gemma-4"))
        self.assertIsNone(sources.parse_release({"id": "no-slash", "createdAt": "2026-11-03T10:00:00Z"}, "x"))

    def test_hf_new_keeps_repos_since_and_flags_a_cut_list(self):
        org = config.parse({"orgs": [{"name": "Qwen", "hf_author": "Qwen"}]}).orgs[0]
        rows = [{"id": f"Qwen/m{i}", "createdAt": f"2026-11-0{i}T00:00:00Z"} for i in (4, 3, 2)]
        fetch = {sources.hf_new_url("Qwen", 3): (200, json.dumps(rows))}.get
        since = datetime(2026, 11, 3, tzinfo=timezone.utc)
        got, cut = sources.hf_new(fetch, org, since, 3)
        self.assertEqual(([r.id for r in got], cut), (["Qwen/m4", "Qwen/m3"], False))
        got, cut = sources.hf_new(fetch, org, datetime(2026, 11, 1, tzinfo=timezone.utc), 3)
        self.assertEqual((len(got), cut), (3, True))

    def test_openrouter_rows(self):
        text = json.dumps({"data": [{"id": "qwen/qwen3.5-plus", "created": 1793750400, "name": "Qwen: Qwen3.5 Plus"},
                                    {"id": "broken"}, {"id": "x/y", "created": 1793750400, "hugging_face_id": "X/Y"}]})
        got = sources.openrouter(lambda url: (200, text))
        self.assertEqual([(m.id, m.hugging_face_id) for m in got], [("qwen/qwen3.5-plus", None), ("x/y", "X/Y")])
        self.assertEqual(got[0].created.date(), date(2026, 11, 4))

    def test_non_200_is_a_source_error(self):
        with self.assertRaises(sources.SourceError):
            sources.openrouter(lambda url: (429, ""))


class CoveredTest(unittest.TestCase):
    def test_map_and_news_folder_flag_but_near_miss_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "map.csv").write_text("news_url,query,type\n/news/qwen3-5-launch/,qwen 3.5 open weights,coverage\n")
            (root / "news").mkdir()
            (root / "news" / "deepseek-v4-release.md").write_text("x")
            items = covered.load({"HARNESS_SEO_MAP": str(root / "map.csv"), "HARNESS_SITE_NEWS_DIR": str(root / "news")})
        self.assertEqual(covered.maybe_covered("Qwen/Qwen3.5-397B-A17B", items), ("qwen3-5-launch",))
        self.assertEqual(covered.maybe_covered("deepseek-ai/DeepSeek-V4", items), ("deepseek-v4-release",))
        self.assertEqual(covered.maybe_covered("moonshotai/Kimi-K3", items), ())

    def test_nothing_set_is_empty(self):
        self.assertEqual(covered.load({}), [])


class StateTest(unittest.TestCase):
    def test_mark_seen_keeps_the_first_day(self):
        seen = state.mark_seen({}, ["a"], date(2026, 11, 3))
        self.assertEqual(state.mark_seen(seen, ["a", "b"], date(2026, 11, 4)), {"a": "2026-11-03", "b": "2026-11-04"})

    def test_round_trip_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.assertEqual((state.read_seen(folder), state.read_rejected(folder)), ({}, set()))
            state.write_json(folder / "seen.json", {"a": "2026-11-03"})
            self.assertEqual(state.read_seen(folder), {"a": "2026-11-03"})
            (folder / "rejected.json").write_text('["qwen/qwen-3-5"]')
            self.assertEqual(state.read_rejected(folder), {"qwen/qwen-3-5"})
            (folder / "seen.json").write_text("{not json")
            with self.assertRaises(state.StateError):
                state.read_seen(folder)

    def test_radar_dir(self):
        self.assertEqual(state.radar_dir({"HARNESS_RADAR_DIR": "/r", "HARNESS_DAILY_LOG_DIR": "/a/logs"}), Path("/r"))
        self.assertEqual(state.radar_dir({"HARNESS_DAILY_LOG_DIR": "/a/logs"}), Path("/a/radar"))
        self.assertEqual(state.radar_dir({}).name, "harness-radar")


class ConfigTest(unittest.TestCase):
    def test_committed_config_loads_and_every_org_has_an_hf_author(self):
        cfg = config.load()
        self.assertTrue(all(o.hf_author for o in cfg.orgs))
        self.assertEqual({o.hf_author for o in cfg.orgs if not o.verified},
                         {"LiquidAI", "tencent", "MiniMaxAI", "CohereLabs", "ByteDance-Seed", "XiaomiMiMo"})
        self.assertEqual(len(cfg.orgs), 18)
        self.assertEqual(cfg.settings.cap, 3)

    def test_bad_config_is_refused(self):
        for raw in ({"orgs": []}, {"orgs": [{"name": "x"}]}, {"orgs": [{"name": "x", "hf_author": "x"}],
                                                             "settings": {"cap": -1}},
                    {"orgs": [{"name": "x", "hf_author": "x", "feeds": [{"url": "u", "kind": "html"}]}]}):
            with self.assertRaises(config.ConfigError, msg=raw):
                config.parse(raw)


class DecodeBodyTest(unittest.TestCase):
    def test_gzip_body_is_unpacked_without_an_encoding_header(self):
        import gzip
        text = "<?xml version='1.0'?><rss><channel><title>Feed</title></channel></rss>"
        self.assertEqual(sources.decode_body(gzip.compress(text.encode())), text)

    def test_plain_body_is_unchanged_and_bad_gzip_is_empty(self):
        self.assertEqual(sources.decode_body(b"<rss/>"), "<rss/>")
        self.assertEqual(sources.decode_body(b"\x1f\x8bnot really gzip"), "")


if __name__ == "__main__":
    unittest.main()
