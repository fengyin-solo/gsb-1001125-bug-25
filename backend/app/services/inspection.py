"""隧道现场检修业务编排：草稿续存/合并、检修单回写、并发补写与一致性核对。

控制器只做参数解析，领域规则与事务边界都收在这里。
"""
from __future__ import annotations

from typing import Any

from app.inspection_store import (
    OptimisticLockError,
    inspection_store as istore,
)
from app.store import store

TUNNEL_MODULE = "tunnel"
VENTILATION_SECTION = "通风"
FIRE_SECTION = "消防"
SECTIONS = (VENTILATION_SECTION, FIRE_SECTION)


class InspectionError(ValueError):
    """业务校验失败（对应 400）。"""


class ConflictError(Exception):
    """乐观锁冲突（对应 409），携带服务端当前证据版本。"""

    def __init__(self, current: dict[str, Any]) -> None:
        super().__init__("现场证据版本已过期")
        self.current = current


class InspectionService:
    # ------------------------------------------------------------- 隧道与档案

    def tunnel_codes(self) -> list[str]:
        return [str(row["隧道编号"]) for row in store.rows(TUNNEL_MODULE) if row.get("隧道编号")]

    def _require_tunnel(self, tunnel_code: str) -> dict[str, Any]:
        for row in store.rows(TUNNEL_MODULE):
            if str(row.get("隧道编号")) == tunnel_code:
                return row
        raise InspectionError(f"隧道编号 {tunnel_code} 不存在或已归档")

    # ------------------------------------------------------------------ 草稿

    def save_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        tunnel_code = self._code(payload)
        section = self._section(payload)
        inspector = str(payload.get("inspector") or "").strip()
        content = str(payload.get("content") or "")
        client_seq = self._client_seq(payload)
        draft_id = payload.get("draft_id")
        attachments = self._attachments(payload)
        if not inspector:
            raise InspectionError("缺少检查员")
        self._require_tunnel(tunnel_code)
        with istore.transaction():
            return istore.upsert_draft(
                tunnel_code=tunnel_code,
                section=section,
                inspector=inspector,
                content=content,
                client_seq=client_seq,
                source=str(payload.get("source") or "现场"),
                attachments=attachments,
                draft_id=int(draft_id) if draft_id else None,
            )

    def confirm_draft(self, draft_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        sections = payload.get("sections")
        sections = [str(s) for s in sections] if isinstance(sections, list) else None
        with istore.transaction():
            try:
                return istore.mark_confirmed(draft_id, sections)
            except KeyError as exc:
                raise InspectionError(str(exc).strip("'")) from exc

    def merge_drafts(self, tunnel_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        surviving_id = payload.get("surviving_draft_id")
        absorbed_ids = payload.get("absorbed_draft_ids") or []
        operator = str(payload.get("operator") or "").strip()
        if not surviving_id:
            raise InspectionError("缺少主草稿 id（surviving_draft_id）")
        if not isinstance(absorbed_ids, list) or not absorbed_ids:
            raise InspectionError("缺少待合并草稿 id 列表（absorbed_draft_ids）")
        if not operator:
            raise InspectionError("缺少合并操作人")
        self._require_tunnel(tunnel_code)
        with istore.transaction():
            try:
                return istore.merge_drafts(
                    tunnel_code,
                    surviving_id=int(surviving_id),
                    absorbed_ids=[int(x) for x in absorbed_ids],
                    operator=operator,
                )
            except KeyError as exc:
                raise InspectionError(str(exc).strip("'")) from exc
            except ValueError as exc:
                raise InspectionError(str(exc)) from exc

    def migrate_legacy(self, payload: dict[str, Any]) -> dict[str, Any]:
        tunnel_code = self._code(payload)
        origin = str(payload.get("origin") or "").strip()
        if not origin:
            raise InspectionError("旧草稿迁移必须提供来源（origin）")
        self._require_tunnel(tunnel_code)
        with istore.transaction():
            return istore.migrate_legacy_draft(
                tunnel_code=tunnel_code,
                section=self._section(payload),
                inspector=str(payload.get("inspector") or "").strip() or "迁移",
                content=str(payload.get("content") or ""),
                origin=origin,
                attachments=self._attachments(payload),
            )

    # ------------------------------------------------------------------ 证据

    def evidence(self, tunnel_code: str) -> dict[str, Any]:
        self._require_tunnel(tunnel_code)
        return istore.get_evidence(tunnel_code)

    def patch_evidence(self, payload: dict[str, Any]) -> dict[str, Any]:
        tunnel_code = self._code(payload)
        base_version = payload.get("base_version")
        if base_version is None:
            raise InspectionError("并发补写必须携带 base_version")
        inspector = str(payload.get("inspector") or "").strip()
        if not inspector:
            raise InspectionError("缺少检查员")
        self._require_tunnel(tunnel_code)
        with istore.transaction():
            try:
                return istore.patch_evidence(
                    tunnel_code=tunnel_code,
                    section=self._section(payload),
                    inspector=inspector,
                    content=str(payload.get("content") or ""),
                    base_version=int(base_version),
                    attachments=self._attachments(payload),
                )
            except OptimisticLockError as exc:
                raise ConflictError(exc.current_evidence) from exc

    # ------------------------------------------------------------ 检修单回写

    def submit_work_order(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        tunnel_code = self._code(payload)
        client_seq = self._client_seq(payload)
        inspector = str(payload.get("inspector") or "").strip()
        draft_id = payload.get("draft_id")
        title = str(payload.get("title") or "").strip()
        if not inspector:
            raise InspectionError("缺少检查员")
        self._require_tunnel(tunnel_code)

        # 幂等优先于草稿状态校验：弱网重传时草稿可能已随首次提交收口，
        # 同一 (隧道编号, 本地序列号) 必须原样返回，而不是报"草稿已收口"。
        existing = self._find_existing_order(tunnel_code, client_seq)
        if existing is not None:
            return existing, False

        draft = None
        if draft_id:
            draft = istore.get_draft(int(draft_id))
            if draft is None:
                raise InspectionError(f"草稿 {draft_id} 不存在")
            if draft["tunnel_code"] != tunnel_code:
                raise InspectionError("草稿与隧道编号不匹配")
            if draft["merged_into"]:
                raise InspectionError("该草稿已收口，不能重复回写检修单")

        sections, attachments = self._compose_sections(payload, draft)

        with istore.transaction():
            return istore.submit_work_order(
                tunnel_code=tunnel_code,
                client_seq=client_seq,
                inspector=inspector,
                draft_id=int(draft_id) if draft_id else None,
                sections=sections,
                attachments=attachments,
                title=title,
            )

    @staticmethod
    def _find_existing_order(tunnel_code: str, client_seq: int) -> dict[str, Any] | None:
        for order in istore.list_work_orders(tunnel_code):
            if int(order["client_seq"]) == client_seq:
                return order
        return None

    def _compose_sections(
        self, payload: dict[str, Any], draft: dict[str, Any] | None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """组装回写内容：以草稿（已按现场确认合并）为主，请求内补充覆盖。"""
        sections: dict[str, Any] = {}
        if draft:
            for name, body in draft["sections"].items():
                sections[name] = {
                    "content": body.get("content", ""),
                    "confirmed": bool(body.get("confirmed")),
                }
        inline = payload.get("sections")
        if isinstance(inline, dict):
            for name, body in inline.items():
                name = str(name)
                if name not in SECTIONS:
                    raise InspectionError(f"未知专项「{name}」")
                content = body.get("content", "") if isinstance(body, dict) else str(body)
                sections[name] = {
                    "content": str(content),
                    "confirmed": bool((body or {}).get("confirmed")) if isinstance(body, dict) else False,
                }
        # 兼容单专项直提（消防表场景）
        if payload.get("section") and payload.get("content") is not None:
            sections[self._section(payload)] = {
                "content": str(payload.get("content") or ""),
                "confirmed": bool(payload.get("confirmed")),
            }
        if not sections or not any(str(s.get("content") or "").strip() for s in sections.values()):
            raise InspectionError("通风笔记与消防检查均为空，不能回写空检修单")

        attachments = list(draft.get("attachments", []) if draft else [])
        attachments.extend(self._attachments(payload))
        return sections, attachments

    # --------------------------------------------------------------- 读模型

    def list_drafts(self, tunnel_code: str | None) -> list[dict[str, Any]]:
        return istore.list_drafts(tunnel_code)

    def list_work_orders(self, tunnel_code: str | None) -> list[dict[str, Any]]:
        return istore.list_work_orders(tunnel_code)

    def list_todos(self, tunnel_code: str | None) -> list[dict[str, Any]]:
        return istore.list_todos(tunnel_code)

    def list_digest(self, tunnel_codes: list[str]) -> dict[str, Any]:
        return istore.digest(tunnel_codes)

    def consistency(self, tunnel_code: str) -> dict[str, Any]:
        self._require_tunnel(tunnel_code)
        return istore.consistency_view(tunnel_code)

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _code(payload: dict[str, Any]) -> str:
        code = str(payload.get("tunnel_code") or "").strip()
        if not code:
            raise InspectionError("缺少隧道编号")
        return code

    @staticmethod
    def _section(payload: dict[str, Any]) -> str:
        section = str(payload.get("section") or "").strip()
        if section not in SECTIONS:
            raise InspectionError(f"section 必须是 {SECTIONS} 之一")
        return section

    @staticmethod
    def _client_seq(payload: dict[str, Any]) -> int:
        value = payload.get("client_seq")
        if value is None:
            raise InspectionError("缺少本地序列号 client_seq（离线重传幂等键）")
        try:
            seq = int(value)
        except (TypeError, ValueError) as exc:
            raise InspectionError("client_seq 必须是整数") from exc
        if seq <= 0:
            raise InspectionError("client_seq 必须为正整数")
        return seq

    @staticmethod
    def _attachments(payload: dict[str, Any]) -> list[dict[str, Any]]:
        raw = payload.get("attachments") or []
        if not isinstance(raw, list):
            raise InspectionError("attachments 必须是数组")
        result = []
        for item in raw:
            if not isinstance(item, dict):
                raise InspectionError("附件必须包含 name 与 sha256")
            name = str(item.get("name") or "").strip()
            sha = str(item.get("sha256") or "").strip()
            if not name or not sha:
                raise InspectionError("附件必须包含 name 与 sha256")
            result.append({"name": name, "sha256": sha})
        return result


inspection_service = InspectionService()
