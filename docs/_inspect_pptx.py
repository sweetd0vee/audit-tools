"""Extract new pptx text for review."""
from pathlib import Path
from zipfile import ZipFile
import xml.etree.ElementTree as ET
import re

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
pptx = Path(__file__).resolve().parent / "Аудитор_ИИ-агент_презентация.pptx"
out = []
with ZipFile(pptx) as z:
    slides = sorted(
        [n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)],
        key=lambda x: int(re.search(r"slide(\d+)", x).group(1)),
    )
    out.append(f"Total slides: {len(slides)}")
    for i, name in enumerate(slides, 1):
        root = ET.fromstring(z.read(name))
        texts = [t.text for t in root.iter(A + "t") if t.text]
        out.append(f"\n========== SLIDE {i} ==========")
        out.append("\n".join(texts))
Path(__file__).resolve().parent.joinpath("_pptx_extract.txt").write_text(
    "\n".join(out), encoding="utf-8"
)
print(len(slides))
