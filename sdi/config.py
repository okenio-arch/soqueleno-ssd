from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def load_env_file(path: Path | None = None) -> None:
    """Small .env reader; does not overwrite variables already set by Windows."""
    env_path = path or ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def setting(name: str, default: str) -> str:
    load_env_file()
    return os.getenv(name, default)


def db_path() -> str:
    return setting("SDI_DB", "data/sdi.sqlite3")
