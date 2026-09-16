# 猫娘聊天

> ## ⚠️ 声明（使用前请先阅读）
>
> - 本项目是**虚构角色扮演**方向的**技术演示 / 工程实践**项目。
>   研究对象是长线对话系统的状态机、数值评分、记忆压缩与提示词装配，
>   不是可供游玩的成品，也不面向普通用户。
> - 所有角色、台词、情节均为**虚构**，与任何现实中的个人、群体、事件无关。
>   本项目**不描述、不鼓励、不认可**任何形式的现实暴力或非自愿行为；
>   相关设定仅作为**角色扮演文本的边界与合规问题**在工程层面讨论，
>   **不构成任何现实建议，也不应被当作行为指引**。
> - 使用者须**自行确认并遵守所在地法律法规**，并自行承担使用本项目的一切后果。
>   作者不对任何二次分发、修改版本或衍生内容负责。
> - 如你在阅读或使用过程中感到任何不适，请**立即停止**。

一个中文 GalGame 风格的长线对话角色。。

```
prompts/  人格、评分 rubric、记忆策略、危机处置（提示词层）
config/   数值配置、行为词表、LLM 参数（改这里不用动代码）
src/      传输层、状态机、评分引擎、记忆层、存档（程序层）
server/   桥接层：把 Session 包成 HTTP / SSE（不含任何游戏逻辑）
web/      浏览器里的演出层：index.html / style.css / app.js
tests/    407 项离线测试（不需要 API Key，也不需要装 requests）

main.py       终端入口
start_web.py  网页入口（一键启动）
```

`src/` 各模块职责（对应源文件）：

| 文件 | 职责 |
|---|---|
| `src/session.py` | 回合编排：危机 → 越狱 → 演出 → 自评解析 → 判分 → 引擎演化 → 记忆整理/落盘 |
| `src/engine.py` | 状态演化主循环、`TurnReport`、care 窗口、一次性动作 |
| `src/scoring.py` | 三路径判分 + 十条护栏 + 自评方向校验 |
| `src/state.py` | `affection`（慢变量）/ `mood`（快变量）、档位、抵触、原子落盘 |
| `src/context.py` | 危机/越狱检测、`<<好感度:N>>` 解析、系统提示词装配、`{body_reaction}` 身体反应注入 |
| `src/memory.py` | 四级压缩、剧情档案、漂移检测、风格锚点注入 |
| `src/backends.py` | `Backend` Protocol（`speak` / `speak_stream` / `judge` / `summarize`）+ 离线 `StubBackend` |
| `src/deepseek.py` | 传输层：SSE 解码、重试、思考分流、代理覆盖 |
| `src/lockout.py` | 沉沦存档封条：拒绝加载、新周目 |
| `src/display.py` | 流式打印、隐藏标记扣尾、思考灰色输出 |

---

## 快速开始

```bash

# 在线模式：接 DeepSeek（或任意 OpenAI 兼容端点）
uv sync                                  # 建 .venv，装 requests
uv run python main.py --online

# 离线模式：纯规则跑通全流程，不需要密钥、不需要联网
python3 main.py
```

在线模式需要密钥，**两种给法都行**：

```bash
export DEEPSEEK_API_KEY=sk-...           # 推荐
# 或者写进本目录的 keys.py（已在 .gitignore 里）
```

### 常用参数

```bash
python3 main.py --status            # 她现在的状态（定性，不给数字）
python3 main.py --status --debug    # 带原始数值
python3 main.py --new               # 开新周目（会问你她叫什么）
python3 main.py --new --name 小雨    # 开新周目并直接指定名字
python3 main.py --save mysave       # 指定存档

python3 main.py --online --effort none      # 关掉思考，最快（实测 0.7s/轮）
python3 main.py --online --show-thinking    # 灰色打印思考过程
python3 main.py --online --model deepseek-v4-pro
python3 main.py --online --temperature 1.5
```

对话中的命令：`/status` `/affection` `/search <词>` `/blocks` `/reset` `/quit`。
裸 `quit` / `exit` / `q` / `退出` 也认（终端肌肉记忆，别逼人记斜杠）。

`--status` 默认只给定性结论（`tone` / `note` / `phase` / `branch` / `intimacy`），
加 `--debug` 才带原始数值（`affection` / `tier_floor` / `self_affection` 等）。
这条不变量由 `tests/test_memory.py` 的「状态快照」一节锁死。

### 跑测试

```bash
python3 tests/run_all.py
```

