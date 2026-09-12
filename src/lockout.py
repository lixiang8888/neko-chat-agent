"""锁定机制：WITHER 之后的内容封锁与新周目入口。

需求原文：
    「锁定之后整个程序锁定，重启也无法，只有重装文件」

实际落地（经确认调整为「锁死内容，保留新周目」）：
    - 沉沦的存档**永久不可继续**。重启无效、读档无效。
    - 但允许开新档 —— 强制退出再重装不是好的体验，而损失感来自
      「那一个她回不来了」，不是来自「你得重装软件」。
    - 锁定标记同时写进存档本体和一份独立的封条文件。
      封条存在的意义：即使有人手改存档中的 withered 字段，
      封条仍然能把它按回去。这让「不可逆」是真的不可逆。
"""

from __future__ import annotations

import json
from pathlib import Path

from .state import GameState, SaveStore


SEAL_SUFFIX = ".sealed"


class LockedSaveError(RuntimeError):
    """试图继续一个已沉沦的存档。"""

    def __init__(self, save_id: str) -> None:
        super().__init__(
            f"存档「{save_id}」已经沉沦。她不再有想要的东西了。\n"
            f"这个存档无法继续 —— 重启不会改变什么。\n"
            f"你可以开始新的一周目，但那是另一个她。"
        )
        self.save_id = save_id


def seal_path(store: SaveStore, save_id: str) -> Path:
    return store.root / f"{save_id}{SEAL_SUFFIX}"


def seal(state: GameState, store: SaveStore) -> None:
    """给存档盖上封条，并把状态本身也标记掉。"""
    state.withered = True
    state.branch = "wither"
    store.save(state)

    payload = {
        "save_id": state.save_id,
        "sealed_at": _now(),
        "final_turn": state.turn,
        "final_affection": state.affection,
        "note": "这条线里，她不再有想要的东西了。",
    }
    path = seal_path(store, state.save_id)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def is_sealed(store: SaveStore, save_id: str) -> bool:
    return seal_path(store, save_id).exists()


def check(store: SaveStore, save_id: str, state: GameState | None = None) -> None:
    """加载前的闸门。封条优先于存档内容。"""
    if is_sealed(store, save_id):
        raise LockedSaveError(save_id)
    if state is not None and state.withered:
        # 存档标记了但封条缺失 —— 补一次，然后拒绝
        seal(state, store)
        raise LockedSaveError(save_id)


def seal_info(store: SaveStore, save_id: str) -> dict | None:
    path = seal_path(store, save_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def new_game(
    store: SaveStore, save_id: str | None = None, her_name: str = "猫娘"
) -> GameState:
    """开新周目。旧的封条保留 —— 它是记录，不是障碍。"""
    from .state import _now_iso

    if save_id is None:
        existing = set(store.list_saves())
        n = 2
        while f"save{n}" in existing:
            n += 1
        save_id = f"save{n}"

    state = GameState(save_id=save_id, her_name=(her_name or "猫娘").strip() or "猫娘")
    state.created_at = _now_iso()
    state.bible.relationship_note = "初次见面。"
    store.save(state)
    return state


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")
