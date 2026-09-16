#!/usr/bin/env python3
"""猫娘聊天 的命令行入口。

用法
----
    python main.py                       # 离线模式，纯规则跑通全流程
    python main.py --online              # 用 config/llm.json 里的端点（DeepSeek）
    python main.py --status              # 查看当前状态（定性，不给数字）
    python main.py --status --debug      # 带头看原始数值
    python main.py --new                 # 开新周目
    python main.py --save mysave         # 指定存档名

在线模式的可调参数
------------------
    --model deepseek-v4-pro        换模型（默认 deepseek-flash）
    --effort {low,medium,high,max,none}   思考深度；none 最快
    --temperature 1.5              覆盖演出的 temperature
    --show-thinking                把思考过程用灰色打出来

密钥来源：环境变量 DEEPSEEK_API_KEY 优先，其次读同目录 keys.py（已 gitignore）。
**密钥不要写进任何会被提交的文件。**
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import lockout  # noqa: E402
from src.backends import LLMBackend, StubBackend  # noqa: E402
from src.deepseek import build_client, load_api_key, load_llm_config  # noqa: E402
from src.display import StreamPrinter  # noqa: E402
from src.session import Session  # noqa: E402

BANNER = """
╭──────────────────────────────────────────────╮
│            猫 娘  ·  GalGame                 │
╰──────────────────────────────────────────────╯
  /status  她现在怎么样   /affection  只看感情
  /search <词>  检索记忆  /blocks     压缩块列表
  /reset   开新周目       /quit       退出
