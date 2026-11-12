"""端到端验证：补采失败时主题计数与书目、截止日一起回滚。

用法（在仓库根目录、依赖已安装的解释器下）：
    python -I scripts/test_subject_rollback.py
脚本独立建库，不影响 /data 下的正式数据。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, harvester  # noqa: E402
from app.oai_client import OAIError, OAIPage  # noqa: E402


class FlakyClient:
    """首页成功、续页令牌持续失效：重抓一次后仍失败，整批必须回滚。"""

    def __init__(self) -> None:
        pass

    def __enter__(self) -> "FlakyClient":
        return self

    def __exit__(self, *exc) -> None:
        pass

    def close(self) -> None:
        pass

    def list_records_first(self, *, from_date, until_date):
        return OAIPage(
            records=[{"fake": True}],
            resumption_token="token-a",
            complete_list_size=27,
            cursor=0,
        )

    def list_records_resume(self, token):
        raise OAIError("badResumptionToken", "模拟令牌持续失效")


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="subject-rollback-")
    db_path = os.path.join(tmpdir, "failtest.db")
    db.DB_PATH = db_path  # harvester 通过模块级 DB_PATH 取连接
    db.init_db(db_path)

    conn = db.get_connection(db_path)
    with db.transaction(conn):
        # 预置一条在架书目和一版词表计数，制造“失败前已有计数”的状态
        now = db.utc_now_iso()
        conn.execute(
            "INSERT INTO bib_records (identifier, datestamp, status, raw_dc, "
            "updated_at) VALUES (?, ?, 'active', ?, ?)",
            ("oai:sample.lib:rec-001", "2026-10-01",
             '{"subject": ["计算机"]}', now),
        )
        conn.execute(
            "INSERT INTO subject_mapping (raw_subject, standard_subject) "
            "VALUES ('计算机', '信息技术')"
        )
        conn.execute(
            "INSERT INTO subject_mapping_version (id, version, replaced_at) "
            "VALUES (1, 1, ?)", (now,),
        )
        conn.execute(
            "INSERT INTO record_subjects (identifier, standard_subject) "
            "VALUES ('oai:sample.lib:rec-001', '信息技术')"
        )
        conn.execute(
            "INSERT INTO subject_counts (standard_subject, count) "
            "VALUES ('信息技术', 1)"
        )
        conn.commit()
    conn.close()

    try:
        harvester.harvest(
            FlakyClient(), until_date="2026-10-27", from_date="2026-10-01"
        )
    except OAIError as exc:
        print("预期失败抛出:", exc)
    else:
        print("ERROR: 采集竟然成功了")
        return 1

    conn = db.get_connection(db_path)
    try:
        bib_count = conn.execute(
            "SELECT COUNT(*) FROM bib_records"
        ).fetchone()[0]
        counts = dict(conn.execute(
            "SELECT standard_subject, count FROM subject_counts"
        ).fetchall())
        index_rows = conn.execute(
            "SELECT COUNT(*) FROM record_subjects"
        ).fetchone()[0]
        last_until = db.get_last_until(conn)
        run = conn.execute(
            "SELECT status FROM harvest_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    print("书目条数（应仍为 1）:", bib_count)
    print("主题计数（应仍为 信息技术=1）:", counts)
    print("去重索引行数（应仍为 1）:", index_rows)
    print("last_until（应为 None）:", last_until)
    print("最近运行状态（应为 failed）:", run["status"])

    ok = (
        bib_count == 1
        and counts == {"信息技术": 1}
        and index_rows == 1
        and last_until is None
        and run["status"] == "failed"
    )
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
