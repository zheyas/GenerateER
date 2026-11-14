"""Проверка файлов страницы: импорты JS указывают на существующие файлы, синтаксис модулей корректен."""
import re
import shutil
import subprocess
from pathlib import Path

from common import Counter

T = Counter()
STATIC = Path(__file__).resolve().parents[1] / "er_editor" / "web" / "static"
for js in sorted(STATIC.glob("*.js")):
    src = js.read_text(encoding="utf-8")
    for m in re.finditer(r"from '\./([\w.-]+)'", src):
        T.check(f"{js.name}: импорт {m.group(1)}", (STATIC / m.group(1)).is_file())
    if shutil.which("node"):
        res = subprocess.run(["node", "--input-type=module", "--check"], input=src, capture_output=True, text=True)
        T.check(f"{js.name}: синтаксис", res.returncode == 0, res.stderr[:300])
html = (STATIC / "index.html").read_text(encoding="utf-8")
for ident in ("canvas", "panel", "toolbar", "inline", "welcome", "modal-root", "toast", "file", "status-text"):
    T.check(f"index.html: #{ident}", f'id="{ident}"' in html)
T.finish()
