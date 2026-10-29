import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

from harness import run_record as rr

SHA = "44ff438eaa2742fd6bbc70b09b41940dd9f66cf43022230b43fc8770ee83db8a"
V100_BYTES = 34089730048


class FakeNVMLError(Exception):
    pass


def fake_nvml(count=1, name="Tesla V100-SXM2-32GB", driver="570.86.15", cuda=12080,
              fail_on=None):
    log = []

    def call(label, value):
        def f(*args):
            if fail_on == label:
                raise FakeNVMLError(label)
            return value
        return f

    ns = SimpleNamespace(
        nvmlInit=lambda: log.append("init"),
        nvmlShutdown=lambda: log.append("shutdown"),
        nvmlDeviceGetCount=call("count", count),
        nvmlDeviceGetHandleByIndex=lambda i: i,
        nvmlDeviceGetName=call("name", name),
        nvmlDeviceGetMemoryInfo=call("mem", SimpleNamespace(total=V100_BYTES)),
        nvmlDeviceGetCudaComputeCapability=call("cc", (7, 0)),
        nvmlSystemGetDriverVersion=call("driver", driver),
        nvmlSystemGetCudaDriverVersion_v2=call("cuda", cuda),
    )
    ns.log = log
    return ns


class ReadHardwareTest(unittest.TestCase):
    def test_reads_every_hardware_field(self):
        nvml = fake_nvml()
        hw = rr.read_hardware(nvml)
        self.assertEqual(hw.driver_version, "570.86.15")
        self.assertEqual(hw.cuda_driver_version, "12.8")
        self.assertEqual(len(hw.gpus), 1)
        gpu = hw.gpus[0]
        self.assertEqual(gpu.name, "Tesla V100-SXM2-32GB")
        self.assertEqual(gpu.total_vram_bytes, V100_BYTES)
        self.assertEqual(gpu.compute_capability, "7.0")
        self.assertEqual(nvml.log, ["init", "shutdown"])

    def test_records_only_the_gpus_named_in_cuda_visible_devices(self):
        with unittest.mock.patch.dict("os.environ", {"CUDA_VISIBLE_DEVICES": "0"}):
            hw = rr.read_hardware(fake_nvml(count=8))
        self.assertEqual(len(hw.gpus), 1)
        with unittest.mock.patch.dict("os.environ", {"CUDA_VISIBLE_DEVICES": "2,5"}):
            hw = rr.read_hardware(fake_nvml(count=8))
        self.assertEqual(len(hw.gpus), 2)

    def test_records_every_gpu_when_cuda_visible_devices_is_not_set(self):
        with unittest.mock.patch.dict("os.environ", clear=True):
            hw = rr.read_hardware(fake_nvml(count=8))
        self.assertEqual(len(hw.gpus), 8)

    def test_cuda_visible_devices_that_cannot_be_read_fails_loudly(self):
        for bad in ("GPU-1234-abcd", "9", "", "0,0"):
            with self.subTest(bad=bad), unittest.mock.patch.dict("os.environ", {"CUDA_VISIBLE_DEVICES": bad}):
                with self.assertRaises(rr.HardwareInfoError) as cm:
                    rr.read_hardware(fake_nvml(count=8))
                self.assertIn("CUDA_VISIBLE_DEVICES", str(cm.exception))

    def test_cuda_version_decoding(self):
        self.assertEqual(rr.cuda_version_str(12090), "12.9")
        self.assertEqual(rr.cuda_version_str(13000), "13.0")

    def test_no_gpu_fails_loudly(self):
        with self.assertRaisesRegex(rr.HardwareInfoError, "0 GPUs"):
            rr.read_hardware(fake_nvml(count=0))

    def test_empty_values_fail_loudly_and_name_the_field(self):
        with self.assertRaisesRegex(rr.HardwareInfoError, "gpu name"):
            rr.read_hardware(fake_nvml(name=""))
        with self.assertRaisesRegex(rr.HardwareInfoError, "driver version"):
            rr.read_hardware(fake_nvml(driver=""))

    def test_nvml_errors_pass_through_and_nvml_is_still_shut_down(self):
        nvml = fake_nvml(fail_on="mem")
        with self.assertRaises(FakeNVMLError):
            rr.read_hardware(nvml)
        self.assertEqual(nvml.log[-1], "shutdown")


