"""Where the plugin keeps its own files, and what platform this is."""
import os
import platform
import sys
from pathlib import Path

APP_DIR_NAME = "kicad-autorouter"


def data_dir() -> Path:
    """Per-user, per-platform data directory (engine downloads, jobs, logs).

    Shared by every KiCad version, so the 120 MB of engine is downloaded once.
    """
    override = os.environ.get("KICAD_AUTOROUTER_HOME")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(base or Path.home() / "AppData" / "Local") / APP_DIR_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_DIR_NAME


def platform_os() -> str:
    """'mac', 'windows' or 'linux' (the Adoptium API's names)."""
    if sys.platform == "darwin":
        return "mac"
    if sys.platform == "win32":
        return "windows"
    return "linux"


def platform_arch() -> str:
    """'x64' or 'aarch64' (the Adoptium API's names)."""
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "aarch64"
    return "x64"
