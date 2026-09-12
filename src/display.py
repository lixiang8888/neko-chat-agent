"""终端显示：流式打印、扣尾、思考分流。

**扣尾**解决一个具体问题：模型每回合末尾要输出隐藏标记 ``<<好感度:N>>``，
收到即打印的话它会闪在屏幕上。扣住最后 ``tail_keep`` 个字符不打印，
等整轮收完、剥掉标记，再把合法的尾部补打出来。

**思考分流**解决另一个：DeepSeek 的 ``reasoning_content`` 走独立字段，
不进正文、不进 history（塞回上下文既费 token，又会让模型纠结上一轮的分析）。
默认不显示；``show_thinking`` 打开时用灰色实时打出来。
"""

from __future__ import annotations

import sys
from typing import Any

from .backends import CONTENT, REASONING


class StreamPrinter:
    """边收边打。用法：

        p = StreamPrinter(show_thinking=True)
        for kind, text in backend.speak_stream(messages):
            p.push(kind, text)
        p.close(clean_text)          # 补打被扣住的尾部
    """

    def __init__(
        self,
        out: Any = None,
        *,
        show_thinking: bool = False,
        tail_keep: int = 24,
        thinking_color: str = "\033[90m",
    ) -> None:
        self.out = out if out is not None else sys.stdout
        self.show_thinking = show_thinking
        self.tail_keep = max(0, tail_keep)
        self.thinking_color = thinking_color

        self._raw: list[str] = []
        self._buf = ""
        self._printed = 0
        self._thinking_open = False

    # -- 供给 ------------------------------------------------------------

    def push(self, kind: str, text: str) -> None:
        if kind == REASONING:
            self._push_reasoning(text)
            return
        if kind != CONTENT:
            return

        if self._thinking_open:          # 正文开始，收起灰色
            self.out.write("\033[0m\n")
            self._thinking_open = False

        self._raw.append(text)
        self._buf += text
        if len(self._buf) > self.tail_keep:
            head, self._buf = self._buf[: -self.tail_keep], self._buf[-self.tail_keep :]
            self.out.write(head)
            self.out.flush()
            self._printed += len(head)

    def _push_reasoning(self, text: str) -> None:
        if not self.show_thinking:
            return
        if not self._thinking_open:
            self.out.write(f"\n{self.thinking_color}[思考] ")
            self._thinking_open = True
        self.out.write(text.replace("\n", "\n       "))
        self.out.flush()

    # -- 收尾 ------------------------------------------------------------

    @property
    def raw(self) -> str:
        """收到的完整原文（含隐藏标记）。"""
        return "".join(self._raw)

    def close(self, clean: str) -> None:
        """整轮收完。``clean`` 是剥掉隐藏标记之后的正文。"""
        if self._thinking_open:
            self.out.write("\033[0m\n")
            self._thinking_open = False
        # 补打被扣住的部分。标记落在扣尾区间内时，这里正好把它跳过。
        if self._printed < len(clean):
            self.out.write(clean[self._printed :])
        self.out.write("\n")
        self.out.flush()
