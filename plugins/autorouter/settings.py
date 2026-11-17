"""Plugin settings, persisted as JSON in the plugin's data directory."""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .engine.options import RouteOptions
from .engine.paths import data_dir


@dataclass
class Settings:
    codex_path: str = ""             # "" = find the codex CLI automatically
    ai_model: str = ""               # "" = the provider's default model
    ai_provider: str = "codex-cli"   # codex-cli | claude-cli | anthropic-api | openai-api (engine/ai/llm.py)
    claude_path: str = ""            # "" = find the claude CLI automatically
    guide_id: str = "router-default" # the routing guide (engine/guides.py) used for runs
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
        return cls(codex_path=data.get("codex_path", ""), ai_model=data.get("ai_model", ""),
                   ai_provider=data.get("ai_provider", "codex-cli"), claude_path=data.get("claude_path", ""),
                   guide_id=data.get("guide_id", "router-default"),
                   options=RouteOptions.from_dict(data.get("options", {})))

    def save(self) -> None:
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"codex_path": self.codex_path, "ai_model": self.ai_model, "ai_provider": self.ai_provider,
                   "claude_path": self.claude_path, "guide_id": self.guide_id,
                   "options": self.options.to_dict()}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
