"""暂停 / 恢复调度。

覆盖：
- 暂停后两种领取方式都不可再领取；任务图、尝试次数、已发租约保留；
- 暂停期间已领取任务仍可凭原租约上报；租约到期仍按重试/失败级联处理；
- 暂停期间任务全部终结，工作流仍进入终态；
- 恢复后按依赖关系与原尝试次数继续领取；
- 暂停结果与查询显示 paused；重复暂停/恢复幂等；
- 终态/已取消工作流暂停、恢复返回明确冲突；取消暂停中的工作流沿用取消语义；
- 暂停状态在服务重启后保持；跨工作流领取跳过暂停工作流。
"""
from tests.conftest import make_workflow, t
from tests.test_lifecycle import claim, fail, status_map, submit, succeed


PAST = "2000-01-01T00:00:00+00:00"


def force_expire(settings, task_id: str) -> None:
    from app.db import connect
    from app.service import transaction

    conn = connect(settings)
    try:
        with transaction(conn):
            conn.execute(
                "UPDATE tasks SET leased_until = ? WHERE id = ?",
                (PAST, task_id),
            )
    finally:
        conn.close()


def pause(client, wf_id: str):
    return client.post(f"/workflows/{wf_id}/pause")


def resume(client, wf_id: str):
    return client.post(f"/workflows/{wf_id}/resume")


