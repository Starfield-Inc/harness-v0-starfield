"""Run record: the hardware and build facts that must sit next to every result file."""
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

ENGINES = ("vllm", "llama.cpp")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class HardwareInfoError(RuntimeError):
    pass


class RecordError(ValueError):
    pass


@dataclass(frozen=True)
class Gpu:
    name: str
    total_vram_bytes: int
    compute_capability: str


@dataclass(frozen=True)
class HardwareInfo:
    driver_version: str
    cuda_driver_version: str
    gpus: tuple[Gpu, ...]


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    run_date_utc: str
    hardware: HardwareInfo
    engine: str
    engine_version: str
    cuda_runtime_version: str
    model_id: str
    model_revision: str
    precision: str
    prompt_set_id: str
    prompt_set_sha256: str
    settings: dict


def cuda_version_str(v: int) -> str:
    return f"{v // 1000}.{(v % 1000) // 10}"


def _required(field: str, value: str) -> str:
    if not value:
        raise HardwareInfoError(f"NVML returned an empty {field}")
    return value


def _visible_indices(count):
    """GPU indices the run can use. NVML lists every GPU on the machine and ignores
    CUDA_VISIBLE_DEVICES, so a run on one GPU of several would otherwise record all of them."""
    raw = os.environ.get("CUDA_VISIBLE_DEVICES")
    if raw is None:
        return range(count)
    try:
        indices = [int(part) for part in raw.split(",")]
    except ValueError:
        indices = []
    if not indices or len(set(indices)) != len(indices) or any(not 0 <= i < count for i in indices):
        raise HardwareInfoError(
            f"CUDA_VISIBLE_DEVICES={raw!r} is not a list of distinct GPU indices below {count}")
    return indices


def read_hardware(nvml=None) -> HardwareInfo:
    if nvml is None:
        import pynvml as nvml
    nvml.nvmlInit()
    try:
        count = nvml.nvmlDeviceGetCount()
        if count < 1:
            raise HardwareInfoError("NVML reports 0 GPUs")
        gpus = []
        for i in _visible_indices(count):
            h = nvml.nvmlDeviceGetHandleByIndex(i)
            major, minor = nvml.nvmlDeviceGetCudaComputeCapability(h)
            vram = nvml.nvmlDeviceGetMemoryInfo(h).total
            if vram <= 0:
                raise HardwareInfoError(f"NVML returned {vram} bytes of vram for GPU {i}")
            gpus.append(Gpu(
                name=_required("gpu name", nvml.nvmlDeviceGetName(h)),
                total_vram_bytes=vram,
                compute_capability=f"{major}.{minor}",
            ))
        # _v2 raises when libcuda is missing; v1 can return an invented version
        cuda = nvml.nvmlSystemGetCudaDriverVersion_v2()
        if cuda <= 0:
            raise HardwareInfoError(f"NVML returned CUDA driver version {cuda}")
        return HardwareInfo(
            driver_version=_required("driver version", nvml.nvmlSystemGetDriverVersion()),
            cuda_driver_version=cuda_version_str(cuda),
            gpus=tuple(gpus),
        )
    finally:
        nvml.nvmlShutdown()


def make_record(*, run_id, run_date_utc, hardware, engine, engine_version,
                cuda_runtime_version, model_id, model_revision, precision,
                prompt_set_id, prompt_set_sha256, settings) -> RunRecord:
    for name, value in (("run_id", run_id), ("engine_version", engine_version),
                        ("cuda_runtime_version", cuda_runtime_version),
                        ("model_id", model_id), ("model_revision", model_revision),
                        ("precision", precision)):
        if not value:
            raise RecordError(f"{name} must not be empty")
    if not _DATE.fullmatch(run_date_utc):
        raise RecordError(f"run_date_utc must look like 2026-10-22T10:00:00Z, got {run_date_utc!r}")
    if not isinstance(settings, dict) or not settings:
        raise RecordError("settings must be a non-empty dict")
    if engine not in ENGINES:
        raise RecordError(f"engine must be one of {ENGINES}, got {engine!r}")
    if engine == "vllm":
        if not prompt_set_id:
            raise RecordError("prompt_set_id must not be empty for vllm")
        if not _SHA256.fullmatch(prompt_set_sha256):
            raise RecordError("prompt_set_sha256 must be 64 lowercase hex characters for vllm")
    elif prompt_set_id or prompt_set_sha256:
        raise RecordError("prompt_set_id and prompt_set_sha256 must be empty for llama.cpp")
    return RunRecord(
        run_id=run_id, run_date_utc=run_date_utc, hardware=hardware, engine=engine,
        engine_version=engine_version, cuda_runtime_version=cuda_runtime_version,
        model_id=model_id, model_revision=model_revision, precision=precision,
        prompt_set_id=prompt_set_id, prompt_set_sha256=prompt_set_sha256, settings=settings,
    )


def record_path(out_dir, prefix: str, run_id: str) -> Path:
    return Path(out_dir) / f"{prefix}-{run_id}.record.json"


def write_record(path, record: RunRecord) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(asdict(record), sort_keys=True, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")


def git_state(repo_dir) -> dict:
    def git(*args):
        done = subprocess.run(["git", "-C", str(repo_dir), *args], capture_output=True, text=True)
        if done.returncode:
            raise RecordError(f"git {' '.join(args)} failed in {repo_dir}: {done.stderr.strip()}")
        return done.stdout.strip()

    return {"harness_commit": git("rev-parse", "HEAD"),
            "harness_dirty": bool(git("status", "--porcelain"))}
