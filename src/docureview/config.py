import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator


class Identity(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    tenant: str = Field(min_length=1, max_length=100)
    role: Literal["uploader", "reviewer"]
    key: SecretStr = Field(min_length=16)


class Settings(BaseModel):
    api_key: SecretStr | None = Field(default=None, min_length=16)
    reviewer_key: SecretStr | None = Field(default=None, min_length=16)
    reviewer_name: str = "local-reviewer"
    identities: list[Identity] = Field(default_factory=list)
    database: Path = Path("data/docureview.sqlite3")
    provider: Literal["demo", "labels", "ollama"] = "demo"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = ""
    review_threshold: float = Field(default=0.9, ge=0, le=1)
    auto_accept: bool = False
    require_worker: bool = False
    sync_upload_enabled: bool = True
    max_upload_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    max_text_chars: int = Field(default=12000, gt=0)
    max_pages: int = Field(default=10, gt=0)
    ocr_enabled: bool = False
    parse_timeout_seconds: int = Field(default=60, ge=1, le=120)
    retention_days: int = Field(default=7, ge=1, le=30)
    max_pending_jobs: int = Field(default=100, ge=1, le=10000)

    @model_validator(mode="after")
    def validate_security(self):
        if self.auto_accept:
            raise ValueError("Automatic acceptance is locked off pending live-model evaluation")
        if self.identities:
            if self.api_key is not None or self.reviewer_key is not None:
                raise ValueError("Use identities or legacy keys, not both")
            keys = [identity.key.get_secret_value() for identity in self.identities]
            names = [(identity.tenant, identity.name) for identity in self.identities]
            if len(keys) != len(set(keys)) or len(names) != len(set(names)):
                raise ValueError("Identity names and keys must be unique")
        elif self.api_key is None or self.reviewer_key is None or self.api_key == self.reviewer_key:
            raise ValueError("Two distinct API keys or an identities file are required")
        if self.provider == "ollama" and not self.ollama_model:
            raise ValueError("OLLAMA_MODEL is required for the ollama provider")
        return self

    def principals(self) -> list[Identity]:
        return self.identities or [
            Identity(name="local-uploader", tenant="local", role="uploader", key=self.api_key),
            Identity(
                name=self.reviewer_name, tenant="local", role="reviewer", key=self.reviewer_key
            ),
        ]

    @classmethod
    def from_env(cls):
        names = {
            "api_key": "DOCUREVIEW_API_KEY",
            "reviewer_key": "DOCUREVIEW_REVIEWER_KEY",
            "reviewer_name": "DOCUREVIEW_REVIEWER_NAME",
            "database": "DOCUREVIEW_DATABASE",
            "provider": "DOCUREVIEW_PROVIDER",
            "ollama_url": "OLLAMA_URL",
            "ollama_model": "OLLAMA_MODEL",
            "review_threshold": "DOCUREVIEW_REVIEW_THRESHOLD",
            "require_worker": "DOCUREVIEW_REQUIRE_WORKER",
            "sync_upload_enabled": "DOCUREVIEW_SYNC_UPLOAD_ENABLED",
            "max_pending_jobs": "DOCUREVIEW_MAX_PENDING_JOBS",
            "parse_timeout_seconds": "DOCUREVIEW_PARSE_TIMEOUT_SECONDS",
            "auto_accept": "DOCUREVIEW_AUTO_ACCEPT",
            "ocr_enabled": "DOCUREVIEW_OCR_ENABLED",
            "retention_days": "DOCUREVIEW_RETENTION_DAYS",
        }
        values = {key: os.environ[env] for key, env in names.items() if env in os.environ}
        if path := os.environ.get("DOCUREVIEW_IDENTITIES_FILE"):
            values["identities"] = json.loads(Path(path).read_text())
        return cls(**values)
