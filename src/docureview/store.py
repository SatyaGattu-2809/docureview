import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from docureview.config import Identity
from docureview.models import Document, ReviewEvent, ReviewRequest


class ReviewConflict(ValueError):
    pass


class QueueFull(ValueError):
    pass


def now():
    return datetime.now(UTC).isoformat()


def authorized(document: Document, identity: Identity) -> bool:
    return document.tenant == identity.tenant and (
        identity.role == "reviewer" or document.owner == identity.name
    )


class Store:
    def __init__(self, path: Path):
        self.path = path

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA secure_delete=ON")
        return connection

    def initialize(self, retention_days=7):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, status TEXT NOT NULL,
                created_at TEXT NOT NULL, body TEXT NOT NULL)""")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(documents)")}
            for name, declaration in [
                ("tenant", "TEXT NOT NULL DEFAULT 'local'"),
                ("owner", "TEXT NOT NULL DEFAULT 'local-uploader'"),
                ("expires_at", "TEXT"),
            ]:
                if name not in columns:
                    connection.execute(f"ALTER TABLE documents ADD COLUMN {name} {declaration}")
            for row in connection.execute(
                "SELECT id, body FROM documents WHERE expires_at IS NULL"
            ):
                document = Document.model_validate_json(row["body"])
                document.expires_at = document.created_at + timedelta(days=retention_days)
                connection.execute(
                    "UPDATE documents SET body=?, expires_at=? WHERE id=?",
                    (document.model_dump_json(), document.expires_at.isoformat(), document.id),
                )
            connection.execute("""CREATE INDEX IF NOT EXISTS documents_queue
                ON documents(tenant, status, created_at, id)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS originals (
                document_id TEXT PRIMARY KEY, payload BLOB NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, tenant TEXT NOT NULL, owner TEXT NOT NULL,
                idempotency_key TEXT NOT NULL, sha256 TEXT NOT NULL, media_type TEXT NOT NULL,
                payload BLOB, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                available_at TEXT NOT NULL, lease_until TEXT, lease_token TEXT,
                document_id TEXT, error TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                UNIQUE(tenant, owner, idempotency_key))""")
            connection.execute("CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(state, available_at)")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS worker_health "
                "(id INTEGER PRIMARY KEY, at TEXT NOT NULL)"
            )
        self.purge()

    @staticmethod
    def _insert(connection, document, original):
        connection.execute(
            """INSERT INTO documents
            (id,status,created_at,body,tenant,owner,expires_at) VALUES (?,?,?,?,?,?,?)""",
            (
                document.id,
                document.status,
                document.created_at.isoformat(),
                document.model_dump_json(),
                document.tenant,
                document.owner,
                document.expires_at.isoformat(),
            ),
        )
        if original is not None:
            connection.execute("INSERT INTO originals VALUES (?,?)", (document.id, original))

    def insert(self, document: Document, original: bytes | None = None):
        if document.expires_at is None:
            document.expires_at = document.created_at + timedelta(days=7)
        with closing(self.connect()) as connection, connection:
            self._insert(connection, document, original)

    def get(self, document_id: str, identity: Identity | None = None) -> Document | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT body FROM documents WHERE id=? AND expires_at>?", (document_id, now())
            ).fetchone()
        document = Document.model_validate_json(row["body"]) if row else None
        if document and identity and not authorized(document, identity):
            return None
        return document

    def original(self, document_id: str, identity: Identity) -> bytes | None:
        if self.get(document_id, identity) is None:
            return None
        with closing(self.connect()) as connection:
            row = connection.execute(
                """SELECT payload FROM originals o JOIN documents d
                ON d.id=o.document_id WHERE d.id=? AND d.expires_at>?""",
                (document_id, now()),
            ).fetchone()
        return row["payload"] if row else None

    def pending(self, limit: int, offset: int, tenant="local") -> list[Document]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT body FROM documents
                WHERE status='needs_review' AND tenant=? AND expires_at>?
                ORDER BY created_at,id LIMIT ? OFFSET ?""",
                (tenant, now(), limit, offset),
            ).fetchall()
        return [Document.model_validate_json(row["body"]) for row in rows]

    def review(
        self, document_id: str, request: ReviewRequest, reviewer: str, tenant="local"
    ) -> Document:
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT body FROM documents
                WHERE id=? AND tenant=? AND expires_at>?""",
                (document_id, tenant, now()),
            ).fetchone()
            if row is None:
                raise KeyError(document_id)
            document = Document.model_validate_json(row["body"])
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
                "UPDATE documents SET status=?,body=? WHERE id=?",
                (document.status, document.model_dump_json(), document_id),
            )
        return document

    def delete(self, document_id: str, tenant: str) -> bool:
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM documents WHERE id=? AND tenant=?", (document_id, tenant)
            ).fetchone()
            if not exists:
                return False
            connection.execute("DELETE FROM jobs WHERE document_id=?", (document_id,))
            connection.execute("DELETE FROM originals WHERE document_id=?", (document_id,))
            connection.execute("DELETE FROM documents WHERE id=?", (document_id,))
        return True

    def purge(self):
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = now()
            connection.execute(
                "DELETE FROM originals WHERE document_id IN "
                "(SELECT id FROM documents WHERE expires_at<=?)",
                (timestamp,),
            )
            connection.execute("DELETE FROM documents WHERE expires_at<=?", (timestamp,))
            connection.execute("DELETE FROM jobs WHERE expires_at<=?", (timestamp,))

    def enqueue(
        self,
        data: bytes,
        media_type: str,
        identity: Identity,
        key: str,
        retention_days: int,
        capacity: int,
    ):
        digest = hashlib.sha256(data).hexdigest()
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM jobs
                WHERE tenant=? AND owner=? AND idempotency_key=?""",
                (identity.tenant, identity.name, key),
            ).fetchone()
            if row:
                if row["sha256"] != digest or row["media_type"] != media_type:
                    raise ReviewConflict("Idempotency key was used with different content")
                return self.job_public(row)
            count = connection.execute(
                "SELECT count(*) FROM jobs WHERE state IN ('queued','running')"
            ).fetchone()[0]
            if count >= capacity:
                raise QueueFull("Processing queue is full; try later")
            job_id, timestamp = str(uuid4()), now()
            expiry = (datetime.now(UTC) + timedelta(days=retention_days)).isoformat()
            connection.execute(
                """INSERT INTO jobs
                (id,tenant,owner,idempotency_key,sha256,media_type,payload,state,
                 available_at,created_at,expires_at) VALUES (?,?,?,?,?,?,?,'queued',?,?,?)""",
                (
                    job_id,
                    identity.tenant,
                    identity.name,
                    key,
                    digest,
                    media_type,
                    data,
                    timestamp,
                    timestamp,
                    expiry,
                ),
            )
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return self.job_public(row)

    @staticmethod
    def job_public(row):
        return {
            name: row[name]
            for name in [
                "id",
                "state",
                "attempts",
                "document_id",
                "error",
                "created_at",
                "expires_at",
            ]
        }

    def job(self, job_id: str, identity: Identity):
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id=? AND tenant=? AND expires_at>?",
                (job_id, identity.tenant, now()),
            ).fetchone()
            if not row or (identity.role != "reviewer" and row["owner"] != identity.name):
                return None
            return self.job_public(row)

    def delete_job(self, job_id: str, tenant: str):
        with closing(self.connect()) as connection, connection:
            # Removing a pending/running job fences any in-flight worker completion.
            return (
                connection.execute(
                    "DELETE FROM jobs WHERE id=? AND tenant=? AND document_id IS NULL",
                    (job_id, tenant),
                ).rowcount
                > 0
            )

    def claim(self):
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = now()
            connection.execute(
                """UPDATE jobs SET state='failed',error='Worker retry limit reached',
                payload=NULL WHERE state='running' AND lease_until<? AND attempts>=3""",
                (timestamp,),
            )
            row = connection.execute(
                """SELECT * FROM jobs WHERE expires_at>? AND attempts<3 AND
                ((state='queued' AND available_at<=?) OR (state='running' AND lease_until<?))
                ORDER BY created_at,id LIMIT 1""",
                (timestamp, timestamp, timestamp),
            ).fetchone()
            if not row:
                return None
            token = str(uuid4())
            lease = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
            connection.execute(
                """UPDATE jobs SET state='running',attempts=attempts+1,
                lease_until=?,lease_token=? WHERE id=?""",
                (lease, token, row["id"]),
            )
            return dict(
                connection.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            )

    def finish(self, job, document: Document):
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT 1 FROM jobs WHERE id=? AND state='running'
                AND lease_token=? AND expires_at>?""",
                (job["id"], job["lease_token"], now()),
            ).fetchone()
            if row is None:
                return False
            self._insert(connection, document, job["payload"])
            connection.execute(
                "UPDATE jobs SET state='succeeded',document_id=?,payload=NULL,error=NULL "
                "WHERE id=?",
                (document.id, job["id"]),
            )
        return True

    def fail(self, job, error: str, retry: bool):
        state = "queued" if retry and job["attempts"] < 3 else "failed"
        available = (datetime.now(UTC) + timedelta(seconds=5 * 2 ** job["attempts"])).isoformat()
        with closing(self.connect()) as connection, connection:
            connection.execute(
                """UPDATE jobs SET state=?,error=?,available_at=?,
                payload=CASE WHEN ?='failed' THEN NULL ELSE payload END
                WHERE id=? AND lease_token=? AND state='running' """,
                (state, error, available, state, job["id"], job["lease_token"]),
            )

    def heartbeat(self):
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "INSERT INTO worker_health VALUES (1,?) "
                "ON CONFLICT(id) DO UPDATE SET at=excluded.at",
                (now(),),
            )

    def worker_ready(self):
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT at FROM worker_health WHERE id=1").fetchone()
        return bool(
            row and datetime.fromisoformat(row["at"]) > datetime.now(UTC) - timedelta(seconds=300)
        )

    def queue_metrics(self, tenant):
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT state, count(*) AS n, min(created_at) AS oldest FROM jobs "
                "WHERE tenant=? AND expires_at>? GROUP BY state",
                (tenant, now()),
            ).fetchall()
        result = {state: 0 for state in ("queued", "running", "succeeded", "failed")}
        oldest = []
        for row in rows:
            result[row["state"]] = row["n"]
            if row["state"] in ("queued", "running"):
                oldest.append(datetime.fromisoformat(row["oldest"]))
        result["oldest_pending_seconds"] = (
            max(0, (datetime.now(UTC) - min(oldest)).total_seconds()) if oldest else 0
        )
        return result
