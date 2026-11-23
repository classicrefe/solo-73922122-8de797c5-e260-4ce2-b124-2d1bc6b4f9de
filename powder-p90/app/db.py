"""SQLite 持久层：批次、档表、档三级结构，全部存整数，按档不逐粒。"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager

from .core import ROLE_NAMES, Bin, BinTable, parse_table

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS tables (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    total       INTEGER NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS bins (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id    INTEGER NOT NULL REFERENCES tables(id) ON DELETE CASCADE,
    lo          INTEGER NOT NULL,
    hi          INTEGER NOT NULL,
    count       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tables_batch ON tables(batch_id);
CREATE INDEX IF NOT EXISTS idx_bins_table ON bins(table_id);
CREATE TABLE IF NOT EXISTS screening_sheets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    aperture    INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS screening_roles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sheet_id    INTEGER NOT NULL REFERENCES screening_sheets(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('feed', 'undersize', 'oversize')),
    batch_id    INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    UNIQUE (sheet_id, role),
    UNIQUE (sheet_id, batch_id),
    UNIQUE (batch_id)
);
CREATE INDEX IF NOT EXISTS idx_screening_roles_sheet ON screening_roles(sheet_id);
"""


def init_db(path: str) -> None:
    """建表并切到 WAL。

    两个 gunicorn 工作进程会各自调用一次；并发建表/切换日志模式可能短暂
    SQLITE_BUSY，因此带忙等重试。
    """
    last: Exception | None = None
    for _ in range(20):
        try:
            with connect(path) as conn:
                conn.executescript(SCHEMA)
                # WAL：读写互不阻塞；登记的长核对阶段不持有写锁，其他批仍可补报。
                conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:  # database is locked / busy
            last = exc
            time.sleep(0.05)
    raise RuntimeError(f"初始化数据库失败：{last}")


@contextmanager
def connect(path: str):
    conn = sqlite3.connect(path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def list_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT b.id, b.name, b.created_at,
                  (SELECT COUNT(*) FROM tables t WHERE t.batch_id = b.id) AS table_count,
                  (SELECT total FROM tables t WHERE t.batch_id = b.id
                     ORDER BY t.id LIMIT 1) AS total
             FROM batches b ORDER BY b.id"""
    ).fetchall()


def get_batch_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM batches WHERE name = ?", (name,)).fetchone()


def create_batch(conn: sqlite3.Connection, name: str) -> int:
    cur = conn.execute("INSERT INTO batches(name) VALUES (?)", (name,))
    return int(cur.lastrowid)


def list_tables(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM tables WHERE batch_id = ? ORDER BY id", (batch_id,)
    ).fetchall()


def load_table(conn: sqlite3.Connection, table_id: int) -> BinTable:
    rows = conn.execute(
        "SELECT lo, hi, count FROM bins WHERE table_id = ? ORDER BY id", (table_id,)
    ).fetchall()
    bins = tuple(Bin(int(r["lo"]), int(r["hi"]), int(r["count"])) for r in rows)
    total = sum(b.count for b in bins)
    cuts = [bins[0].lo]
    cum = [0]
    for b in bins:
        cuts.append(b.hi + 1)
        cum.append(cum[-1] + b.count)
    return BinTable(bins, total, tuple(cuts), tuple(cum))


def load_batch_tables(conn: sqlite3.Connection, batch_id: int) -> list[BinTable]:
    trows = list_tables(conn, batch_id)
    return [load_table(conn, int(r["id"])) for r in trows]


def insert_table(conn: sqlite3.Connection, batch_id: int, table: BinTable, note: str) -> int:
    cur = conn.execute(
        "INSERT INTO tables(batch_id, total, note) VALUES (?, ?, ?)",
        (batch_id, table.total, note),
    )
    table_id = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO bins(table_id, lo, hi, count) VALUES (?, ?, ?, ?)",
        [(table_id, b.lo, b.hi, b.count) for b in table.bins],
    )
    return table_id


def parse_and_preview(text: str) -> BinTable:
    return parse_table(text)


# ---------------------------------------------------------------------------
# 筛分核对单
# ---------------------------------------------------------------------------

def get_screening_by_batch(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    """返回该批所在核对单（含 role 与筛孔），未登记则 None。"""
    return conn.execute(
        """SELECT s.id, s.aperture, s.created_at, r.role, r.batch_id
             FROM screening_roles r JOIN screening_sheets s ON s.id = r.sheet_id
            WHERE r.batch_id = ?""",
        (batch_id,),
    ).fetchone()


def list_screening_sheets(conn: sqlite3.Connection) -> list[dict]:
    sheets = conn.execute(
        "SELECT id, aperture, created_at FROM screening_sheets ORDER BY id"
    ).fetchall()
    out = []
    for s in sheets:
        roles = {
            r["role"]: dict(r)
            for r in conn.execute(
                """SELECT r.role, b.id AS batch_id, b.name
                     FROM screening_roles r JOIN batches b ON b.id = r.batch_id
                    WHERE r.sheet_id = ?""",
                (s["id"],),
            ).fetchall()
        }
        out.append(
            {
                "id": int(s["id"]),
                "aperture": int(s["aperture"]),
                "created_at": s["created_at"],
                "roles": roles,
            }
        )
    return out


def insert_screening(
    conn: sqlite3.Connection, aperture: int, feed_id: int, under_id: int, over_id: int
) -> int:
    cur = conn.execute(
        "INSERT INTO screening_sheets(aperture) VALUES (?)", (aperture,)
    )
    sheet_id = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO screening_roles(sheet_id, role, batch_id) VALUES (?, ?, ?)",
        [
            (sheet_id, "feed", feed_id),
            (sheet_id, "undersize", under_id),
            (sheet_id, "oversize", over_id),
        ],
    )
    return sheet_id


def merged_guard(
    conn: sqlite3.Connection, selected_ids: list[int]
) -> tuple[bool, str]:
    """合并核算前的拦截：同一张核对单的原批不得与任一产物同时选中。

    两个产物（筛下批 + 筛上批）可以同时选中，仍按合并口径核算。
    """
    selected = set(selected_ids)
    for sheet in list_screening_sheets(conn):
        roles = sheet["roles"]
        feed = roles.get("feed")
        if not feed or int(feed["batch_id"]) not in selected:
            continue
        partners = [
            r
            for key, r in roles.items()
            if key in ("undersize", "oversize") and int(r["batch_id"]) in selected
        ]
        if partners:
            names = "、".join(f"{p['name']}（{ROLE_NAMES[p['role']]}）" for p in partners)
            return False, (
                f"核对单 #{sheet['id']} 中，原批 {feed['name']} 不能与任一产物同时参与合并核算"
                f"（同时选中了 {names}）。总粒数会重复计算；请改选原批单独核算，"
                "或只选筛下批与筛上批两个产物一起核算。"
            )
    return True, ""