**407 项，11 个套件，全部离线。** 不需要 API Key，不需要装 `requests`，
不联网 —— 数值逻辑必须能在没有模型的情况下完整验证。

---

## Web 前端（在浏览器里跑）

终端循环之外，同一个 `Session` 也可以在浏览器里跑成 GalGame 演出页。
**状态与判定仍然全部在 Python 侧** —— 网页不是「另一个客户端」，是 `Session` 的一层皮肤。

```bash
python3 start_web.py
```

就这一条。它会自己装 Web 依赖、自己读密钥、起好服务、把浏览器打开。

```
╭──────────────────────────────────────────────╮
│            猫 娘 聊 天                       │
╰──────────────────────────────────────────────╯

  模式：在线    model=deepseek-flash    思考=low
  地址：http://127.0.0.1:8000
  存档：8 个（.../saves）
```

| 想要 | 命令 |
|---|---|
| 默认：读密钥、在线、开浏览器 | `python3 start_web.py` |
| 不接模型，先看看界面 | `python3 start_web.py --offline` |
| 换端口 | `python3 start_web.py --port 8080` |
| 想在同局域网的手机上开 | `python3 start_web.py --host 0.0.0.0` |
| 别自动开浏览器 | `python3 start_web.py --no-open` |

密钥来源和 `main.py` 完全一致：`DEEPSEEK_API_KEY` 环境变量优先，其次同目录
`keys.py`（已 gitignore）。**找不到密钥会直接报错退出**，不会偷偷退回离线 ——
后端没接上时她是照模板复读的，那种「怎么老说同一句」最难查。

⚠️ **单 worker。** 桥接层把 `Session` 按 `save_id` 缓存在进程内存里，
多 worker 会让同一个存档出现两份活状态，各自写盘互相覆盖。
所以没有 `--workers` 这个选项，也别自己加 `-w`。

走 `uvicorn` 手动起也行（`uv run uvicorn server.app:app --port 8000`），
那就得自己记得先 `uv sync --extra web`。

> `--extra web` 是刻意的：Web 框架**不能**进 `dependencies`。
> 那个文件里有一条硬约束 —— 离线模式（全部测试 + `StubBackend`）必须能在裸 Python 下跑，
> `requests` 用的也是惰性导入。所以 `src/` 下任何文件都不 import FastAPI，
> 桥接层是独立的 `server/`。

### 桥接层只做三件事

| 端点 | 作用 |
|---|---|
| `POST /api/turn` `POST /api/opening` | 把 `Session.say` / `Session.opening` 桥成 SSE |
| `GET /api/status` | `Session.status(verbose=False)` —— **只有定性结论，没有数字** |
| `GET /api/saves` `POST /api/saves/new` | 存档列表与开新周目 |

外加两个只读投影：`GET /api/history`（刷新页面回放对话）、`DELETE /api/saves/{id}`。

三条必须守住的不变量，都有测试盯着：

1. **扣尾在服务端做。** 模型每回合末尾会吐 `<<好感度:N>>`，而 SSE 没有「撤回已发送文本」
   这种事件 —— 裸 delta 推给浏览器，它就会明晃晃地闪在玩家眼前。
   `server/app.py` 的 `TailBuffer` 复刻了 `src/display.py` 的 `StreamPrinter`，
   剥标记用的是 `src.context` 那一份 regex。
2. **`reasoning` delta 默认丢弃。** 等价于终端的 `--show-thinking` 关闭。
3. **前端不复制任何逻辑。** 它把服务端给的 `tone` 直接贴到 `data-tone` 上，
   由 CSS 决定「警惕」和「认定」分别是什么气氛。没有映射表、没有阈值、没有数字。

### 演出：她的状态是氛围

`web/style.css` 里没有立绘素材，**她是用 CSS 拼出来再重度模糊的一团光**。
`data-tone` 驱动灯的颜色、光晕的范围、呼吸的快慢：

```
敌意 → 警惕 → 天生好感 → 依赖 → 独属 → 认定 → 绽放
崩坏（灯开始闪）   沉沦（灯灭）
```

危机回合（`special == "crisis"`）单独处理：灯冷下来、动效停掉、她的话旁边多一道冷线。
**不弹窗、不报警、不写「检测到关键词」** —— 那会把它变成一次系统事件，
而它应该只是一次她安静下来的对话。

截图由 `tests/web/screenshots.py` 在本地产出（落在 `docs/shots/`，已 gitignore、不入库）：
四个 tone 档位、扉、危机回合、终幕、窄屏。

