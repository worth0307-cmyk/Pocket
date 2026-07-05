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
    sort_ts   INTEGER,
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
            # 旧库迁移：补列（已存在则忽略）
            for ddl in (
                "ALTER TABLE wallets ADD COLUMN unread INTEGER DEFAULT 0",
                "ALTER TABLE wallets ADD COLUMN sort_ts INTEGER",
            ):
                try:
                    self._conn.execute(ddl)
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
        self, chain: str, address: str, label: str, chat_id: str,
        sort_ts: Optional[int] = None,
    ) -> tuple[bool, Optional[Wallet]]:
        """Insert a wallet. Returns (created, wallet). created=False if it existed.

        ``sort_ts`` is the manual-order key (bigger = closer to the top);
        defaults to now so newly added wallets appear first."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM wallets WHERE chain=? AND address=?",
                (chain, address),
            )
            existing = cur.fetchone()
            if existing:
                return False, self._row(existing)
            now = int(time.time())
            self._conn.execute(
                "INSERT INTO wallets (chain, address, label, chat_id, cursor,"
                " added_at, sort_ts) VALUES (?,?,?,?,?,?,?)",
                (chain, address, label, chat_id, None, now,
                 int(sort_ts) if sort_ts is not None else now),
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
            # 手动排序键优先（置顶/上移/导入置顶都改它），老数据退回添加时间。
            rows = self._conn.execute(
                "SELECT * FROM wallets"
                " ORDER BY COALESCE(sort_ts, added_at, 0) DESC, id DESC"
            ).fetchall()
            return [self._row(r) for r in rows]

    def set_sort_ts(self, wallet_id: int, sort_ts: int) -> None:
        """置顶/换位用：直接设排序键（越大越靠前）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE wallets SET sort_ts=? WHERE id=?",
                (int(sort_ts), wallet_id),
            )
            self._conn.commit()

    def move_wallet(self, wallet_id: int, action: str) -> bool:
        """up/down=与相邻项交换排序键；top=排到最前。返回是否有变化。"""
        wallets = self.list_wallets()  # 已按当前显示顺序
        idx = next((i for i, w in enumerate(wallets) if w.id == wallet_id), None)
        if idx is None:
            return False
        if action == "top":
            if idx == 0:
                return False
            with self._lock:
                row = self._conn.execute(
                    "SELECT MAX(COALESCE(sort_ts, added_at, 0)) AS m FROM wallets"
                ).fetchone()
                top_key = int(row["m"] or 0) + 1
                self._conn.execute(
                    "UPDATE wallets SET sort_ts=? WHERE id=?", (top_key, wallet_id)
                )
                self._conn.commit()
            return True
        other = idx - 1 if action == "up" else idx + 1
        if other < 0 or other >= len(wallets):
            return False  # 已在最顶/最底

        def key(w: Wallet) -> int:
            row = self._conn.execute(
                "SELECT COALESCE(sort_ts, added_at, 0) AS k FROM wallets WHERE id=?",
                (w.id,),
            ).fetchone()
            return int(row["k"]) if row else 0

        with self._lock:
            ka, kb = key(wallets[idx]), key(wallets[other])
            if ka == kb:  # 键相同（同秒导入），错开一位保证交换生效
                kb += 1 if action == "up" else -1
            self._conn.execute(
                "UPDATE wallets SET sort_ts=? WHERE id=?", (kb, wallets[idx].id)
            )
            self._conn.execute(
                "UPDATE wallets SET sort_ts=? WHERE id=?", (ka, wallets[other].id)
            )
            self._conn.commit()
        return True

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
