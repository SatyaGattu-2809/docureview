import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docureview.api import create_app
from docureview.backup import backup
from docureview.config import Settings
from docureview.extraction import LabelsExtractor
from docureview.store import Store
from docureview.validation import assess


@pytest.mark.parametrize(
    "case",
    json.loads((Path(__file__).parents[1] / "evaluation/invoices.json").read_text()),
    ids=lambda c: c["id"],
)
def test_observed_layout_regressions(case):
    result = assess(LabelsExtractor().extract(case["text"]), case["text"], 0.9, False)
    assert result.normalized_values == case["expected"]
    assert result.status == "needs_review"


def test_missing_multiline_value_does_not_consume_next_label():
    result = LabelsExtractor().extract("Tax\nTotal\n1080.00\n")
    assert result.tax.value is None
    assert result.total.value == "1080.00"


def test_worker_readiness_and_durable_only_mode(tmp_path):
    settings = Settings(
        database=tmp_path / "db",
        api_key="u" * 32,
        reviewer_key="r" * 32,
        require_worker=True,
        sync_upload_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        headers = {"X-API-Key": "r" * 32}
        assert client.get("/readyz", headers=headers).status_code == 503
        store = Store(settings.database)
        store.heartbeat()
        assert client.get("/readyz", headers=headers).status_code == 200
        assert client.get("/v1/operations", headers={"X-API-Key": "u" * 32}).status_code == 403
        assert client.get("/v1/operations", headers=headers).json()["queue"]["queued"] == 0
        assert client.post("/v1/documents", headers=headers, content=b"invoice").status_code == 409
        with closing(store.connect()) as connection, connection:
            connection.execute("UPDATE worker_health SET at='2000-01-01T00:00:00+00:00'")
        assert client.get("/readyz", headers=headers).status_code == 503


def test_snapshot_restores_committed_wal_and_refuses_overwrite(tmp_path):
    source, target = tmp_path / "live.db", tmp_path / "backup.db"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE records (value TEXT)")
        connection.execute("INSERT INTO records VALUES ('retained')")
        connection.commit()
        backup(source, target)
    with closing(sqlite3.connect(target)) as restored:
        assert restored.execute("SELECT value FROM records").fetchone()[0] == "retained"
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        backup(source, target)