# --------------------------------------------------------------------------- #
# 暂停阻止领取；状态可见
# --------------------------------------------------------------------------- #
def test_pause_pending_workflow_blocks_both_claim_kinds(client):
    wf = submit(client, [t("a")], key="pause-pending")
    wf_id = wf["id"]

    resp = pause(client, wf_id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["paused"] is True
    assert body["status"] == "pending"

    # 工作流内领取与跨工作流领取都返回 204
    assert client.post(f"/workflows/{wf_id}/claim", json={}).status_code == 204
    assert client.post("/claims/claim", json={}).status_code == 204

    detail = client.get(f"/workflows/{wf_id}").json()
    assert detail["paused"] is True
    assert detail["status"] == "pending"
    assert status_map(detail) == {"a": "pending"}


def test_pause_result_and_query_reflect_paused(client):
    wf = submit(client, [t("a")], key="pause-visible")
    body = pause(client, wf["id"]).json()
    assert set(["id", "status", "paused", "updated_at"]).issubset(body)

    resumed = resume(client, wf["id"]).json()
    assert resumed["paused"] is False
    assert client.get(f"/workflows/{wf['id']}").json()["paused"] is False


# --------------------------------------------------------------------------- #
# 保留任务图 / 尝试次数 / 已发租约；暂停期间上报照旧
# --------------------------------------------------------------------------- #
def test_pause_preserves_graph_attempts_and_lease(client):
    wf = submit(
        client, [t("a", max_attempts=5), t("b", ["a"])], key="pause-keep"
    )
    wf_id = wf["id"]
    lease = claim(client, wf_id)
    assert fail(client, lease).status_code == 200  # attempts=1，回到 pending

    assert pause(client, wf_id).status_code == 200
    detail = client.get(f"/workflows/{wf_id}").json()
    tasks = {x["key"]: x for x in detail["tasks"]}
    assert tasks["a"]["attempts"] == 1  # 尝试次数保留
    assert tasks["a"]["max_attempts"] == 5
    assert tasks["a"]["status"] == "pending"
    assert tasks["b"]["dependencies"] == ["a"]  # 任务图保留
    assert tasks["b"]["status"] == "pending"


def test_leased_task_can_still_report_within_lease_while_paused(client):
    wf = submit(client, [t("a"), t("b", ["a"])], key="pause-report")
    wf_id = wf["id"]
    lease = claim(client, wf_id)  # a 已领取
    assert pause(client, wf_id).status_code == 200

    # 原租约内上报成功照常受理
    assert succeed(client, lease, result="ok").status_code == 200

    detail = client.get(f"/workflows/{wf_id}").json()
    assert detail["paused"] is True
    assert status_map(detail) == {"a": "succeeded", "b": "pending"}
    # b 已就绪但暂停中仍不可领取
    assert claim(client, wf_id) is None


def test_fail_report_while_paused_keeps_retry_for_after_resume(client):
    wf = submit(client, [t("a", max_attempts=3)], key="pause-fail")
    wf_id = wf["id"]
    lease = claim(client, wf_id)
    pause(client, wf_id)

    # 暂停期间上报失败：未达上限回到 pending，尝试次数累加，但不可领取
    resp = fail(client, lease, error="transient")
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    assert resp.json()["attempts"] == 1
    assert claim(client, wf_id) is None

    # 恢复后按原尝试次数继续：下一次是第 2 次尝试
    assert resume(client, wf_id).status_code == 200
    again = claim(client, wf_id)
    assert again["attempt"] == 2
    assert succeed(client, again).status_code == 200
    detail = client.get(f"/workflows/{wf_id}").json()
    assert detail["status"] == "succeeded"
    assert detail["paused"] is False


# --------------------------------------------------------------------------- #
# 租约到期：暂停期间仍按重试 / 失败级联规则处理
# --------------------------------------------------------------------------- #
def test_lease_expiry_while_paused_retries_then_resume_keeps_attempts(
    client, settings
):
    wf = submit(client, [t("a", max_attempts=3)], key="pause-expire-retry")
    wf_id = wf["id"]
    lease = claim(client, wf_id)  # 第 1 次尝试
    pause(client, wf_id)

    # 租约在暂停期间到期：GET 触发懒回收，任务回 pending，attempts 保留
    force_expire(settings, lease["task_id"])
    detail = client.get(f"/workflows/{wf_id}").json()
    task = detail["tasks"][0]
    assert task["status"] == "pending"
    assert task["attempts"] == 1
    assert claim(client, wf_id) is None  # 暂停中仍不可领取

    resume(client, wf_id)
    again = claim(client, wf_id)
    assert again["attempt"] == 2  # 恢复后沿用原尝试次数
    assert succeed(client, again).status_code == 200


def test_lease_expiry_at_max_attempts_while_paused_cascades_and_finalizes(
    client, settings
):
    wf = submit(
        client, [t("a", max_attempts=1), t("b", ["a"])],
        key="pause-expire-fail",
    )
    wf_id = wf["id"]
    lease = claim(client, wf_id)
    assert lease["attempt"] == 1
    pause(client, wf_id)

    # 到期达到上限：a failed，后继 b 级联 skipped；暂停期间工作流仍终结
    force_expire(settings, lease["task_id"])
    detail = client.get(f"/workflows/{wf_id}").json()
    assert detail["status"] == "failed"
    assert detail["paused"] is False  # 进入终态时暂停标记清除
    assert status_map(detail) == {"a": "failed", "b": "skipped"}
    assert claim(client, wf_id) is None


# --------------------------------------------------------------------------- #
# 恢复后按依赖关系继续领取
# --------------------------------------------------------------------------- #
def test_resume_continues_claim_order_by_dependencies(client):
    wf = submit(
        client, [t("a"), t("b", ["a"]), t("c", ["a"]), t("d", ["b", "c"])],
        key="pause-deps",
    )
    wf_id = wf["id"]
    first = claim(client, wf_id)
    assert first["key"] == "a"
    succeed(client, first)
    pause(client, wf_id)
    assert claim(client, wf_id) is None

    resume(client, wf_id)
    lb = claim(client, wf_id)
    assert lb["key"] == "b"
    succeed(client, lb)
    lc = claim(client, wf_id)
    assert lc["key"] == "c"
    succeed(client, lc)
    # b、c 均成功后 d 可领取
    ld = claim(client, wf_id)
    assert ld["key"] == "d"
    succeed(client, ld)
    detail = client.get(f"/workflows/{wf_id}").json()
    assert detail["status"] == "succeeded"


# --------------------------------------------------------------------------- #
# 幂等
# --------------------------------------------------------------------------- #
def test_repeated_pause_and_resume_are_idempotent(client):
    wf = submit(client, [t("a")], key="pause-idem")
    wf_id = wf["id"]

    first = pause(client, wf_id).json()
    second = pause(client, wf_id)
    assert second.status_code == 200
    assert second.json() == first

    r1 = resume(client, wf_id).json()
    r2 = resume(client, wf_id)
    assert r2.status_code == 200
    assert r2.json() == r1
    assert r1["paused"] is False

    # 恢复后确实可以领取
    assert claim(client, wf_id) is not None


# --------------------------------------------------------------------------- #
# 终态 / 已取消：明确冲突
# --------------------------------------------------------------------------- #
def test_pause_resume_terminal_or_cancelled_conflict(client):
    wf = submit(client, [t("a")], key="pause-conflict")
    wf_id = wf["id"]
    lease = claim(client, wf_id)
    succeed(client, lease)
    client.get(f"/workflows/{wf_id}")  # 触发终结判定

    detail = client.get(f"/workflows/{wf_id}").json()
    assert detail["status"] == "succeeded"
    for op in (pause, resume):
        resp = op(client, wf_id)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "workflow_terminal"

    wf2 = submit(client, [t("a")], key="pause-conflict-cancel")
    client.post(f"/workflows/{wf2['id']}/cancel")
    for op in (pause, resume):
        resp = op(client, wf2["id"])
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "workflow_terminal"


def test_pause_resume_unknown_workflow_404(client):
    assert pause(client, "nope").status_code == 404
    assert resume(client, "nope").status_code == 404


# --------------------------------------------------------------------------- #
# 取消暂停中的工作流：沿用现有取消语义
# --------------------------------------------------------------------------- #
def test_cancel_paused_workflow_uses_existing_cancel_semantics(client):
    wf = submit(
        client, [t("a"), t("b", ["a"]), t("c", ["a"])], key="pause-cancel"
    )
    wf_id = wf["id"]
    lease = claim(client, wf_id)
    succeed(client, lease)
    lb = claim(client, wf_id)  # b 已租约
    pause(client, wf_id)

    resp = client.post(f"/workflows/{wf_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert resp.json()["cancelled_tasks"] == 2  # b(leased) + c(pending)

    detail = client.get(f"/workflows/{wf_id}").json()
    assert detail["status"] == "cancelled"
    assert detail["paused"] is False
    assert status_map(detail) == {
        "a": "succeeded",
        "b": "cancelled",
        "c": "cancelled",
    }
    # 旧令牌失效（现有取消语义）
    assert succeed(client, lb).status_code == 403
    # 已取消工作流再暂停/恢复：明确冲突
    assert pause(client, wf_id).status_code == 409
    assert resume(client, wf_id).status_code == 409


# --------------------------------------------------------------------------- #
# 跨工作流领取跳过暂停工作流
# --------------------------------------------------------------------------- #
def test_global_claim_skips_paused_workflow(client):
    paused_wf = submit(client, [t("a")], key="pause-global-1")
    other_wf = submit(client, [t("p")], key="pause-global-2")
    pause(client, paused_wf["id"])

    resp = client.post("/claims/claim", json={})
    assert resp.status_code == 200
    assert resp.json()["workflow_id"] == other_wf["id"]
    assert client.post("/claims/claim", json={}).status_code == 204

    # 恢复暂停工作流后，其任务重新可被跨工作流领取
    resume(client, paused_wf["id"])
    resp = client.post("/claims/claim", json={})
    assert resp.status_code == 200
    assert resp.json()["workflow_id"] == paused_wf["id"]


# --------------------------------------------------------------------------- #
# 重启后暂停状态保持
# --------------------------------------------------------------------------- #
def test_paused_state_survives_restart(client):
    from fastapi.testclient import TestClient

    from app.main import create_app

    wf = submit(
        client, [t("a"), t("b", ["a"])], key="pause-restart"
    )
    wf_id = wf["id"]
    lease = claim(client, wf_id)
    succeed(client, lease)
    pause(client, wf_id)

    app2 = create_app()
    with TestClient(app2) as c2:
        detail = c2.get(f"/workflows/{wf_id}").json()
        assert detail["paused"] is True
        assert status_map(detail) == {"a": "succeeded", "b": "pending"}
        # 重启后仍然不能领取
        assert c2.post(f"/workflows/{wf_id}/claim", json={}).status_code == 204
        assert c2.post("/claims/claim", json={}).status_code == 204

        # 恢复后即可继续
        assert c2.post(f"/workflows/{wf_id}/resume").status_code == 200
        lb = c2.post(f"/workflows/{wf_id}/claim", json={}).json()
        assert lb["key"] == "b"
        resp = c2.post(
            f"/tasks/{lb['task_id']}/succeed",
            json={"token": lb["token"]},
        )
        assert resp.status_code == 200
        final = c2.get(f"/workflows/{wf_id}").json()
        assert final["status"] == "succeeded"
        assert final["paused"] is False


# --------------------------------------------------------------------------- #
# 旧库迁移：无 paused 列的数据库升级后默认未暂停
# --------------------------------------------------------------------------- #
def test_legacy_schema_migrates_paused_column(settings):
    import os
    import sqlite3

    legacy_path = settings.db_path + ".legacy"
    for suffix in ("", "-wal", "-shm"):
        p = legacy_path + suffix
        if os.path.exists(p):
            os.remove(p)

    raw = sqlite3.connect(legacy_path)
    try:
        raw.execute(
            "CREATE TABLE workflows ("
            "id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, "
            "request_hash TEXT NOT NULL, status TEXT NOT NULL, "
            "default_max_attempts INTEGER NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        raw.execute(
            "INSERT INTO workflows VALUES "
            "('w1','k1','h1','pending',3,'t0','t0')"
        )
        raw.commit()
    finally:
        raw.close()

    from dataclasses import replace

    from app.db import connect, init_db

    legacy_settings = replace(settings, db_path=legacy_path)
    init_db(legacy_settings)  # 不应报错，且补齐 paused 列

    conn = connect(legacy_settings)
    try:
        row = conn.execute(
            "SELECT status, paused FROM workflows WHERE id = 'w1'"
        ).fetchone()
        assert row["status"] == "pending"
        assert row["paused"] == 0
    finally:
        conn.close()
