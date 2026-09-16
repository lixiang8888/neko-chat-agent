#!/usr/bin/env python3
"""桥接层的两条卫生标准，端到端验证。

1. **隐藏标记不上屏** —— ``<<好感度:N>>`` 不许出现在 SSE 流里。
   验证它需要一个**真的会吐标记**的后端（``MarkerBackend``），
   ``StubBackend`` 吐不出来，拿它测等于什么都没测。

2. **思考过程不出去** —— ``reasoning`` 分片不进 SSE。
   等价于终端的 ``--show-thinking`` 关闭。

    python tests/web/test_stream_hygiene.py
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

PORT = 8022
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        FAILS.append(f"{name} —— {detail}")


def wait_port(port: int, timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def sse(path: str, params: dict[str, str]) -> str:
    url = f"http://127.0.0.1:{PORT}{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=60) as res:
        return res.read().decode("utf-8")


def parse(raw: str) -> list[tuple[str, dict]]:
    out = []
    for frame in re.split(r"\r?\n\r?\n", raw):
        if not frame.strip() or frame.lstrip().startswith(":"):
            continue
        event, data = "message", []
        for line in frame.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if data:
            out.append((event, json.loads("\n".join(data))))
    return out


def main() -> int:
    print("=" * 52)
    print("  桥接层 · 流卫生")
    print("=" * 52)

    proc = subprocess.Popen(
        [sys.executable, str(HERE / "run_marker_server.py"), str(PORT)],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        if not wait_port(PORT):
            print("服务起不来：")
            print((proc.stderr.read() or b"").decode("utf-8", "replace")[:2000])
            return 1

        # 先开一个新周目，避免碰到本机已有的存档
        fresh = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/api/saves/new",
            data=json.dumps({"her_name": "测试"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(fresh, timeout=20) as res:
            save_id = json.loads(res.read())["save_id"]

        raw = sse("/api/turn", {"save_id": save_id, "text": "我回来了"})
        events = parse(raw)

        from marker_backend import BODY, THOUGHT  # noqa: E402

        deltas = "".join(d["text"] for e, d in events if e == "delta")
        done = next((d for e, d in events if e == "done"), None)
        error = next((d for e, d in events if e == "error"), None)

        check("回合跑完了（没有 error 事件）", error is None, str(error))
        check("收到 done 事件", done is not None)
        if done is None:
            return 1

        # --- 1. 隐藏标记 ---
        check("SSE 原文里没有「好感度」", "好感度" not in raw)
        check("SSE 原文里没有任何 '<<'", "<<" not in raw)
        check("上屏正文里没有「好感度」", "好感度" not in deltas)
        check("最终 line 里没有「好感度」", "好感度" not in done["line"])
        check("「好感度」也没有被拆成残留片段", ">>" not in deltas)

        # --- 2. 正文确实流出去了 ---
        check(
            "流出来的正文就是后端吐的那段",
            deltas.replace("\n", "") == BODY.replace("\n", ""),
            f"got={deltas!r}",
        )
        check("done.line 与流出来的正文一致", done["line"] == deltas)
        check("流是分多批到的（不是一次性）", len([1 for e, _ in events if e == "delta"]) > 1,
              f"{len(events)} 个事件")

        # --- 3. 思考过程 ---
        check("SSE 原文里没有思考内容", THOUGHT not in raw)
        check("上屏正文里没有思考内容", THOUGHT not in deltas)

        # --- 4. done 里带的定性状态，没有数字 ---
        st = done.get("status") or {}
        forbidden = {"affection", "mood", "care_ratio", "neg_depth", "self_affection", "tier_floor"}
        check("status 里没有原始数值字段", not (forbidden & set(st)),
              f"多余的键：{forbidden & set(st)}")
        check("status 带了定性标签 tone", bool(st.get("tone")), str(st))

        # --- 5. 流里也没有数字型的隐藏信息 ---
        check("delta 里没有 '<<' 开头的残留", not deltas.strip().endswith("<<"))

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print()
    if FAILS:
        print(f"  {len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"    - {f}")
        return 1
    print("  流卫生：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