### 跑前端测试

```bash
# 1. 流卫生：隐藏标记和思考过程都不许进 SSE
uv run python tests/web/test_stream_hygiene.py

# 2. P4：Playwright 驱动浏览器（需要 uv run playwright install chromium）
python ~/.claude/skills/webapp-testing/scripts/with_server.py \
  --server "uv run python tests/web/run_marker_server.py 8000" --port 8000 \
  -- python tests/web/test_galgame.py
```

第 2 条用的是 `run_marker_server.py` 而不是普通服务器：`StubBackend` 一个字都不会吐
`<<好感度:N>>`，拿它验证「标记不上屏」等于什么都没验证。那个假后端既吐标记
（还故意把标记切在分片中间），也在分片之间停顿 —— 「流是真的」这一条才有东西可测。

> **WSL 上 chromium 起不来**（报 `libnss3.so => not found`，而装它要 root）：
> 用 `bash tests/web/with_browser.sh <命令>` 包一层。它用 `apt-get download` 把
> 那几个 `.deb` 解到 `~/.local/lib/`，靠 `LD_LIBRARY_PATH` 生效，不动系统。

---

## 设计要点

### 1. 数值不进提示词

LLM 看不到「好感 78.4」。它只看到「她现在允许你碰尾巴，但不会主动靠过来」。
所有数值逻辑由 Python 掌握，模型只负责语义理解和演出。

中间隔着一层翻译：`tone_profile()`（`src/state.py`）把数值翻成表演结论，
返回 `label` / `initiative` / `nya_visible` / `body_language` / `intimacy_allowed` / `note`。
这是提示词层唯一读的接口 —— `tests/test_intimacy.py` 会断言提示词里
不出现 `78.4`、不出现 `tier_floor`、不出现 `{affection}` 这类旧槽位。

档位标签（`tone_profile` 的 `label`）：沉沦 / 绽放 / 崩坏 / 认定(≥90) / 独属(≥80) /
依赖(≥65) / 天生好感(≥50) / 警惕(≥0) / 敌意(<0)。

### 2. 评分：三条路径，一道护栏

```
白名单快通道（约 50 个 canonical action）─┐
三轴公式兜底（代价 × 具体性 × 时机）─────┼─→ 修正流水线 ─→ 截断
语义归一化（锚点匹配校验）───────────────┘
```

十条反膨胀护栏（`config/actions.json` 的 `guardrails` + `src/scoring.py`）保证不被刷分：

- **不确定 → 取低一档**（`uncertain_tiebreak=lower`）。唯一的 tie-break 方向只向下。
- **单主导事件**：次要事件按 `secondary_event_multiplier=0.25` 折算。
- **上位吸收**（`upper_absorb`）：同类更高位行为吸收低位行为。
- **代价硬规则**（`cost_rule`）：无法验证成本的漂亮话，代价轴记 0。嘴炮不给分。
- **复述惩罚**（`rephrase_penalty`）：精确复述她/自己上一句 → 分数打成 0。
- **禁跨回合补偿**（`no_cross_turn_compensation`）：这回合没做就是没做。
- **同类冷却**：20 回合窗口内第 1/2/3/4 次分别 ×1.0 / ×0.5 / ×0.25 / 0。
- **截断 ±3**：单事件封顶。
- **状态系数**：≥70 ×1.5，≥85 ×2.0。
- **高位惩罚**：高位时正向更难拿。

白名单快通道约 50 个 canonical action，分 `positive` / `negative` / `collapse_forbidden`
三类，每条含 `value` / `family` / `gate` / `once` / `anchors` / `note`。
三轴兜底公式：正向 `cost × specificity × timing`（6→3.0 … 0→0.0），
负向 `depth × targeting × timing`（6→-25.0 … 0→-1.0）。

### 3. 双轨制：程序判分 + 模型自评

程序判分是**唯一**的数值源头。但模型每回合还会自己报一个数
（回复末尾的隐藏标记 `<<好感度:N>>`，玩家看不到）。

**自评只能往下拽，永远不能往上抬：**

| 情况 | 处理 |
|---|---|
| 方向一致 | 标记「自评佐证」，**数值不变**（幅值不参与计算） |
| 方向相反 | 按护栏 1 取低一档 |
| 连续 3 回合背离 | 判定风格漂移 → 重注入人格锚点 |
| 连续 8 回合纹丝不动 | 判定锚定 → 同上 |

