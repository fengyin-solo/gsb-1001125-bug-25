import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.tunnel_worksheet import (  # noqa: E402
    DRAFT_TABLE,
    ORDER_TABLE,
    TODO_TABLE,
    WorksheetError,
    service,
)
from app.store import store  # noqa: E402


class TunnelWorksheetServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        import app.store as store_module

        self.original_tables = store_module.deepcopy(store._tables)
        for name in (DRAFT_TABLE, ORDER_TABLE, TODO_TABLE):
            store._tables[name] = []
        for row in store.rows("tunnel"):
            for field in ("通风笔记", "消防状态", "消防核查记录", "最近检修单号"):
                row.pop(field, None)

    def tearDown(self) -> None:
        store._tables = self.original_tables

    def payload(self, **overrides):
        values = {
            "tunnel_code": "TUNN-0001",
            "local_seq": 100,
            "operator": "张检查",
            "ventilation_note": "风机复测正常",
            "fire_note": "灭火器压力正常",
            "fire_result": "正常",
            "attachments": [{"name": "风机照片.jpg", "sha256": "hash-a", "uploaded_by": "张检查"}],
            "sources": ["消防工作表"],
        }
        values.update(overrides)
        return values

    def test_save_keeps_ventilation_note_and_idempotent_draft(self):
        first = service.save_draft(self.payload())
        second = service.save_draft(self.payload(fire_note="消火栓压力正常"))

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["fire_note"], "消火栓压力正常")
        self.assertEqual(second["ventilation_note"], "风机复测正常")
        self.assertEqual(len(store.rows(DRAFT_TABLE)), 1)

    def test_merge_and_writeback_commit_draft_order_todo_and_archive_atomically(self):
        draft = service.merge_draft(self.payload())
        result = service.write_back(int(draft["id"]), {})
        repeated = service.write_back(int(draft["id"]), {})

        self.assertEqual(len(store.rows(ORDER_TABLE)), 1)
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(result["work_order"]["work_order_no"], result["draft"]["work_order_no"])
        self.assertEqual(result["todo"]["work_order_no"], result["work_order"]["work_order_no"])

        tunnel = store.find("tunnel", 1)
        self.assertEqual(tunnel["最近检修单号"], result["work_order"]["work_order_no"])
        self.assertEqual(tunnel["通风笔记"], "风机复测正常")
        self.assertEqual(tunnel["消防状态"], "正常")
        self.assertFalse(tunnel["pending"])

    def test_transaction_rolls_back_when_archive_update_fails(self):
        draft = service.merge_draft(self.payload())
        original_find = service._find_tunnel

        def broken_find(tunnel_code):
            row = original_find(tunnel_code)
            if row is not None:
                # 在订单和待办写入后模拟档案更新失败，事务必须整体回滚。
                row.pop("id", None)
            return row

        with patch.object(service, "_find_tunnel", side_effect=broken_find):
            with self.assertRaises(KeyError):
                service.write_back(int(draft["id"]), {})

        self.assertEqual(store.rows(ORDER_TABLE), [])
        self.assertEqual(store.rows(TODO_TABLE), [])
        self.assertIsNone(store.rows(DRAFT_TABLE)[0].get("work_order_no"))

    def test_retry_after_evidence_still_returns_existing_order_without_duplicate(self):
        draft = service.merge_draft(self.payload())
        service.write_back(int(draft["id"]), {})
        service.append_evidence(int(draft["id"]), {"operator": "王复核", "note": "重连后的补证"})

        result = service.write_back(int(draft["id"]), {})

        self.assertTrue(result["idempotent"])
        self.assertEqual(len(store.rows(ORDER_TABLE)), 1)

    def test_legacy_migration_retains_source_and_original_payload(self):
        legacy = {"note": "旧版字段名里的通风记录", "device": "旧风机"}
        result = service.migrate_legacy_draft({
            **self.payload(confirmed=False, fire_result=None),
            "source": "旧版消防App",
            "legacy": legacy,
        })

        self.assertIn("旧草稿迁移：旧版消防App", result["source_trace"])
        self.assertEqual(result["migrated_from"][0]["legacy_payload"], legacy)

    def test_concurrent_evidence_appends_without_overwriting_original_attachments(self):
        draft = service.merge_draft(self.payload())
        service.write_back(int(draft["id"]), {})
        order_no = str(draft["work_order_no"])

        service.append_evidence(int(draft["id"]), {
            "operator": "王复核",
            "note": "补充视频证据",
            "attachments": [{"name": "补证.mp4", "sha256": "hash-b", "uploaded_by": "王复核"}],
        })

        refreshed = service.get_draft(int(draft["id"]))
        order_no = str(refreshed["work_order_no"])
        order = service._find_order(order_no)
        self.assertEqual([item["sha256"] for item in refreshed["attachments"]], ["hash-a", "hash-b"])
        self.assertEqual(order["attachments"][0]["sha256"], "hash-a")
        self.assertEqual(order["supplemental_evidence"][0]["note"], "补充视频证据")

    def test_stale_concurrent_revision_is_rejected(self):
        draft = service.merge_draft(self.payload())
        old_revision = int(draft["revision"]) - 1

        with self.assertRaises(WorksheetError) as caught:
            service.append_evidence(
                int(draft["id"]),
                {"operator": "王复核", "note": "新证据", "expected_revision": old_revision},
            )
        self.assertEqual(caught.exception.status_code, 409)

    def test_duplicate_attachments_are_deduplicated_by_hash(self):
        draft = service.save_draft(self.payload())
        service.save_draft(self.payload(
            attachments=[
                {"name": "风机照片.jpg", "sha256": "hash-a"},
                {"name": "另一张.jpg", "sha256": "hash-c"},
            ],
        ))
        draft = service.get_draft(int(draft["id"]))
        self.assertEqual([item["sha256"] for item in draft["attachments"]], ["hash-a", "hash-c"])


if __name__ == "__main__":
    unittest.main()
