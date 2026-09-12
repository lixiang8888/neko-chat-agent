"""密钥泄露防线。

这个套件存在的理由是一段真实历史：这个工程的 key 曾经以明文躺在
一个**没有被 .gitignore 覆盖**的 readme.md 里。只要有人 `git add .`，
它就上去了。

所以这里做三件事：

1. **扫描全工程**，任何看起来像 API Key 的字面量都算失败 ——
   `keys.py` 除外，它是被指定的密钥文件，内容含 key 是正常的。
2. **检查 .gitignore 确实覆盖了密钥文件**。光有 keys.py 不够，它必须被忽略。
3. **检查源码和配置里没有硬编码密钥**。

跑这个套件不需要联网，也不需要密钥本身。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _fixtures import Runner  # noqa: E402

# ---------------------------------------------------------------------------
# 扫描规则
# ---------------------------------------------------------------------------

# 形如 sk-xxxxxxxxxxxxxxxx 的字面量。OpenAI 兼容端点的 key 都是这个形状。
# 故意写得宽松：宁可误报，不可漏报。
KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")

# 不扫的目录：依赖、缓存、存档、版本控制
SKIP_DIRS = {
    ".venv", "venv", "__pycache__", ".git", "saves",
    ".pytest_cache", ".uv", "node_modules", ".idea", ".vscode",
}
SKIP_SUFFIXES = {".pyc", ".pyo", ".lock", ".bak", ".sealed"}

# 被指定的密钥文件本身。它**应该**含 key —— 前提是 .gitignore 覆盖了它
# （由下面单独断言）。除此之外任何文件都不许有。
KEY_FILES = {"keys.py"}

# 本文件自己会包含 "sk-" 这个模式本身，跳过
SELF = Path(__file__).resolve()


def _iter_files(root: Path):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.resolve() == SELF:
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix in SKIP_SUFFIXES:
            continue
        if p.name in KEY_FILES:
            continue
        yield p


def _scan(root: Path) -> list[tuple[str, str]]:
    """返回 [(相对路径, 命中片段)]。片段只留前 8 个字符，不泄露完整 key。"""
    hits: list[tuple[str, str]] = []
    for p in _iter_files(root):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in KEY_PATTERN.finditer(text):
            found = m.group(0)
            hits.append((str(p.relative_to(root)), f"{found[:8]}…（{len(found)} 字符）"))
    return hits


def run() -> int:
    r = Runner()

    # ---------------------------------------------------------- 明文扫描
    r.section("工程内没有明文密钥")

    hits = _scan(ROOT)
    r.check("除 keys.py 外没有 sk- 字面量", not hits, f"命中={hits}")

    # keys.py 本身确实存在，且确实是被指定的那个文件
    keys_py = ROOT / "keys.py"
    r.check("keys.py 存在（密钥的存放处）", keys_py.exists())

    # ---------------------------------------------------------- gitignore
    r.section(".gitignore 覆盖密钥文件")

    gi_path = ROOT / ".gitignore"
    r.check(".gitignore 存在", gi_path.exists())
    if gi_path.exists():
        gi = gi_path.read_text(encoding="utf-8")
        for pattern in ("keys.py", "*.key", ".env", "saves/", "__pycache__/"):
            r.check(f".gitignore 覆盖 {pattern}", pattern in gi,
                    "pattern 不在 .gitignore 里")

    # ---------------------------------------------------------- 密钥来源
    r.section("密钥只来自环境变量或 keys.py")

    from src.deepseek import load_api_key

    saved = {k: os.environ.pop(k, None)
             for k in ("DEEPSEEK_API_KEY", "CATGIRL_API_KEY")}
    try:
        got = load_api_key(keys_paths=("/nonexistent/keys.py",))
        r.eq("找不到密钥时返回空串（不抛异常）", got, "")

        os.environ["DEEPSEEK_API_KEY"] = "sk-env-priority-test-000000"
        got = load_api_key(keys_paths=("/nonexistent/keys.py",))
        r.eq("环境变量优先", got, "sk-env-priority-test-000000")
    finally:
        # 原样还原：本来没有的就删掉，本来就有的就放回去
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ---------------------------------------------------------- 硬编码
    r.section("源码与配置里没有硬编码密钥")

    hardcoded = []
    for p in _iter_files(ROOT / "src"):
        if KEY_PATTERN.search(p.read_text(encoding="utf-8", errors="ignore")):
            hardcoded.append(p.name)
    r.check("src/ 里没有字面量密钥", not hardcoded, f"命中={hardcoded}")

    cfg_dir = ROOT / "config"
    cfg_hits = [p.name for p in cfg_dir.glob("*.json")
                if KEY_PATTERN.search(p.read_text(encoding="utf-8", errors="ignore"))]
    r.check("config/ 里没有字面量密钥", not cfg_hits, f"命中={cfg_hits}")

    llm_cfg = (cfg_dir / "llm.json").read_text(encoding="utf-8")
    r.check("config/llm.json 不含 api_key 字段",
            "api_key" not in llm_cfg and "API_KEY" not in llm_cfg)

    # .gitignore 之外，任何文档也不该示范真 key
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    r.check("README 里的 key 示范是占位符", not KEY_PATTERN.search(readme))

    return r.summary()


if __name__ == "__main__":
    raise SystemExit(run())
