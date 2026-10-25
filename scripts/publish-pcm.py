#!/usr/bin/env python3
"""Publish the current version to the PCM distribution repository.

The code repository is private; KiCad's Plugin and Content Manager fetches
metadata and zips without credentials, so a separate public repository
holds only what the PCM needs: repository.json, packages.json, resources.zip
(the icon) and the package zips. This script builds the package, writes the
metadata and pushes it there.

    python3 scripts/publish-pcm.py [--repo hulryung/kicad-autorouter-pcm] [--dir ../kicad-autorouter-pcm] [--no-push]

Users then add this URL once in PCM > Manage > Add Repository:
    https://raw.githubusercontent.com/hulryung/kicad-autorouter-pcm/main/repository.json
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
DEFAULT_DIST = "hulryung/kicad-autorouter-pcm"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv):
    dist_repo = DEFAULT_DIST
    dist_dir = REPO.parent / "kicad-autorouter-pcm"
    push = True
    args = argv[1:]
    while args:
        a = args.pop(0)
        if a == "--repo":
            dist_repo = args.pop(0)
        elif a == "--dir":
            dist_dir = Path(args.pop(0))
        elif a == "--no-push":
            push = False
    raw = f"https://raw.githubusercontent.com/{dist_repo}/main"

    # 1. build the package zip
    import importlib
    build = importlib.import_module("build-package")
    out = REPO / "release"
    out.mkdir(exist_ok=True)
    version = build.version()
    package = build.build_package(out)
    zip_path = Path(package["zip"])
    install_size = package["install_size"]

    # 2. the distribution checkout
    if not (dist_dir / ".git").exists():
        dist_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["gh", "repo", "clone", dist_repo, str(dist_dir)], check=True)
    dirty = subprocess.check_output(["git", "-C", str(dist_dir), "status", "--porcelain"], text=True)
    if dirty.strip():
        raise RuntimeError(f"Distribution checkout has uncommitted changes: {dist_dir}")
    subprocess.run(["git", "-C", str(dist_dir), "pull", "-q", "--ff-only"], check=True)
    packages_dir = dist_dir / "packages"
    packages_dir.mkdir(exist_ok=True)
    shutil.copy(zip_path, packages_dir / zip_path.name)

    # 3. resources.zip: <identifier>/icon.png
    metadata = json.loads((REPO / "metadata.json").read_text(encoding="utf-8"))
    ident = metadata["identifier"]
    resources = dist_dir / "resources.zip"
    with zipfile.ZipFile(resources, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(REPO / "resources" / "icon.png", f"{ident}/icon.png")

    # 4. packages.json: this version plus any older ones already listed
    packages_file = dist_dir / "packages.json"
    old_versions = []
    if packages_file.exists():
        try:
            for pkg in json.loads(packages_file.read_text(encoding="utf-8")).get("packages", []):
                if pkg.get("identifier") == ident:
                    old_versions = [v for v in pkg.get("versions", []) if v.get("version") != version]
        except ValueError:
            pass
    entry = dict(metadata)
    entry.setdefault("maintainer", metadata["author"])
    entry["versions"] = [{
        **metadata["versions"][0],
        "version": version,
        "download_url": f"{raw}/packages/{zip_path.name}",
        "download_sha256": sha256(zip_path),
        "download_size": zip_path.stat().st_size,
        "install_size": install_size,
    }] + old_versions
    packages_file.write_text(json.dumps({"packages": [entry]}, indent=2) + "\n", encoding="utf-8")

    # 5. repository.json
    now = int(time.time())
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now))
    repository = {
        "$schema": "https://gitlab.com/kicad/code/kicad/-/raw/master/kicad/pcm/schemas/pcm.v1.schema.json#/definitions/Repository",
        "name": "KiCad Autorouter Repository",
        "maintainer": metadata["author"],
        "packages": {"url": f"{raw}/packages.json", "sha256": sha256(packages_file),
                     "update_time_utc": stamp, "update_timestamp": now},
        "resources": {"url": f"{raw}/resources.zip", "sha256": sha256(resources),
                      "update_time_utc": stamp, "update_timestamp": now},
    }
    (dist_dir / "repository.json").write_text(json.dumps(repository, indent=2) + "\n", encoding="utf-8")
    readme = dist_dir / "README.md"
    readme.write_text(f"""# KiCad Autorouter: PCM repository

Install the Autorouter plugin from KiCad's Plugin and Content Manager:

1. **Plugin and Content Manager → Manage… → Add Repository**, paste

       {raw}/repository.json

2. Pick **KiCad Autorouter Repository** in the repository dropdown, install **Autorouter**, **Apply Pending Changes**.

This repository only carries the installable packages and their metadata. Current version: {version}.
""", encoding="utf-8")

    # 6. commit and push
    subprocess.run(["git", "-C", str(dist_dir), "add", "-A"], check=True)
    status = subprocess.run(["git", "-C", str(dist_dir), "status", "--porcelain"], capture_output=True, text=True).stdout
    if status.strip():
        subprocess.run(["git", "-C", str(dist_dir), "commit", "-q", "-m", f"Autorouter {version}"], check=True)
    if push:
        subprocess.run(["git", "-C", str(dist_dir), "push", "-q", "origin", "HEAD:main"], check=True)
        print(f"Published {zip_path.name} ({zip_path.stat().st_size:,} bytes) to {dist_repo}")
    else:
        print(f"Prepared {zip_path.name} ({zip_path.stat().st_size:,} bytes) locally; not pushed to {dist_repo}")
    print(f"PCM repository URL: {raw}/repository.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
