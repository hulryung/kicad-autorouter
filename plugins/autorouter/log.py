"""Logging to a rotating file under the plugin's data directory."""
import logging
import logging.handlers

from .engine.paths import data_dir

BASE = "kicad_autorouter"
_configured = False


def log_file():
    return data_dir() / "logs" / f"{BASE}.log"


def get_logger(name: str = "") -> logging.Logger:
    global _configured
    logger = logging.getLogger(f"{BASE}.{name}" if name else BASE)
    if not _configured:
        root = logging.getLogger(BASE)
        root.setLevel(logging.INFO)
        # KiCad re-imports the plugin when plugins are refreshed: this module
        # starts over, the logger does not, and a second handler would write
        # every line twice
        for h in list(root.handlers):
            if getattr(h, "_autorouter", False):
                root.removeHandler(h)
                h.close()
        try:
            path = log_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s"))
            handler._autorouter = True
            root.addHandler(handler)
        except OSError:
            handler = logging.StreamHandler()
            handler._autorouter = True
            root.addHandler(handler)
        _configured = True
    return logger
