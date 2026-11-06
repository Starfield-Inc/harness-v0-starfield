"""A fake `claude` for the Write this up tests. A test writes a sh wrapper that runs
`python claude.py <control.json> <args...>`, since the draft step gives claude a scrubbed environment and no
FAKE_* variable reaches it.

control.json: {"modes": [...], "fixtures": dir, "log": path, "env_out": path, "pid_out": path, "release": path}.
Call n uses modes[n-1], or the last mode. Each call appends {"argv", "cwd", "prompt"} to log and writes the
environment it saw to env_out. Modes:
  good    copy page.md, row.json and claims.md from fixtures/good into the current folder
  bad     the same from fixtures/bad
  block   wait for the release file, then good
  hang    write its pid and a child's pid to pid_out, then sleep
  exit    exit 1 with no result
  budget  print an error_max_budget_usd result and exit 1
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

RESULT = {"type": "result", "subtype": "success", "is_error": False, "result": "done", "num_turns": 3}


def main(argv):
    control = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    prompt = sys.stdin.read()
    log = Path(control["log"])
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": argv[1:], "cwd": os.getcwd(), "prompt": prompt}) + "\n")
    Path(control["env_out"]).write_text(json.dumps(dict(os.environ), indent=2), encoding="utf-8")
    modes = control["modes"]
    mode = modes[min(len(calls), len(modes) - 1)]
    if mode == "block":
        while not Path(control["release"]).exists():
            time.sleep(0.05)
        mode = "good"
    if mode in ("good", "bad"):
        for name in ("page.md", "row.json", "claims.md"):
            shutil.copyfile(Path(control["fixtures"]) / mode / name, name)
        print(json.dumps(RESULT))
        return 0
    if mode == "hang":
        child = subprocess.Popen(["sleep", "600"])
        Path(control["pid_out"]).write_text(f"{os.getpid()} {child.pid}\n")
        time.sleep(600)
        return 0
    if mode == "budget":
        print(json.dumps({**RESULT, "subtype": "error_max_budget_usd", "is_error": True, "result": ""}))
        return 1
    sys.stderr.write("fake claude: failed\n")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
