"""一个**故意违规**的后端：它会吐出隐藏标记和思考过程。

``StubBackend`` 永远不会吐 ``<<好感度:N>>``，所以拿它验证「隐藏标记不上屏」
是验证不到的 —— 那只是在证明「没有标记的时候页面上没有标记」。

这个后端把两种情况都造出来：

* 正文末尾带 ``<<好感度:N>>``（含全角冒号、前后空格的变体）
* 中间夹 ``reasoning`` 分片

桥接层如果扣尾做对了、思考分流做对了，这两样都不会出现在 SSE 流里。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.backends import CONTENT, REASONING, StubBackend  # noqa: E402

# 这段话里含「好感度」三个字以外的正常内容，用来确认正文确实流过去了。
BODY = (
    "（她从窗台上跳下来，小跑两步，在你脚边刹住。）\n"
    "主人回来啦喵。今天的风好大，我把窗户关上了喵。\n"
    "……你外套上有一点冷。"
)
MARKER = "<< 好感度 ： 88 >>"      # 故意用全角冒号和空格
THOUGHT = "（我在想，要不要先示好。嗯，先示好。）"


class MarkerBackend(StubBackend):
    """演出一律用固定的句子，方便断言。"""

    def speak(self, messages, *, profile: str = "speak") -> str:
        return BODY + MARKER

    def speak_stream(
        self, messages, *, profile: str = "speak"
    ) -> Iterator[tuple[str, str]]:
        # 每片之间停一下，好让「流是真的」这条验收有可测的时间差。
        # 真模型本来就会停顿，这里只是把它变成确定的。
        delay = float(os.environ.get("NEKO_CHUNK_DELAY", "0.03"))

        # 思考先来 —— 它不该出现在任何玩家能看到的地方
        yield REASONING, THOUGHT
        time.sleep(delay)
        # 正文切成小块流出去，模拟真实的分片
        step = 7
        for i in range(0, len(BODY), step):
            yield REASONING, "（继续想。）"
            yield CONTENT, BODY[i : i + step]
            time.sleep(delay)
        # 标记分两片吐，模拟「跨 delta 被切断」这种最容易漏的情况
        yield CONTENT, MARKER[:6]
        yield CONTENT, MARKER[6:]


def install() -> None:
    """把桥接层的后端工厂换掉。必须在起服务之前调用。"""
    import server.app as bridge

    bridge._build_backend = lambda: MarkerBackend()  # type: ignore[assignment]
