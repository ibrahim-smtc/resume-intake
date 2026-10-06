"""Runs every tests/test_*.py script, each in its own process, and prints one line per script.

    python tests/run_all.py            all offline tests (a few seconds to a minute)
    RUN_LIVE=1 python tests/run_all.py  also the live checks against Questlight's dev services (test_live.py)

Exit code 0 only when every script passes.
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
failed = []
total_checks = 0

print(f"{'script':28} {'result':22} time")
for script in sorted(HERE.glob("test_*.py")):
    started = time.time()
    run = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, cwd=HERE.parent, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    out = run.stdout + run.stderr
    summary = re.findall(r"(\d+)/(\d+) checks passed", out)
    if run.returncode == 0 and summary:
        passed, count = map(int, summary[-1])
        total_checks += count
        result = f"{passed}/{count} passed"
    elif run.returncode == 0:
        result = "skipped" if "skipped" in out else "ok"
    else:
        result = "FAILED"
        failed.append((script.name, out))
    print(f"{script.name:28} {result:22} {time.time() - started:4.1f}s")

for name, out in failed:
    print(f"\n===== {name} =====")
    print("\n".join(line for line in out.splitlines() if line.startswith("BAD") or "Traceback" in line or "Error" in line)[-3000:] or out[-2000:])
print(f"\n{total_checks} checks in {len(list(HERE.glob('test_*.py'))) - len(failed)} scripts passed" + (f"; {len(failed)} script(s) FAILED" if failed else ""))
sys.exit(1 if failed else 0)
