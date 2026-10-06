#!/usr/bin/env python3
"""暂停 / 恢复调度调用示例（仅用标准库，无需安装依赖）。

用法：
    python examples/pause_resume_demo.py [BASE_URL]

流程演示：
1. 提交一个 3 任务链式工作流，领取 a 并上报成功；
2. 暂停工作流：两种领取方式都拿不到 b；重复暂停幂等；
3. 暂停期间已领取的任务仍可凭原租约上报（先领取 b 再暂停）；
4. 恢复后按依赖关系继续领取直到完成；重复恢复幂等；
5. 对已终结工作流暂停 / 恢复返回 409 冲突。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def request(method: str, path: str, body: dict | None = None):
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


def claim(workflow_id: str):
    code, payload = request("POST", f"/workflows/{workflow_id}/claim", {})
    if code == 204:
        return None
    assert code == 200, payload
    return payload


def succeed(lease: str, result: str | None = None):
    return request(
        "POST",
        f"/tasks/{lease['task_id']}/succeed",
        {"token": lease["token"], "result": result},
    )


def main() -> None:
    idem_key = f"pause-demo-{int(time.time())}"
    body = {
        "idempotency_key": idem_key,
        "max_attempts": 3,
        "tasks": [
            {"key": "a", "dependencies": []},
            {"key": "b", "dependencies": ["a"]},
            {"key": "c", "dependencies": ["b"]},
        ],
    }

    print("== 1. 提交并完成 a ==")
    _, wf = request("POST", "/workflows", body)
    wf_id = wf["id"]
    la = claim(wf_id)
    print("  claimed", la["key"], "attempt", la["attempt"])
    print("  ", succeed(la, result="a done"))

    print("== 2. 暂停：重复暂停幂等，两种领取都被阻止 ==")
    code, paused = request("POST", f"/workflows/{wf_id}/pause")
    print(" ", code, "paused =", paused["paused"], "status =", paused["status"])
    code, again = request("POST", f"/workflows/{wf_id}/pause")
    print("   repeat pause ->", code, "paused =", again["paused"])

    code, _ = request("POST", f"/workflows/{wf_id}/claim", {})
    print("   workflow-scoped claim ->", code, "(204 = 无任务可领)")
    code, _ = request("POST", "/claims/claim", {})
    print("   cross-workflow claim  ->", code, "(204 = 无任务可领)")

    print("== 3. 恢复后已领取的任务仍可上报、后续任务继续领取 ==")
    code, resumed = request("POST", f"/workflows/{wf_id}/resume")
    print(" ", code, "paused =", resumed["paused"])
    code, _ = request("POST", f"/workflows/{wf_id}/resume")
    print("   repeat resume ->", code, "(幂等)")

    lb = claim(wf_id)
    print("  claimed", lb["key"], "attempt", lb["attempt"])

    # 运行中再次暂停：b 的租约保留，仍可凭原令牌上报
    code, _ = request("POST", f"/workflows/{wf_id}/pause")
    print("   paused again ->", code)
    code, res = succeed(lb, result="b done while paused")
    print("   report b while paused ->", code, res["status"])
    code, _ = request("POST", f"/workflows/{wf_id}/claim", {})
    print("   claim c while paused ->", code, "(204 = 仍被阻止)")

    print("== 4. 恢复并跑完剩余任务 ==")
    request("POST", f"/workflows/{wf_id}/resume")
    lc = claim(wf_id)
    print("  claimed", lc["key"], "attempt", lc["attempt"])
    print("  ", succeed(lc, result="c done"))
    assert claim(wf_id) is None

    _, detail = request("GET", f"/workflows/{wf_id}")
    print("   final status =", detail["status"], "paused =", detail["paused"])

    print("== 5. 终态工作流暂停 / 恢复 -> 409 ==")
    code, err = request("POST", f"/workflows/{wf_id}/pause")
    print("   pause ->", code, err["error"]["code"])
    code, err = request("POST", f"/workflows/{wf_id}/resume")
    print("   resume ->", code, err["error"]["code"])


if __name__ == "__main__":
    main()
