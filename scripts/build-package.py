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
IGNORED = ("__pycache__", ".DS_Store", ".pyc")


def version() -> str:
    text = (PACKAGE / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'^__version__ = "([^"]+)"', text, re.M).group(1)


def files():
    yield REPO / "metadata.json", "metadata.json"
    yield REPO / "resources" / "icon.png", "resources/icon.png"
    for path in sorted(PACKAGE.rglob("*")):
        if path.is_file() and not any(part in IGNORED or part.endswith(".pyc") for part in path.parts):
            yield path, f"plugins/{path.relative_to(PACKAGE.parent).as_posix()}"


def main(argv):
    outdir = Path(argv[1] if len(argv) > 1 else "release")
    outdir.mkdir(parents=True, exist_ok=True)
    metadata = json.loads((REPO / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["versions"][0]["version"] == version(), "metadata.json and __init__.py disagree on the version"
    target = outdir / f"kicad-autorouter-{version()}.zip"
    install_size = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, name in files():
            zf.write(path, name)
            install_size += path.stat().st_size
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    print(json.dumps({"zip": str(target), "download_sha256": digest,
                      "download_size": target.stat().st_size, "install_size": install_size}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