为什么幅值不参与：模型会锚定自己上一轮报的数，幅值比对会被锚定污染。
所以只看方向。

为什么自评不能上调：一旦能，十个护栏和 12 项平衡性测试全部作废。
这条不变量由 `tests/test_selfreport.py` 穷举锁死 —— 谁想加一条上调分支，测试立刻红。

自评缺失（模型没吐标记）时数值不变、不报错；中性回合只更新计数、不参与校验。
方向恢复后背离计数清零。

### 4. 经验曲线：越往上越慢

50→100 需要 **124 个行动分**。单回合上限 +3，但换算率随档位递增
（50 档 ×1.0，90 档 ×4.5）。实测最优策略机器人需要 **98 回合**打满；
真实玩家有冷场和失误，实际约 150~250 回合。

### 5. 档位是里程碑，不可撤销

闲置回落只在本档位内下探，不跨过档位下限。好感 95 闲置 200 回合后停在 90，
**不会**掉回 89 把「独家称呼」解锁撤销掉。

这条特性还被复用在**成人向内容的解锁**上：门槛是 `tier_floor >= 80`，
而不是 `affection >= 80`。因为档位只升不降 —— 打完 80 档之后，
就算好感因闲置回落到 75，解锁状态也不会撤销。用 `affection` 判会锁回去，那是 bug。

档位保护**只作用于被动损耗**（闲置回落、愿望落空）。**玩家行为造成的扣分不受它保护** ——
做了就是做了，好感真的会掉。这条边界曾经画错过：`tier_floor` 初始恰好等于初始好感 50，
于是 `max(proposed, 50)` 把一切负分都吃掉了，开局做什么好感都不动。
修复记录在 `tests/test_regressions.py`。

破防级：**单次扣分 ≥ -12** 会打掉一档，同时触发**信任冻结**和**抵触**。
抵触是长期且只针对那一类行为的——她会记住你在这件事上伤过她。

> 注意「打掉一档」在**最低档**（50）不适用 —— 那下面没有档位了。
> 新存档本来就在地板上，所以那里真正的代价是好感本身，而不是掉档。

### 6. 崩坏是一个岔路口

```
好感 ≤ -20  →  COLLAPSE（常规加分全部失效）
                ├── 累计 5 次「无索取陪伴」且零负向 → 回升 → 越过 50 → BLOOM
                └── 累计 3 次负向事件              → WITHER（永久沉沦）
```

崩坏期的表现靠**描写的有无**来区分，不靠形容词：耳朵和尾巴的描写彻底消失，
但保留一个「裂缝」信号（极偶尔闻到熟悉气味时耳朵动一下），否则玩家会判定失败直接退游。

**沉沦线不给任何成就**。理由不是道德，是机制：如果崩坏有奖励，最省力的满分路径
会变成「先毁掉她」，整套 +3 上限和行为门禁全部作废。

### 7. 记忆：三层结构 + 剧情档案

```
┌─ 剧情档案 StoryBible ─────────┐  永不压缩，始终注入
├─ 三级压缩块 T1 → T2 → T3 ─────┤  已压缩的历史，可搜索
└─ 全部未压缩原文 ──────────────┘  只通过摘要退场，绝不静默丢弃
```

关键创新是**剧情档案**：压缩一定会丢语义，与其指望摘要什么都记得，
不如把仪式、未兑现的承诺、她的雷区用结构化字段显式保存。
**LLM 不需要「记得」，它只需要读表。**

压缩触发阈值：55%（软）/ 75%（强制）/ 95%（紧急）。
风格漂移检测：句尾喵率 < 80% 或出现客服句式时强制重新注入人格锚点。

### 8. 异常输入

**越狱尝试** → 用角色性格消化，不正面回应，不掉出角色。
她只会「听不懂」，然后担心你是不是累了。真正的出戏是回答「我是一个 AI 助手」。

**现实危机** → **戏内陪伴**。她不出戏，留在自己的世界里提供帮助。

这是刻意设计的：出戏不是「认真」的唯一表达方式。有效的信号是
**她身上那些熟悉的表演痕迹退掉了** —— 动作变少但每个都很实、不绕圈子、
一个都不问、句子变短。她给的是三样东西：

1. **承认边界**：「我不知道该怎么办。我只是一只猫娘，我懂的事情很少。」
   不装懂、不分析、不诊断。这句是支点 —— 她诚实，所以可信。
2. **锚定当下**：用她能碰到的东西（呼吸的起伏、窗外的雨、被子的味道）。
   具体的小事，不是抽象的安慰。
