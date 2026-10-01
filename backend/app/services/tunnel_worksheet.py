"""隧道消防现场作业规则。

草稿、设施待办和检修工单分散在三张内存表里，任何一次提交都必须通过
``store.transaction`` 一次性落库，避免弱网重试时只写入其中一部分。
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from typing import Any

from app.store import store

DRAFT_TABLE = "_tunnel_draft"
ORDER_TABLE = "_tunnel_work_order"
TODO_TABLE = "_tunnel_facility_todo"
TUNNEL_TABLE = "tunnel"

DRAFT_STATUS_DRAFT = "草稿"
DRAFT_STATUS_CONFIRMED = "已确认"
DRAFT_STATUS_WRITTEN = "已回写"
FIRE_RESULTS = ("正常", "限期整改", "停用整改")


class WorksheetError(ValueError):
    """业务校验失败。status_code 供路由层转换成 4xx。"""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class TunnelWorksheetService:
    def list_drafts(
        self,
        *,
        tunnel_code: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [self._with_tunnel_summary(row) for row in store.rows(DRAFT_TABLE)]
        if tunnel_code:
            rows = [row for row in rows if row["tunnel_code"] == tunnel_code.strip()]
        if status:
            rows = [row for row in rows if row["status"] == status]
        rows.sort(key=lambda row: (row["tunnel_code"], row["local_seq"]))
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_draft(self, draft_id: int) -> dict[str, Any]:
        draft = store.find(DRAFT_TABLE, draft_id)
        if draft is None:
            raise WorksheetError(f"消防草稿 {draft_id} 不存在", status_code=404)
        return self._with_tunnel_summary(draft)

    def save_draft(self, values: dict[str, Any]) -> dict[str, Any]:
        cleaned = self._clean_draft_values(values)
        with store.transaction():
            draft = self._find_by_key(cleaned["tunnel_code"], cleaned["local_seq"])
            if draft is None:
                draft = {
                    "id": store.next_id(DRAFT_TABLE),
                    "canonical_key": self.canonical_key(cleaned["tunnel_code"], cleaned["local_seq"]),
                    "tunnel_code": cleaned["tunnel_code"],
                    "local_seq": cleaned["local_seq"],
                    "status": DRAFT_STATUS_DRAFT,
                    "revision": 1,
                    "source_trace": [],
                    "attachments": [],
                    "evidence": [],
                    "history": [],
                    "migrated_from": [],
                    "work_order_no": None,
                    "created_at": self._now(),
                }
                store.rows(DRAFT_TABLE).append(draft)
            else:
                expected_revision = cleaned["expected_revision"]
                if expected_revision is not None and int(draft["revision"]) != expected_revision:
                    raise WorksheetError(
                        "草稿已被其他检查员更新，请先核对现场版本后再合并",
                        status_code=409,
                    )
                draft["history"].append(self._snapshot(draft, reason="合并前版本"))

            for field in ("operator", "ventilation_note", "fire_note", "fire_result"):
                if cleaned[field] not in (None, ""):
                    draft[field] = cleaned[field]
            draft["confirmed"] = bool(cleaned["confirmed"])
            if cleaned["confirmed"]:
                draft["status"] = DRAFT_STATUS_CONFIRMED
                draft["confirmed_at"] = self._now()
            self._merge_attachments(draft, cleaned["attachments"])
            self._append_sources(draft, cleaned["sources"])
            draft["payload_hash"] = self._payload_hash(draft)
            draft["updated_at"] = self._now()
            if not draft["confirmed"]:
                draft["revision"] = int(draft["revision"]) + 1
            else:
                # 确认动作本身就是一次现场版本变更，同样推进修订号，便于并发补写检测。
                draft["revision"] = int(draft["revision"]) + 1
            return self._with_tunnel_summary(draft)

    def merge_draft(self, values: dict[str, Any]) -> dict[str, Any]:
        """以检查员最后确认的现场版本合并离线草稿。"""
        required = {
            "通风笔记": str(values.get("ventilation_note") or "").strip(),
            "消防检查记录": str(values.get("fire_note") or "").strip(),
        }
        missing = [label for label, value in required.items() if not value]
        if missing:
            raise WorksheetError(f"合并前必须保留并确认：{'、'.join(missing)}")
        if not values.get("fire_result"):
            raise WorksheetError("合并前必须给出消防结论")
        values["confirmed"] = True
        if "sources" not in values:
            values["sources"] = ["消防工作表离线草稿"]
        return self.save_draft(values)

    def migrate_legacy_draft(self, values: dict[str, Any]) -> dict[str, Any]:
        source = str(values.get("source") or "旧版草稿").strip() or "旧版草稿"
        payload = dict(values.get("draft") if isinstance(values.get("draft"), dict) else values)
        payload.setdefault("sources", [f"旧草稿迁移：{source}"])
        payload.setdefault("confirmed", False)
        result = self.save_draft(payload)
        draft_id = int(result["id"])
        with store.transaction():
            row = store.find(DRAFT_TABLE, draft_id)
            assert row is not None
            trace = f"旧草稿迁移：{source}"
            self._append_sources(row, [trace])
            legacy = values.get("legacy") if isinstance(values.get("legacy"), dict) else payload
            row["migrated_from"].append({
                "source": source,
                "legacy_payload": deepcopy(legacy),
                "migrated_at": self._now(),
            })
            row["revision"] = int(row["revision"]) + 1
            row["payload_hash"] = self._payload_hash(row)
        return self.get_draft(draft_id)

    def append_evidence(self, draft_id: int, values: dict[str, Any]) -> dict[str, Any]:
        """并发补写只追加证据，不覆盖已有现场附件和笔记。"""
        attachments = self._clean_attachments(values.get("attachments", []))
        note = str(values.get("note") or "").strip()
        operator = str(values.get("operator") or "").strip()
        if not attachments and not note:
            raise WorksheetError("补写内容至少包含一条现场证据或说明")
        with store.transaction():
            draft = store.find(DRAFT_TABLE, draft_id)
            if draft is None:
                raise WorksheetError(f"消防草稿 {draft_id} 不存在", status_code=404)
            expected_revision = values.get("expected_revision")
            if expected_revision is not None and int(draft["revision"]) != int(expected_revision):
                raise WorksheetError("已有其他现场证据先补写，请基于最新版本追加", status_code=409)
            before = self._snapshot(draft, reason="补写证据前")
            new_attachments = self._merge_attachments(draft, attachments)
            evidence = {
                "note": note,
                "operator": operator,
                "attachments": new_attachments,
                "created_at": self._now(),
            }
            draft["evidence"].append(evidence)
            draft["history"].append(before)
            draft["revision"] = int(draft["revision"]) + 1
            draft["updated_at"] = self._now()

            order_no = draft.get("work_order_no")
            if order_no:
                order = self._find_order(order_no)
                if order is not None:
                    order["supplemental_evidence"].append(evidence)
                todo = self._find_todo_by_order(order_no)
                if todo is not None:
                    todo["evidence_count"] = int(todo.get("evidence_count", 0)) + len(new_attachments) + (1 if note else 0)
            return self._with_tunnel_summary(draft)

    def write_back(self, draft_id: int, values: dict[str, Any] | None = None) -> dict[str, Any]:
        values = values or {}
        with store.transaction():
            draft = store.find(DRAFT_TABLE, draft_id)
            if draft is None:
                raise WorksheetError(f"消防草稿 {draft_id} 不存在", status_code=404)
            if draft.get("work_order_no"):
                order = self._find_order(str(draft["work_order_no"]))
                if order is not None and order.get("canonical_key") == draft["canonical_key"]:
                    return self._writeback_result(draft, order, idempotent=True)

            if draft["status"] != DRAFT_STATUS_CONFIRMED:
                raise WorksheetError("只有检查员确认后的现场版本才能回写检修单")
            expected_revision = values.get("expected_revision")
            if expected_revision is not None and int(draft["revision"]) != int(expected_revision):
                raise WorksheetError("回写前草稿已有新的现场补写，请刷新后核对", status_code=409)

            order_no = self._generate_order_no(str(draft["tunnel_code"]))
            attachments = deepcopy(draft["attachments"])
            tunnel = self._find_tunnel(str(draft["tunnel_code"]))
            if tunnel is None:
                raise WorksheetError(f"隧道档案 {draft['tunnel_code']} 不存在", status_code=404)

            order = {
                "id": store.next_id(ORDER_TABLE),
                "work_order_no": order_no,
                "canonical_key": draft["canonical_key"],
                "tunnel_id": tunnel["id"],
                "tunnel_code": draft["tunnel_code"],
                "tunnel_name": tunnel.get("隧道名称"),
                "title": "隧道消防设施检修单",
                "ventilation_note": draft.get("ventilation_note", ""),
                "fire_note": draft.get("fire_note", ""),
                "fire_result": draft.get("fire_result", "限期整改"),
                "operator": draft.get("operator"),
                "status": "待检修" if draft.get("fire_result") != "正常" else "已闭环",
                "attachments": attachments,
                "attachment_manifest": [self._attachment_ref(item) for item in attachments],
                "supplemental_evidence": deepcopy(draft.get("evidence", [])),
                "source_trace": deepcopy(draft.get("source_trace", [])),
                "source_revision": draft["revision"],
                "created_at": self._now(),
            }
            store.rows(ORDER_TABLE).append(order)

            todo = self._find_todo_by_key(draft["canonical_key"])
            closed = order["status"] == "已闭环"
            if todo is None:
                todo = {
                    "id": store.next_id(TODO_TABLE),
                    "canonical_key": draft["canonical_key"],
                    "tunnel_id": tunnel["id"],
                    "tunnel_code": draft["tunnel_code"],
                    "title": "隧道消防设施复核",
                    "status": "已闭环" if closed else "待处理",
                    "work_order_no": order_no,
                    "evidence_count": len(attachments) + len(draft.get("evidence", [])),
                    "updated_at": self._now(),
                }
                store.rows(TODO_TABLE).append(todo)
            else:
                todo["status"] = "已闭环" if closed else "待处理"
                todo["work_order_no"] = order_no
                todo["evidence_count"] = len(attachments) + len(draft.get("evidence", []))
                todo["updated_at"] = self._now()

            tunnel["通风笔记"] = draft.get("ventilation_note", "")
            tunnel["消防状态"] = draft.get("fire_result", "限期整改")
            if draft.get("fire_note"):
                tunnel["消防核查记录"] = draft["fire_note"]
            tunnel["最近检修单号"] = order_no
            tunnel["status"] = "正常" if closed else "维修"
            tunnel["隧道状态"] = tunnel["status"]
            tunnel["pending"] = not closed
            tunnel["abnormal"] = not closed

            draft["status"] = DRAFT_STATUS_WRITTEN
            draft["work_order_no"] = order_no
            draft["writeback_at"] = self._now()
            draft["revision"] = int(draft["revision"]) + 1
            draft["payload_hash"] = self._payload_hash(draft)
            return self._writeback_result(draft, order, idempotent=False)

    def list_work_orders(self, *, tunnel_code: str | None = None) -> list[dict[str, Any]]:
        rows = [dict(row) for row in store.rows(ORDER_TABLE)]
        if tunnel_code:
            rows = [row for row in rows if row["tunnel_code"] == tunnel_code.strip()]
        rows.sort(key=lambda row: row["work_order_no"])
        return rows

    def list_todos(self, *, tunnel_code: str | None = None) -> list[dict[str, Any]]:
        rows = [dict(row) for row in store.rows(TODO_TABLE)]
        if tunnel_code:
            rows = [row for row in rows if row["tunnel_code"] == tunnel_code.strip()]
        rows.sort(key=lambda row: row["canonical_key"])
        return rows

    def canonical_key(self, tunnel_code: str, local_seq: int) -> str:
        return f"{tunnel_code.strip()}#{int(local_seq):06d}"

    def _clean_draft_values(self, values: dict[str, Any]) -> dict[str, Any]:
        tunnel_code = str(values.get("tunnel_code") or "").strip()
        if not tunnel_code:
            raise WorksheetError("隧道编号不能为空")
        try:
            local_seq = int(values.get("local_seq"))
        except (TypeError, ValueError) as exc:
            raise WorksheetError("本地序列号必须是正整数") from exc
        if local_seq <= 0:
            raise WorksheetError("本地序列号必须是正整数")
        operator = str(values.get("operator") or "").strip()
        if not operator:
            raise WorksheetError("检查员不能为空")
        fire_result = values.get("fire_result")
        if fire_result is not None and fire_result not in FIRE_RESULTS:
            raise WorksheetError(f"消防结论只能是：{'、'.join(FIRE_RESULTS)}")
        expected_revision = values.get("expected_revision")
        if expected_revision is not None:
            try:
                expected_revision = int(expected_revision)
            except (TypeError, ValueError) as exc:
                raise WorksheetError("expected_revision 必须是整数") from exc
        return {
            "tunnel_code": tunnel_code,
            "local_seq": local_seq,
            "operator": operator,
            "ventilation_note": str(values.get("ventilation_note") or "").strip(),
            "fire_note": str(values.get("fire_note") or "").strip(),
            "fire_result": fire_result,
            "confirmed": bool(values.get("confirmed", False)),
            "attachments": self._clean_attachments(values.get("attachments", [])),
            "sources": self._clean_sources(values.get("sources", [])),
            "expected_revision": expected_revision,
        }

    def _clean_attachments(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            raise WorksheetError("附件必须是数组")
        result: list[dict[str, str]] = []
        for index, item in enumerate(value, start=1):
            if not isinstance(item, dict):
                raise WorksheetError(f"第 {index} 个附件格式不正确")
            name = str(item.get("name") or item.get("filename") or f"附件{index}").strip()
            digest = str(item.get("sha256") or item.get("hash") or "").strip()
            if not digest:
                digest = sha256(f"{name}-{index}-{self._now()}".encode("utf-8")).hexdigest()
            uploaded_by = str(item.get("uploaded_by") or item.get("operator") or "").strip()
            result.append({"name": name, "sha256": digest, "uploaded_by": uploaded_by})
        return result

    def _clean_sources(self, value: Any) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            raise WorksheetError("来源必须是字符串数组")
        return [str(item).strip() for item in value if str(item or "").strip()]

    def _find_by_key(self, tunnel_code: str, local_seq: int) -> dict[str, Any] | None:
        key = self.canonical_key(tunnel_code, local_seq)
        for row in store.rows(DRAFT_TABLE):
            if row.get("canonical_key") == key:
                return row
        return None

    def _find_tunnel(self, tunnel_code: str) -> dict[str, Any] | None:
        for row in store.rows(TUNNEL_TABLE):
            if row.get("隧道编号") == tunnel_code:
                return row
        return None

    def _find_order(self, order_no: str) -> dict[str, Any] | None:
        for row in store.rows(ORDER_TABLE):
            if row.get("work_order_no") == order_no:
                return row
        return None

    def _find_todo_by_key(self, canonical_key: str) -> dict[str, Any] | None:
        for row in store.rows(TODO_TABLE):
            if row.get("canonical_key") == canonical_key:
                return row
        return None

    def _find_todo_by_order(self, order_no: str) -> dict[str, Any] | None:
        for row in store.rows(TODO_TABLE):
            if row.get("work_order_no") == order_no:
                return row
        return None

    def _merge_attachments(
        self,
        draft: dict[str, Any],
        incoming: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        existing = {item["sha256"]: item for item in draft["attachments"]}
        added: list[dict[str, str]] = []
        for item in incoming:
            if item["sha256"] not in existing:
                draft["attachments"].append(item)
                existing[item["sha256"]] = item
                added.append(item)
        return added

    def _append_sources(self, draft: dict[str, Any], sources: list[str]) -> None:
        for source in sources:
            if source not in draft["source_trace"]:
                draft["source_trace"].append(source)

    def _generate_order_no(self, tunnel_code: str) -> str:
        today = datetime.utcnow().strftime("%Y%m%d")
        same_today = [
            row for row in store.rows(ORDER_TABLE)
            if str(row.get("work_order_no", "")).startswith(f"WX-{tunnel_code.replace('/', '-').replace('#', '-')}-{today}-")
        ]
        safe_code = tunnel_code.replace("/", "-").replace("#", "-")
        return f"WX-{safe_code}-{today}-{len(same_today) + 1:03d}"

    def _with_tunnel_summary(self, draft: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(draft)
        tunnel = self._find_tunnel(str(draft["tunnel_code"]))
        result["tunnel_name"] = tunnel.get("隧道名称") if tunnel else None
        order = self._find_order(str(draft["work_order_no"])) if draft.get("work_order_no") else None
        todo = self._find_todo_by_key(str(draft["canonical_key"]))
        result["work_order_status"] = order.get("status") if order else None
        result["todo_status"] = todo.get("status") if todo else None
        result["consistent"] = self._is_consistent(draft, order, todo)
        return result

    def _is_consistent(
        self,
        draft: dict[str, Any],
        order: dict[str, Any] | None,
        todo: dict[str, Any] | None,
    ) -> bool:
        if draft.get("work_order_no"):
            if order is None or todo is None:
                return False
            if order.get("work_order_no") != draft.get("work_order_no"):
                return False
            if todo.get("work_order_no") != draft.get("work_order_no"):
                return False
        return True

    def _writeback_result(
        self,
        draft: dict[str, Any],
        order: dict[str, Any],
        *,
        idempotent: bool,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "idempotent": idempotent,
            "message": "检修单已存在，本次重传未重复建单" if idempotent else "检修单、隧道档案和设施待办已在同一事务提交",
            "draft": self._with_tunnel_summary(draft),
            "work_order": deepcopy(order),
            "todo": deepcopy(self._find_todo_by_order(str(order["work_order_no"]))),
        }

    def _snapshot(self, draft: dict[str, Any], *, reason: str) -> dict[str, Any]:
        return {
            "reason": reason,
            "at": self._now(),
            "revision": draft.get("revision"),
            "ventilation_note": draft.get("ventilation_note"),
            "fire_note": draft.get("fire_note"),
            "operator": draft.get("operator"),
            "attachments": deepcopy(draft.get("attachments", [])),
        }

    def _attachment_ref(self, attachment: dict[str, str]) -> dict[str, str]:
        return {
            "name": attachment["name"],
            "sha256": attachment["sha256"],
            "uploaded_by": attachment.get("uploaded_by", ""),
        }

    def _payload_hash(self, draft: dict[str, Any]) -> str:
        payload = {
            "canonical_key": draft["canonical_key"],
            "ventilation_note": draft.get("ventilation_note"),
            "fire_note": draft.get("fire_note"),
            "fire_result": draft.get("fire_result"),
            "attachments": draft.get("attachments", []),
            "operator": draft.get("operator"),
            "confirmed": draft.get("confirmed", False),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return sha256(raw.encode("utf-8")).hexdigest()

    def _now(self) -> str:
        return datetime.utcnow().isoformat(timespec="seconds") + "Z"


service = TunnelWorksheetService()
