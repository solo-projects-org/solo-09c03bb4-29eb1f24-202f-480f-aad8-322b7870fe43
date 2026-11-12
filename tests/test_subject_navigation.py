"""主题导航端到端测试：词表换表、去重计数、补采增量、事务与并发一致性。"""
from __future__ import annotations

import threading

from app import db as db_mod
from app.locking import WRITE_LOCK

# 馆员提交的对照表：旧词/新词混用，原词精确匹配
MAPPING = {
    "电脑": "信息技术",
    "计算机": "信息技术",
    "机械": "工程技术",
    "机械工程": "工程技术",
    "医学": "医药卫生",
    "医药": "医药卫生",
}


def _harvest(api, payload):
    resp = api.post("/api/v1/harvest", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _put_mapping(api, mapping=MAPPING):
    resp = api.put("/api/v1/subject-mapping", json={"mapping": mapping})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _nav_counts(api):
    resp = api.get("/api/v1/subject-navigation")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body["version"], {item["standard_subject"]: item["count"]
                             for item in body["subjects"]}


def test_no_mapping_means_empty_navigation(client):
    api, _db, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})

    # 尚未提交词表：导航为空且版本为 None，计数不会凭空出现
    version, counts = _nav_counts(api)
    assert version is None
    assert counts == {}

    got = api.get("/api/v1/subject-mapping").json()
    assert got["version"] is None
    assert got["mapping"] == {}


def test_replace_mapping_builds_counts_from_active_catalog(client):
    api, _db, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})

    result = _put_mapping(api)
    # 首窗 14 条在架：信息技术 4、工程技术 5、医药卫生 5
    assert result["version"] == 1
    assert result["entries"] == 6
    assert result["standard_subjects"] == 3
    assert result["active_records_indexed"] == 14
    assert result["subject_counts"] == {
        "信息技术": 4, "工程技术": 5, "医药卫生": 5,
    }

    version, counts = _nav_counts(api)
    assert version == 1
    assert counts == {"信息技术": 4, "工程技术": 5, "医药卫生": 5}


def test_two_raw_terms_same_subject_counted_once_per_record(client):
    api, db, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-27"})
    _put_mapping(api)

    conn = db.get_connection()
    try:
        # rec-016 同时带“电脑/计算机”两个原词：索引里只占一行
        rows = conn.execute(
            "SELECT standard_subject FROM record_subjects "
            "WHERE identifier = 'oai:sample.lib:rec-016'"
        ).fetchall()
        assert [r["standard_subject"] for r in rows] == ["信息技术"]

        # rec-024 跨两个标准主题：两个主题各计一次
        rows = conn.execute(
            "SELECT standard_subject FROM record_subjects "
            "WHERE identifier = 'oai:sample.lib:rec-024'"
        ).fetchall()
        assert {r["standard_subject"] for r in rows} == {
            "工程技术", "信息技术",
        }

        # rec-020 的“杂项”未收录：不挂任何主题
        assert conn.execute(
            "SELECT COUNT(*) FROM record_subjects "
            "WHERE identifier = 'oai:sample.lib:rec-020'"
        ).fetchone()[0] == 0

        # 去重索引：25 条单主题在架各占 1 行 + rec-024 跨两主题占 2 行 = 26 行
        # （rec-020 原词未收录、rec-013 已撤下，均不占行）
        assert conn.execute(
            "SELECT COUNT(*) FROM record_subjects"
        ).fetchone()[0] == 26
    finally:
        conn.close()

    # 全量在架 26 条：9 / 9 / 8，导航只返回非零主题
    version, counts = _nav_counts(api)
    assert version == 1
    assert counts == {"信息技术": 9, "工程技术": 9, "医药卫生": 8}
    assert all(value > 0 for value in counts.values())


def test_incremental_harvest_adjusts_counts_without_full_rescan(client):
    api, _db, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    _put_mapping(api)

    # 续采 10-15~10-27：只按本批书目前后状态做增量，词表版本不变
    result = _harvest(api, {"until": "2026-10-27"})
    assert result["inserted"] == 12

    version, counts = _nav_counts(api)
    assert version == 1  # 没有换表，版本保持
    assert counts == {"信息技术": 9, "工程技术": 9, "医药卫生": 8}


def test_reharvest_withdrawn_record_decrements_subject_count(client):
    api, _db, oai_mod = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-27"})
    _put_mapping(api)

    # 源馆把 rec-001（信息技术）标记删除，重新补采同一区间
    rec = next(r for r in oai_mod.RECORDS
               if r["identifier"] == "oai:sample.lib:rec-001")
    rec["deleted"] = True
    try:
        _harvest(api, {"from": "2026-10-01", "until": "2026-10-27"})
    finally:
        rec["deleted"] = False

    catalog = api.get(
        "/api/v1/catalog/oai:sample.lib:rec-001"
    ).json()
    assert catalog["status"] == "withdrawn"

    _, counts = _nav_counts(api)
    assert counts == {"信息技术": 8, "工程技术": 9, "医药卫生": 8}


def test_reharvest_changed_subject_moves_count_between_subjects(client):
    api, _db, oai_mod = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-27"})
    _put_mapping(api)

    # 源馆把 rec-002 的主题从“机械”改成“计算机”：工程技术 -1、信息技术 +1
    rec = next(r for r in oai_mod.RECORDS
               if r["identifier"] == "oai:sample.lib:rec-002")
    original = rec["subjects"]
    rec["subjects"] = ["计算机"]
    try:
        _harvest(api, {"from": "2026-10-01", "until": "2026-10-27"})
    finally:
        rec["subjects"] = original

    _, counts = _nav_counts(api)
    assert counts == {"信息技术": 10, "工程技术": 8, "医药卫生": 8}


