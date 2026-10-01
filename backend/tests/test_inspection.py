"""隧道现场检修链路测试。

对应用户描述的缺陷与不变量：

1. 弱网下从隧道详情（通风）切到消防表，通风笔记不丢，重连不重复检修单；
2. 先核对通风笔记、再合并草稿、最后回写检修单；
3. 回写后返回列表不再显示同一草稿，档案/待办/工单一致；
4. 合并以检查员最后确认的现场版为准，历史工单按原附件留痕，旧草稿迁移保留来源；
5. 草稿、附件、工单号同一事务（失败整体回滚）；
6. 离线重传按 (隧道编号, 本地序列号) 幂等；
7. 并发补写不覆盖他人现场证据（409 + 当前版本）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client() -> TestClient:
    # 每个用例重建内存数据，避免检修单序号相互干扰
    import app.inspection_store as istore_mod
    import app.store as store_mod
    import app.services.inspection as svc_mod
    import app.services.tunnel as tunnel_svc_mod
    import app.routers.inspection as router_mod
    import app.routers.tunnel as tunnel_router_mod

    store_mod.store = store_mod.Store()
    new_istore = istore_mod.TunnelInspectionStore()
    istore_mod.inspection_store = new_istore
    istore_mod.store = store_mod.store  # 事务回滚要操作同一份隧道档案

    svc_mod.inspection_service = svc_mod.InspectionService()
    svc_mod.istore = new_istore
    router_mod.service = svc_mod.inspection_service
    tunnel_svc_mod.store = store_mod.store
    tunnel_router_mod.service = tunnel_svc_mod.TunnelService()

    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def tunnel_code(client: TestClient) -> str:
    items = client.get("/api/tunnel").json()["items"]
    return str(items[0]["隧道编号"])


# ------------------------------------------------------------ 1. 弱网切换不丢笔记

def test_ventilation_note_survives_switch_to_fire_sheet(client: TestClient, tunnel_code: str) -> None:
    # 详情页先写下通风笔记（请求在弱网下"看似失败"，本地稍后重传）
    vent = client.post("/api/tunnel/inspection/drafts", json={
        "tunnel_code": tunnel_code, "section": "通风", "inspector": "张三",
        "content": "射流风机2号异响", "client_seq": 1,
    })
    assert vent.status_code == 200
    draft_id = vent.json()["id"]

    # 检查员转换到消防工作表，补消防检查项 —— 同一草稿续传，通风笔记仍在
    fire = client.post("/api/tunnel/inspection/drafts", json={
        "tunnel_code": tunnel_code, "section": "消防", "inspector": "张三",
        "content": "灭火器压力正常；消火栓箱封条破损", "client_seq": 2,
        "draft_id": draft_id,
        "attachments": [{"name": "fire-seal.jpg", "sha256": "abc123"}],
    })
    assert fire.status_code == 200
    draft = fire.json()
    assert draft["sections"]["通风"]["content"] == "射流风机2号异响"
    assert draft["sections"]["消防"]["content"].startswith("灭火器压力正常")
    assert {a["name"] for a in draft["attachments"]} == {"fire-seal.jpg"}

    # 列表页此时应显示 1 个未收口草稿（不是 2 个）
    digest = client.get("/api/tunnel/inspection/digest").json()["items"][tunnel_code]
    assert digest["draft_count"] == 1
    assert digest["open_draft_ids"] == [draft_id]


def test_retry_same_draft_save_does_not_duplicate(client: TestClient, tunnel_code: str) -> None:
    body = {
        "tunnel_code": tunnel_code, "section": "通风", "inspector": "张三",
        "content": "笔记A", "client_seq": 7,
    }
    first = client.post("/api/tunnel/inspection/drafts", json=body).json()
    # 弱网重试同一 client_seq、同一 draft_id：只更新，不新增草稿
    client.post("/api/tunnel/inspection/drafts", json={**body, "draft_id": first["id"]})
    drafts = client.get("/api/tunnel/inspection/drafts",
                        params={"tunnel_code": tunnel_code}).json()["items"]
    assert len(drafts) == 1


# ------------------------------------------------------- 2/3. 核对→合并→回写→一致

def test_confirm_merge_submit_flow_and_list_consistency(client: TestClient, tunnel_code: str) -> None:
    # 详情页通风草稿
    vent = client.post("/api/tunnel/inspection/drafts", json={
        "tunnel_code": tunnel_code, "section": "通风", "inspector": "张三",
        "content": "通风旧版", "client_seq": 1,
    }).json()
    # 消防表上产生了第二份草稿（如两台设备分别离线作业）
    fire = client.post("/api/tunnel/inspection/drafts", json={
        "tunnel_code": tunnel_code, "section": "消防", "inspector": "张三",
        "content": "消防现场版", "client_seq": 10,
    }).json()

    # 用户先核对通风笔记（最后确认的现场版本）
    confirmed = client.post(f"/api/tunnel/inspection/drafts/{vent['id']}/confirm", json={}).json()
    assert confirmed["sections"]["通风"]["confirmed"] is True

    # 再合并草稿：通风稿为主，吸收消防稿
    merged = client.post(f"/api/tunnel/inspection/tunnels/{tunnel_code}/merge", json={
        "surviving_draft_id": vent["id"],
        "absorbed_draft_ids": [fire["id"]],
        "operator": "张三",
    }).json()
    assert merged["sections"]["通风"]["content"] == "通风旧版"
    assert merged["sections"]["消防"]["content"] == "消防现场版"
    # 被并草稿仍可查到，但带 merged_into 与来源
    absorbed = client.get("/api/tunnel/inspection/drafts").json()["items"]
    assert all(d["id"] != fire["id"] for d in absorbed)  # 开放列表里不再出现

    # 最后回写检修单（草稿+附件+工单号同事务）
    resp = client.post("/api/tunnel/inspection/work-orders", json={
        "tunnel_code": tunnel_code, "client_seq": 100, "inspector": "张三",
        "draft_id": vent["id"],
        "attachments": [{"name": "vent-noise.mp3", "sha256": "v1"}],
    })
    assert resp.status_code == 200
    result = resp.json()
    assert result["created"] is True
    order = result["order"]
    assert order["order_no"] == f"WO-{tunnel_code}-0100"
    assert {a["name"] for a in order["attachments_snapshot"]} == {"vent-noise.mp3"}

    # 返回隧道列表：同一草稿不再显示，工单与待办各 1
    digest = client.get("/api/tunnel/inspection/digest").json()["items"][tunnel_code]
    assert digest["open_draft"] is False
    assert digest["open_draft_ids"] == []
    assert digest["work_order_count"] == 1
    assert digest["todo_count"] == 2  # 通风 + 消防

    todos = client.get("/api/tunnel/inspection/todos",
                       params={"tunnel_code": tunnel_code}).json()["items"]
    assert {t["section"] for t in todos} == {"通风", "消防"}
    assert all(t["order_no"] == order["order_no"] for t in todos)

    # 隧道档案 pending 标记与待办一致
    view = client.get(
        f"/api/tunnel/inspection/tunnels/{tunnel_code}/consistency").json()
    assert view["consistent"] is True
    assert view["archive_pending"] is True
    assert view["open_draft_ids"] == []


def test_reconnect_does_not_duplicate_work_order(client: TestClient, tunnel_code: str) -> None:
    draft = client.post("/api/tunnel/inspection/drafts", json={
        "tunnel_code": tunnel_code, "section": "消防", "inspector": "李四",
        "content": "消防栓渗漏", "client_seq": 3,
    }).json()
    body = {
        "tunnel_code": tunnel_code, "client_seq": 55, "inspector": "李四",
        "draft_id": draft["id"],
    }
    first = client.post("/api/tunnel/inspection/work-orders", json=body).json()
    assert first["created"] is True
    # 弱网重连后重传：幂等返回原单，不重复显示
    second = client.post("/api/tunnel/inspection/work-orders", json=body).json()
    assert second["created"] is False
    assert second["order"]["id"] == first["order"]["id"]

    orders = client.get("/api/tunnel/inspection/work-orders",
                        params={"tunnel_code": tunnel_code}).json()["items"]
    assert len(orders) == 1


# ------------------------------------------------- 4. 合并取舍 / 历史留痕 / 迁移来源

def test_merge_prefers_last_confirmed_on_site_version(client: TestClient, tunnel_code: str) -> None:
    # 主草稿里的通风未确认；被并草稿的通风是现场最后确认版
    draft_a = client.post("/api/tunnel/inspection/drafts", json={
        "tunnel_code": tunnel_code, "section": "通风", "inspector": "张三",
        "content": "后台暂存版本", "client_seq": 1,
    }).json()
    draft_b = client.post("/api/tunnel/inspection/drafts", json={
        "tunnel_code": tunnel_code, "section": "通风", "inspector": "张三",
        "content": "现场最后确认版本", "client_seq": 2,
    }).json()
    client.post(f"/api/tunnel/inspection/drafts/{draft_b['id']}/confirm", json={})

    merged = client.post(f"/api/tunnel/inspection/tunnels/{tunnel_code}/merge", json={
        "surviving_draft_id": draft_a["id"],
        "absorbed_draft_ids": [draft_b["id"]],
        "operator": "张三",
    }).json()
    assert merged["sections"]["通风"]["content"] == "现场最后确认版本"
    origins = [p for p in merged["provenance"] if p.get("source_draft") == draft_b["id"]]
    assert origins and origins[0]["resolution"] == "confirmed-on-site-wins"

    # 已合并的草稿不能再次合并
    again = client.post(f"/api/tunnel/inspection/tunnels/{tunnel_code}/merge", json={
        "surviving_draft_id": draft_a["id"],
        "absorbed_draft_ids": [draft_b["id"]],
        "operator": "张三",
    })
    assert again.status_code == 400


def test_history_order_keeps_original_attachments(client: TestClient, tunnel_code: str) -> None:
    draft = client.post("/api/tunnel/inspection/drafts", json={
        "tunnel_code": tunnel_code, "section": "消防", "inspector": "王五",
        "content": "烟感故障", "client_seq": 1,
        "attachments": [{"name": "orig.png", "sha256": "h1"}],
    }).json()
    order = client.post("/api/tunnel/inspection/work-orders", json={
        "tunnel_code": tunnel_code, "client_seq": 9, "inspector": "王五",
        "draft_id": draft["id"],
    }).json()["order"]

    # 提交后再往"已收口"的事实上游做任何变更，不影响历史工单附件快照
    orders = client.get("/api/tunnel/inspection/work-orders").json()["items"]
    assert orders[0]["attachments_snapshot"] == order["attachments_snapshot"]
    assert orders[0]["attachments_snapshot"][0]["sha256"] == "h1"


def test_legacy_draft_migration_keeps_origin(client: TestClient, tunnel_code: str) -> None:
    resp = client.post("/api/tunnel/inspection/migrations/legacy-draft", json={
        "tunnel_code": tunnel_code, "section": "通风", "inspector": "赵六",
        "content": "旧系统里的通风备注", "origin": "2025旧版PDA#TD-88",
    })
    assert resp.status_code == 200
    draft = resp.json()
    assert draft["legacy"] is True
    assert draft["legacy_origin"] == "2025旧版PDA#TD-88"
    assert draft["provenance"][0]["source"].startswith("旧草稿迁移:")

    # 缺来源必须拒绝
    bad = client.post("/api/tunnel/inspection/migrations/legacy-draft", json={
        "tunnel_code": tunnel_code, "section": "通风", "inspector": "赵六",
        "content": "无来源迁移",
    })
    assert bad.status_code == 400


# ------------------------------------------------------------- 5. 事务原子回滚

def test_work_order_submit_is_atomic(client: TestClient, tunnel_code: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.inspection_store as istore_mod

    draft = client.post("/api/tunnel/inspection/drafts", json={
        "tunnel_code": tunnel_code, "section": "通风", "inspector": "张三",
        "content": "待回写", "client_seq": 1,
    }).json()

    # 模拟在生成工单号/写幂等键之后提交中断：事务必须把草稿、待办、档案、幂等键全部回滚
    def failing_submit(self, **kwargs):  # type: ignore[no-untyped-def]
        with self._lock:
            self._order_seq += 1
            self._idem_index[(kwargs["tunnel_code"], kwargs["client_seq"])] = self._order_seq
        raise RuntimeError("提交中断")

    monkeypatch.setattr(istore_mod.TunnelInspectionStore, "submit_work_order", failing_submit)
    resp = client.post("/api/tunnel/inspection/work-orders", json={
        "tunnel_code": tunnel_code, "client_seq": 200, "inspector": "张三",
        "draft_id": draft["id"],
    })
    assert resp.status_code == 500
    monkeypatch.undo()

    # 回滚后：工单不存在、幂等键未占用（同一 client_seq 可重新提交成功）、草稿仍开放
    assert client.get("/api/tunnel/inspection/work-orders").json()["items"] == []
    retry = client.post("/api/tunnel/inspection/work-orders", json={
        "tunnel_code": tunnel_code, "client_seq": 200, "inspector": "张三",
        "draft_id": draft["id"],
    })
    assert retry.status_code == 200
    assert retry.json()["created"] is True



def test_empty_work_order_rejected_without_side_effects(client: TestClient, tunnel_code: str) -> None:
    resp = client.post("/api/tunnel/inspection/work-orders", json={
        "tunnel_code": tunnel_code, "client_seq": 1, "inspector": "张三",
        "sections": {"通风": {"content": "   "}},
    })
    assert resp.status_code == 400
    assert client.get("/api/tunnel/inspection/work-orders").json()["items"] == []


def test_inline_sections_work_order_without_draft(client: TestClient, tunnel_code: str) -> None:
    """纯离线命令重放：本地从未拿到 draft_id，凭内嵌专项内容与附件也能成单。"""
    resp = client.post("/api/tunnel/inspection/work-orders", json={
        "tunnel_code": tunnel_code,
        "client_seq": 300,
        "inspector": "张三",
        "sections": {
            "通风": {"content": "通风记录", "confirmed": True},
            "消防": {"content": "消防记录", "confirmed": False},
        },
        "attachments": [{"name": "offline.jpg", "sha256": "h9"}],
    })
    assert resp.status_code == 200
    order = resp.json()["order"]
    assert set(order["sections"]) == {"通风", "消防"}
    assert order["attachments_snapshot"][0]["name"] == "offline.jpg"


# ------------------------------------------------------------- 7. 并发补写 409

def test_concurrent_evidence_patch_uses_optimistic_lock(client: TestClient, tunnel_code: str) -> None:
    # 甲、乙都基于版本 0 读到通风证据
    ev0 = client.get(f"/api/tunnel/inspection/tunnels/{tunnel_code}/evidence").json()
    assert ev0["version"] == 0

    ok = client.post("/api/tunnel/inspection/evidence", json={
        "tunnel_code": tunnel_code, "section": "通风", "inspector": "甲",
        "content": "甲的现场证据：风机底座松动", "base_version": 0,
    })
    assert ok.status_code == 200
    assert ok.json()["version"] == 1

    # 乙仍拿过期的 base_version=0 补写：必须 409，甲的证据不得被覆盖
    stale = client.post("/api/tunnel/inspection/evidence", json={
        "tunnel_code": tunnel_code, "section": "通风", "inspector": "乙",
        "content": "乙的补写：照明正常", "base_version": 0,
    })
    assert stale.status_code == 409
    assert stale.json()["detail"]["current"]["version"] == 1

    current = client.get(f"/api/tunnel/inspection/tunnels/{tunnel_code}/evidence").json()
    assert current["sections"]["通风"]["content"] == "甲的现场证据：风机底座松动"
    assert current["sections"]["通风"]["inspector"] == "甲"

    # 乙基于最新版本补写成功（不丢自己的新内容时，应改走合并/新专项）
    refreshed = client.post("/api/tunnel/inspection/evidence", json={
        "tunnel_code": tunnel_code, "section": "消防", "inspector": "乙",
        "content": "乙基于最新版补写：消防通道畅通", "base_version": 1,
    })
    assert refreshed.status_code == 200
    assert refreshed.json()["version"] == 2
    assert refreshed.json()["sections"]["通风"]["inspector"] == "甲"


# ---------------------------------------------------------------- 校验类

def test_unknown_tunnel_rejected(client: TestClient) -> None:
    resp = client.post("/api/tunnel/inspection/drafts", json={
        "tunnel_code": "NO-SUCH", "section": "通风", "inspector": "张三",
        "content": "x", "client_seq": 1,
    })
    assert resp.status_code == 400


def test_closed_draft_cannot_submit_twice(client: TestClient, tunnel_code: str) -> None:
    draft = client.post("/api/tunnel/inspection/drafts", json={
        "tunnel_code": tunnel_code, "section": "消防", "inspector": "张三",
        "content": "回写一次", "client_seq": 1,
    }).json()
    client.post("/api/tunnel/inspection/work-orders", json={
        "tunnel_code": tunnel_code, "client_seq": 1, "inspector": "张三",
        "draft_id": draft["id"],
    })
    # 换一个 client_seq 想把同一草稿再回写一次：拒绝
    resp = client.post("/api/tunnel/inspection/work-orders", json={
        "tunnel_code": tunnel_code, "client_seq": 2, "inspector": "张三",
        "draft_id": draft["id"],
    })
    assert resp.status_code == 400
