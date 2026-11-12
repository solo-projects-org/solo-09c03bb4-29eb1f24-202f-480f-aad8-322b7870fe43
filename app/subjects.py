"""主题词表服务：馆员提交 JSON 对照表并重建在架书目计数。

换表语义：
- 请求体为 {dc:subject 原词: 标准主题}，原词精确匹配，未收录原词不计；
- 词表、书目主题索引、非零计数在同一个 SQLite 事务内整体替换，
  任何失败全部回滚，原书目一行都不改；
- 换表期间与补采互斥（共享 WRITE_LOCK），查询在 WAL 快照隔离下
  只能看到某一次已提交的“词表 + 计数”组合，看不到中间态。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import db
from .locking import WRITE_LOCK, WriteConflictError


@dataclass
class MappingReplaceResult:
    version: int
    entries: int
    standard_subjects: int
    active_records_indexed: int
    subject_counts: dict[str, int]


def replace_mapping(mapping: dict[str, str]) -> MappingReplaceResult:
    if not WRITE_LOCK.acquire(blocking=False):
        raise WriteConflictError("已有补采或换表任务正在执行，请稍后再试")
    conn = db.get_connection()
    try:
        try:
            with db.transaction(conn):
                version = db.replace_subject_mapping(conn, mapping)
                indexed = conn.execute(
                    "SELECT COUNT(*) FROM record_subjects"
                ).fetchone()[0]
                counts = {
                    row["standard_subject"]: row["count"]
                    for row in db.list_subject_counts(conn)
                }
                conn.commit()
        except Exception:
            # 词表与计数任一环节失败：整体回滚，原书目不动
            conn.rollback()
            raise
        return MappingReplaceResult(
            version=version,
            entries=len(mapping),
            standard_subjects=len(set(mapping.values())),
            active_records_indexed=indexed,
            subject_counts=counts,
        )
    finally:
        conn.close()
        WRITE_LOCK.release()
