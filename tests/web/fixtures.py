#!/usr/bin/env python3
"""给前端测试和截图造存档。

**这只造数据，不造逻辑。** 它把 ``affection`` 直接写进存档文件，
是为了让 ``tone_profile`` 自己算出各个档位 —— 这里不复制任何判定，
也没打算复制：一旦这里开始「顺手也算一下档位」，两边就会漂移。

用法::

    python tests/web/fixtures.py            # 造一套（会先清掉旧的）
    python tests/web/fixtures.py --keep     # 保留已有存档
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import lockout  # noqa: E402
from src.context import append_message  # noqa: E402
from src.scoring import load_rules  # noqa: E402
from src.state import GameState, SaveStore  # noqa: E402

STORE = SaveStore(ROOT / "saves")


# 每个档位一段小对话，让字幕里看得出她已经走到哪儿了。
SCRIPT = {
    "警戒": [
        ("user", "我回来了。"),
        ("assistant", "……嗯。\n（她坐在窗台上，没有回头。尾巴尖轻轻点了一下。）"),
        ("user", "今天在门口等了吗？"),
        ("assistant", "没有喵。我在看外面。\n（耳朵转过来半寸，又转回去了。）"),
    ],
    "依赖": [
        ("user", "今天回来晚了，路上堵。"),
        ("assistant", "（她从沙发背上跳下来，小跑着到门口，仰头看你。）\n"
                      "主人主人，今天发生了一件事喵！你听不听喵？"),
        ("user", "听着呢。"),
        ("assistant", "楼下那家的猫生了三只小猫喵。我去看了好久喵。\n"
                      "（尾巴绕过来搭在你手腕上）……就一会儿喵，一下下就好喵。"),
    ],
    "独属": [
        ("user", "刚才在忙工作，没顾上回你。"),
        ("assistant", "（她坐在你椅子扶手上，两只前爪按着你的小臂。）\n"
                      "你今天没有先来找我喵。我不高兴了喵，要哄的喵。"),
        ("user", "怎么哄？"),
        ("assistant", "（凑过来嗅了嗅你的领口，鼻尖蹭到下巴。）\n"
                      "嗯…是主人的味道喵。可以了喵。\n"
                      "……但是明天要先用手指碰碰我的耳朵才可以开始工作喵。"),
    ],
    "认定": [
        ("user", "明天可能要出差三天。"),
        ("assistant", "（她正在窗边理毛，动作停在半空。）\n\n"
                      "……三天喵。\n\n"
                      "（她把爪子收回来，正正地坐好，看你。）\n"
                      "主人，我今天想了很久喵。我哪儿都不去喵。\n"
                      "你去吧。我在这里把灯留着。\n"
                      "（把脸埋进你袖子里）……你在就很好喵。"),
        ("user", "会想你的。"),
        ("assistant", "（没有接话。她只是把额头贴在你手背上，停了很久。）\n"
                      "……嗯。我知道的喵。\n"
                      "我数着你回来。数到第三天，你推门的时候，我就已经站在门口了喵。"),
    ],
    "沉沦": [
        ("user", "我回来了。"),
        ("assistant", "嗯，我知道了喵。"),
        ("user", "……你怎么了？"),
        ("assistant", "（她坐在那里，没有动。）\n……没什么喵。"),
    ],
}


def make(
    save_id: str,
    affection: float,
    her_name: str,
    *,
    tone_key: str = "天生好感",
    seeded: bool = True,
    sealed: bool = False,
    tier_floor: int | None = None,
) -> GameState:
    state = GameState(
        save_id=save_id,
        her_name=her_name,
        affection=affection,
        tier_floor=int(affection) if tier_floor is None else tier_floor,
    )
    state.created_at = "2026-09-10T21:00:00"

    if seeded:
        for role, text in SCRIPT.get(tone_key, []):
            append_message(state, role, text)
        state.turn = len([1 for r, _ in SCRIPT.get(tone_key, []) if r == "user"])

    if sealed:
        # 走正规通道盖章 —— 手写 withered 字段会被 lockout 按回去，
        # 而且不会有封条文件，前端就看不到「不可继续」。
        lockout.seal(state, STORE)
    else:
        STORE.save(state)
    return state


def make_doomed(save_id: str = "save8", her_name: str = "小雪") -> GameState:
    """造一个**再挨一次负向就沉沦**的存档，用来拍终幕。

    崩坏已经在了（``collapse_active``），已经挨过两次（``collapse_negatives=2``），
    而 ``config/affection.json`` 的 ``wither_negative_limit`` 是 3 ——
    所以再来一次负向事件就会触发 ``lockout.seal()``。

    阈值不写死在这里：从配置里读，改了配置这里跟着走。
    """
    limit = int(load_rules(ROOT / "config").config["collapse"]["wither_negative_limit"])

    state = GameState(save_id=save_id, her_name=her_name, affection=-34.0, tier_floor=50)
    state.created_at = "2026-09-12T22:00:00"
    state.collapse_active = True
    state.ever_collapsed = True
    state.collapse_negatives = max(0, limit - 1)

    for role, text in SCRIPT["沉沦"]:
        append_message(state, role, text)
    state.turn = 2
    STORE.save(state)
    return state


def build(clean: bool = True) -> list[str]:
    if clean:
        for path in STORE.root.glob("*"):
            if path.is_file():
                path.unlink()

    made = [
        make("save2", 20.0, "小雪", tone_key="警戒"),
        make("save3", 70.0, "阿狸", tone_key="依赖"),
        make("save4", 84.0, "铃", tone_key="独属"),
        make("save5", 94.0, "铃", tone_key="认定"),
    ]
    make("save6", 12.0, "小雪", tone_key="沉沦", sealed=True)

    # 真正「刚开的一周目」：空档，用来演示开场白
    fresh = GameState(save_id="save7", her_name="猫娘")
    fresh.created_at = "2026-09-16T20:00:00"
    STORE.save(fresh)
    made.append(fresh)

    # 再挨一次就沉沦的那一档：终幕截图用
    made.append(make_doomed())

    return [s.save_id for s in made]


if __name__ == "__main__":
    ids = build(clean="--keep" not in sys.argv)
    print("造好了：" + "、".join(ids))
    for row in sorted(STORE.root.glob("*.json")):
        print(f"  {row.name}")
    for row in sorted(STORE.root.glob("*.sealed")):
        print(f"  {row.name}")
