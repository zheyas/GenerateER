#!/usr/bin/env python3
"""Запускает все автоматические проверки: ``python tests/run_all.py``. Код возврата — число упавших наборов."""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUITES = ["test_normalform.py", "test_normalform_random.py", "test_describe.py", "test_fdmine.py", "test_ondelete.py", "test_guard.py", "test_experiments.py", "test_editor_ui.py", "test_web_api.py", "test_web_static.py"]

if __name__ == "__main__":
    failed = 0
    for name in SUITES:
        res = subprocess.run([sys.executable, str(HERE / name)], capture_output=True, text=True)
        tail = [ln for ln in res.stdout.strip().splitlines() if ln.strip()][-1:] or ["(нет вывода)"]
        print(f"{'OK  ' if res.returncode == 0 else 'FAIL'} {name}: {tail[0]}")
        if res.returncode:
            failed += 1
            print(res.stdout[-2000:], res.stderr[-2000:])
    sys.exit(failed)