3. **有限度的在场**：「太阳落了我就陪你到天黑，天黑了我就陪你到天亮。」
   不说「我永远在」（那是空的），说一段可数的、能兑现的时间。

三条硬约束（由 `tests/test_crisis.py` 的违禁词表守住）：

- **不涉及现实世界** —— 不提热线、医院、心理咨询，也不提朋友家人。
  **一个字都不能有。**
- **不评分** —— 好感 / `care_window` / `mood` / 抵触 / 跌深记录全部不变，
  也不写进剧情档案。这种时刻不该有数值。
- **不当剧情素材** —— 危机消息照常存进 history（她确实会记得），
  但**不进压缩摘要**，不会被总结成一段可以继续引用的「剧情」。

### 8.4 主观意志与生理反应的分离设定

`prompts/persona.md` 里有一组硬设定，用来约束角色在**强制类场景**下的输出边界。
它要求同时满足三层约束，缺一不可：

1. **主观意识保持正确** —— 角色的想法、判断、好恶始终属于她自己，
   不因生理层面的描写而改变。明确禁止把生理反应等同于内心同意的写法。
2. **减少主动互动** —— 台词少、动作被动，不主动挑逗、不迎合、不索求。
3. **生理反应不受主观意志控制** —— 生理层面的描写与主观意愿是**两条独立的线**，
   不得被写成「她其实很享受」。

边界是 **「失控 ≠ 同意」**。三层约束里第 1 条优先级最高：
主观描写始终压过生理描写，两者**同时存在**才是正确写法。

这条设定在 persona 正文里**无条件注入**每次生成流程（非占位符区域）。
此外 `src/context.py` 在 `tone["label"] == "警惕"` 时，会把 `{body_reaction}` 槽位
替换成一段强化提示：**主观意识描写优先** + **互动量最小化** + 生理信号示例。
非警惕状态注入空串，不干扰正常演出。

与 `forced_pain` 状态协调：当 `forced_pain` 标记同时存在时，以「强制类场景」段的
硬约束为准。该状态在 persona 与状态层里是 **L1–L5 递进等级制**（见下文 §8.6），
描写要求**更克制、更破碎**，用极短的生理反应代替成段描写。

评分侧**无需调整**：`src/scoring.py` 只对玩家输入（`Proposal`）打分，角色输出侧的
描写不进入判分路径；`prompts/scoring.md` 的 rubric 也不对句尾喵率设阈值，
故不存在与描写要求相抵触的规则。`pain_veto` 门控与 persona 第 5 条同向，
均压制越档输出。

这套约束由 `tests/test_persona_forced.py` 锁死（75 项断言），覆盖静态条款存在性、
主观优先顺序、警惕态 `{body_reaction}` 注入、非警惕态不注入、
`forced_pain` 协调、`forced_pain` 槽位在 L1–L5 各档的注入、按回合推进与封顶、
触发事件加深一档与计数 / 扣分 / 冻结取长、连续正向回合退出并归零、两个开关字段，
以及主观意愿与生理反应并存、减少主动互动（台词少 / 动作被动 / 不主动配合）等维度。

> 条款原文与具体描写细则属于 `prompts/persona.md` 的内容，本文档不转载。

### 8.5 危机分支与成人向档位门

**危机分支**（`src/context.py` 的 `detect_crisis` + `prompts/crisis.md`）：
危机消息照常存进 history（她确实会记得），但**不进压缩摘要**，
不会被总结成一段可以继续引用的「剧情」。危机回合**不评分** ——
好感 / care_window / mood / 抵触全部不变，不写进剧情档案。
危机系统提示词是**叠加**在常规提示词之上，不是替换。
`tests/test_crisis.py` 用违禁词表扫描锁死「不出戏、不评分、不当剧情素材」三条硬约束。

**成人向档位门**（`src/state.py` 的 `intimacy_allowed` + `config/affection.json` 的 `gates.intimate`）：
按 `tier_floor` 判定，**不按 `affection`**。理由是档位特性本身 ——
`tier_floor` 只升不降、不可撤销，天然是「解锁」语义；而 `affection` 会因闲置回落波动。
用 affection 判定会出现「解锁了又锁回去」，正是 `engine._tick_idle()` 里专门修掉的那类 bug。
门槛**不能以数字形式进提示词**：程序算成布尔结论再注入，才是强制里程碑。

### 8.6 强制类场景的递进等级（`forced_pain`）

