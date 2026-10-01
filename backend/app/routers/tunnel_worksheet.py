"""隧道消防工作表接口。

所有写接口都只做参数转发和错误码转换；合并、回写和并发补写规则在服务层完成。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.tunnel_worksheet import WorksheetError, service

router = APIRouter(prefix="/api/tunnel-worksheets", tags=["隧道消防工作表"])


def _error(exc: WorksheetError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("", response_model=PageResult[dict])
def list_drafts(
    tunnel_code: str | None = Query(default=None, description="按隧道编号筛选"),
    status: str | None = Query(default=None, description="草稿、已确认、已回写"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    items, total = service.list_drafts(tunnel_code=tunnel_code, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/work-orders")
def list_work_orders(tunnel_code: str | None = None) -> dict[str, object]:
    items = service.list_work_orders(tunnel_code=tunnel_code)
    return {"items": items, "total": len(items)}


@router.get("/todos")
def list_todos(tunnel_code: str | None = None) -> dict[str, object]:
    items = service.list_todos(tunnel_code=tunnel_code)
    return {"items": items, "total": len(items)}


@router.post("", response_model=ActionResult)
def save_draft(payload: EntryPayload) -> ActionResult:
    try:
        draft = service.save_draft(payload.values)
    except WorksheetError as exc:
        raise _error(exc) from exc
    return ActionResult(ok=True, message="消防草稿已按隧道编号和本地序列号保存", entry=draft)


@router.post("/merge", response_model=ActionResult)
def merge_draft(payload: EntryPayload) -> ActionResult:
    try:
        draft = service.merge_draft(payload.values)
    except WorksheetError as exc:
        raise _error(exc) from exc
    return ActionResult(ok=True, message="已按检查员最后确认的现场版本合并草稿", entry=draft)


@router.post("/migrate", response_model=ActionResult)
def migrate_legacy_draft(payload: EntryPayload) -> ActionResult:
    try:
        draft = service.migrate_legacy_draft(payload.values)
    except WorksheetError as exc:
        raise _error(exc) from exc
    return ActionResult(ok=True, message="旧草稿已迁移，来源和原始载荷均已保留", entry=draft)


@router.get("/{draft_id}")
def get_draft(draft_id: int) -> dict:
    try:
        return service.get_draft(draft_id)
    except WorksheetError as exc:
        raise _error(exc) from exc


@router.post("/{draft_id}/evidence", response_model=ActionResult)
def append_evidence(draft_id: int, payload: EntryPayload) -> ActionResult:
    try:
        draft = service.append_evidence(draft_id, payload.values)
    except WorksheetError as exc:
        raise _error(exc) from exc
    return ActionResult(ok=True, message="现场证据已追加，未覆盖既有记录", entry=draft)


@router.post("/{draft_id}/write-back")
def write_back(draft_id: int, payload: EntryPayload | None = None) -> dict:
    values = payload.values if payload is not None else {}
    try:
        return service.write_back(draft_id, values)
    except WorksheetError as exc:
        raise _error(exc) from exc
