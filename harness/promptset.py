"""Build the fixed prompt set from prompts/material.json.

Selection uses sha256 keys, not the random module, so the output does not
depend on the Python version.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
JSONL_NAME = "prompts.jsonl"
LONG_PASSAGES = 5


def _key(seed, bucket, i, slot, attempt):
    return hashlib.sha256(f"{seed}|{bucket}|{i}|{slot}|{attempt}".encode("utf-8")).hexdigest()


def _pick(seed, bucket, i, slot, attempt, n):
    return int(_key(seed, bucket, i, slot, attempt), 16) % n


def _record(bucket, i, prompt):
    return {
        "id": f"{bucket}-{i + 1:03d}",
        "bucket": bucket,
        "prompt": prompt,
        "words": len(prompt.split()),
    }


def _short(material, seed):
    prompts = material["short"]
    order = sorted(range(len(prompts)), key=lambda j: _key(seed, "short", j, 0, 0))
    return [_record("short", i, prompts[j]) for i, j in enumerate(order[: material["counts"]["short"]])]


def _medium(material, seed):
    paragraphs, instructions = material["paragraphs"], material["instructions"]
    used, records = set(), []
    for i in range(material["counts"]["medium"]):
        attempt = 0
        while True:
            p = _pick(seed, "medium", i, "paragraph", attempt, len(paragraphs))
            s = _pick(seed, "medium", i, "instruction", attempt, len(instructions))
            if (p, s) not in used:
                break
            attempt += 1
        used.add((p, s))
        records.append(_record("medium", i, paragraphs[p] + "\n\n" + instructions[s]))
    return records


def _long(material, seed):
    paragraphs, instructions = material["paragraphs"], material["instructions"]
    used, records = set(), []
    for i in range(material["counts"]["long"]):
        attempt = 0
        while True:
            ranked = sorted(
                range(len(paragraphs)),
                key=lambda p: _key(seed, "long", i, p, attempt),
            )[:LONG_PASSAGES]
            s = _pick(seed, "long", i, "instruction", attempt, len(instructions))
            if (tuple(sorted(ranked)), s) not in used:
                break
            attempt += 1
        used.add((tuple(sorted(ranked)), s))
        body = "\n\n".join(f"Passage {n}:\n{paragraphs[p]}" for n, p in enumerate(ranked, 1))
        records.append(_record("long", i, body + "\n\n" + instructions[s]))
    return records


def build(material, seed):
    return _short(material, seed) + _medium(material, seed) + _long(material, seed)


def to_jsonl(records):
    lines = (json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in records)
    return "".join(lines).encode("utf-8")


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def check(prompts_dir):
    prompts_dir = Path(prompts_dir)
    material = json.loads((prompts_dir / "material.json").read_text(encoding="utf-8"))
    built = to_jsonl(build(material, 0))
    problems = []
    jsonl = prompts_dir / JSONL_NAME
    if not jsonl.exists():
        return [f"{JSONL_NAME} is missing"]
    if jsonl.read_bytes() != built:
        problems.append(f"{JSONL_NAME} differs from a rebuild at seed 0")
    hash_file = prompts_dir / f"{JSONL_NAME}.sha256"
    if not hash_file.exists():
        problems.append(f"{hash_file.name} is missing")
    elif hash_file.read_text(encoding="utf-8") != f"{sha256_hex(jsonl.read_bytes())}  {JSONL_NAME}\n":
        problems.append(f"{hash_file.name} does not match {JSONL_NAME}")
    return problems


def main():
    parser = argparse.ArgumentParser(prog="python3 -m harness.promptset")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        problems = check(PROMPTS_DIR)
        for p in problems:
            print(p)
        if problems:
            sys.exit(1)
        print("ok")
        return
    material = json.loads((PROMPTS_DIR / "material.json").read_text(encoding="utf-8"))
    data = to_jsonl(build(material, args.seed))
    (PROMPTS_DIR / JSONL_NAME).write_bytes(data)
    (PROMPTS_DIR / f"{JSONL_NAME}.sha256").write_text(f"{sha256_hex(data)}  {JSONL_NAME}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
