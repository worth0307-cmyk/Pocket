"""SQLite storage for the wallet watch-list and per-wallet tracking cursor.

The cursor is the newest seen tx hash/signature for a wallet; the poller uses
it to detect which transactions are new since the last check.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chain     TEXT NOT NULL,
    address   TEXT NOT NULL,
    label     TEXT,
    chat_id   TEXT,
    cursor    TEXT,
    added_at  INTEGER,
    UNIQUE(chain, address)
);
"""


@dataclass
class Wallet:
    id: int
    chain: str
    address: str
    label: str
    chat_id: str
    cursor: Optional[str]
    added_at: int


class WalletDB:
    def __init__(self, path: str):
        # check_same_thread=False so the PTB job loop and handlers can share it;
        # all access is serialized through the lock below.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> Wallet:
        return Wallet(
            id=row["id"],
            chain=row["chain"],
            address=row["address"],
            label=row["label"] or "",
            chat_id=row["chat_id"] or "",
            cursor=row["cursor"],
            added_at=row["added_at"] or 0,
        )

    def add_wallet(
        self, chain: str, address: str, label: str, chat_id: str
    ) -> tuple[bool, Optional[Wallet]]:
        """Insert a wallet. Returns (created, wallet). created=False if it existed."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM wallets WHERE chain=? AND address=?",
                (chain, address),
            )
            existing = cur.fetchone()
            if existing:
                return False, self._row(existing)
            self._conn.execute(
                "INSERT INTO wallets (chain, address, label, chat_id, cursor, added_at)"
                " VALUES (?,?,?,?,?,?)",
                (chain, address, label, chat_id, None, int(time.time())),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM wallets WHERE chain=? AND address=?",
                (chain, address),
            ).fetchone()
            return True, self._row(row)

    def remove_by_id(self, wallet_id: int) -> Optional[Wallet]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM wallets WHERE id=?", (wallet_id,)
            ).fetchone()
            if not row:
                return None
            self._conn.execute("DELETE FROM wallets WHERE id=?", (wallet_id,))
            self._conn.commit()
            return self._row(row)

    def remove(self, chain: str, address: str) -> Optional[Wallet]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM wallets WHERE chain=? AND address=?",
                (chain, address),
            ).fetchone()
            if not row:
                return None
            self._conn.execute(
                "DELETE FROM wallets WHERE chain=? AND address=?", (chain, address)
            )
            self._conn.commit()
            return self._row(row)

    def get_by_id(self, wallet_id: int) -> Optional[Wallet]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM wallets WHERE id=?", (wallet_id,)
            ).fetchone()
            return self._row(row) if row else None

    def list_wallets(self) -> list[Wallet]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM wallets ORDER BY chain, id"
            ).fetchall()
            return [self._row(r) for r in rows]

    def set_cursor(self, wallet_id: int, cursor: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE wallets SET cursor=? WHERE id=?", (cursor, wallet_id)
            )
            self._conn.commit()
