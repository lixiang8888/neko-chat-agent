#!/usr/bin/env python3
"""猫娘聊天 的网页版入口。一键启动。

    python3 start_web.py                 # 自动读密钥，起在线模式，开浏览器
    python3 start_web.py --offline       # 强制离线（纯规则，不花钱）
    python3 start_web.py --port 8080     # 换端口
    python3 start_web.py --no-open       # 别自动开浏览器

它会自己处理三件事：

1. **Web 依赖**。第一次跑时 `.venv` 里没有 FastAPI —— 自动 `uv sync --extra web`
   然后换到 venv 的解释器重进，不需要你先记住装什么。这几个包是**可选依赖**，
   不能进 `dependencies`（那会破坏「离线 407 项测试零依赖」这条性质）。
2. **密钥**。环境变量 `DEEPSEEK_API_KEY` 优先，其次同目录的 `keys.py`（已 gitignore）。
   密钥只在服务端，浏览器一个字节都碰不到。
3. **端口**。被占了就直接说清楚是谁占的、怎么腾 —— 而不是让 uvicorn 抛一句
   `address already in use` 让人去猜。

⚠️ **单 worker。** 桥接层把 `Session` 按 save_id 缓存在进程内存里，
多 worker 会让同一个存档出现两份活状态，各自写盘互相覆盖。
所以这里不提供 `--workers`，也别自己加 `-w`。
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import os
import socket
import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BANNER = """
╭──────────────────────────────────────────────╮
│            猫 娘 聊 天                       │
╰──────────────────────────────────────────────╯
"""


# --------------------------------------------------------------------------
# 依赖自举
# --------------------------------------------------------------------------


def _web_deps_missing() -> list[str]:
    return [
        name
        for name in ("fastapi", "uvicorn", "sse_starlette")
        if importlib.util.find_spec(name) is None
    ]


def ensure_web_deps() -> None:
    missing = _web_deps_missing()
    if not missing:
        return

    # os.execv 会顶掉当前进程，不打这行就什么提示都看不见了
    print(f"缺 Web 依赖：{'、'.join(missing)}", flush=True)
    if subprocess.run(["which", "uv"], capture_output=True).returncode != 0:
        sys.exit(
            "这台机器上没有 uv，装不了。两条路：\n"
            "  1) 装 uv（https://docs.astral.sh/uv/）后重跑本脚本\n"
            "  2) pip install fastapi 'uvicorn[standard]' sse-starlette\n"
        )

    print("正在装（uv sync --extra web）…\n", flush=True)
    if subprocess.run(["uv", "sync", "--extra", "web"], cwd=str(ROOT)).returncode != 0:
        sys.exit("装依赖失败。手动跑一次 `uv sync --extra web` 看报错。")

    # 装完再问一次「导得进来吗」。问得进来就说明当前解释器是对的；
    # 还导不进来，就说明它不是 .venv 里那个 —— 换过去重新进来。
    #
    # ⚠️ 判据必须是「导得进来」，不能是「sys.executable 和 .venv/bin/python
    # 是不是同一个路径」：uv 建的 .venv/bin/python 是指向 /usr/bin/python3 的
    # **符号链接**，resolve() 之后两边完全一样，那种比较会误判成「已经在 venv 里」，
    # 于是跳过换解释器，然后在下一行 import fastapi 炸掉。
    importlib.invalidate_caches()
    still_missing = _web_deps_missing()
    if still_missing:
        venv_python = ROOT / ".venv" / "bin" / "python"
        if venv_python.exists():
            os.execv(
                str(venv_python),
                [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]],
            )
        sys.exit(
            f"依赖装好了，但当前解释器仍然导不进来：{'、'.join(still_missing)}\n"
            f"（{sys.executable}，venv 里没有可用的解释器）\n"
            f"试试直接用它跑：{ROOT / '.venv' / 'bin' / 'python'} start_web.py"
        )


# --------------------------------------------------------------------------
# 密钥
# --------------------------------------------------------------------------


def resolve_key() -> str:
    """环境变量优先，其次 ``keys.py``。和 ``main.py`` 同一套规则。"""
    from src.deepseek import load_api_key

    return load_api_key(keys_paths=(str(ROOT / "keys.py"),))


def no_key_message() -> str:
    return (
        "\n没找到 API Key，起不了在线模式。两种给法：\n\n"
        "    export DEEPSEEK_API_KEY=sk-...        # 推荐\n"
        "    或者写进本目录的 keys.py（已在 .gitignore 里）\n\n"
        "只想先看看界面长什么样，不接模型：\n\n"
        "    python3 start_web.py --offline\n"
    )


# --------------------------------------------------------------------------
# 端口
# --------------------------------------------------------------------------


def port_holder(port: int) -> int | None:
    """谁在监听这个端口？拿到 pid 才能给人一句能照做的提示。"""
    try:
        out = subprocess.run(
            ["ss", "-lptnH", f"sport = :{port}"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    # ss 把进程信息塞成一个 token：users:(("uvicorn",pid=498613,fd=13))
    # 所以不能看 token 开头，得在里面找 pid=
    at = out.find("pid=")
    if at == -1:
        return None
    digits = ""
    for ch in out[at + 4 :]:
        if not ch.isdigit():
            break
        digits += ch
    return int(digits) if digits else None


def port_free(port: int) -> bool:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


# --------------------------------------------------------------------------
# 开浏览器
# --------------------------------------------------------------------------


def open_browser(url: str) -> None:
    """WSL 里最省事的是让 Windows 那边开。失败就算了，不该拖垮启动。"""
    for cmd in (["wslview", url], ["xdg-open", url], ["cmd.exe", "/c", "start", "", url]):
        try:
            subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return
        except (OSError, subprocess.SubprocessError):
            continue
    webbrowser.open(url)


# --------------------------------------------------------------------------
# 起
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="猫娘聊天 · 网页版")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1",
                        help="想在同局域网的手机上开就填 0.0.0.0")
    parser.add_argument("--offline", action="store_true",
                        help="强制离线后端（纯规则，不连模型）")
    parser.add_argument("--model", help="覆盖 config/llm.json 里的模型 id")
    parser.add_argument("--no-open", action="store_true", help="别自动开浏览器")
    args = parser.parse_args()

    ensure_web_deps()

    if not port_free(args.port):
        pid = port_holder(args.port)
        who = f"（pid {pid}）" if pid else ""
        hint = f"    kill {pid}" if pid else "    # 找出占用它的进程并结束"
        sys.exit(
            f"\n端口 {args.port} 已经被占了{who}。\n\n"
            f"要么腾出来：\n\n{hint}\n\n"
            f"要么换个端口：\n\n    python3 start_web.py --port {args.port + 1}\n"
        )

    # --- 后端 ---
    if args.offline:
        os.environ["NEKO_OFFLINE"] = "1"
        mode, detail = "离线", "StubBackend —— 纯规则，不连模型"
    else:
        key = resolve_key()
        if not key:
            sys.exit(no_key_message())
        os.environ["DEEPSEEK_API_KEY"] = key      # 让桥接层从环境里拿到
        os.environ.pop("NEKO_OFFLINE", None)

        from src.deepseek import load_llm_config

        cfg = load_llm_config(str(ROOT / "config" / "llm.json"))
        model = args.model or cfg["model"]
        effort = (cfg.get("speak") or {}).get("reasoning_effort", "low")
        mode = "在线"
        detail = f"model={model}    思考={effort}"

    # --- 静态前端 ---
    if not (ROOT / "web" / "index.html").exists():
        sys.exit("web/index.html 不见了 —— 前端没装全。")

    from server.app import STORE, app

    # flush=True：重定向到文件时 stdout 是块缓冲的，而 uvicorn 的日志走 stderr，
    # 不刷的话 banner 会被自己的启动日志盖到后头去。
    print(BANNER, flush=True)
    print(f"  模式：{mode}    {detail}", flush=True)
    print(
        f"  地址：http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}",
        flush=True,
    )
    print(f"  存档：{len(STORE.list_saves())} 个（{STORE.root}）", flush=True)
    print("\n  Ctrl+C 收工。\n", flush=True)

    if not args.no_open:
        open_browser(f"http://127.0.0.1:{args.port}")

    import uvicorn

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    except KeyboardInterrupt:
        pass
    print("\n（她歪头看着你离开的背影喵……）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
