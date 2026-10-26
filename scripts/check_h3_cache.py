"""Export H3 cache metadata offline; compare the exported report on a connected host."""
import argparse
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

MODEL = "MiniMaxAI/MiniMax-H3"


def export_cache():
    home = Path(os.environ.get("HF_HOME", str(Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "huggingface")))
    cache = Path(os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE") or str(home / "hub"))
    repo = cache / "models--MiniMaxAI--MiniMax-H3"
    refs = repo / "refs"
    report = {"model": MODEL, "cache": str(cache), "refs": {}, "snapshots": []}
    if refs.is_dir():
        report["refs"] = {str(p.relative_to(refs)): p.read_text().strip() for p in refs.rglob("*") if p.is_file()}
    root = repo / "snapshots"
    for snapshot in sorted(root.iterdir()) if root.is_dir() else []:
        if not snapshot.is_dir():
            continue
        files = {}
        for p in sorted(snapshot.rglob("*")):
            if not (p.is_file() or p.is_symlink()):
                continue
            exists = p.is_file()
            files[str(p.relative_to(snapshot))] = {
                "size": p.stat().st_size if exists else None,
                "blob_id": p.resolve().name if p.is_symlink() else None,
                "broken_link": p.is_symlink() and not exists,
            }
        report["snapshots"].append({"revision": snapshot.name, "files": files})
    return report


def compare_cache(report):
    endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    headers = {"User-Agent": "h3-cache-revision-check"}
    if os.environ.get("HF_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["HF_TOKEN"]

    def metadata(revision):
        request = Request(f"{endpoint}/api/models/{report['model']}/revision/{revision}?blobs=true", headers=headers)
        with urlopen(request, timeout=30) as response:
            info = json.load(response)
        return info["sha"], {f["rfilename"]: {"id": (f.get("lfs") or {}).get("sha256", f.get("blobId")), "size": f.get("size")} for f in info["siblings"]}

    latest_sha, latest = metadata("main")
    result = {"model": report["model"], "latest_revision": latest_sha, "refs": report["refs"], "snapshots": []}
    for snapshot in report["snapshots"]:
        sha = snapshot["revision"]
        _, old = metadata(sha) if sha != latest_sha else (latest_sha, latest)
        local = snapshot["files"]
        result["snapshots"].append({
            "revision": sha,
            "matches_latest_revision": sha == latest_sha,
            "cached_file_count": len(local),
            "missing_local_files": sorted(old.keys() - local.keys()),
            "extra_local_files": sorted(local.keys() - old.keys()),
            "broken_links": sorted(k for k, f in local.items() if f["broken_link"]),
            "blob_identity_mismatches": sorted(k for k in local.keys() & old.keys() if local[k]["blob_id"] and old[k]["id"] and local[k]["blob_id"] != old[k]["id"]),
            "size_mismatches": sorted(k for k in local.keys() & old.keys() if old[k]["size"] is not None and local[k]["size"] != old[k]["size"]),
            "unverified_blob_identity": sorted(k for k, f in local.items() if not f["blob_id"]),
            "added_upstream": sorted(latest.keys() - old.keys()),
            "removed_upstream": sorted(old.keys() - latest.keys()),
            "changed_upstream": sorted(k for k in old.keys() & latest.keys() if old[k] != latest[k]),
        })
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare", type=Path, help="Compare an exported JSON report with live Hub metadata")
    parser.add_argument("--output", type=Path, default=Path("h3-cache-revision-report.json"))
    args = parser.parse_args()
    report = compare_cache(json.loads(args.compare.read_text())) if args.compare else export_cache()
    text = json.dumps(report, indent=2)
    args.output.write_text(text + "\n")
    print(text, flush=True)
    if not report["snapshots"]:
        raise SystemExit("No H3 snapshots found in this agent cache")


if __name__ == "__main__":
    main()
