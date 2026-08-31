import zipfile
from collections import Counter
from pathlib import Path

try:
    import win32com.client
except ImportError:
    print("NO_WIN32COM")
    raise SystemExit(0)

src = next(
    Path(r"c:\Users\audit\Work\Arina\2026\audit-tools\backend\data\audit_cases\78899a2d8dab\reports").glob(
        "zakluchenie*.docx"
    )
)
out = Path.home() / "AppData/Local/Temp/zakluchenie_word_all.docx"
out.write_bytes(src.read_bytes())

with zipfile.ZipFile(src) as z:
    settings = z.read("word/settings.xml").decode("utf-8")
print("SETTINGS", settings[:2500])
print("---compat---")
idx = settings.find("compat")
print(settings[idx : idx + 1200] if idx >= 0 else "NO COMPAT")

word = win32com.client.Dispatch("Word.Application")
word.Visible = False
doc = word.Documents.Open(str(out))
try:
    counts = Counter()
    weird = []
    for i in range(1, doc.Paragraphs.Count + 1):
        p = doc.Paragraphs(i)
        fmt = p.Format
        key = (
            round(float(fmt.SpaceBefore), 2),
            round(float(fmt.SpaceAfter), 2),
            int(fmt.LineSpacingRule),
            round(float(fmt.LineSpacing), 2),
            str(p.Style.NameLocal),
        )
        counts[key] += 1
        if key[0] != 0 or key[1] != 0 or key[2] != 0:
            text = (p.Range.Text or "").strip()[:80]
            weird.append((i, key, text))
    print("UNIQUE FORMATS", len(counts))
    for key, n in counts.most_common():
        print(n, key)
    print("WEIRD", len(weird))
    for row in weird[:20]:
        print(row)
    print("compat mode", doc.CompatibilityMode)
finally:
    doc.Close(False)
    word.Quit()
