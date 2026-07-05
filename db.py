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
    unread    INTEGER DEFAULT 0,
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
    unread: int = 0  # 未读提醒数（TG 推送后 +1，前端查看后清零）


class WalletDB:
    def __init__(self, path: str):
        # check_same_thread=False so the PTB job loop and handlers can share it;
        # all access is serialized through the lock below.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # 旧库迁移：补 unread 列（已存在则忽略）
            try:
                self._conn.execute(
                    "ALTER TABLE wallets ADD COLUMN unread INTEGER DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass
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
            unread=row["unread"] or 0,
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

    def clear(self) -> int:
        """Delete all wallets; returns how many were removed."""
        with self._lock:
            n = self._conn.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
            self._conn.execute("DELETE FROM wallets")
            self._conn.commit()
            return int(n)

    def get_by_id(self, wallet_id: int) -> Optional[Wallet]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM wallets WHERE id=?", (wallet_id,)
            ).fetchone()
            return self._row(row) if row else None

    def list_wallets(self) -> list[Wallet]:
        with self._lock:
            # Newest-added first (highest id) so the latest wallet shows on top.
            rows = self._conn.execute(
                "SELECT * FROM wallets ORDER BY id DESC"
            ).fetchall()
            return [self._row(r) for r in rows]

    def set_label(self, wallet_id: int, label: str) -> Optional[Wallet]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM wallets WHERE id=?", (wallet_id,)
            ).fetchone()
            if not row:
                return None
            self._conn.execute(
                "UPDATE wallets SET label=? WHERE id=?", (label, wallet_id)
            )
            self._conn.commit()
            return self._row(
                self._conn.execute(
                    "SELECT * FROM wallets WHERE id=?", (wallet_id,)
                ).fetchone()
            )

    def set_cursor(self, wallet_id: int, cursor: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE wallets SET cursor=? WHERE id=?", (cursor, wallet_id)
            )
            self._conn.commit()

    def bump_unread(self, wallet_id: int, n: int = 1) -> None:
        """TG 推送后累加该钱包的未读提醒数（前端红圈用）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE wallets SET unread=COALESCE(unread,0)+? WHERE id=?",
                (n, wallet_id),
            )
            self._conn.commit()

    def clear_unread(self, wallet_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE wallets SET unread=0 WHERE id=?", (wallet_id,)
            )
            self._conn.commit()
