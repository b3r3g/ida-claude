"""
Configuration management for IDA Claude.
"""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Config:
    """Plugin configuration."""

    api_key: str = ""
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 8192
    auto_refresh: bool = True
    thinking_enabled: bool = False
    thinking_budget: int = 10000  # tokens for thinking (min 1024)
    interleaved_thinking: bool = True  # Enable thinking between tool calls (Claude 4+)

    @classmethod
    def load(cls) -> "Config":
        """Load config from file or environment."""
        config = cls()

        # Try environment variable first
        if api_key := os.environ.get("ANTHROPIC_API_KEY"):
            config.api_key = api_key

        # Try config file
        config_path = cls._config_path()
        if config_path.exists():
            try:
                with open(config_path) as f:
                    data = json.load(f)
                    if "api_key" in data:
                        config.api_key = data["api_key"]
                    if "model" in data:
                        config.model = data["model"]
                    if "max_tokens" in data:
                        config.max_tokens = data["max_tokens"]
                    if "auto_refresh" in data:
                        config.auto_refresh = data["auto_refresh"]
                    if "thinking_enabled" in data:
                        config.thinking_enabled = data["thinking_enabled"]
                    if "thinking_budget" in data:
                        config.thinking_budget = data["thinking_budget"]
                    if "interleaved_thinking" in data:
                        config.interleaved_thinking = data["interleaved_thinking"]
            except Exception:
                pass

        return config

    def save(self):
        """Save config to file."""
        config_path = self._config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)

        with open(config_path, "w") as f:
            json.dump(asdict(self), f, indent=2)

    @staticmethod
    def _config_path() -> Path:
        """Get the config file path."""
        # Use IDA's user directory if available
        try:
            import ida_diskio

            user_dir = ida_diskio.get_user_idadir()
            return Path(user_dir) / "ida_claude_config.json"
        except ImportError:
            # Fallback
            return Path.home() / ".ida_claude" / "config.json"


# Global config instance
_config: Config | None = None


def get_config() -> Config:
    """Get the global config instance."""
    global _config
    if _config is None:
        _config = Config.load()
    return _config


def reload_config():
    """Reload config from disk."""
    global _config
    _config = Config.load()
    return _config
