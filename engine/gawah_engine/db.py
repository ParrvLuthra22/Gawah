"""SQLite access and migrations (stdlib sqlite3).

Every connection is autocommit (`isolation_level=None`) so transactions are explicit:
writers run `BEGIN IMMEDIATE`, which takes the write lock up front. That is what makes the
ledger's "read last hash, then insert" and the budget's "check remaining, then reserve"
race-free. Schema changes are numbered migrations applied at startup by `init_db`
(tracked in `PRAGMA user_version`).
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_PREFIX = "sqlite:///"

MIGRATIONS: list[list[str]] = [
    # 1: mandates, budget reservations, append-only ledger
    [
        """CREATE TABLE mandates (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            category TEXT NOT NULL,
            item_description TEXT NOT NULL,
            max_amount_minor INTEGER NOT NULL CHECK (max_amount_minor >= 0),
            currency TEXT NOT NULL,
            allowed_merchants TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'expired')),
            signature TEXT NOT NULL,
            signing_alg TEXT NOT NULL,
            key_id TEXT NOT NULL,
            spent_minor INTEGER NOT NULL DEFAULT 0,
            reserved_minor INTEGER NOT NULL DEFAULT 0,
            CHECK (spent_minor >= 0 AND reserved_minor >= 0
                   AND spent_minor + reserved_minor <= max_amount_minor)
        )""",
        """CREATE TABLE reservations (
            mandate_id TEXT NOT NULL REFERENCES mandates(id),
            idempotency_key TEXT NOT NULL,
            amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
            state TEXT NOT NULL CHECK (state IN ('reserved', 'committed', 'released')),
            created_at TEXT NOT NULL,
            PRIMARY KEY (mandate_id, idempotency_key)
        )""",
        """CREATE TABLE ledger (
            seq INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            data TEXT NOT NULL,
            prev_hash TEXT NOT NULL,
            hash TEXT NOT NULL,
            signature TEXT NOT NULL,
            key_id TEXT NOT NULL
        )""",
        "CREATE INDEX ledger_subject ON ledger(subject_id)",
        """CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger
           BEGIN SELECT RAISE(ABORT, 'ledger is append-only: UPDATE blocked'); END""",
        """CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger
           BEGIN SELECT RAISE(ABORT, 'ledger is append-only: DELETE blocked'); END""",
    ],
    # 2: merchant registry (public keys only; one row per merchant key id)
    [
        """CREATE TABLE merchants (
            merchant_id TEXT NOT NULL,
            key_id TEXT NOT NULL,
            alg TEXT NOT NULL,
            public_key_pem TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            PRIMARY KEY (merchant_id, key_id)
        )""",
    ],
    # 3: Guardian: proposals, decisions (one per proposal id), semantic-check cache
    [
        """CREATE TABLE proposals (
            id TEXT PRIMARY KEY,
            mandate_id TEXT NOT NULL,
            merchant_id TEXT NOT NULL,
            cart_id TEXT NOT NULL,
            body TEXT NOT NULL,
            received_at TEXT NOT NULL
        )""",
        "CREATE INDEX proposals_cart ON proposals(mandate_id, merchant_id, cart_id)",
        """CREATE TABLE decisions (
            id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL UNIQUE REFERENCES proposals(id),
            body TEXT NOT NULL,
            advisories TEXT NOT NULL,
            decided_at TEXT NOT NULL
        )""",
        """CREATE TABLE semantic_cache (
            key TEXT PRIMARY KEY,
            result TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
    ],
]


def db_path(database_url: str) -> str:
    """`sqlite:///./gawah.db` -> `./gawah.db`; `sqlite:////abs/x.db` -> `/abs/x.db`."""
    if not database_url.startswith(_PREFIX):
        raise ValueError(f"unsupported DATABASE_URL {database_url!r}; use sqlite:///path/to.db")
    return database_url[len(_PREFIX):]


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def write_tx(path: str) -> Iterator[sqlite3.Connection]:
    """One write transaction: BEGIN IMMEDIATE ... COMMIT, ROLLBACK on any exception."""
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


def init_db(path: str) -> None:
    """Create the file if needed and apply any pending migrations. Safe to call repeatedly."""
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        for version, statements in enumerate(MIGRATIONS, start=1):
            if conn.execute("PRAGMA user_version").fetchone()[0] >= version:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Re-check under the lock: another process may have migrated meanwhile.
                if conn.execute("PRAGMA user_version").fetchone()[0] < version:
                    for stmt in statements:
                        conn.execute(stmt)
                    conn.execute(f"PRAGMA user_version = {version}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()
