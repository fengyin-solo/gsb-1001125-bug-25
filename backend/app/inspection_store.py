"""隧道现场检修工作台。

承载三类相互关联、必须保持一致的数据：

* drafts：检查员在隧道详情页 / 消防工作表之间切换时未提交的现场草稿
  （通风笔记、消防检查项、附件、现场确认标记）。
* work_orders：草稿合并并回写后生成的检修单，附件以提交当时的快照留痕。
* todos：由检修单派生的设施待办；隧道档案上的待办标记同步推进。

所有写操作都经过 ``transaction()``：在同一把锁内复制相关表快照（含通用
store 中的隧道档案表），业务函数抛错时整批回滚，保证「草稿、附件、工单号
同一事务提交」。

弱网重传用 (隧道编号, 本地序列号) 作为幂等键；并发补写用证据版本号做乐观
并发控制，后写不得覆盖先写的现场证据。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from typing import Any, Iterator

from app.store import store

TUNNEL_MODULE = "tunnel"


class OptimisticLockError(Exception):
    """证据版本号落后于服务端当前版本（409 Conflict）。"""

    def __init__(self, current_version: int, current_evidence: dict[str, Any]) -> None:
        super().__init__(f"现场证据已被推进到第 {current_version} 版，本次补写基于过期版本")
        self.current_version = current_version
        self.current_evidence = current_evidence


class TunnelInspectionStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._drafts: list[dict[str, Any]] = []
        self._work_orders: list[dict[str, Any]] = []
        self._todos: list[dict[str, Any]] = []
        # 隧道编号 -> 现场证据（通风/消防等各专项笔记及版本号）
        self._evidence: dict[str, dict[str, Any]] = {}
        # (隧道编号, 本地序列号) -> 已提交工单：离线重传幂等
        self._idem_index: dict[tuple[str, int], int] = {}
        self._draft_seq = 0
        self._order_seq = 0
        self._todo_seq = 0

    # ------------------------------------------------------------------ 事务

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """在同一事务里提交草稿/附件/工单/档案标记；异常时整体回滚。"""
        with self._lock:
            snapshot = (
                deepcopy(self._drafts),
                deepcopy(self._work_orders),
                deepcopy(self._todos),
                deepcopy(self._evidence),
                deepcopy(self._idem_index),
                self._draft_seq,
                self._order_seq,
                self._todo_seq,
                # 隧道档案也纳入同一事务，回写失败时档案标记一并回滚
                deepcopy(store.rows(TUNNEL_MODULE)),
            )
            try:
                yield
            except Exception:
                (
                    self._drafts,
                    self._work_orders,
                    self._todos,
                    self._evidence,
                    self._idem_index,
                    self._draft_seq,
                    self._order_seq,
                    self._todo_seq,
                    tunnel_rows,
                ) = snapshot
                store.replace_rows(TUNNEL_MODULE, tunnel_rows)
                raise

    # ------------------------------------------------------------------ 草稿

    def list_drafts(self, tunnel_code: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = [deepcopy(row) for row in self._drafts if not row["merged_into"]]
        if tunnel_code:
            rows = [row for row in rows if row["tunnel_code"] == tunnel_code]
        return rows

    def get_draft(self, draft_id: int) -> dict[str, Any] | None:
        with self._lock:
            for row in self._drafts:
                if row["id"] == draft_id:
                    return deepcopy(row)
        return None

    def upsert_draft(
        self,
        *,
        tunnel_code: str,
        section: str,
        inspector: str,
        content: str,
        client_seq: int,
        source: str = "现场",
        attachments: list[dict[str, Any]] | None = None,
        draft_id: int | None = None,
    ) -> dict[str, Any]:
        """新建或续存一版草稿；指定 draft_id 续传时不覆盖其他专项的旧笔记。"""
        with self._lock:
            row = self._row_by_id(self._drafts, draft_id) if draft_id else None
            if row is None:
                self._draft_seq += 1
                row = {
                    "id": self._draft_seq,
                    "tunnel_code": tunnel_code,
                    "sections": {},
                    "attachments": [],
                    "inspector": inspector,
                    "source": source,
                    "legacy": False,
                    "legacy_origin": None,
                    "client_seq": client_seq,
                    "confirmed": False,
                    "merged_into": None,
                    "merge_note": None,
                    "provenance": [],
                    "created_at": _now(),
                    "updated_at": _now(),
                }
                self._drafts.append(row)
                row["provenance"].append(
                    {"source": source, "client_seq": client_seq, "at": row["created_at"]}
                )
            section_data = row["sections"].setdefault(section, {})
            section_data["content"] = content
            section_data["updated_at"] = _now()
            section_data["client_seq"] = client_seq
            for item in attachments or []:
                row["attachments"].append(
                    {
                        "name": str(item.get("name") or "").strip(),
                        "sha256": str(item.get("sha256") or "").strip(),
                        "section": section,
                        "client_seq": client_seq,
                    }
                )
            row["inspector"] = inspector or row["inspector"]
            row["client_seq"] = max(int(row["client_seq"]), int(client_seq))
            row["updated_at"] = _now()
            return deepcopy(row)

    def mark_confirmed(self, draft_id: int, sections: list[str] | None = None) -> dict[str, Any]:
        """检查员在现场最后核对确认；被确认的各专项内容以本版为准参与合并。"""
        with self._lock:
            row = self._require_draft(draft_id)
            targets = sections or list(row["sections"].keys())
            for section in targets:
                if section not in row["sections"]:
                    raise KeyError(f"草稿中不存在专项「{section}」")
                row["sections"][section]["confirmed"] = True
                row["sections"][section]["confirmed_at"] = _now()
            if set(row["sections"]) <= set(targets):
                row["confirmed"] = True
            row["updated_at"] = _now()
            return deepcopy(row)

    def merge_drafts(
        self,
        tunnel_code: str,
        *,
        surviving_id: int,
        absorbed_ids: list[int],
        operator: str,
    ) -> dict[str, Any]:
        """合并同一隧道的多份草稿，现场最后确认版为准；被并草稿保留来源不删除。"""
        with self._lock:
            survivor = self._require_draft(surviving_id)
            if survivor["merged_into"]:
                raise ValueError("主草稿已收口，不能再次合并")
            if survivor["tunnel_code"] != tunnel_code:
                raise ValueError("主草稿与隧道编号不匹配")
            for absorbed_id in absorbed_ids:
                if absorbed_id == surviving_id:
                    continue
                other = self._require_draft(absorbed_id)
                if other["tunnel_code"] != tunnel_code:
                    raise ValueError(f"草稿 {absorbed_id} 不属于隧道 {tunnel_code}")
                if other["merged_into"]:
                    raise ValueError(f"草稿 {absorbed_id} 已合并过，不能重复合并")
                for section, payload in other["sections"].items():
                    here = survivor["sections"].get(section)
                    if here is None:
                        # 主草稿没有该专项（如只有消防表才有消防检查项）：直接补入
                        survivor["sections"][section] = deepcopy(payload)
                        resolution = "absent-on-survivor"
                    elif payload.get("confirmed") and not here.get("confirmed"):
                        # 检查员在现场最后确认的版本为准
                        survivor["sections"][section] = deepcopy(payload)
                        resolution = "confirmed-on-site-wins"
                    else:
                        # 双方确认/均未确认时保留主草稿，对方内容仅在来源中留痕
                        resolution = "kept-survivor"
                    survivor["provenance"].append(
                        {
                            "source_draft": other["id"],
                            "origin": other.get("source"),
                            "legacy_origin": other.get("legacy_origin"),
                            "section": section,
                            "resolution": resolution,
                        }
                    )
                survivor["attachments"].extend(deepcopy(other.get("attachments", [])))
                survivor["provenance"].extend(deepcopy(other.get("provenance", [])))
                other["merged_into"] = surviving_id
                other["merge_note"] = (
                    f"由 {operator} 合并入草稿 {surviving_id}；"
                    "历史内容保留来源，现场最后确认版为准"
                )
            survivor["updated_at"] = _now()
            return deepcopy(survivor)

    def migrate_legacy_draft(
        self,
        *,
        tunnel_code: str,
        section: str,
        inspector: str,
        content: str,
        origin: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """迁移旧系统草稿：内容带入，来源（origin）必须留痕。"""
        with self._lock:
            row = self.upsert_draft(
                tunnel_code=tunnel_code,
                section=section,
                inspector=inspector,
                content=content,
                client_seq=0,
                source=f"旧草稿迁移:{origin}",
                attachments=attachments,
            )
            self._drafts[-1]["legacy"] = True
            self._drafts[-1]["legacy_origin"] = origin
            return deepcopy(self._drafts[-1])

    # --------------------------------------------------------------- 现场证据

    def get_evidence(self, tunnel_code: str) -> dict[str, Any]:
        with self._lock:
            return deepcopy(
                self._evidence.get(
                    tunnel_code, {"tunnel_code": tunnel_code, "version": 0, "sections": {}}
                )
            )

    def patch_evidence(
        self,
        *,
        tunnel_code: str,
        section: str,
        inspector: str,
        content: str,
        base_version: int,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """并发补写：携带 base_version，过期写入被拒（不覆盖他人现场证据）。"""
        with self._lock:
            evidence = self._evidence.setdefault(
                tunnel_code, {"tunnel_code": tunnel_code, "version": 0, "sections": {}}
            )
            if int(base_version) != int(evidence["version"]):
                raise OptimisticLockError(evidence["version"], deepcopy(evidence))
            evidence["sections"][section] = {
                "content": content,
                "inspector": inspector,
                "attachments": deepcopy(attachments or []),
                "updated_at": _now(),
            }
            evidence["version"] += 1
            evidence["updated_at"] = _now()
            return deepcopy(evidence)

    # ------------------------------------------------------------------ 工单

    def submit_work_order(
        self,
        *,
        tunnel_code: str,
        client_seq: int,
        inspector: str,
        draft_id: int | None,
        sections: dict[str, Any],
        attachments: list[dict[str, Any]],
        title: str,
    ) -> tuple[dict[str, Any], bool]:
        """回写检修单。返回 (工单, 是否新建)。

        同一 (隧道编号, 本地序列号) 的重传直接返回原工单（幂等，不会重复出现
        检修单）；首次提交与待办、证据、档案标记、草稿收口在同一事务内完成。
        """
        idem_key = (tunnel_code, int(client_seq))
        with self._lock:
            existing_id = self._idem_index.get(idem_key)
            if existing_id is not None:
                return self._require_order(existing_id), False

            now = _now()
            self._order_seq += 1
            order = {
                "id": self._order_seq,
                "order_no": f"WO-{tunnel_code}-{int(client_seq):04d}",
                "tunnel_code": tunnel_code,
                "title": title or f"{tunnel_code} 现场检修单",
                "inspector": inspector,
                "sections": deepcopy(sections),
                # 历史工单按原附件留痕：提交瞬间冻结快照，之后草稿/证据变更不回改
                "attachments_snapshot": deepcopy(attachments),
                "draft_id": draft_id,
                "client_seq": int(client_seq),
                "status": "待处理",
                "created_at": now,
            }
            self._work_orders.append(order)
            self._idem_index[idem_key] = order["id"]

            for section, payload in sections.items():
                if not str(payload.get("content") or "").strip():
                    continue
                self._todo_seq += 1
                self._todos.append({
                    "id": self._todo_seq,
                    "order_no": order["order_no"],
                    "tunnel_code": tunnel_code,
                    "section": section,
                    "summary": str(payload.get("content"))[:120],
                    "status": "待处理",
                    "assignee": inspector,
                    "created_at": now,
                })

            evidence = self._evidence.setdefault(
                tunnel_code, {"tunnel_code": tunnel_code, "version": 0, "sections": {}}
            )
            for section, payload in sections.items():
                evidence["sections"][section] = {
                    "content": payload.get("content", ""),
                    "inspector": inspector,
                    "from_order": order["order_no"],
                    "updated_at": now,
                }
            evidence["version"] += 1
            evidence["updated_at"] = now

            # 隧道档案待办标记同步推进（与上面同事务，失败一并回滚）
            pending = self._todos and any(
                todo["tunnel_code"] == tunnel_code and todo["status"] == "待处理"
                for todo in self._todos
            )
            archive = self._find_archive(tunnel_code)
            if archive is not None:
                archive["pending"] = pending
                archive["last_order_no"] = order["order_no"]

            if draft_id is not None:
                draft = self._row_by_id(self._drafts, draft_id)
                if draft is not None:
                    draft["merged_into"] = f"work_order:{order['id']}"
                    draft["merge_note"] = f"已回写检修单 {order['order_no']}，草稿收口"

            return deepcopy(order), True

    def list_work_orders(self, tunnel_code: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = [deepcopy(row) for row in self._work_orders]
        if tunnel_code:
            rows = [row for row in rows if row["tunnel_code"] == tunnel_code]
        return rows

    def list_todos(self, tunnel_code: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = [deepcopy(row) for row in self._todos]
        if tunnel_code:
            rows = [row for row in rows if row["tunnel_code"] == tunnel_code]
        return rows

    # ------------------------------------------------------------------ 汇总

    def digest(self, tunnel_codes: list[str]) -> dict[str, dict[str, Any]]:
        """给隧道列表页提供每座隧道的草稿/待办/工单计数，三处页面共用同一口径。"""
        with self._lock:
            result: dict[str, dict[str, Any]] = {
                code: {
                    "open_draft": False,
                    "open_draft_ids": [],
                    "draft_count": 0,
                    "todo_count": 0,
                    "work_order_count": 0,
                    "evidence_version": 0,
                }
                for code in tunnel_codes
            }
            for row in self._drafts:
                code = row["tunnel_code"]
                if code not in result or row["merged_into"]:
                    continue
                result[code]["open_draft"] = True
                result[code]["open_draft_ids"].append(row["id"])
                result[code]["draft_count"] += 1
            for row in self._work_orders:
                if row["tunnel_code"] in result:
                    result[row["tunnel_code"]]["work_order_count"] += 1
            for row in self._todos:
                if row["tunnel_code"] in result and row["status"] == "待处理":
                    result[row["tunnel_code"]]["todo_count"] += 1
            for code, evidence in self._evidence.items():
                if code in result:
                    result[code]["evidence_version"] = evidence["version"]
            return result

    def consistency_view(self, tunnel_code: str) -> dict[str, Any]:
        """隧道档案 / 设施待办 / 工单 / 草稿 四方一致性视图。"""
        with self._lock:
            open_drafts = [
                row["id"]
                for row in self._drafts
                if row["tunnel_code"] == tunnel_code and not row["merged_into"]
            ]
            orders = [
                row for row in self._work_orders if row["tunnel_code"] == tunnel_code
            ]
            todos = [
                row for row in self._todos
                if row["tunnel_code"] == tunnel_code and row["status"] == "待处理"
            ]
            archive = self._find_archive(tunnel_code)
            expected_pending = bool(todos)
            consistent = archive is None or bool(archive.get("pending")) == expected_pending
            return {
                "tunnel_code": tunnel_code,
                "open_draft_ids": open_drafts,
                "work_order_count": len(orders),
                "todo_count": len(todos),
                "archive_pending": None if archive is None else bool(archive.get("pending")),
                "expected_pending": expected_pending,
                "consistent": consistent and not open_drafts if orders else consistent,
            }

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _find_archive(tunnel_code: str) -> dict[str, Any] | None:
        for row in store.rows(TUNNEL_MODULE):
            if str(row.get("隧道编号")) == tunnel_code:
                return row
        return None

    @staticmethod
    def _row_by_id(rows: list[dict[str, Any]], row_id: int | None) -> dict[str, Any] | None:
        if row_id is None:
            return None
        for row in rows:
            if row["id"] == row_id:
                return row
        return None

    def _require_draft(self, draft_id: int) -> dict[str, Any]:
        row = self._row_by_id(self._drafts, draft_id)
        if row is None:
            raise KeyError(f"草稿 {draft_id} 不存在")
        return row

    def _require_order(self, order_id: int) -> dict[str, Any]:
        for row in self._work_orders:
            if row["id"] == order_id:
                return deepcopy(row)
        raise KeyError(f"检修单 {order_id} 不存在")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


inspection_store = TunnelInspectionStore()
