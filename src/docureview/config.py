import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr


class Settings(BaseModel):
    api_key: SecretStr = Field(min_length=16)
    reviewer_key: SecretStr = Field(min_length=16)
    reviewer_name: str = "local-reviewer"
    database: Path = Path("data/docureview.sqlite3")
    provider: Literal["demo", "ollama"] = "demo"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = ""
    review_threshold: float = Field(default=0.9, ge=0, le=1)
    auto_accept: bool = False
    max_upload_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    max_text_chars: int = Field(default=12000, gt=0)
    max_pages: int = Field(default=10, gt=0)

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
            "auto_accept": "DOCUREVIEW_AUTO_ACCEPT",
        }
        settings = cls(**{key: os.environ[env] for key, env in names.items() if env in os.environ})
        if settings.api_key == settings.reviewer_key:
            raise ValueError("Upload and reviewer API keys must be different")
        if settings.provider == "ollama" and not settings.ollama_model:
            raise ValueError("OLLAMA_MODEL is required for the ollama provider")
        return settings
