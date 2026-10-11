"""Downloading a file with progress, cancellation and a checksum."""
import hashlib
import os
import shutil
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

USER_AGENT = "kicad-autorouter"
CHUNK = 256 * 1024

ProgressFn = Callable[[int, Optional[int]], None]      # (bytes so far, total or None)
CancelFn = Callable[[], bool]


class DownloadCancelled(Exception):
    pass


class ChecksumMismatch(Exception):
    pass


def fetch_json(url: str, timeout: float = 30.0):
    import json
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def download(url: str, target: Path, sha256: Optional[str] = None,
             progress: Optional[ProgressFn] = None,
             cancelled: Optional[CancelFn] = None) -> Path:
    """Download url to target (through a .part file). Verifies sha256 when given."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(part, "wb") as out:
            total = response.headers.get("Content-Length")
            total = int(total) if total and total.isdigit() else None
            done = 0
            while True:
                if cancelled and cancelled():
                    raise DownloadCancelled()
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    if sha256 and digest.hexdigest().lower() != sha256.lower():
        part.unlink(missing_ok=True)
        raise ChecksumMismatch(f"{target.name}: expected sha256 {sha256}, got {digest.hexdigest()}")
    os.replace(part, target)
    return target


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member(base: Path, name: str) -> bool:
    """Refuse archive members that would land outside base."""
    dest = (base / name).resolve()
    return base.resolve() == dest or base.resolve() in dest.parents


def extract_archive(archive: Path, into: Path) -> None:
    """Extract a .tar.gz or .zip into a fresh directory, refusing path escapes."""
    if into.exists():
        shutil.rmtree(into)
    into.mkdir(parents=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                if not _safe_member(into, member.filename):
                    raise ValueError(f"unsafe path in archive: {member.filename}")
            zf.extractall(into)
    else:
        with tarfile.open(archive, "r:*") as tf:
            for member in tf.getmembers():
                if not _safe_member(into, member.name) or member.islnk():
                    raise ValueError(f"unsafe path in archive: {member.name}")
                if member.issym() and not _safe_member(into, os.path.join(
                        os.path.dirname(member.name), member.linkname)):
                    raise ValueError(f"unsafe symlink in archive: {member.name}")
            tf.extractall(into)
