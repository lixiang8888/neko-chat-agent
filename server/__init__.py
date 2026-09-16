"""桥接层。

把 ``src/`` 的 ``Session`` 包成 HTTP / SSE，供浏览器里的演出层使用。

**这一层没有任何游戏逻辑。** 好感怎么算、什么时候沉沦、记忆怎么压缩，
全在 ``src/``；这里只做「转发 + 扣尾 + 存档列表」。
看到 ``if affection > 80`` 出现在这个包里，就是逻辑漏出去了。

⚠️ ``src/`` 下任何文件都不许 import 这里的任何东西 ——
离线 407 项测试必须能在裸 Python 下跑起来，而 FastAPI 是可选依赖
（``pyproject.toml`` 的 ``[project.optional-dependencies] web``）。
"""