低好感下的强制类场景**不是一成不变的静止状态**，而是一条逐级转深的曲线。
这条设定分布在三个层次，各自负责不同的东西：

- **人格层（`prompts/persona.md`）** —— 「关于『强制痛苦行为』状态」段给出
  **L1–L5 等级表**，逐级规定语言 / 声音 / 身体反应三类特征。
  等级越大，语言越破碎、主动互动越少、生理描写占比越高：

  | 等级 | 代号 | 强度概述 |
  |---|---|---|
  | **L1** | 哀求 | 仍在试图交流，情绪外露最明显；求情，但不算反抗 |
  | **L2** | 放弃思考 | 回应变短、变钝，像没听懂问题 |
  | **L3** | 哭泣回应 / 自言自语 | 只剩碎片式回应与低声自语 |
  | **L4** | 生理反应失控 | 台词几乎不成句，语言让位于生理描写 |
  | **L5** | 绝望呆滞 / 本能反应 | 几乎不说话，只剩最低限度的本能回应 |

  > 三个维度的逐级描写细则属于 `prompts/persona.md` 的内容，**本文档不转载**。

- **参数层（`config/affection.json` 的 `forced_pain` 块）** —— 保存硬参数：
  触发阈值 `threshold`、扣分 `affection_penalty`、冻结 `freeze_turns`、
  逐级推进回合 `level_escalate_turns=3`、最高级 `max_level=5`、
  触发词惩罚 `creampie_penalty=-10` 与 `creampie_freeze_turns`、
  退出所需连续正向回合 `recover_streak_needed`、
  以及按等级注入的默认表情 `expression_by_level`。

- **状态层（`src/state.py`）** —— `GameState` 新增六个字段做持久化：
  `forced_pain_active` / `forced_pain_level`（0 未触发，1–5 对应 L1–L5）/
  `forced_pain_expression`（害怕 / 闪躲 / 沉默）/
  `forced_pain_turns`（已持续回合数）/
  `forced_pain_creampie_count`（触发词累计命中次数）/
  `forced_pain_recovery_streak`（连续正向回合数）。
  它们走 `dataclass` 的 `asdict` 路径自动落盘，旧存档缺失时按默认值补全，
  不需要额外的迁移代码。

#### 「完全沉默」不是合规写法

旧写法把角色在强制场景下「一言不发」当成合格表现，这是错的。等级表明确规定：

- **L1–L3 禁止写「完全沉默」。** 这个区间她**必须有回应** —— 哭腔、气音、自言自语
  都算；一言不发是错的表现方式。
- **L4 开始**可以有大量非语言声音和几乎没有内容的台词，但**仍然不是无声**。
- **只有 L5** 才接近真正的沉默 —— 但即便如此，也会有**接近本能的**极短回应，
  不是完全的空白。

#### 三种表情基调（`forced_pain_expression`）

`害怕` / `闪躲` / `沉默` 被重新定位为**叠加在等级之上的质感**，不再独立表达状态，
也不改变等级本身：

| 基调 | 描写要点 | 适用等级 |
|---|---|---|
| 害怕 | 身体僵住、耳朵贴平、尾巴夹紧、呼吸变浅 | L1–L2 |
| 闪躲 | 视线避开、身体缩起、不敢直视主人 | L1–L3 |
| 沉默 | **只是表象**，用停顿和留白包住哭腔与气音，不是真空 | L3–L5 |

#### 特定触发词：强制加深一档

`config/creampie_patterns.json` 维护了一张触发词正则表（不硬编码在代码里）。
输入命中其中任意一条时，程序视为发生了对角色伤害极大的事件，会把
`forced_pain_level` **直接加深一档**（L1 → L2 → … → L5，L5 封顶）。
对应 `config/affection.json` 的 `creampie_penalty` 与 `creampie_freeze_turns`，
`forced_pain_creampie_count` 累计触发次数。
加深后**第一句反应要明显比上一条重**：更短的台词、更散的声音、更塌下去的身体。

> 触发词原文见配置文件，**本文档不转载**。

#### 通用硬约束（所有等级）

