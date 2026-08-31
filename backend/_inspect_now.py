import re
import zipfile
from pathlib import Path

f = next(
    Path(r"c:\Users\audit\Work\Arina\2026\audit-tools\backend\data\audit_cases\78899a2d8dab\reports").glob(
        "zakluchenie*.docx"
    )
)
print("file", f)
print("mtime", f.stat().st_mtime, "size", f.stat().st_size)
with zipfile.ZipFile(f) as z:
    print("parts", [n for n in z.namelist() if "style" in n.lower() or n.endswith(".xml") and n.startswith("word/")])
    effects = z.read("word/stylesWithEffects.xml").decode("utf-8")
    styles = z.read("word/styles.xml").decode("utf-8")
    doc = z.read("word/document.xml").decode("utf-8")
    foot1 = z.read("word/footer1.xml").decode("utf-8") if "word/footer1.xml" in z.namelist() else ""
    foot2 = z.read("word/footer2.xml").decode("utf-8") if "word/footer2.xml" in z.namelist() else ""

print("styles==effects", styles == effects)
print("effects has 276", 'w:line="276"' in effects)
print("effects has after200", 'w:after="200"' in effects)
print("styles has 276", 'w:line="276"' in styles)
print("doc has 360", 'w:line="360"' in doc)
print("doc has 276", 'w:line="276"' in doc)
print("docGrid", "w:docGrid" in doc)
print("unique doc spacing:")
for s in sorted(set(re.findall(r"<w:spacing[^/]*/>", doc))):
    print(" ", s)
print("pStyle values", sorted(set(re.findall(r'w:val="([^"]+)"', "\n".join(re.findall(r"<w:pStyle[^/]*/>", doc))))))
