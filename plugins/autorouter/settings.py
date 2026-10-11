"""Plugin settings, persisted as JSON in the plugin's data directory."""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .engine.freerouting import RouteOptions
from .engine.paths import data_dir


@dataclass
class Settings:
    java_path: str = ""              # "" = find one automatically
    jar_path: str = ""               # "" = the managed download
    options: RouteOptions = field(default_factory=RouteOptions)

    @staticmethod
    def path() -> Path:
        return data_dir() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(cls.path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        return cls(java_path=data.get("java_path", ""), jar_path=data.get("jar_path", ""),
                   options=RouteOptions.from_dict(data.get("options", {})))

    def save(self) -> None:
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"java_path": self.java_path, "jar_path": self.jar_path,
                   "options": self.options.to_dict()}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
