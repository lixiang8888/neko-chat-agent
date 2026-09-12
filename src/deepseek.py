"""DeepSeek 传输层：SSE 流式、断线重试、代理绕过、思考分流。

这一层只关心「怎么把字节从 api.deepseek.com 拿回来」，不关心拿回来做什么。
演出、判分、摘要的语义都在 ``backends.py``，调用参数在 ``config/llm.json``。

这里集中了全部只能靠实测知道的坑，逐条都有注释。改这个文件之前先读注释。

为什么从 ``backends.py`` 拆出来
------------------------------
传输细节一旦塞进 ``Backend.speak()``，再加上流式、重试、思考分流，
那个方法会变成一团。传输和接口是两个不同的关注点：
传输关心「字节怎么回来」，接口关心「模型该演什么、该怎么判分」。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterator

# ``requests`` 是**可选依赖**：只有在线模式才需要。
# 离线模式（StubBackend + 全部测试）必须能在裸 Python 下跑起来 ——
# 「全部测试不需要 API Key、不联网」这个性质，靠的就是这里不硬依赖。
# 别改成模块级 import。
try:
    import requests

    _REQUEST_ERRORS: tuple[type[BaseException], ...] = (requests.RequestException,)
except ImportError:  # pragma: no cover - 取决于环境
    requests = None  # type: ignore[assignment]
    _REQUEST_ERRORS = ()

# 流式 delta 的两种类型。分开 yield，让下游能选择隐藏而不必污染正文。
CONTENT = "content"
REASONING = "reasoning"


class TransportError(RuntimeError):
    """传输层失败。已经是重试过之后的最终失败。"""


def _require_requests() -> None:
    if requests is None:
        raise TransportError(
            "在线模式需要 requests：pip install requests（离线模式不需要）"
        )


# --------------------------------------------------------------------------
# JSON 提取（模型输出常见 markdown 围栏）
# --------------------------------------------------------------------------


def extract_json(raw: str) -> dict[str, Any]:
    """从可能带 markdown 围栏或前后废话的输出里抠出 JSON。

    模型即使被要求「只输出 JSON」，也经常会裹一层 ```json。
    """
    text = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except ValueError:
        pass
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        return json.loads(brace.group(0))
    raise ValueError(f"无法从输出中解析 JSON：{raw[:200]}")


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


class DeepSeekClient:
    """一个极薄的 OpenAI 兼容客户端，专为 DeepSeek 调过。

    持有连接配置（base_url / key / proxy / timeout / retries），
    采样参数每次调用时传入 —— 这样同一个客户端能同时服务演出（高温）
    和判分（低温）。
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        model: str,
        proxy: str | None = None,
        timeout: tuple[int, int] = (10, 120),
        retries: int = 3,
        retry_backoff: float = 1.0,
    ) -> None:
        if not api_key:
            raise ValueError("缺少 API Key")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.proxy = proxy
        self.timeout = timeout
        self.retries = max(1, retries)
        self.retry_backoff = retry_backoff
        self.last_error: str | None = None

    # -- 内部 ------------------------------------------------------------

    @property
    def _proxies(self) -> dict[str, str | None]:
        """显式传 None 覆盖掉环境变量里的 http(s)_proxy。

        requests 对 ``proxies={"https": None}`` 的处理是「不使用代理」，
        这正是我们要的。不传这个参数则会走环境变量。
        """
        return {"http": self.proxy, "https": self.proxy}

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _body(
        self,
        messages: list[dict[str, str]],
        *,
        stream: bool,
        temperature: float,
        max_tokens: int,
        top_p: float | None,
        reasoning_effort: str | None,
    ) -> dict[str, Any]:
        """拼请求体。值为 None 的可选参数直接不传，让服务端用自己的默认值。"""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if top_p is not None:
            body["top_p"] = top_p

        # --- 思考：DeepSeek 扩展参数，非 OpenAI 标准 ---
        # 这个模型**默认就开着思考**，不传任何参数也会返回 reasoning_content。
        # "none" 必须显式传 thinking:{type:disabled} 才真的不思考。
        # None 则完全不传，用服务端默认行为。
        if reasoning_effort and reasoning_effort != "none":
            body["reasoning_effort"] = reasoning_effort
            body["thinking"] = {"type": "enabled"}
        elif reasoning_effort == "none":
            body["thinking"] = {"type": "disabled"}

        # 说明：n>1 和 response_format 在这个模型上会 400（已实测），所以不暴露。
        return body

    # -- 非流式 ----------------------------------------------------------

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 800,
        top_p: float | None = None,
        reasoning_effort: str | None = "none",
    ) -> str:
        """阻塞式调用。用于判分、摘要这类不需要边收边打的场景。"""
        body = self._body(
            messages,
            stream=False,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            reasoning_effort=reasoning_effort,
        )
        _require_requests()
        last_exc: Exception | None = None

        for attempt in range(1, self.retries + 1):
            try:
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=body,
                    timeout=self.timeout,
                    proxies=self._proxies,
                )
                if resp.status_code >= 400:
                    # HTTP 4xx/5xx 是确定性的（模型名写错、额度用尽……），重试没意义
                    raise TransportError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            except TransportError:
                raise
            except _REQUEST_ERRORS + (KeyError, ValueError) as exc:
                last_exc = exc
                self.last_error = str(exc)
                if attempt == self.retries:
                    break
                print(
                    f"\n[连接中断：{type(exc).__name__}，重试 {attempt}/{self.retries - 1}]",
                    file=sys.stderr,
                )
                time.sleep(self.retry_backoff * attempt)

        raise TransportError(f"重试 {self.retries} 次后仍失败：{last_exc}")

    # -- 流式 ------------------------------------------------------------

    def _stream_once(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        top_p: float | None,
        reasoning_effort: str | None,
    ) -> Iterator[tuple[str, str]]:
        """单次连接尝试，逐块 yield (类型, 文本)。"""
        _require_requests()
        body = self._body(
            messages,
            stream=True,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            reasoning_effort=reasoning_effort,
        )
        with requests.post(
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            json=body,
            timeout=self.timeout,
            stream=True,
            proxies=self._proxies,
        ) as resp:
            if resp.status_code >= 400:
                raise TransportError(f"HTTP {resp.status_code}: {resp.text[:300]}")

            for raw_line in resp.iter_lines():
                if not raw_line:
                    continue
                # 手动 decode：SSE 响应头没有 charset，iter_lines(decode_unicode=True)
                # 会按 ISO-8859-1 解，中文会乱码。
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                # 思考内容走独立的 reasoning_content 字段，和 content 并存。
                if delta.get("reasoning_content"):
                    yield REASONING, delta["reasoning_content"]
                if delta.get("content"):
                    yield CONTENT, delta["content"]

    def stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 1.3,
        max_tokens: int = 800,
        top_p: float | None = 0.95,
        reasoning_effort: str | None = "low",
    ) -> Iterator[tuple[str, str]]:
        """流式调用，逐段 yield (类型, 文本)。

        网络中断会自动重试，但**只在还没吐出任何文字时**才重试 —— 已经打印了
        一半再重连会把正文重复输出，那种情况直接抛出去。
        """
        last_exc: Exception | None = None

        for attempt in range(1, self.retries + 1):
            yielded = False
            try:
                for delta in self._stream_once(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    top_p=top_p,
                    reasoning_effort=reasoning_effort,
                ):
                    yielded = True
                    yield delta
                return
            except TransportError:
                # 确定性失败，不重试
                raise
            except _REQUEST_ERRORS as exc:
                last_exc = exc
                self.last_error = str(exc)
                if yielded or attempt == self.retries:
                    raise
                print(
                    f"\n[连接中断：{type(exc).__name__}，重试 {attempt}/{self.retries - 1}]",
                    file=sys.stderr,
                )
                time.sleep(self.retry_backoff * attempt)

        raise TransportError(f"重试 {self.retries} 次后仍失败：{last_exc}")


# --------------------------------------------------------------------------
# 从配置构造
# --------------------------------------------------------------------------


def load_llm_config(path: str) -> dict[str, Any]:
    """读 llm.json，剔除所有 ``_`` 开头的注释键。"""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def build_client(cfg: dict[str, Any], api_key: str, *, model: str | None = None) -> DeepSeekClient:
    """从 llm.json 的配置构造客户端。``model`` 可被命令行覆盖。"""
    return DeepSeekClient(
        api_key,
        base_url=cfg["base_url"],
        model=model or cfg["model"],
        proxy=cfg.get("proxy"),
        timeout=tuple(cfg.get("timeout", (10, 120))),
        retries=int(cfg.get("retries", 3)),
        retry_backoff=float(cfg.get("retry_backoff", 1.0)),
    )


# --------------------------------------------------------------------------
# 密钥加载
# --------------------------------------------------------------------------


def load_api_key(
    *,
    env_names: tuple[str, ...] = ("DEEPSEEK_API_KEY", "CATGIRL_API_KEY"),
    key_module_attr: str = "DEEPSEEK_API_KEY",
    keys_paths: tuple[str, ...] = (),
) -> str:
    """环境变量优先，其次按顺序找 keys.py（已 gitignore）。

    密钥**只**应该存在于环境变量或 keys.py 里。任何把 key 写进代码、
    配置或 README 的做法都会在提交时泄露 —— 这个工程的历史版本就这么干过。

    ``keys_paths`` 允许传多个候选位置，方便工程移目录时不至于找不到密钥。
    """
    import importlib.util
    import os

    for name in env_names:
        val = (os.environ.get(name) or "").strip()
        if val:
            return val

    for path in keys_paths:
        p = Path(path)
        if not p.exists():
            continue
        try:
            spec = importlib.util.spec_from_file_location("_neko_keys", str(p))
            if not (spec and spec.loader):
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            val = str(getattr(mod, key_module_attr, "") or "").strip()
            if val:
                return val
        except (OSError, AttributeError, SyntaxError, ImportError):
            continue
    return ""
