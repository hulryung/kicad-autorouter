"""Finding a Java runtime that can run the Freerouting jar, or installing one.

Search order: the path from the plugin settings, JAVA_HOME, java on PATH, a
runtime the plugin installed earlier, then the usual vendor locations. When
nothing recent enough is found, a Temurin JRE can be installed into the
plugin's data directory from the Adoptium API (no admin rights, nothing
touched outside that directory).
"""
import glob
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from .download import download, extract_archive, fetch_json
from .paths import data_dir, platform_arch, platform_os

ADOPTIUM_API = "https://api.adoptium.net/v3/assets/latest/{major}/hotspot"
_VERSION = re.compile(r'version "(\d+)(?:\.(\d+))?')


@dataclass(frozen=True)
class JavaInfo:
    path: str
    major: int

    def __str__(self):
        return f"Java {self.major} ({self.path})"


def parse_java_version(output: str) -> Optional[int]:
    """The major version from `java -version` output; 1.8 is 8."""
    match = _VERSION.search(output)
    if not match:
        return None
    major = int(match.group(1))
    if major == 1 and match.group(2):
        return int(match.group(2))
    return major


def _run_flags():
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def probe_java(path: str) -> Optional[JavaInfo]:
    """Run `java -version` and report the major version, or None if unusable."""
    if not path or not os.path.isfile(path):
        return None
    try:
        result = subprocess.run([path, "-version"], capture_output=True, text=True,
                                timeout=20, **_run_flags())
    except (OSError, subprocess.SubprocessError):
        return None
    major = parse_java_version(result.stderr + result.stdout)
    return JavaInfo(path, major) if major else None


def managed_jre_dir() -> Path:
    return data_dir() / "jre"


def _exe(name: str) -> str:
    return name + (".exe" if sys.platform == "win32" else "")


def managed_javas() -> List[str]:
    """java executables of runtimes the plugin installed itself, newest first."""
    found = []
    for root in sorted(managed_jre_dir().glob("*"), reverse=True):
        for pattern in (f"**/bin/{_exe('java')}",):
            found.extend(str(p) for p in root.glob(pattern))
    return found


def candidate_javas(preferred: Optional[str] = None) -> Iterable[str]:
    seen = set()

    def emit(path):
        if path and path not in seen:
            seen.add(path)
            yield path

    yield from emit(preferred)
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        yield from emit(os.path.join(java_home, "bin", _exe("java")))
    yield from emit(shutil.which("java"))
    for path in managed_javas():
        yield from emit(path)
    patterns = []
    if sys.platform == "darwin":
        patterns += ["/opt/homebrew/opt/openjdk*/bin/java",
                     "/usr/local/opt/openjdk*/bin/java",
                     "/Library/Java/JavaVirtualMachines/*/Contents/Home/bin/java",
                     os.path.expanduser("~/Library/Java/JavaVirtualMachines/*/Contents/Home/bin/java")]
    elif sys.platform == "win32":
        for base in filter(None, (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                                  os.environ.get("LOCALAPPDATA"))):
            for vendor in ("Eclipse Adoptium", "Java", "Microsoft", "Zulu", "Amazon Corretto",
                           "BellSoft", "OpenJDK", r"Programs\Eclipse Adoptium"):
                patterns.append(os.path.join(base, vendor, "*", "bin", "java.exe"))
    else:
        patterns += ["/usr/lib/jvm/*/bin/java", "/usr/lib64/jvm/*/bin/java",
                     "/opt/java/*/bin/java", "/usr/java/*/bin/java",
                     os.path.expanduser("~/.sdkman/candidates/java/*/bin/java")]
    for pattern in patterns:
        for path in sorted(glob.glob(pattern), reverse=True):
            yield from emit(path)


def find_java(min_major: int, preferred: Optional[str] = None) -> Optional[JavaInfo]:
    """The first runtime that is at least min_major; a preferred path wins if it qualifies."""
    for path in candidate_javas(preferred):
        info = probe_java(path)
        if info and info.major >= min_major:
            return info
    return None


def adoptium_asset(major: int) -> dict:
    """The latest Temurin JRE for this platform: name, link, size, sha256."""
    url = (ADOPTIUM_API.format(major=major)
           + f"?image_type=jre&os={platform_os()}&architecture={platform_arch()}&vendor=eclipse")
    assets = fetch_json(url)
    if not assets:
        raise RuntimeError(f"Adoptium offers no Java {major} JRE for {platform_os()}/{platform_arch()}")
    package = assets[0]["binary"]["package"]
    return {"release": assets[0]["release_name"], "name": package["name"],
            "link": package["link"], "size": package.get("size"),
            "sha256": package.get("checksum")}


def install_jre(major: int, progress: Optional[Callable[[str, int, Optional[int]], None]] = None,
                cancelled: Optional[Callable[[], bool]] = None) -> JavaInfo:
    """Download and unpack a Temurin JRE into the plugin's data directory."""
    asset = adoptium_asset(major)
    archive = managed_jre_dir() / "downloads" / asset["name"]
    if progress:
        progress(f"Downloading {asset['name']}", 0, asset["size"])
    download(asset["link"], archive, asset["sha256"],
             progress=(lambda done, total: progress(f"Downloading {asset['name']}", done, total))
             if progress else None, cancelled=cancelled)
    if progress:
        progress("Unpacking Java runtime", 0, None)
    target = managed_jre_dir() / asset["release"]
    extract_archive(archive, target)
    archive.unlink(missing_ok=True)
    javas = [str(p) for p in target.glob(f"**/bin/{_exe('java')}")]
    if not javas:
        raise RuntimeError(f"No java executable found in {target}")
    if sys.platform != "win32":
        for path in target.glob("**/bin/*"):
            path.chmod(path.stat().st_mode | 0o111)
    info = probe_java(javas[0])
    if not info:
        raise RuntimeError(f"The downloaded Java runtime does not run: {javas[0]}")
    return info