"""


# --------------------------------------------------------------------------
# 后端装配
# --------------------------------------------------------------------------


def build_backend(args) -> tuple[object, dict]:
    """返回 (后端, llm 配置)。离线模式返回空配置。"""
    if not args.online:
        return StubBackend(seed=args.seed), {}

    cfg = load_llm_config(str(ROOT / "config" / "llm.json"))

    # 命令行覆盖采样参数（只影响演出；判分/摘要要保持低温）
    if args.temperature is not None:
        cfg["speak"]["temperature"] = args.temperature
    if args.effort:
        cfg["speak"]["reasoning_effort"] = args.effort

    # 密钥只认两个地方：环境变量、keys.py。keys.py 在 .gitignore 里。
    key = load_api_key(keys_paths=(str(ROOT / "keys.py"),))
    if not key:
        print("未找到 API Key（设 DEEPSEEK_API_KEY 环境变量，或写入同目录 keys.py）。")
        print("回退到离线模式。\n")
        return StubBackend(seed=args.seed), {}

    try:
        client = build_client(cfg, key, model=args.model)
    except Exception as exc:  # noqa: BLE001
        print(f"构造客户端失败：{exc}\n回退到离线模式。\n")
        return StubBackend(seed=args.seed), {}

    return LLMBackend(client, cfg), cfg


# --------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------


def cmd_status(session: Session, debug: bool) -> None:
    st = session.status(verbose=debug)
    print("\n── 她 · 现在 ─────────────────────────")
    print(f"  {st['tone']}")
    print(f"  {st['note']}")
    print()
    print(f"  状态：{st['phase']}    分支：{st['branch']}    亲密：{st['intimacy']}")
    if st["frozen_left"]:
        print(f"  信任冻结中（还剩 {st['frozen_left']} 回合）")
    if debug:
        print("\n── 原始数值（--debug）─────────────────")
        for k, v in st.items():
            if k in ("tone", "note"):
                continue
            print(f"  {k:<16} {v}")
    print("──────────────────────────────────────\n")


def cmd_affection(session: Session) -> None:
    """只看感情 —— 定性，不给数字。"""
    st = session.status()
    print(f"\n  她现在是「{st['tone']}」。\n  {st['note']}\n")


def cmd_search(session: Session, keyword: str) -> None:
    from src.memory import search_blocks

    hits = search_blocks(session.state, keyword)
    if not hits:
        print("（没找到相关的记忆喵）\n")
        return
    for b in hits:
        print(f"  [{b.block_id} T{b.tier}] {b.topic}：{b.summary[:80]}…")
    print()


def cmd_blocks(session: Session) -> None:
    blocks = session.state.blocks
    if not blocks:
        print("（还没有压缩块）\n")
        return
    for b in blocks:
        flag = "active" if b.active else "dead"
        print(f"  {b.block_id} T{b.tier} [{flag}] {b.topic}（回合 {b.start_turn}-{b.end_turn}）")
    print()


# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------


def _ask_name(args) -> str:
    """开新周目时问她叫什么。

    只在交互式终端里问 —— 管道输入（脚本、测试、`printf | python main.py`）
    不该被一个额外的问题卡住，那种情况下用默认名。
    """
    if getattr(args, "name", None):
        return args.name.strip() or "猫娘"
    if not sys.stdin.isatty():
        return "猫娘"
    try:
        got = input("给她起个名字（直接回车 = 猫娘）：").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return "猫娘"
    return got or "猫娘"


def _fresh_session(args, backend, *, new: bool, her_name: str = "猫娘") -> Session:
    if new:
        from src.state import SaveStore

        store = SaveStore(ROOT / "saves")
        state = lockout.new_game(
            store,
            save_id=args.save if args.save != "default" else None,
            her_name=her_name,
        )
        return Session(ROOT, backend, save_id=state.save_id)
    return Session(ROOT, backend, save_id=args.save)


def main() -> int:
    parser = argparse.ArgumentParser(description="猫娘聊天")
    parser.add_argument("--save", default="default", help="存档名")
    parser.add_argument("--new", action="store_true", help="开新周目")
    parser.add_argument("--name", help="她的名字（默认开新周目时询问，回车 = 猫娘）")
    parser.add_argument("--status", action="store_true", help="只显示状态")
    parser.add_argument("--debug", action="store_true", help="状态里显示原始数值")
    parser.add_argument("--online", action="store_true", help="使用在线模型")
    parser.add_argument("--seed", type=int, default=None, help="离线模式随机种子")
    parser.add_argument("--model", help="覆盖模型 id")
    parser.add_argument("--effort", choices=["low", "medium", "high", "max", "none"],
                        help="思考深度；none = 完全不思考（最快）")
    parser.add_argument("--temperature", type=float, help="覆盖演出的 temperature")
    parser.add_argument("--show-thinking", action="store_true",
                        help="把思考过程用灰色打印出来")
    args = parser.parse_args()

    backend, llm_cfg = build_backend(args)
    tail_keep = int(llm_cfg.get("tail_keep", 24))

    her_name = _ask_name(args) if args.new else "猫娘"
    try:
        session = _fresh_session(args, backend, new=args.new, her_name=her_name)
    except lockout.LockedSaveError as exc:
        print("\n" + str(exc) + "\n")
        print("运行 `python main.py --new` 开始新的一周目。\n")
        return 2

    if args.new:
        print(f"已开新周目：{session.state.save_id}（她叫「{session.state.her_name}」）\n")

    if args.status:
        cmd_status(session, args.debug)
        return 0

    mode = "在线" if args.online and isinstance(backend, LLMBackend) else "离线"
    print(BANNER)
    print(f"  模式：{mode}", end="")
    if mode == "在线":
        print(f"    model={getattr(backend, 'client').model}"
              f"    思考={llm_cfg.get('speak', {}).get('reasoning_effort')}"
              f"{'（显示思考过程）' if args.show_thinking else ''}")
    else:
        print()

    # --- 开场白：新周目时让猫娘先说第一句话 ---
    if session.state.turn == 0 and not session.state.messages:
        printer = StreamPrinter(show_thinking=args.show_thinking, tail_keep=tail_keep)
        try:
            line = session.opening(on_delta=printer.push)
            printer.close(line)
        except Exception as exc:  # noqa: BLE001 - 开场失败不该卡住
            print(f"（开场失败：{exc}）\n")

    while True:
        try:
            text = input("\n主人 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n（她歪头看着你离开的背影喵……）")
            return 0

        if not text:
            continue
        # 裸 quit/exit/q 也认 —— 终端用户的肌肉记忆，别逼人记斜杠
        if text in ("/quit", "/exit", "退出") or text.lower() in ("quit", "exit", "q"):
            print("（她挥了挥爪子喵，尾巴在门边晃了一下）")
            return 0
        if text == "/status":
            cmd_status(session, args.debug)
            continue
        if text == "/affection":
            cmd_affection(session)
            continue
        if text == "/blocks":
            cmd_blocks(session)
            continue
        if text.startswith("/search"):
            cmd_search(session, text[len("/search"):].strip())
            continue
        if text == "/reset":
            session = _fresh_session(args, backend, new=True, her_name=session.state.her_name)
            print(f"\n—— 重新开始（她叫「{session.state.her_name}」）——\n")
            continue

        # ⚠️ 前缀必须在 say() **之前**打印：流式内容是在 say() 里实时吐出来的，
        # 放到后面会让正文跑到名字前面去，尾部补打又会黏在名字后面。
        print(f"\n{session.state.her_name} > ", end="", flush=True)

        printer = StreamPrinter(show_thinking=args.show_thinking, tail_keep=tail_keep)
        try:
            outcome = session.say(text, on_delta=printer.push)
        except Exception as exc:  # noqa: BLE001 - 网络出错不退出循环
            print(f"\n[请求失败] {exc}\n")
            continue

        printer.close(outcome.line)

        if outcome.report:
            # 只给表演结论，不给数字
            from src.state import tone_profile

            tone = tone_profile(session.state, session.cfg["gates"]["intimate"])
            print(f"  · {tone['label']}")
        if outcome.notes:
            print(f"  · {' / '.join(outcome.notes)}")
        print()

        if session.state.withered:
            print("\n" + "=" * 46)
            print("  这条线里，她不再有想要的东西了。")
            print("  这个存档无法继续。重启也不会改变什么。")
            print("  你可以开新的一周目 —— 但那是另一个她。")
            print("=" * 46 + "\n")
            return 3


if __name__ == "__main__":
    raise SystemExit(main())
