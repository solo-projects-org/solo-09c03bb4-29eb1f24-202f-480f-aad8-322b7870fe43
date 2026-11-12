"""请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class HarvestRequest(BaseModel):
    until_date: str = Field(
        ...,
        alias="until",
        description="采集截止日（含当天），格式 YYYY-MM-DD",
        examples=["2026-10-15"],
    )
    from_date: str | None = Field(
        default=None,
        alias="from",
        description=(
            "起始日（含当天），首次采集另填；之后留空则自动从上次成功"
            "采集的截止日续采"
        ),
        examples=["2026-10-01"],
    )

    model_config = {"populate_by_name": True}


class HarvestResponse(BaseModel):
    status: str = Field(..., description="success / failed")
    from_date: str = Field(..., description="实际使用的起始日（含）")
    until_date: str = Field(..., description="截止日（含）")
    pages: int = Field(..., description="本次成功拉取的页数")
    fetched: int = Field(..., description="拉取到的记录总数（含删除标记）")
    inserted: int
    updated: int
    withdrawn: int = Field(..., description="因删除标记撤下的记录数")
    retries: int = Field(..., description="令牌失效后按原区间重抓的次数（0 或 1）")
    run_id: int
    note: str | None = None


class ProgressResponse(BaseModel):
    last_until: str | None = Field(
        None, description="上次成功采集的截止日（含）；无记录表示尚未采集"
    )
    last_harvest_at: str | None = None


class BibRecordOut(BaseModel):
    identifier: str
    datestamp: str
    status: str
    title: str | None
    creator: str | None
    subject: str | None = Field(
        None, description="首个 dc:subject 原词；完整多值见原始记录"
    )
    date: str | None
    identifier_dc: str | None
    updated_at: str


class RunOut(BaseModel):
    id: int
    started_at: str
    finished_at: str | None
    from_date: str
    until_date: str
    status: str
    pages: int
    fetched: int
    inserted: int
    updated: int
    withdrawn: int
    retries: int
    error: str | None


class SubjectMappingRequest(BaseModel):
    """馆员提交的 JSON 对照表：键为 dc:subject 原词，值为标准主题。

    原词精确匹配；未收录进对照表的原词不计入任何主题。
    """

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "电脑": "信息技术",
                    "计算机": "信息技术",
                    "机械": "工程技术",
                }
            ]
        }
    }

    mapping: dict[str, str]

    @model_validator(mode="after")
    def _validate_non_blank(self) -> "SubjectMappingRequest":
        if not self.mapping:
            raise ValueError("对照表不能为空，至少包含一条原词 -> 标准主题")
        blanks = [
            key for key, value in self.mapping.items()
            if not key.strip() or not value.strip()
        ]
        if blanks:
            raise ValueError("原词和标准主题都不能是空字符串或纯空白")
        return self


class SubjectMappingReplaceResponse(BaseModel):
    status: str = Field("success", description="success / failed")
    version: int = Field(..., description="换表后的词表版本（每次换表 +1）")
    entries: int = Field(..., description="对照表条目数（不同原词数）")
    standard_subjects: int = Field(..., description="对照表覆盖的标准主题数")
    active_records_indexed: int = Field(
        ..., description="重建后命中至少一个标准主题的在架书目数（去重后）"
    )
    subject_counts: dict[str, int] = Field(
        ..., description="各标准主题的在架书目非零计数"
    )


class SubjectMappingOut(BaseModel):
    version: int | None = Field(
        None, description="当前词表版本；尚未提交过词表为 null"
    )
    mapping: dict[str, str] = Field(
        ..., description="当前生效的对照表（原词 -> 标准主题）"
    )


class SubjectCountItem(BaseModel):
    standard_subject: str = Field(..., description="标准主题")
    count: int = Field(..., gt=0, description="该主题下去重后的在架书目数")


class SubjectNavigationResponse(BaseModel):
    version: int | None = Field(
        None, description="计数对应的词表版本；尚未提交过词表为 null"
    )
    subjects: list[SubjectCountItem] = Field(
        ..., description="非零计数的标准主题列表"
    )
