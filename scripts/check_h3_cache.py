"""Compare local H3 snapshot blob identities with live Hub metadata (no downloads)."""
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

model = "MiniMaxAI/MiniMax-H3"
endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
cache = Path(os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE") or str(Path(os.environ.get("HF_HOME", str(Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "huggingface"))) / "hub"))
repo = cache / "models--MiniMaxAI--MiniMax-H3"
headers = {"User-Agent": "h3-cache-revision-check"}
token = os.environ.get("HF_TOKEN")
if token:
    headers["Authorization"] = "Bearer " + token

def metadata(revision):
    request = Request(f"{endpoint}/api/models/{model}/revision/{revision}?blobs=true", headers=headers)
    with urlopen(request, timeout=60) as response:
        return json.load(response)

def files(info):
    return {f["rfilename"]: (f.get("lfs") or {}).get("sha256", f.get("blobId")) for f in info["siblings"]}

latest = metadata("main")
latest_files = files(latest)
report = {"model": model, "endpoint": endpoint, "cache": str(cache), "latest_revision": latest["sha"], "refs": {}, "snapshots": []}
if (repo / "refs").exists():
    report["refs"] = {str(p.relative_to(repo / "refs")): p.read_text().strip() for p in (repo / "refs").rglob("*") if p.is_file()}
snapshots = sorted((repo / "snapshots").iterdir()) if (repo / "snapshots").is_dir() else []
for snapshot in snapshots:
    if not snapshot.is_dir():
        continue
    old = files(metadata(snapshot.name))
    local = {str(p.relative_to(snapshot)): p for p in snapshot.rglob("*") if p.is_file() or p.is_symlink()}
    changed = sorted(k for k in old.keys() & latest_files.keys() if old[k] != latest_files[k])
    mismatched = sorted(k for k, p in local.items() if k in old and p.is_symlink() and p.resolve().name != old[k])
    report["snapshots"].append({"revision": snapshot.name, "matches_latest_revision": snapshot.name == latest["sha"], "cached_file_count": len(local), "missing_local_files": sorted(set(old) - set(local)), "broken_links": sorted(k for k, p in local.items() if not p.exists()), "blob_identity_mismatches": mismatched, "added_upstream": sorted(set(latest_files) - set(old)), "removed_upstream": sorted(set(old) - set(latest_files)), "changed_upstream": changed})
print(json.dumps(report, indent=2), flush=True)
Path("h3-cache-revision-report.json").write_text(json.dumps(report, indent=2) + "\n")
if not snapshots:
    raise SystemExit("No H3 snapshots found in this agent cache")
