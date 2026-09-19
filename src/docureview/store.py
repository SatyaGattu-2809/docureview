import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from docureview.models import Document, ReviewEvent, ReviewRequest


class ReviewConflict(ValueError):
    pass


class Store:
    def __init__(self, path: Path):
        self.path = path

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, status TEXT NOT NULL,
                created_at TEXT NOT NULL, body TEXT NOT NULL)""")
            connection.execute("""CREATE INDEX IF NOT EXISTS documents_status_created
                ON documents(status, created_at, id)""")

    def insert(self, document: Document):
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?)",
                (
                    document.id,
                    document.status,
                    document.created_at.isoformat(),
                    document.model_dump_json(),
                ),
            )

    def get(self, document_id: str) -> Document | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT body FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
        return Document.model_validate_json(row[0]) if row else None

    def pending(self, limit: int, offset: int) -> list[Document]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT body FROM documents
                WHERE status = 'needs_review' ORDER BY created_at, id LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        return [Document.model_validate_json(row[0]) for row in rows]

    def review(self, document_id: str, request: ReviewRequest, reviewer: str) -> Document:
        with closing(self.connect()) as connection, connection:
            # Lock before reading so concurrent decisions cannot both pass the version check.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT body FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            if row is None:
                raise KeyError(document_id)
            document = Document.model_validate_json(row[0])
            if document.status != "needs_review" or document.version != request.expected_version:
                raise ReviewConflict("Document was already reviewed or the version is stale")
            if request.decision == "rejected" and request.corrected_invoice is not None:
                raise ValueError("A rejected document cannot contain a corrected invoice")
            final_invoice = request.corrected_invoice or document.assessment.invoice
            if request.decision == "approved" and final_invoice is None:
                raise ValueError("Approval requires a complete, valid corrected invoice")
            document.status = request.decision
            document.final_invoice = final_invoice if request.decision == "approved" else None
            document.version += 1
            document.reviews.append(
                ReviewEvent(
                    reviewer=reviewer,
                    decision=request.decision,
                    note=request.note,
                    at=datetime.now(UTC),
                    version=document.version,
                )
            )
            connection.execute(
                "UPDATE documents SET status = ?, body = ? WHERE id = ?",
                (
                    document.status,
                    document.model_dump_json(),
                    document_id,
                ),
            )
        return document