def hardware():
    return rr.read_hardware(fake_nvml())


def record(**over):
    args = dict(
        run_id="r-001", run_date_utc="2026-10-22T10:00:00Z", hardware=hardware(),
        engine="vllm", engine_version="0.19.1", cuda_runtime_version="12.9",
        model_id="Qwen/Qwen3-8B", model_revision="abc123", precision="fp16",
        prompt_set_id="model-watch-prompts-v1", prompt_set_sha256=SHA,
        settings={"max_concurrency": 8, "repeats": 3},
    )
    args.update(over)
    return rr.make_record(**args)


class MakeRecordTest(unittest.TestCase):
    def test_complete_record_is_accepted(self):
        self.assertEqual(record().engine, "vllm")

    def test_each_empty_required_field_is_refused_by_name(self):
        for field in ("run_id", "engine_version", "cuda_runtime_version", "model_id",
                      "model_revision", "precision"):
            with self.assertRaisesRegex(rr.RecordError, field):
                record(**{field: ""})

    def test_date_must_be_utc_iso(self):
        with self.assertRaisesRegex(rr.RecordError, "run_date_utc"):
            record(run_date_utc="22 Oct 2026")

    def test_empty_settings_are_refused(self):
        with self.assertRaisesRegex(rr.RecordError, "settings"):
            record(settings={})

    def test_vllm_needs_the_prompt_set_hash(self):
        with self.assertRaisesRegex(rr.RecordError, "prompt_set_sha256"):
            record(prompt_set_sha256="")
        with self.assertRaisesRegex(rr.RecordError, "prompt_set_sha256"):
            record(prompt_set_sha256="not-a-hash")

    def test_llama_cpp_must_not_claim_a_prompt_set(self):
        ok = record(engine="llama.cpp", engine_version="b11753", prompt_set_id="",
                    prompt_set_sha256="")
        self.assertEqual(ok.prompt_set_sha256, "")
        with self.assertRaisesRegex(rr.RecordError, "prompt_set"):
            record(engine="llama.cpp", engine_version="b11753")

    def test_unknown_engine_is_refused(self):
        with self.assertRaisesRegex(rr.RecordError, "engine"):
            record(engine="tgi")


class HardwareSanityTest(unittest.TestCase):
    def test_zero_vram_or_zero_cuda_version_is_refused(self):
        nvml = fake_nvml()
        nvml.nvmlDeviceGetMemoryInfo = lambda h: SimpleNamespace(total=0)
        with self.assertRaisesRegex(rr.HardwareInfoError, "vram"):
            rr.read_hardware(nvml)
        with self.assertRaisesRegex(rr.HardwareInfoError, "CUDA"):
            rr.read_hardware(fake_nvml(cuda=0))


class GitStateTest(unittest.TestCase):
    def test_reports_the_commit_and_whether_the_tree_is_dirty(self):
        state = rr.git_state(Path(__file__).resolve().parent.parent)
        self.assertRegex(state["harness_commit"], r"^[0-9a-f]{40}$")
        self.assertIsInstance(state["harness_dirty"], bool)

    def test_outside_a_git_repo_fails_loudly(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(rr.RecordError):
                rr.git_state(d)


class WriteRecordTest(unittest.TestCase):
    def test_json_has_every_required_field_and_no_gpu_uuid(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "vllm-r-001.record.json"
            rr.write_record(path, record())
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["hardware"]["driver_version"], "570.86.15")
        self.assertEqual(data["hardware"]["cuda_driver_version"], "12.8")
        self.assertEqual(data["hardware"]["gpus"][0]["name"], "Tesla V100-SXM2-32GB")
        self.assertEqual(data["hardware"]["gpus"][0]["total_vram_bytes"], V100_BYTES)
        for key in ("run_id", "run_date_utc", "engine", "engine_version",
                    "cuda_runtime_version", "model_id", "model_revision", "precision",
                    "prompt_set_id", "prompt_set_sha256", "settings"):
            self.assertIn(key, data)
        self.assertNotIn("uuid", json.dumps(data).lower())

    def test_same_record_gives_identical_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "a.json", Path(d) / "b.json"
            rr.write_record(a, record())
            rr.write_record(b, record())
            self.assertEqual(a.read_bytes(), b.read_bytes())


if __name__ == "__main__":
    unittest.main()