def test_replace_mapping_rebuilds_and_bumps_version(client):
    api, _db, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-27"})
    _put_mapping(api)

    # 换成只保留信息技术两个原词的词表：按全库在架重建，其余主题计数消失
    result = _put_mapping(api, {"电脑": "信息技术", "计算机": "信息技术"})
    assert result["version"] == 2
    assert result["subject_counts"] == {"信息技术": 9}

    version, counts = _nav_counts(api)
    assert version == 2
    assert counts == {"信息技术": 9}

    got = api.get("/api/v1/subject-mapping").json()
    assert got["version"] == 2
    assert got["mapping"] == {"电脑": "信息技术", "计算机": "信息技术"}


def test_blank_or_empty_mapping_rejected(client):
    api, _db, _oai = client
    assert api.put(
        "/api/v1/subject-mapping", json={"mapping": {}}
    ).status_code == 422
    assert api.put(
        "/api/v1/subject-mapping", json={"mapping": {"": "信息技术"}}
    ).status_code == 422
    assert api.put(
        "/api/v1/subject-mapping", json={"mapping": {"电脑": "  "}}
    ).status_code == 422


def test_failed_harvest_rolls_back_counts_and_progress(client, monkeypatch):
    api, db, oai_mod = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-27"})
    _put_mapping(api)
    before_version, before_counts = _nav_counts(api)

    # 令牌持续失效：整批失败，书目、计数、截止日一起回滚
    real_consume = oai_mod.TOKENS.consume

    def always_invalid(token):
        real_consume(token)
        return None

    monkeypatch.setattr(oai_mod.TOKENS, "consume", always_invalid)
    resp = api.post(
        "/api/v1/harvest",
        json={"from": "2026-10-01", "until": "2026-10-27"},
    )
    assert resp.status_code == 502

    version, counts = _nav_counts(api)
    assert version == before_version
    assert counts == before_counts
    conn = db.get_connection()
    try:
        assert db.get_last_until(conn) == "2026-10-27"
    finally:
        conn.close()


def test_failed_replace_rolls_back_mapping_and_counts(client, monkeypatch):
    api, db, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-27"})
    _put_mapping(api)

    # 让换表事务在版本 upsert（词表/索引/计数写完后的收尾步骤）失败，
    # 触发整体回滚；只抛一次，回滚后的查询不受影响
    real_now = db_mod.utc_now_iso
    call_count = {"n": 0}

    def boom_once():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("模拟换表收尾失败")
        return real_now()

    monkeypatch.setattr(db_mod, "utc_now_iso", boom_once)
    resp = api.put(
        "/api/v1/subject-mapping",
        json={"mapping": {"电脑": "信息技术"}},
    )
    assert resp.status_code == 500

    # 回滚后词表、版本、计数全部保持换表前状态
    got = api.get("/api/v1/subject-mapping").json()
    assert got["version"] == 1
    assert len(got["mapping"]) == 6
    _, counts = _nav_counts(api)
    assert counts == {"信息技术": 9, "工程技术": 9, "医药卫生": 8}
    # 锁已释放，后续写操作仍可进行
    assert WRITE_LOCK.acquire(blocking=False)
    WRITE_LOCK.release()


def test_replace_mapping_conflicts_with_running_harvest(client):
    api, _db, _oai = client
    WRITE_LOCK.acquire()
    try:
        resp = api.put(
            "/api/v1/subject-mapping", json={"mapping": MAPPING}
        )
        assert resp.status_code == 409
    finally:
        WRITE_LOCK.release()


def test_harvest_conflicts_with_running_replace(client):
    api, _db, _oai = client
    WRITE_LOCK.acquire()
    try:
        resp = api.post(
            "/api/v1/harvest",
            json={"from": "2026-10-01", "until": "2026-10-15"},
        )
        assert resp.status_code == 409
    finally:
        WRITE_LOCK.release()


def test_reader_never_sees_partial_write_state(client):
    """写事务未提交时，其他连接只能看到上一版已提交的词表与计数。"""
    api, db, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-27"})
    _put_mapping(api)  # 先建立第 1 版词表与计数

    # SQLite 连接不能跨线程，写连接与读连接都在工作线程内创建
    entered = threading.Event()
    reader_done = threading.Event()
    release = threading.Event()
    snapshot: dict = {}
    error: Exception | None = None

    def worker():
        nonlocal error
        conn_w = db.get_connection()
        conn_r = db.get_connection()
        try:
            with db.transaction(conn_w):
                db.replace_subject_mapping(
                    conn_w, {"电脑": "信息技术", "计算机": "信息技术"}
                )
                entered.set()
                # 写事务挂起期间，用另一个连接读：必须仍是旧版快照
                snapshot["version"] = db.get_mapping_version(conn_r)
                snapshot["subjects"] = {
                    row["standard_subject"]
                    for row in db.list_subject_counts(conn_r)
                }
                reader_done.set()
                release.wait(timeout=5)
                conn_w.commit()
        except Exception as exc:  # pragma: no cover - 失败即让断言报错
            error = exc
            entered.set()
        finally:
            conn_w.close()
            conn_r.close()

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(timeout=5)
    assert reader_done.wait(timeout=5)
    try:
        # 写事务未提交：读者看到的仍是 version=1 的三主题计数
        assert snapshot["version"] == 1
        assert snapshot["subjects"] == {
            "信息技术", "工程技术", "医药卫生",
        }
    finally:
        release.set()
        thread.join(timeout=5)

    assert error is None
    # 提交后新连接看到新版
    conn = db.get_connection()
    try:
        assert db.get_mapping_version(conn) == 2
        assert {r["standard_subject"] for r in db.list_subject_counts(conn)} == {
            "信息技术",
        }
    finally:
        conn.close()
