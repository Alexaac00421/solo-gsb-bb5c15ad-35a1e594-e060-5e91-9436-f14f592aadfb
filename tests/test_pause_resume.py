"""暂停/恢复：领取门控、租约与上报保持、终态冲突、幂等、取消与重启保持。"""
from app.db import connect, get_settings
from app.service import transaction
from tests.conftest import t
from tests.test_lifecycle import claim, fail, status_map, submit, succeed


def pause(client, wf_id):
    return client.post(f"/workflows/{wf_id}/pause")


def resume(client, wf_id):
    return client.post(f"/workflows/{wf_id}/resume")


def force_lease_expiry(task_id):
    """把指定任务的租约截止时间改到过去，模拟租约到期。"""
    conn = connect(get_settings())
    try:
        with transaction(conn):
            conn.execute(
                "UPDATE tasks SET leased_until = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", task_id),
            )
    finally:
        conn.close()


def test_pause_blocks_both_claim_modes_and_resume_restores(client):
    wf1 = submit(client, [t("a")], key="p-block-1")
    wf2 = submit(client, [t("p")], key="p-block-2")

    resp = pause(client, wf1["id"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["paused"] is True
    assert body["status"] == "pending"

    # 指定工作流领取：明确冲突
    resp = client.post(f"/workflows/{wf1['id']}/claim", json={})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "workflow_paused"

    # 跨工作流领取：跳过暂停的工作流，领到 wf2 的任务后无任务可领
    resp = client.post("/claims/claim", json={})
    assert resp.status_code == 200
    assert resp.json()["workflow_id"] == wf2["id"]
    assert client.post("/claims/claim", json={}).status_code == 204

    # 恢复后按原依赖与尝试次数领取
    resp = resume(client, wf1["id"])
    assert resp.status_code == 200
    assert resp.json()["paused"] is False
    lease = claim(client, wf1["id"])
    assert lease["key"] == "a"
    assert lease["attempt"] == 1


def test_pause_and_resume_are_idempotent(client):
    wf = submit(client, [t("a")], key="p-idem")

    first = pause(client, wf["id"])
    assert first.status_code == 200
    again = pause(client, wf["id"])
    assert again.status_code == 200
    assert again.json() == first.json()  # 重复暂停不刷新 updated_at

    detail = client.get(f"/workflows/{wf['id']}").json()
    assert detail["paused"] is True

    r1 = resume(client, wf["id"])
    assert r1.status_code == 200
    assert r1.json()["paused"] is False
    r2 = resume(client, wf["id"])
    assert r2.status_code == 200
    assert r2.json() == r1.json()

    assert client.get(f"/workflows/{wf['id']}").json()["paused"] is False


def test_pause_resume_on_terminal_or_cancelled_conflicts(client):
    # succeeded
    wf_ok = submit(client, [t("a")], key="p-term-ok")
    succeed(client, claim(client, wf_ok["id"]))
    assert client.get(f"/workflows/{wf_ok['id']}").json()["status"] == "succeeded"

    # failed
    wf_bad = submit(client, [t("a", max_attempts=1)], key="p-term-fail")
    fail(client, claim(client, wf_bad["id"]))
    assert client.get(f"/workflows/{wf_bad['id']}").json()["status"] == "failed"

    # cancelled
    wf_cx = submit(client, [t("a")], key="p-term-cancel")
    client.post(f"/workflows/{wf_cx['id']}/cancel")

    for wf_id in (wf_ok["id"], wf_bad["id"], wf_cx["id"]):
        for resp in (pause(client, wf_id), resume(client, wf_id)):
            assert resp.status_code == 409
            assert resp.json()["error"]["code"] == "workflow_terminal"

    # 不存在的工作流
    assert pause(client, "nope").status_code == 404
    assert resume(client, "nope").status_code == 404


def test_pause_preserves_lease_and_allows_report_within_lease(client):
    wf = submit(client, [t("a"), t("b", ["a"])], key="p-lease")
    wf_id = wf["id"]
    lease_a = claim(client, wf_id)
    assert lease_a["attempt"] == 1

    assert pause(client, wf_id).status_code == 200

    # 已领取任务仍可在原租约内上报
    resp = succeed(client, lease_a, result="done-while-paused")
    assert resp.status_code == 200
    assert resp.json()["status"] == "succeeded"

    # 暂停期间后继任务不可领取（两种领取方式）
    assert client.post(f"/workflows/{wf_id}/claim", json={}).status_code == 409
    assert client.post("/claims/claim", json={}).status_code == 204

    # 恢复后继续按依赖领取，尝试次数从原值累计
    assert resume(client, wf_id).status_code == 200
    lease_b = claim(client, wf_id)
    assert lease_b["key"] == "b"
    assert lease_b["attempt"] == 1
    detail = client.get(f"/workflows/{wf_id}").json()
    assert detail["tasks"][0]["attempts"] == 1  # a 的尝试次数未被暂停重置


def test_fail_report_during_pause_returns_to_pending_but_not_claimable(client):
    wf = submit(client, [t("a", max_attempts=2)], key="p-fail")
    wf_id = wf["id"]
    lease = claim(client, wf_id)

    assert pause(client, wf_id).status_code == 200
    resp = fail(client, lease, error="flaky")
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"  # 未达上限，回到待领取

    # 回到 pending 也不能在暂停期间领取
    assert client.post(f"/workflows/{wf_id}/claim", json={}).status_code == 409

    assert resume(client, wf_id).status_code == 200
    again = claim(client, wf_id)
    assert again["attempt"] == 2


def test_lease_expiry_during_pause_retries_after_resume(client):
    wf = submit(client, [t("a", max_attempts=2)], key="p-expire")
    wf_id = wf["id"]
    lease = claim(client, wf_id)

    assert pause(client, wf_id).status_code == 200
    force_lease_expiry(lease["task_id"])

    # 查询触发懒回收：暂停期间租约到期仍按既有规则回到 pending
    detail = client.get(f"/workflows/{wf_id}").json()
    assert status_map(detail)["a"] == "pending"
    assert detail["tasks"][0]["attempts"] == 1
    assert detail["paused"] is True

    # 到期旧令牌已失效；暂停期间不可领取
    assert succeed(client, lease).status_code == 403
    assert client.post(f"/workflows/{wf_id}/claim", json={}).status_code == 409

    # 恢复后按原尝试次数重领
    assert resume(client, wf_id).status_code == 200
    again = claim(client, wf_id)
    assert again["attempt"] == 2


def test_lease_expiry_at_max_attempts_during_pause_fails_and_cascades(client):
    wf = submit(
        client,
        [t("a", max_attempts=1), t("b", ["a"]), t("c")],
        key="p-cascade",
    )
    wf_id = wf["id"]
    lease_a = claim(client, wf_id)
    assert lease_a["key"] == "a"

    assert pause(client, wf_id).status_code == 200
    force_lease_expiry(lease_a["task_id"])

    # 暂停期间达到上限的到期租约仍置失败并级联跳过
    detail = client.get(f"/workflows/{wf_id}").json()
    assert status_map(detail)["a"] == "failed"
    assert status_map(detail)["b"] == "skipped"
    assert status_map(detail)["c"] == "pending"
    assert detail["paused"] is True

    # 独立分支也要等恢复后才能领取
    assert client.post(f"/workflows/{wf_id}/claim", json={}).status_code == 409
    assert resume(client, wf_id).status_code == 200
    lease_c = claim(client, wf_id)
    assert lease_c["key"] == "c"
    succeed(client, lease_c)

    # 全部终结后工作流进入终态（a 失败 -> failed）；此前已恢复，paused 为 False
    detail = client.get(f"/workflows/{wf_id}").json()
    assert detail["status"] == "failed"
    assert detail["paused"] is False
    # 终态后暂停/恢复均为明确冲突
    assert pause(client, wf_id).status_code == 409
    assert resume(client, wf_id).status_code == 409


def test_workflow_reaches_terminal_while_paused(client):
    wf = submit(client, [t("a")], key="p-terminal")
    wf_id = wf["id"]
    lease = claim(client, wf_id)

    assert pause(client, wf_id).status_code == 200
    # 暂停期间最后一个任务在原租约内上报成功 -> 工作流照常进入终态
    assert succeed(client, lease).status_code == 200

    detail = client.get(f"/workflows/{wf_id}").json()
    assert detail["status"] == "succeeded"
    assert detail["paused"] is True

    # 已终结：暂停/恢复都返回明确冲突
    for resp in (pause(client, wf_id), resume(client, wf_id)):
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "workflow_terminal"


def test_cancel_paused_workflow_follows_existing_semantics(client):
    wf = submit(client, [t("a"), t("b", ["a"])], key="p-cancel")
    wf_id = wf["id"]
    lease_a = claim(client, wf_id)

    assert pause(client, wf_id).status_code == 200
    resp = client.post(f"/workflows/{wf_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert resp.json()["cancelled_tasks"] == 2  # a 已租约、b 待领取，都被取消

    # 旧令牌失效、不可领取
    assert succeed(client, lease_a).status_code == 403
    assert client.post(f"/workflows/{wf_id}/claim", json={}).status_code == 409

    # 已取消：暂停/恢复均为明确冲突；重复取消仍幂等
    assert pause(client, wf_id).status_code == 409
    assert resume(client, wf_id).status_code == 409
    assert client.post(f"/workflows/{wf_id}/cancel").status_code == 200


def test_queries_expose_paused_flag(client):
    wf = submit(client, [t("a")], key="p-query")
    wf_id = wf["id"]

    # 提交响应与查询均带 paused 字段
    assert wf["paused"] is False
    detail = client.get(f"/workflows/{wf_id}").json()
    assert detail["paused"] is False

    pause(client, wf_id)
    detail = client.get(f"/workflows/{wf_id}").json()
    assert detail["paused"] is True

    listed = client.get("/workflows").json()
    entry = next(x for x in listed if x["id"] == wf_id)
    assert entry["paused"] is True


def test_pause_state_survives_restart(client):
    from fastapi.testclient import TestClient

    from app.main import create_app

    wf = submit(client, [t("a"), t("b", ["a"])], key="p-restart")
    wf_id = wf["id"]
    lease_a = claim(client, wf_id)
    assert pause(client, wf_id).status_code == 200

    # —— 模拟重启：重新 create_app ——
    app2 = create_app()
    with TestClient(app2) as c2:
        detail = c2.get(f"/workflows/{wf_id}").json()
        assert detail["paused"] is True
        assert detail["status"] == "running"

        # 重启后两种领取方式仍被暂停阻断
        resp = c2.post(f"/workflows/{wf_id}/claim", json={})
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "workflow_paused"
        assert c2.post("/claims/claim", json={}).status_code == 204

        # 已发租约在重启后仍可上报
        resp = c2.post(
            f"/tasks/{lease_a['task_id']}/succeed",
            json={"token": lease_a["token"]},
        )
        assert resp.status_code == 200

        # 恢复后调度继续
        assert c2.post(f"/workflows/{wf_id}/resume").json()["paused"] is False
        lease_b = c2.post(f"/workflows/{wf_id}/claim", json={}).json()
        assert lease_b["key"] == "b"


def test_init_db_migrates_database_without_paused_column(tmp_path):
    """老版本库（无 paused 列）启动时自动迁移，既有数据保持不变。"""
    import sqlite3

    from app.config import Settings
    from app.db import init_db

    db_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE workflows (
            id              TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            request_hash    TEXT NOT NULL,
            status          TEXT NOT NULL,
            default_max_attempts INTEGER NOT NULL,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );
        INSERT INTO workflows VALUES
            ('wf-old', 'k-old', 'h', 'running', 3, '2026-01-01', '2026-01-01');
        """
    )
    conn.close()

    settings = Settings(
        db_path=db_path,
        lease_seconds=30,
        default_max_attempts=3,
        reaper_interval_seconds=0,
        busy_timeout_ms=5000,
    )
    init_db(settings)
    init_db(settings)  # 迁移幂等

    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT status, paused FROM workflows WHERE id = 'wf-old'"
        ).fetchone()
        assert row["status"] == "running"
        assert row["paused"] == 0
    finally:
        conn.close()
