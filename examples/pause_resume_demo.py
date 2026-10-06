#!/usr/bin/env python3
"""暂停/恢复调度演示（仅用标准库，无需安装依赖）。

用法：
    python examples/pause_resume_demo.py [BASE_URL]

流程演示：
1. 提交一个 3 任务 DAG 工作流并领取首个任务；
2. 暂停工作流（重复暂停幂等），验证两种领取方式都不再发任务；
3. 已领取任务在原租约内照常上报成功；
4. 恢复工作流（重复恢复幂等），按依赖与原尝试次数继续领取直至完成。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def request(method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def main() -> None:
    workflow_body = {
        "idempotency_key": f"pause-demo-{int(time.time())}",
        "max_attempts": 3,
        "tasks": [
            {"key": "extract", "dependencies": []},
            {"key": "transform", "dependencies": ["extract"]},
            {"key": "load", "dependencies": ["transform"]},
        ],
    }

    print("== 1. 提交工作流并领取首个任务 ==")
    code, wf = request("POST", "/workflows", workflow_body)
    wf_id = wf["id"]
    print(code, "workflow_id =", wf_id, "paused =", wf["paused"])
    code, lease = request("POST", f"/workflows/{wf_id}/claim", {})
    print(code, "claimed", lease["key"], "attempt", lease["attempt"])

    print("== 2. 暂停（重复暂停幂等） ==")
    code, out = request("POST", f"/workflows/{wf_id}/pause")
    print(code, out)
    code, out = request("POST", f"/workflows/{wf_id}/pause")
    print(code, "重复暂停 ->", out)

    print("== 3. 暂停期间两种领取方式都不再发任务 ==")
    code, err = request("POST", f"/workflows/{wf_id}/claim", {})
    print(code, err["error"]["code"])  # 409 workflow_paused
    code, _ = request("POST", "/claims/claim", {})
    print(code, "(204 = 跨工作流领取也拿不到暂停工作流的任务)")

    print("== 4. 已领取任务在原租约内照常上报 ==")
    code, res = request(
        "POST",
        f"/tasks/{lease['task_id']}/succeed",
        {"token": lease["token"], "result": "rows=1024"},
    )
    print(code, res)
    code, detail = request("GET", f"/workflows/{wf_id}")
    print("查询: status =", detail["status"], "paused =", detail["paused"])

    print("== 5. 恢复（重复恢复幂等）并继续执行 ==")
    code, out = request("POST", f"/workflows/{wf_id}/resume")
    print(code, out)
    code, out = request("POST", f"/workflows/{wf_id}/resume")
    print(code, "重复恢复 ->", out)

    while True:
        code, lease = request("POST", f"/workflows/{wf_id}/claim", {})
        if code == 204:
            break
        print("  领取", lease["key"], "第", lease["attempt"], "次尝试")
        code, res = request(
            "POST",
            f"/tasks/{lease['task_id']}/succeed",
            {"token": lease["token"], "result": "ok"},
        )
        print("   succeed ->", code, res["status"])

    print("== 6. 最终状态 ==")
    code, detail = request("GET", f"/workflows/{wf_id}")
    print("workflow status:", detail["status"], "paused:", detail["paused"])
    for task in detail["tasks"]:
        print(f"  - {task['key']:<10} {task['status']:<10} attempts={task['attempts']}")


if __name__ == "__main__":
    main()
