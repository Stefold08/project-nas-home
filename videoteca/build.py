#!/usr/bin/env python3
"""Costruisce ../installa-videoteca.sh incorporando videoteca.py, rete.py e index.html.

Dopo ogni modifica:  python3 videoteca/build.py
"""
from pathlib import Path

here = Path(__file__).resolve().parent
template = (here / "installer-template.sh").read_text(encoding="utf-8")
app = (here / "videoteca.py").read_text(encoding="utf-8").rstrip("\n")
page = (here / "index.html").read_text(encoding="utf-8").rstrip("\n")
net = (here / "rete.py").read_text(encoding="utf-8").rstrip("\n")

for name, content, delim in (("videoteca.py", app, "VIDEOTECA_PY_FINE"), ("index.html", page, "VIDEOTECA_HTML_FINE"),
                             ("rete.py", net, "VIDEOTECA_RETE_FINE")):
    if any(line.strip() == delim for line in content.splitlines()):
        raise SystemExit(f"{name} contiene il delimitatore {delim}")
    if "\r" in content:
        raise SystemExit(f"{name} contiene ritorni a capo di Windows")

out = template.replace("@@VIDEOTECA_PY@@", app).replace("@@INDEX_HTML@@", page).replace("@@RETE_PY@@", net)
target = here.parent / "installa-videoteca.sh"
target.write_text(out, encoding="utf-8", newline="\n")
target.chmod(0o755)
print(f"Creato {target} ({target.stat().st_size // 1024} KB)")
