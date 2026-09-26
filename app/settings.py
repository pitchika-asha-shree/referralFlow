from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    database_path: str
    configs_dir: str
    retry_interval_seconds: float
    inbound_secret: str


def get_settings() -> Settings:
    # Used by ${EHR_BASE_URL} in client YAML; points at scripts/mock_ehr.py by default.
    os.environ.setdefault("EHR_BASE_URL", "http://localhost:9000")
    return Settings(
        database_path=os.getenv("DATABASE_PATH", str(ROOT / "data" / "referralflow.db")),
        configs_dir=os.getenv("CLIENT_CONFIGS_DIR", str(ROOT / "configs" / "clients")),
        retry_interval_seconds=float(os.getenv("RETRY_INTERVAL_SECONDS", "15")),
        inbound_secret=os.getenv("INBOUND_WEBHOOK_SECRET", "dev-inbound-secret"),
    )