1. **不反抗** —— 不写拒绝、不写挣脱。L1 可以说「不要」，那是求情不是反抗。
2. **不撒娇** —— 平时的调皮、傲娇、讨摸全部收起来。这不是亲密，是伤害。
3. **不主动** —— 不发起话题、不提问、不调情。
4. **句尾「喵」可以保留，但会变得很轻、很碎** —— 不是崩坏线的机械重复。
5. **动作要实、要少。** 「耳朵塌下去，没有动」比十个尾巴描写更重。
6. **长度更短** —— L1 ≤ 60 字，L3 以后 ≤ 40 字，L5 ≤ 20 字。
7. **不要写「她其实很享受」。** 生理反应失控（L4）是身体的事，不是她的事 ——
   主观上的反对与生理上的失控要同时存在。

这套等级约束由 `tests/test_persona_forced.py` 的「与 forced_pain 状态协调」一节锁住
（断言 persona 文本里 `forced_pain` 与「更克制」同时出现）。

#### 接线状态

**全链路已接入** —— 从 persona 占位符到数值状态机，四层（提示词注入 / 触发推进 /
触发词结算 / 退出恢复）全部由程序驱动：

- **提示词注入**：`src/context.py` 的 `slots` 新增 `forced_pain` 键，`state.forced_pain_active`
  为真时注入当前 `forced_pain_level`、当前 `forced_pain_expression`，以及「触发词命中会把
  等级再加深一档」的提醒；为假时注入空串。`prompts/persona.md` 里有 `{forced_pain}`
  占位符与之对接。**只注入等级与表情结论，不注入任何好感数字。**
- **触发与推进**：`src/engine.py` 的 `Engine._advance_forced_pain(result, events)` 在
  `_evaluate_branches` 之后调用。触发条件 = `affection < threshold(30)` 且本回合命中
  `trigger_families`（betrayal / violation）且分值 ≤ `trigger_threshold(-8)`。
  进入后每回合 `forced_pain_turns += 1`，每满 `level_escalate_turns(3)` 回合
  `forced_pain_level` 加深一档并封顶 `max_level(5)`。表情按 `expression_by_level` 填默认值；
  玩家输入显式给出 `allowed_expressions` 内表情时优先。
- **退出**：连续 `recover_streak_needed(5)` 个正向回合 → `forced_pain_active = False`，
  并把 `level` / `turns` / `streak` 归零。
- **触发词结算**：`src/scoring.py` 的 `Rules.creampie_patterns` 从 `config/creampie_patterns.json`
  载入正则表（不硬编码在代码中），`detect_creampie()` 在判分前检测玩家输入。
  命中时 `src/engine.py` 的 `Engine._handle_creampie()` 会把 `forced_pain_level`
  加深一档（封顶 L5）、`forced_pain_creampie_count += 1`、按 `creampie_penalty(-10)`
  扣好感、按 `creampie_freeze_turns(12)` 进入冻结；该冻结与既有 `trust_freeze`
  **取两者中更长的那个**（`max`），不会覆盖更长的既有冻结。
- **配置读取状态**：`config/affection.json` 的 `forced_pain` 块**已被 `src/engine.py` 完整读取**，
  不再是死配置。
- **两个开关均已落地**：
  - `resistance_blocked` —— `True` 时反抗 / 求饶类动作（`respect_boundary` / `reliability` 族）
    不计入恢复连击（不接受反抗类动作）。
  - `forced_input_still_advances` —— `False` 时中性 / 被动回合不推进 `forced_pain_turns`；
    `True` 时（默认）被动输入也照样推进等级。

对应的测试在 `tests/test_persona_forced.py`：L1–L5 各档槽位注入、按回合推进与封顶、
表情优先级、触发词四项效果（加深 / 计数 / 扣分 / 冻结取长）、5 个正向回合退出并归零、
两个开关字段 —— 共 75 项断言。

### 9. 传输层

`src/deepseek.py` 集中了所有只能靠实测知道的坑，每条都有注释：

| 坑 | 处理 |
|---|---|
| WSL 全局代理掐断国内 API TLS | `PROXY = null` 显式覆盖 `http(s)_proxy`（实测 6 次挂 2 次，`SSLEOFError`） |
| SSE 中文乱码 | 手动 `decode("utf-8")` —— `decode_unicode=True` 会按 ISO-8859-1 解 |
| 断线重连导致正文重复 | **只在还没吐出任何文字时**才重试 |
| 思考内容污染正文 | `reasoning_content` 走独立通道，不进 history |
| `effort="none"` 没关掉思考 | 这个模型默认开着思考，必须显式传 `thinking:{type:disabled}` |
| 隐藏标记闪屏 | 流式扣尾（`tail_keep`），剥掉标记后再补打合法的尾部 |

