import json
import urllib.request

req = urllib.request.Request(
    "http://localhost:8100/api/v1/cases/8d720e84d910/download",
    method="POST",
)
with urllib.request.urlopen(req, timeout=170) as resp:
    data = json.loads(resp.read().decode("utf-8"))

print("downloaded", data.get("downloaded"), "failed", data.get("failed"))
for doc in data.get("documents") or []:
    status = doc.get("download_status")
    if not doc.get("selected") and status in (None, "skipped"):
        continue
    title = (doc.get("title") or "")[:70]
    print(f"{status:10} {title}")
