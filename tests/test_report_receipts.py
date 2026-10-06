"""重复上报：已受理回执重放、同令牌冲突、未受理令牌失效规则不变。"""
from tests.conftest import t
from tests.test_lifecycle import claim, fail, status_map, submit, succeed


def test_succeed_replay_returns_original_response(client):
    wf = submit(client, [t("a")], key="replay-ok")
    lease = claim(client, wf["id"])

    first = succeed(client, lease, result="rows=1024")
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "succeeded"
    assert body["attempts"] == 1

    # 工作流已终结；重发相同内容仍返回原状态码与响应体
    for _ in range(2):
        replay = succeed(client, lease, result="rows=1024")
        assert replay.status_code == 200
        assert replay.json() == body

    detail = client.get(f"/workflows/{wf['id']}").json()
    assert detail["status"] == "succeeded"
    task = detail["tasks"][0]
    assert task["status"] == "succeeded"
    assert task["attempts"] == 1


def test_fail_replay_does_not_consume_attempts(client):
    wf = submit(client, [t("a", max_attempts=2)], key="replay-fail")
    lease = claim(client, wf["id"])

    first = fail(client, lease, error="boom")
    assert first.status_code == 200
    assert first.json()["status"] == "pending"
    assert first.json()["attempts"] == 1

    replay = fail(client, lease, error="boom")
    assert replay.status_code == 200
    assert replay.json() == first.json()

    # 重放不消耗尝试次数、不改变状态：任务仍可被重领且 attempt=2
    detail = client.get(f"/workflows/{wf['id']}").json()
    assert detail["tasks"][0]["attempts"] == 1
    again = claim(client, wf["id"])
    assert again["attempt"] == 2


def test_fail_terminal_replay_does_not_recascade(client):
    wf = submit(
        client, [t("a", max_attempts=1), t("b", ["a"])], key="replay-cascade"
    )
    lease = claim(client, wf["id"])
    first = fail(client, lease, error="permanent")
    assert first.json()["status"] == "failed"

    replay = fail(client, lease, error="permanent")
    assert replay.status_code == 200
    assert replay.json() == first.json()

    detail = client.get(f"/workflows/{wf['id']}").json()
    assert status_map(detail) == {"a": "failed", "b": "skipped"}
    a_task = next(x for x in detail["tasks"] if x["key"] == "a")
    assert a_task["attempts"] == 1


def test_same_token_type_change_conflicts(client):
    wf = submit(client, [t("a")], key="conf-type")
    lease = claim(client, wf["id"])
    assert succeed(client, lease, result="ok").status_code == 200

    resp = fail(client, lease, error="ok")  # 同令牌改上报类型
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "report_conflict"
    assert body["error"]["accepted_type"] == "succeed"


def test_same_token_content_change_conflicts(client):
    wf = submit(client, [t("a"), t("b", max_attempts=2)], key="conf-content")
    la = claim(client, wf["id"])
    assert succeed(client, la, result="r1").status_code == 200
    resp = succeed(client, la, result="r2")  # 同令牌改成功内容
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "report_conflict"

    lb = claim(client, wf["id"])
    assert lb["key"] == "b"
    assert fail(client, lb, error="e1").json()["status"] == "pending"
    resp = fail(client, lb, error="e2")  # 同令牌改失败内容
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "report_conflict"
    # 冲突不影响已受理回执：相同内容重发仍重放
    assert fail(client, lb, error="e1").status_code == 200


def test_expired_token_without_receipt_stays_rejected(client):
    from app.db import connect, get_settings
    from app.service import transaction
    wf = submit(client, [t("a")], key="expired-noreceipt")
    lease = claim(client, wf["id"])

    conn = connect(get_settings())
    with transaction(conn):
        conn.execute(
            "UPDATE tasks SET leased_until = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", lease["task_id"]),
        )
    conn.close()

    # 首次未受理（租约到期）：无回执，重试仍按失效规则拒绝
    for _ in range(2):
        resp = succeed(client, lease)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "invalid_token"


def test_cancelled_token_without_receipt_stays_rejected(client):
    wf = submit(client, [t("a")], key="cancel-noreceipt")
    lease = claim(client, wf["id"])
    client.post(f"/workflows/{wf['id']}/cancel")

    for _ in range(2):
        resp = succeed(client, lease)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "invalid_token"


def test_receipt_replays_after_reclaim_and_workflow_terminal(client):
    """旧令牌已受理回执在任务重领、工作流终结后仍可重放；新令牌独立受理。"""
    wf = submit(client, [t("a", max_attempts=2)], key="replay-reclaim")
    lease1 = claim(client, wf["id"])
    first = fail(client, lease1, error="boom")  # 受理：回 pending
    assert first.json()["attempts"] == 1

    lease2 = claim(client, wf["id"])
    assert lease2["attempt"] == 2
    assert lease2["token"] != lease1["token"]
    # 新令牌看不到旧回执，走自身正常受理
    assert succeed(client, lease2, result="done").status_code == 200

    # 工作流已 succeeded；lease1 重发相同 fail 仍重放首次受理响应
    replay = fail(client, lease1, error="boom")
    assert replay.status_code == 200
    assert replay.json() == first.json()

    # 重放不改变当前状态与尝试计数
    detail = client.get(f"/workflows/{wf['id']}").json()
    assert detail["status"] == "succeeded"
    assert detail["tasks"][0]["status"] == "succeeded"
    assert detail["tasks"][0]["attempts"] == 2


def test_receipt_survives_restart(client):
    """服务重启后已受理回执仍可重放，同令牌改动内容仍冲突。"""
    from fastapi.testclient import TestClient

    from app.main import create_app
    wf = submit(client, [t("a")], key="replay-restart")
    lease = claim(client, wf["id"])
    first = succeed(client, lease, result="persisted")
    assert first.status_code == 200

    app2 = create_app()
    with TestClient(app2) as c2:
        replay = c2.post(
            f"/tasks/{lease['task_id']}/succeed",
            json={"token": lease["token"], "result": "persisted"},
        )
        assert replay.status_code == 200
        assert replay.json() == first.json()

        conflict = c2.post(
            f"/tasks/{lease['task_id']}/succeed",
            json={"token": lease["token"], "result": "tampered"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "report_conflict"


def test_concurrent_duplicate_reports_settle_consistently(settings):
    """并发相同上报：一个受理、其余重放，尝试次数只增一次。"""
    import threading

    from app import service
    from app.db import connect
    from app.models import WorkflowIn
    from tests.conftest import make_workflow, t

    conn0 = connect(settings)
    try:
        payload = WorkflowIn.model_validate(
            make_workflow([t("a")], key="conc-report", max_attempts=3)
        )
        detail, _ = service.create_workflow(conn0, payload, settings)
        lease = service.claim_task(
            conn0, settings, service.utcnow_iso(), workflow_id=detail["id"]
        )
    finally:
        conn0.close()

    results: list[dict] = []
    lock = threading.Lock()

    def reporter():
        conn = connect(settings)
        try:
            out = service.report_task(
                conn,
                settings,
                lease["task_id"],
                lease["token"],
                success=True,
                error=None,
                result="r",
                now=service.utcnow_iso(),
            )
        finally:
            conn.close()
        with lock:
            results.append(out)

    threads = [threading.Thread(target=reporter) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert len(results) == 8
    assert all(r == results[0] for r in results)
    assert results[0]["status"] == "succeeded"
    assert results[0]["attempts"] == 1
