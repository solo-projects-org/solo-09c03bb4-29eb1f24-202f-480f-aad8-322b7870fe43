"""FastAPI 入口：Swagger 可发起采集、查询目录与进度。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException, Query

from . import db
from .harvester import HarvestConflictError, harvest
from .locking import WriteConflictError
from .oai_client import OAIClient, OAIError
from .schemas import (
    BibRecordOut,
    HarvestRequest,
    HarvestResponse,
    ProgressResponse,
    RunOut,
    SubjectCountItem,
    SubjectMappingOut,
    SubjectMappingReplaceResponse,
    SubjectMappingRequest,
    SubjectNavigationResponse,
)
from .subjects import replace_mapping

OAI_ENDPOINT = os.environ.get(
    "OAI_ENDPOINT", "http://oai-source:8080/oai"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="联合目录补采采集服务",
    description=(
        "通过 OAI-PMH ListRecords(oai_dc) 按日期补采单馆书目，并维护主题导航计数。\n\n"
        "- 首次采集另填起始日；之后从上次成功采集的截止日（含当天）续采\n"
        "- 续页只用 resumptionToken；令牌失效则丢弃已取页、按原区间重抓一次\n"
        "- 整批采完才在一个事务内提交书目变更、主题计数增量和截止日，无匹配也推进截止日\n"
        "- PUT /api/v1/subject-mapping 提交原词→标准主题对照表，按全部在架书目\n"
        "  完整 dc:subject 重建计数；同书目同主题只计一次，未收录原词不计\n"
        "- GET /api/v1/subject-navigation 查询各标准主题的非零在架计数"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


def _validate_iso_date(value: str, field: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"{field} 必须是 YYYY-MM-DD 格式"
        ) from exc
    return value


@app.post(
    "/api/v1/harvest",
    response_model=HarvestResponse,
    summary="发起一次补采",
    tags=["采集"],
)
def trigger_harvest(req: HarvestRequest) -> HarvestResponse:
    _validate_iso_date(req.until_date, "截止日")
    if req.from_date is not None:
        _validate_iso_date(req.from_date, "起始日")
        if req.from_date > req.until_date:
            raise HTTPException(
                status_code=422, detail="起始日不能晚于截止日（起止日都包含）"
            )

    with OAIClient(OAI_ENDPOINT) as client:
        try:
            summary = harvest(
                client,
                until_date=req.until_date,
                from_date=req.from_date,
            )
        except HarvestConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OAIError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "采集失败，本批书目与截止日均未提交（已回滚）",
                    "oai_error": exc.code,
                    "detail": exc.message,
                },
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"采集失败，本批变更已回滚：{exc}",
            ) from exc

    return HarvestResponse(**summary.__dict__)


@app.get(
    "/api/v1/catalog",
    response_model=list[BibRecordOut],
    summary="查询本地联合目录",
    tags=["目录"],
)
def list_catalog(
    status: str = Query(
        "active",
        description="active=在架（默认），withdrawn=已撤下，all=全部",
    ),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[BibRecordOut]:
    allowed = {"active", "withdrawn", "all"}
    if status not in allowed:
        raise HTTPException(
            status_code=422, detail=f"status 只支持 {sorted(allowed)}"
        )
    conn = db.get_connection()
    try:
        if status == "all":
            rows = conn.execute(
                "SELECT * FROM bib_records ORDER BY identifier LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM bib_records WHERE status = ? "
                "ORDER BY identifier LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        return [BibRecordOut(**dict(row)) for row in rows]
    finally:
        conn.close()


@app.get(
    "/api/v1/catalog/{identifier}",
    response_model=BibRecordOut,
    summary="按 OAI 标识查询单条书目",
    tags=["目录"],
)
def get_record(identifier: str) -> BibRecordOut:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM bib_records WHERE identifier = ?", (identifier,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        return BibRecordOut(**dict(row))
    finally:
        conn.close()


@app.get(
    "/api/v1/progress",
    response_model=ProgressResponse,
    summary="查询采集进度（上次成功截止日）",
    tags=["进度"],
)
def get_progress() -> ProgressResponse:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT last_until, last_harvest_at FROM harvest_progress WHERE id = 1"
        ).fetchone()
        if row is None:
            return ProgressResponse(last_until=None, last_harvest_at=None)
        return ProgressResponse(**dict(row))
    finally:
        conn.close()


@app.get(
    "/api/v1/runs",
    response_model=list[RunOut],
    summary="查询采集运行历史",
    tags=["进度"],
)
def list_runs(limit: int = Query(20, ge=1, le=200)) -> list[RunOut]:
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM harvest_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [RunOut(**dict(row)) for row in rows]
    finally:
        conn.close()


@app.get("/health", tags=["系统"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


@app.put(
    "/api/v1/subject-mapping",
    response_model=SubjectMappingReplaceResponse,
    summary="提交主题对照表并按在架书目重建计数（换表）",
    tags=["主题导航"],
)
def put_subject_mapping(
    req: SubjectMappingRequest,
) -> SubjectMappingReplaceResponse:
    """馆员换词表：

    - 请求体为 ``{dc:subject 原词: 标准主题}``，按原词精确匹配，未收录原词不计；
    - 按全部在架书目的完整多值 ``dc:subject`` 重建主题索引与非零计数，
      同一条书目在同一标准主题下只计一次；
    - 词表与计数在同一事务内整体提交，失败全部回滚，原书目不做任何修改；
    - 与补采互斥，撞单时返回 409。
    """
    try:
        result = replace_mapping(req.mapping)
    except WriteConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"换表失败，词表与计数均未提交（已回滚）：{exc}",
        ) from exc
    return SubjectMappingReplaceResponse(
        status="success",
        version=result.version,
        entries=result.entries,
        standard_subjects=result.standard_subjects,
        active_records_indexed=result.active_records_indexed,
        subject_counts=result.subject_counts,
    )


@app.get(
    "/api/v1/subject-mapping",
    response_model=SubjectMappingOut,
    summary="查询当前生效的主题对照表",
    tags=["主题导航"],
)
def get_subject_mapping() -> SubjectMappingOut:
    conn = db.get_connection()
    try:
        return SubjectMappingOut(
            version=db.get_mapping_version(conn),
            mapping=db.get_subject_mapping(conn),
        )
    finally:
        conn.close()


@app.get(
    "/api/v1/subject-navigation",
    response_model=SubjectNavigationResponse,
    summary="主题导航：各标准主题的在架书目非零计数",
    tags=["主题导航"],
)
def get_subject_navigation() -> SubjectNavigationResponse:
    """只返回计数非零的标准主题；同一书目在同一主题下只计一次。"""
    conn = db.get_connection()
    try:
        rows = db.list_subject_counts(conn)
        return SubjectNavigationResponse(
            version=db.get_mapping_version(conn),
            subjects=[
                SubjectCountItem(
                    standard_subject=row["standard_subject"],
                    count=row["count"],
                )
                for row in rows
            ],
        )
    finally:
        conn.close()
