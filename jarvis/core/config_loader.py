"""Configuration loader for Jarvis 2.0."""
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    """Loads environment variables and YAML configuration."""
    # 1. Load .env
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
    else:
        load_dotenv()

    # 2. Load YAML
    target_yaml = config_path or (BASE_DIR / "config" / "config.yaml")
    if not target_yaml.exists():
        raise FileNotFoundError(f"Configuration file not found at: {target_yaml}")

    with open(target_yaml, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    # Augment with base directory for absolute path resolution
    config["_base_dir"] = str(BASE_DIR)
    return config


def get_database_path(config: dict[str, Any] | None = None) -> Path:
    """Retrieves the unified canonical database path (data/jarvis_memory.db)."""
    cfg = config if config is not None else load_config()
    base_dir = Path(cfg.get("_base_dir", BASE_DIR))
    db_rel = cfg.get("memory", {}).get("db_path", "data/jarvis_memory.db")
    return (base_dir / db_rel).resolve()


def get_gemini_api_key() -> str:
    """Retrieves Google Gemini API key from environment."""
    key = os.getenv("GEMINI_API_KEY", "").strip()
    return key


def get_grok_api_key() -> str:
    """Retrieves xAI Grok API key from environment."""
    key = os.getenv("GROK_API_KEY", "").strip() or os.getenv("XAI_API_KEY", "").strip()
    return key


def get_groq_api_key() -> str:
    """Retrieves Groq Cloud API key from environment."""
    key = os.getenv("GROQ_API_KEY", "").strip()
    return key


def get_openrouter_api_key() -> str:
    """Retrieves OpenRouter API key from environment."""
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    return key

