"""SQLite 持久化：书目记录、采集进度、采集运行历史、主题词表与导航计数。

所有写操作在单连接单事务内完成，保证“整批采完才提交”。
主题计数与书目变更、截止日推进在同一个事务内提交，失败则整体回滚。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

DB_PATH = os.environ.get("APP_DB_PATH", "/data/harvester.db")

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS bib_records (
    identifier      TEXT PRIMARY KEY,            -- OAI 记录唯一标识
    datestamp       TEXT NOT NULL,               -- OAI header datestamp
    status          TEXT NOT NULL,               -- active / withdrawn
    title           TEXT,
    creator         TEXT,
    publisher       TEXT,
    subject         TEXT,
    description     TEXT,
    date            TEXT,
    type            TEXT,
    format          TEXT,
    identifier_dc   TEXT,
    source          TEXT,
    language        TEXT,
    relation        TEXT,
    coverage        TEXT,
    rights          TEXT,
    raw_dc          TEXT NOT NULL,               -- 原始 Dublin Core JSON
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS harvest_progress (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    last_until      TEXT NOT NULL,               -- 上次成功采集的截止日（含）
    last_harvest_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS harvest_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    from_date       TEXT NOT NULL,
    until_date      TEXT NOT NULL,
    status          TEXT NOT NULL,               -- success / failed
    pages           INTEGER NOT NULL DEFAULT 0,
    fetched         INTEGER NOT NULL DEFAULT 0,
    inserted        INTEGER NOT NULL DEFAULT 0,
    updated         INTEGER NOT NULL DEFAULT 0,
    withdrawn       INTEGER NOT NULL DEFAULT 0,
    retries         INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);

-- 主题词表：dc:subject 原词 -> 标准主题（原词精确匹配）
CREATE TABLE IF NOT EXISTS subject_mapping (
    raw_subject     TEXT PRIMARY KEY,            -- dc:subject 原词
    standard_subject TEXT NOT NULL               -- 标准主题
);

-- 词表版本：每次换表 +1，查询可据此确认计数对应哪一版词表
CREATE TABLE IF NOT EXISTS subject_mapping_version (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    version         INTEGER NOT NULL,
    replaced_at     TEXT NOT NULL
);

-- 书目与标准主题的去重索引：同一书目在同一标准主题下只占一行
-- （两个原词归入同一主题时，同一条书目只计一次）
CREATE TABLE IF NOT EXISTS record_subjects (
    identifier       TEXT NOT NULL,
    standard_subject TEXT NOT NULL,
    PRIMARY KEY (identifier, standard_subject)
) WITHOUT ROWID;

-- 各标准主题对在架书目的非零计数（计数为 0 的行删除，导航只查非零）
CREATE TABLE IF NOT EXISTS subject_counts (
    standard_subject TEXT PRIMARY KEY,
    count            INTEGER NOT NULL CHECK (count >= 0)
);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: str | None = None) -> None:
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务上下文：异常回滚，正常时由调用方负责 commit。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
    except Exception:
        conn.rollback()
        raise


def get_last_until(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT last_until FROM harvest_progress WHERE id = 1"
    ).fetchone()
    return row["last_until"] if row else None


def advance_progress(conn: sqlite3.Connection, until_date: str) -> None:
    """整批成功后推进截止日（单行 upsert）。"""
    conn.execute(
        """
        INSERT INTO harvest_progress (id, last_until, last_harvest_at)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            last_until = excluded.last_until,
            last_harvest_at = excluded.last_harvest_at
        """,
        (until_date, utc_now_iso()),
    )


def start_run(conn: sqlite3.Connection, from_date: str, until_date: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO harvest_runs (started_at, from_date, until_date, status)
        VALUES (?, ?, ?, 'running')
        """,
        (utc_now_iso(), from_date, until_date),
    )
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, *, status: str,
               pages: int, fetched: int, inserted: int, updated: int,
               withdrawn: int, retries: int, error: str | None) -> None:
    conn.execute(
        """
        UPDATE harvest_runs SET
            finished_at = ?, status = ?, pages = ?, fetched = ?,
            inserted = ?, updated = ?, withdrawn = ?, retries = ?, error = ?
        WHERE id = ?
        """,
        (utc_now_iso(), status, pages, fetched, inserted, updated,
         withdrawn, retries, error, run_id),
    )


