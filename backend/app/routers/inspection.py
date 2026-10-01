"""隧道现场检修接口。

覆盖缺陷复现的三个入口共用的数据链路：

* 隧道管养列表页：``GET /api/tunnel/inspection/digest`` 返回每座隧道的未收口
  草稿/待办/工单计数，回写后列表不再残留同一草稿。
* 隧道详情页：通风笔记草稿续存、现场确认、证据读取、并发补写（409）。
* 消防工作表：消防检查项草稿、与通风草稿合并、回写检修单（幂等）。

另提供历史工单列表（原附件快照留痕）、设施待办与四方一致性核对。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.inspection import (
    ConflictError,
    InspectionError,
    inspection_service as service,
)

router = APIRouter(prefix="/api/tunnel/inspection", tags=["隧道现场检修"])


class InspectionPayload(BaseModel):
    tunnel_code: str | None = None
    section: str | None = None
    inspector: str | None = None
    content: str | None = None
    client_seq: int | None = None
    draft_id: int | None = None
    source: str | None = None
    origin: str | None = None
    title: str | None = None
    base_version: int | None = None
    confirmed: bool = False
    sections: dict[str, Any] | list[Any] | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class MergePayload(BaseModel):
    surviving_draft_id: int | None = None
    absorbed_draft_ids: list[int] = Field(default_factory=list)
    operator: str | None = None


class ConfirmPayload(BaseModel):
    sections: list[str] | None = None


def _bad(exc: InspectionError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


# ----------------------------------------------------------------------- 草稿

@router.post("/drafts")
def save_draft(payload: InspectionPayload) -> dict[str, Any]:
    """续存现场草稿（通风笔记 / 消防检查项），弱网重连后按 draft_id 续传不丢内容。"""
    try:
        return service.save_draft(payload.model_dump())
    except InspectionError as exc:
        raise _bad(exc) from exc


@router.get("/drafts")
def list_drafts(tunnel_code: str | None = None) -> dict[str, Any]:
    return {"items": service.list_drafts(tunnel_code)}


@router.post("/drafts/{draft_id}/confirm")
def confirm_draft(draft_id: int, payload: ConfirmPayload) -> dict[str, Any]:
    """检查员现场最后核对确认；被确认版本在合并时优先。"""
    try:
        return service.confirm_draft(draft_id, payload.model_dump())
    except InspectionError as exc:
        raise _bad(exc) from exc


@router.post("/tunnels/{tunnel_code}/merge")
def merge_drafts(tunnel_code: str, payload: MergePayload) -> dict[str, Any]:
    """合并同一隧道多份草稿：检查员最后确认的现场版本为准，旧草稿保留来源。"""
    try:
        return service.merge_drafts(tunnel_code, payload.model_dump())
    except InspectionError as exc:
        raise _bad(exc) from exc


@router.post("/migrations/legacy-draft")
def migrate_legacy_draft(payload: InspectionPayload) -> dict[str, Any]:
    """旧系统草稿迁移：必须带 origin，来源写入 provenance 长期保留。"""
    try:
        return service.migrate_legacy(payload.model_dump())
    except InspectionError as exc:
        raise _bad(exc) from exc


# --------------------------------------------------------------------- 证据

@router.get("/tunnels/{tunnel_code}/evidence")
def get_evidence(tunnel_code: str) -> dict[str, Any]:
    try:
        return service.evidence(tunnel_code)
    except InspectionError as exc:
        raise _bad(exc) from exc


@router.post("/evidence")
def patch_evidence(payload: InspectionPayload) -> dict[str, Any]:
    """并发补写现场证据：base_version 过期返回 409，不覆盖他人先写入的内容。"""
    try:
        return service.patch_evidence(payload.model_dump())
    except InspectionError as exc:
        raise _bad(exc) from exc
    except ConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": "现场证据已被他人更新，请基于最新版本补写", "current": exc.current},
        ) from exc


# --------------------------------------------------------------------- 工单

@router.post("/work-orders")
def submit_work_order(payload: InspectionPayload) -> dict[str, Any]:
    """回写检修单：草稿、附件、工单号同一事务；(隧道编号, 本地序列号) 幂等。"""
    try:
        order, created = service.submit_work_order(payload.model_dump())
    except InspectionError as exc:
        raise _bad(exc) from exc
    return {"created": created, "order": order}


@router.get("/work-orders")
def list_work_orders(tunnel_code: str | None = None) -> dict[str, Any]:
    """历史工单按提交时的原附件快照留痕。"""
    return {"items": service.list_work_orders(tunnel_code)}


@router.get("/todos")
def list_todos(tunnel_code: str | None = None) -> dict[str, Any]:
    return {"items": service.list_todos(tunnel_code)}


# --------------------------------------------------------------- 列表/一致性

@router.get("/digest")
def digest() -> dict[str, Any]:
    """隧道列表页与详情页共用的草稿/待办/工单计数口径。"""
    return {"items": service.list_digest(service.tunnel_codes())}


@router.get("/tunnels/{tunnel_code}/consistency")
def consistency(tunnel_code: str) -> dict[str, Any]:
    """核对隧道档案、设施待办、工单、草稿是否一致。"""
    try:
        return service.consistency(tunnel_code)
    except InspectionError as exc:
        raise _bad(exc) from exc
