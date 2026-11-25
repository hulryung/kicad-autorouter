#!/usr/bin/env python3
"""Publish the current version to the PCM distribution repository.

KiCad's Plugin and Content Manager fetches
metadata and zips without credentials, so a separate public repository
holds only what the PCM needs: repository.json, packages.json, resources.zip
(the icon) and the package zips. This script builds the package, writes the
metadata and pushes it there.

    python3 scripts/publish-pcm.py [--repo hulryung/kicad-autorouter-pcm] [--dir ../kicad-autorouter-pcm] [--no-push]

Users then add this URL once in PCM > Manage > Add Repository:
    https://raw.githubusercontent.com/hulryung/kicad-autorouter-pcm/main/repository.json
"""
import argparse
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


def validate_checkout(directory, repository):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(directory), *args], text=True).strip()
    expected = {f'https://github.com/{repository}', f'https://github.com/{repository}.git',
                f'git@github.com:{repository}.git', f'ssh://git@github.com/{repository}.git'}
    for args in (('remote', 'get-url', 'origin'), ('remote', 'get-url', '--push', 'origin')):
        if git(*args) not in expected:
            raise RuntimeError('Distribution origin does not match requested GitHub repository')
    if git('branch', '--show-current') != 'main':
        raise RuntimeError('Distribution checkout must be on main')


def prepare_catalogue(path, metadata, package, raw):
    """Fail closed on corrupt catalogues and immutable-version conflicts."""
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'packages': []}
    if not isinstance(data, dict) or not isinstance(data.get('packages'), list):
        raise ValueError('Invalid PCM catalogue: expected packages list')
    rows = data['packages']
    ids = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('identifier'), str) or not isinstance(row.get('versions'), list):
            raise ValueError('Invalid PCM package entry')
        versions = row['versions']
        if any(not isinstance(v, dict) or not isinstance(v.get('version'), str) for v in versions):
            raise ValueError('Invalid PCM version entry')
        if len({v['version'] for v in versions}) != len(versions):
            raise ValueError('Duplicate PCM versions')
        ids.append(row['identifier'])
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate PCM identifiers')
    ident = metadata['identifier']
    version = metadata['versions'][0]['version']
    old = next((row for row in rows if row['identifier'] == ident), None)
    versions = old['versions'] if old else []
    for previous in versions:
        if previous['version'] == version and previous.get('download_sha256') != package['download_sha256']:
            raise RuntimeError(f'Published package bytes differ for {version}; bump the version')
    entry = dict(metadata)
    entry.setdefault('maintainer', metadata['author'])
    entry['versions'] = [{**metadata['versions'][0],
                          'download_url': f"{raw}/packages/{Path(package['zip']).name}",
                          **{k: package[k] for k in ('download_sha256', 'download_size', 'install_size')}}] + [
                              v for v in versions if v['version'] != version]
    data['packages'] = [entry if row['identifier'] == ident else row for row in rows]
    if old is None:
        data['packages'].append(entry)
    return data


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default=DEFAULT_DIST)
    parser.add_argument('--dir', type=Path, default=REPO.parent / 'kicad-autorouter-pcm')
    parser.add_argument('--no-push', action='store_true')
    args = parser.parse_args(argv[1:])
    dist_repo, dist_dir, push = args.repo, args.dir, not args.no_push
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', dist_repo):
        raise ValueError('Expected a GitHub owner/repository')
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
    validate_checkout(dist_dir, dist_repo)
    subprocess.run(["git", "-C", str(dist_dir), "pull", "-q", "--ff-only", "origin", "main"], check=True)
    # Validate all release/catalogue inputs before modifying distribution files.
    metadata = json.loads((REPO / "metadata.json").read_text(encoding="utf-8"))
    ident = metadata["identifier"]
    packages_file = dist_dir / "packages.json"
    catalogue = prepare_catalogue(packages_file, metadata, package, raw)
    packages_dir = dist_dir / "packages"
    destination = packages_dir / zip_path.name
    if destination.exists() and sha256(destination) != sha256(zip_path):
        raise RuntimeError(f"Published package bytes differ for {version}; bump the version")
    resources = dist_dir / "resources.zip"
    icons = {}
    if resources.exists():
        with zipfile.ZipFile(resources) as archive:
            icons = {name: archive.read(name) for name in archive.namelist()}
    icons[f"{ident}/icon.png"] = (REPO / "resources" / "icon.png").read_bytes()
    packages_dir.mkdir(exist_ok=True)
    shutil.copy(zip_path, destination)
    with zipfile.ZipFile(resources, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(icons.items()):
            archive.writestr(name, data)
    packages_file.write_text(json.dumps(catalogue, indent=2) + "\n", encoding="utf-8")

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
