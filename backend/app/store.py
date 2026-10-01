"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from threading import RLock
from typing import Any, Iterator

from app.seed import SEED_ROWS

DRAFT_TABLE = "_tunnel_draft"
ORDER_TABLE = "_tunnel_work_order"
TODO_TABLE = "_tunnel_facility_todo"
INTERNAL_TABLES = (DRAFT_TABLE, ORDER_TABLE, TODO_TABLE)


class Store:
    def __init__(self) -> None:
        self._lock = RLock()
        self._transaction_depth = 0
        self._transaction_snapshot: dict[str, list[dict[str, Any]]] | None = None
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        for name in INTERNAL_TABLES:
            self._tables.setdefault(name, [])
        self._seed_tunnel_worksheets()

    def _seed_tunnel_worksheets(self) -> None:
        """给隧道消防弱网合并和检修回写准备可直接复现的现场数据。"""
        if any(self._tables.get(name) for name in INTERNAL_TABLES):
            return
        self._tables[DRAFT_TABLE] = [
            {
                "id": 1,
                "canonical_key": "TUNN-0001#000001",
                "tunnel_code": "TUNN-0001",
                "local_seq": 1,
                "operator": "张检查",
                "status": "草稿",
                "confirmed": False,
                "revision": 3,
                "ventilation_note": "2#射流风机启动声音偏大，现场复测 30 分钟后转速恢复正常。",
                "fire_note": "",
                "fire_result": None,
                "attachments": [
                    {"name": "2号风机运行声音.m4a", "sha256": "seed-tunn0001-audio", "uploaded_by": "张检查"},
                ],
                "evidence": [],
                "history": [],
                "source_trace": ["隧道详情页通风笔记"],
                "migrated_from": [],
                "work_order_no": None,
                "payload_hash": "seed-draft-1",
                "created_at": "2026-09-30T01:10:00Z",
                "updated_at": "2026-09-30T01:22:00Z",
            },
            {
                "id": 2,
                "canonical_key": "TUNN-0002#000001",
                "tunnel_code": "TUNN-0002",
                "local_seq": 1,
                "operator": "李检查",
                "status": "已回写",
                "confirmed": True,
                "revision": 5,
                "ventilation_note": "北洞排烟风机联动正常，水成膜泡沫箱压力略低。",
                "fire_note": "泡沫箱需补充压力并复核阀门铅封。",
                "fire_result": "限期整改",
                "attachments": [
                    {"name": "泡沫箱压力表.jpg", "sha256": "seed-tunn0002-photo", "uploaded_by": "李检查"},
                ],
                "evidence": [],
                "history": [],
                "source_trace": ["消防工作表", "弱网离线补传"],
                "migrated_from": [],
                "work_order_no": "WX-TUNN-0002-0001",
                "payload_hash": "seed-draft-2",
                "created_at": "2026-09-30T03:10:00Z",
                "updated_at": "2026-09-30T04:20:00Z",
                "writeback_at": "2026-09-30T04:20:00Z",
            },
        ]
        self._tables[ORDER_TABLE] = [
            {
                "id": 1,
                "work_order_no": "WX-TUNN-0002-0001",
                "canonical_key": "TUNN-0002#000001",
                "tunnel_id": 2,
                "tunnel_code": "TUNN-0002",
                "tunnel_name": "隧道管养样例2",
                "title": "隧道消防设施检修单",
                "ventilation_note": "北洞排烟风机联动正常，水成膜泡沫箱压力略低。",
                "fire_note": "泡沫箱需补充压力并复核阀门铅封。",
                "fire_result": "限期整改",
                "operator": "李检查",
                "status": "待检修",
                "attachments": [
                    {"name": "泡沫箱压力表.jpg", "sha256": "seed-tunn0002-photo", "uploaded_by": "李检查"},
                ],
                "attachment_manifest": [
                    {"name": "泡沫箱压力表.jpg", "sha256": "seed-tunn0002-photo", "uploaded_by": "李检查"},
                ],
                "supplemental_evidence": [],
                "source_trace": ["消防工作表", "弱网离线补传"],
                "source_revision": 4,
                "created_at": "2026-09-30T04:20:00Z",
            },
        ]
        self._tables[TODO_TABLE] = [
            {
                "id": 1,
                "canonical_key": "TUNN-0002#000001",
                "tunnel_id": 2,
                "tunnel_code": "TUNN-0002",
                "title": "隧道消防设施复核",
                "status": "待处理",
                "work_order_no": "WX-TUNN-0002-0001",
                "evidence_count": 1,
                "updated_at": "2026-09-30T04:20:00Z",
            },
        ]
        for row in self._tables["tunnel"]:
            if row.get("隧道编号") == "TUNN-0002":
                row.update({
                    "通风笔记": "北洞排烟风机联动正常，水成膜泡沫箱压力略低。",
                    "消防状态": "限期整改",
                    "消防核查记录": "泡沫箱需补充压力并复核阀门铅封。",
                    "最近检修单号": "WX-TUNN-0002-0001",
                })

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """一次性提交跨表变更；嵌套事务共用最外层快照。"""
        with self._lock:
            self._transaction_depth += 1
            if self._transaction_depth == 1:
                self._transaction_snapshot = deepcopy(self._tables)
            try:
                yield
            except Exception:
                if self._transaction_depth == 1 and self._transaction_snapshot is not None:
                    self._tables = self._transaction_snapshot
                    self._transaction_snapshot = None
                self._transaction_depth -= 1
                raise
            if self._transaction_depth == 1:
                self._transaction_snapshot = None
            self._transaction_depth -= 1

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if not name.startswith("_"))

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
