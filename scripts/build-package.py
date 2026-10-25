#!/usr/bin/env python3
"""Build the Plugin and Content Manager zip.

    python3 scripts/build-package.py [<outdir>]        (default: release)

The zip holds metadata.json, resources/icon.png and plugins/autorouter; the
PCM installs the plugins/ tree under <KiCad docs>/<version>/3rdparty/plugins/.
Prints the fields a repository.json needs (sha256, sizes).
"""
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "plugins" / "autorouter"
IGNORED = ("__pycache__", ".DS_Store", ".pyc", ".devreload")


def version() -> str:
    text = (PACKAGE / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'^__version__ = "([^"]+)"', text, re.M).group(1)


def files():
    yield REPO / "metadata.json", "metadata.json"
    yield REPO / "LICENSE", "plugins/autorouter/LICENSE"
    yield REPO / "resources" / "icon.png", "resources/icon.png"
    for path in sorted(PACKAGE.rglob("*")):
        if path.is_file() and not any(part in IGNORED or part.endswith(".pyc") for part in path.parts):
            yield path, f"plugins/{path.relative_to(PACKAGE.parent).as_posix()}"


def build_package(outdir):
    """Build deterministic bytes, shared by local builds and PCM publication."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    metadata = json.loads((REPO / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["versions"][0]["version"] == version(), "metadata.json and __init__.py disagree on the version"
    target = outdir / f"kicad-autorouter-{version()}.zip"
    install_size = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, name in files():
            data = path.read_bytes()
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            zf.writestr(entry, data)
            install_size += len(data)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return {"zip": str(target), "download_sha256": digest,
            "download_size": target.stat().st_size, "install_size": install_size}


def main(argv):
    print(json.dumps(build_package(argv[1] if len(argv) > 1 else "release"), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