def upsert_bib_record(conn: sqlite3.Connection, *, identifier: str,
                      datestamp: str, fields: dict, raw_dc: str) -> str:
    """按标识 upsert 书目；返回 'inserted' 或 'updated'。"""
    existing = conn.execute(
        "SELECT identifier FROM bib_records WHERE identifier = ?",
        (identifier,),
    ).fetchone()
    now = utc_now_iso()
    params = (
        identifier, datestamp, "active",
        fields.get("title"), fields.get("creator"), fields.get("publisher"),
        fields.get("subject"), fields.get("description"), fields.get("date"),
        fields.get("type"), fields.get("format"), fields.get("identifier"),
        fields.get("source"), fields.get("language"), fields.get("relation"),
        fields.get("coverage"), fields.get("rights"), raw_dc, now,
    )
    if existing is None:
        conn.execute(
            """
            INSERT INTO bib_records (
                identifier, datestamp, status, title, creator, publisher,
                subject, description, date, type, format, identifier_dc,
                source, language, relation, coverage, rights, raw_dc, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params,
        )
        return "inserted"
    conn.execute(
        """
        UPDATE bib_records SET
            datestamp = ?, status = ?, title = ?, creator = ?, publisher = ?,
            subject = ?, description = ?, date = ?, type = ?, format = ?,
            identifier_dc = ?, source = ?, language = ?, relation = ?,
            coverage = ?, rights = ?, raw_dc = ?, updated_at = ?
        WHERE identifier = ?
        """,
        params[1:] + (identifier,),
    )
    return "updated"


def withdraw_bib_record(conn: sqlite3.Connection, *, identifier: str,
                        datestamp: str) -> bool:
    """遇删除标记则撤下（标记 withdrawn）。不存在返回 False。"""
    cur = conn.execute(
        """
        UPDATE bib_records
        SET status = 'withdrawn', datestamp = ?, updated_at = ?
        WHERE identifier = ?
        """,
        (datestamp, utc_now_iso(), identifier),
    )
    return cur.rowcount > 0


def insert_withdrawn_tombstone(conn: sqlite3.Connection, *, identifier: str,
                               datestamp: str) -> None:
    """删除标记记录此前不在库中时，写入 withdrawn 墓碑，保留撤下痕迹。"""
    conn.execute(
        """
        INSERT INTO bib_records (
            identifier, datestamp, status, raw_dc, updated_at
        ) VALUES (?, ?, 'withdrawn', ?, ?)
        ON CONFLICT(identifier) DO NOTHING
        """,
        (identifier, datestamp, "{}", utc_now_iso()),
    )


# ---------------------------------------------------------------------------
# 主题词表与导航计数
# ---------------------------------------------------------------------------

def get_subject_mapping(conn: sqlite3.Connection) -> dict[str, str]:
    """返回当前词表（原词 -> 标准主题）；未提交词表时为空。"""
    rows = conn.execute(
        "SELECT raw_subject, standard_subject FROM subject_mapping"
    ).fetchall()
    return {row["raw_subject"]: row["standard_subject"] for row in rows}


def get_mapping_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT version FROM subject_mapping_version WHERE id = 1"
    ).fetchone()
    return row["version"] if row else None


def _standard_subjects(raw_subjects: list[str],
                       mapping: dict[str, str]) -> set[str]:
    """按原词精确匹配映射到标准主题并去重；未收录原词不计。"""
    return {
        mapping[raw]
        for raw in raw_subjects
        if raw in mapping
    }


def map_subjects(raw_subjects: list[str],
                 mapping: dict[str, str]) -> set[str]:
    """公开入口：原词精确匹配 + 同书目同主题去重。"""
    return _standard_subjects(raw_subjects, mapping)


def extract_raw_subjects(raw_dc: str) -> list[str]:
    """从原始 Dublin Core JSON 取完整的多值 dc:subject。"""
    try:
        dc = json.loads(raw_dc or "{}")
    except (ValueError, TypeError):
        return []
    values = dc.get("subject") or []
    return [value for value in values if isinstance(value, str) and value]


def replace_subject_mapping(conn: sqlite3.Connection,
                            mapping: dict[str, str]) -> int:
    """换表：按全部在架书目的完整 dc:subject 重建词表与计数。

    必须在已开启的事务（BEGIN IMMEDIATE）内调用：词表、书目主题索引、
    计数三者一起写入，任何异常由外层事务整体回滚，不改动原书目。
    返回新词表版本号。
    """
    # 1. 替换词表本身
    conn.execute("DELETE FROM subject_mapping")
    conn.executemany(
        "INSERT INTO subject_mapping (raw_subject, standard_subject) "
        "VALUES (?, ?)",
        list(mapping.items()),
    )

    # 2. 按全部在架书目的完整 dc:subject 重建去重索引
    conn.execute("DELETE FROM record_subjects")
    active_rows = conn.execute(
        "SELECT identifier, raw_dc FROM bib_records WHERE status = 'active'"
    ).fetchall()
    index_rows: list[tuple[str, str]] = []
    for row in active_rows:
        subjects = _standard_subjects(extract_raw_subjects(row["raw_dc"]),
                                      mapping)
        index_rows.extend((row["identifier"], subject) for subject in subjects)
    if index_rows:
        conn.executemany(
            "INSERT OR IGNORE INTO record_subjects "
            "(identifier, standard_subject) VALUES (?, ?)",
            index_rows,
        )

    # 3. 由去重索引重建非零计数
    conn.execute("DELETE FROM subject_counts")
    conn.execute(
        """
        INSERT INTO subject_counts (standard_subject, count)
        SELECT standard_subject, COUNT(*)
        FROM record_subjects
        GROUP BY standard_subject
        """
    )

    # 4. 词表版本 +1（词表与计数同事务提交）
    previous = get_mapping_version(conn)
    next_version = (previous or 0) + 1
    conn.execute(
        """
        INSERT INTO subject_mapping_version (id, version, replaced_at)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            version = excluded.version,
            replaced_at = excluded.replaced_at
        """,
        (next_version, utc_now_iso()),
    )
    return next_version


def get_record_snapshot(conn: sqlite3.Connection,
                        identifier: str) -> tuple[str | None, set[str]]:
    """取一条书目变更前的在架状态与标准主题集合（增量计数用快照）。"""
    row = conn.execute(
        "SELECT status FROM bib_records WHERE identifier = ?", (identifier,)
    ).fetchone()
    old_status = row["status"] if row else None
    old_subjects = {
        row["standard_subject"]
        for row in conn.execute(
            "SELECT standard_subject FROM record_subjects WHERE identifier = ?",
            (identifier,),
        ).fetchall()
    }
    return old_status, old_subjects


def _adjust_count(conn: sqlite3.Connection, standard_subject: str,
                  delta: int) -> None:
    """调整某标准主题的在架计数；归零即删除行（导航只查非零）。"""
    row = conn.execute(
        "SELECT count FROM subject_counts WHERE standard_subject = ?",
        (standard_subject,),
    ).fetchone()
    current = row["count"] if row else 0
    new_count = current + delta
    if new_count <= 0:
        conn.execute(
            "DELETE FROM subject_counts WHERE standard_subject = ?",
            (standard_subject,),
        )
    elif row is None:
        conn.execute(
            "INSERT INTO subject_counts (standard_subject, count) VALUES (?, ?)",
            (standard_subject, new_count),
        )
    else:
        conn.execute(
            "UPDATE subject_counts SET count = ? WHERE standard_subject = ?",
            (new_count, standard_subject),
        )


def sync_record_subjects(conn: sqlite3.Connection, *, identifier: str,
                         new_status: str, new_subjects: set[str],
                         old_status: str | None,
                         old_subjects: set[str],
                         mapping_exists: bool) -> None:
    """补采增量：只根据本批这一条书目变更前后的主题/在架状态调整计数。

    不重扫全库。计数与书目写入同一事务，失败随外层一起回滚。
    """
    was_active = old_status == "active"
    is_active = new_status == "active"

    # 没有词表时不维护索引与计数（换表会全量重建）
    if not mapping_exists:
        return

    if is_active:
        conn.execute(
            "DELETE FROM record_subjects WHERE identifier = ?", (identifier,)
        )
        conn.executemany(
            "INSERT OR IGNORE INTO record_subjects "
            "(identifier, standard_subject) VALUES (?, ?)",
            [(identifier, subject) for subject in new_subjects],
        )

    effective_new = new_subjects if is_active else set()
    effective_old = old_subjects if was_active else set()
    for subject in effective_old - effective_new:
        _adjust_count(conn, subject, -1)
    for subject in effective_new - effective_old:
        _adjust_count(conn, subject, +1)

    if not is_active:
        # 撤下/墓碑：该书目不再挂任何主题
        conn.execute(
            "DELETE FROM record_subjects WHERE identifier = ?", (identifier,)
        )


def list_subject_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """导航：各标准主题的非零计数，按主题名排序。"""
    return conn.execute(
        "SELECT standard_subject, count FROM subject_counts "
        "WHERE count > 0 ORDER BY standard_subject"
    ).fetchall()
