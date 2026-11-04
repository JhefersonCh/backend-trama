import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str
    secrets_dir: Path

    @classmethod
    def from_env(cls) -> "Settings":
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL is required")
        return cls(database_url=url, secrets_dir=Path(os.environ.get("SECRETS_DIR", "/run/trama-secrets")))