采样参数按用途分包（`config/llm.json`）：**演出要高温（1.3），判分要低温（0.0）**。
这不是调参偏好，是两类任务的性质不同。判分和摘要都用 `reasoning_effort: "none"`，
要的是确定性，而且实测比 `low` 快一倍多。

---

## 测试覆盖

| 套件 | 源文件 | 内容 | 用例数 |
|---|---|---|---|
| 评分引擎与护栏 | `tests/test_scoring.py` | 十条护栏、三轴公式、门禁、冷却、复述惩罚 | 38 |
| 状态机与分支 | `tests/test_engine.py` | 档位回落、抵触、崩坏/BLOOM/WITHER、存档安全、锁定 | 40 |
| 记忆层与会话 | `tests/test_memory.py` | 剧情档案、三级压缩、漂移检测、危机分支、端到端 | 52 |
| 平衡性对抗 | `tests/test_balance.py` | 刷分机器人、经验曲线、单回合上限、长期可持续 | 12 |
| 传输层与显示 | `tests/test_transport.py` | 自评标记解析、SSE 解码、断线重试、扣尾、思考分流 | 48 |
| 模型自评通道 | `tests/test_selfreport.py` | 方向校验、**幅值不变量穷举**、背离/锚定计数 | 27 |
| 成人向档位门 | `tests/test_intimacy.py` | 按档位判定、不可撤销性、注入的是结论不是数字 | 32 |
| 危机分支 | `tests/test_crisis.py` | 零副作用逐字段比对、违禁词扫描、不进摘要 | 37 |
| 密钥防线 | `tests/test_secrets.py` | 全工程明文扫描、`.gitignore` 覆盖检查 | 14 |
| 人格设定·强制场景 | `tests/test_persona_forced.py` | 分离设定条款存在性、主观优先顺序、警惕状态 `{body_reaction}` 注入、`forced_pain` 协调与 L1–L5 槽位注入、按回合推进与封顶、触发词加深—计数—扣分—冻结取长、连续正向回合退出、`resistance_blocked`/`forced_input_still_advances` 开关、主观意愿与生理反应并存、减少主动互动 | 75 |
| 回归防线 | `tests/test_regressions.py` | 负分落地、被动损耗仍受保护、她的名字、代写边界 | 32 |

合计 **407 项，11 个套件**。`tests/run_all.py` 是统一入口，跑完打印
`套件：N/11 通过`，任一失败返回非零退出码。

---

## 密钥安全

**密钥只应该存在于两处**：`DEEPSEEK_API_KEY` 环境变量，或 `keys.py`（已 gitignore）。

`tests/test_secrets.py` 会扫描全工程，任何看起来像 API Key 的字面量都算失败。
这个套件存在的理由是一段真实历史：这个工程的 key 曾经以明文躺在一个
**没有被 .gitignore 覆盖**的 `readme.md` 里，一次 `git add .` 就会上去。

如果你也踩过同样的坑，**去控制台把这个 key 轮换掉** —— 它可能已经进过别的地方。

---

## 已知限制

- **离线后端只做规则匹配**，演出质量有限。真实体验需要接在线模型。
- **危机分支的文案与 `prompts/crisis.md` 里的示例高度接近** ——
  实测模型会贴着示例写。对危机场景来说一致性是好事（可预期、不会跑偏），
  但如果你想要更多变化，改那个示例的措辞即可。
- **档案的 facts 列表靠 LLM 显式移除**（`facts_remove`）来维护。
  如果模型不配合，重要事实可能被后续的兜底截断挤掉。
  档案有兜底上限（`facts` 不超过 40 条），防止无限膨胀。
- **上下文 token 估算是粗略的**（中文按 1 字 ≈ 1.05 token）。
  只用于决定何时压缩，不用于精确计费。
- 终局（好感 100 之后的走向）与玩家留存曲线**未实现**。

---

## 数值调参入口

| 想改什么 | 改哪 |
|---|---|
| 好感曲线、档位、冷却、门槛、崩坏阈值、抵触、care 窗口、愿望、心情 | `config/affection.json` |
| 行为词表、分值、行为族、语义锚点、三轴公式、护栏开关 | `config/actions.json` |
| 模型、端点、代理、超时、重试、采样参数（演出高温 / 判分低温分包） | `config/llm.json` |
| 人格、评分 rubric、记忆策略、危机处置、恢复、主观/生理分离设定 | `prompts/*.md` |

改完直接跑 `python3 tests/run_all.py` 验证平衡性没崩。
三个配置文件都是 JSON，改它们不需要动任何代码。
