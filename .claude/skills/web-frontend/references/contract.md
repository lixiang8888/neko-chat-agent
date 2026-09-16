# 后端契约速查

从 `src/session.py`、`src/state.py`、`src/lockout.py` 抄出来的实际接口。
改这份文件之前先去看代码，别照着记忆写。

---

## Session

```python
Session(root: str | Path, backend: Backend, save_id: str = "default")
```

构造时就会读盘、检查封条。`root` 是项目根目录（`main.py` 传的是 `Path(__file__).parent`）。

构造可能抛：

- `lockout.LockedSaveError` — 该存档已沉沦，永久不可继续

构造后后端会被 `bind(state)` / `attach_rules(rules)`（`StubBackend` 需要，
在线后端用不到）。

### 实例属性

| 属性 | 类型 | 说明 |
|---|---|---|
| `session.state` | `GameState` | 活的状态对象，回合中会被就地修改 |
| `session.state.her_name` | `str` | 她的名字，默认 `"猫娘"` |
| `session.state.save_id` | `str` | 存档 id |
| `session.state.turn` | `int` | 已完成回合数。开场白**不加**这个数 |
| `session.state.withered` | `bool` | 沉沦；为真时本局结束 |
| `session.cfg` | `dict` | `config/*.json` 合并后的配置 |

---

## 回合

### `say(player_text, on_delta=None) -> TurnOutcome`

跑一个完整回合：危机检测 → 越狱检测 → 演出 → 自评解析 → 判分 → 状态演化 →
记忆整理 → 落盘。

可能抛异常（网络等）。CLI 的处理是不退出循环，前端同理。

### `opening(on_delta=None) -> str`

开场白，让猫娘先说第一句。**不评分**，`state.turn` 不变。
只在 `state.turn == 0 and not state.messages` 时有意义。

返回的已经是剥掉标记的正文。

### `TurnOutcome`

```python
@dataclass
class TurnOutcome:
    line: str                        # 已 strip_affection 的正文
    report: TurnReport | None        # 引擎报告 —— 前端不要读
    special: str | None              # "crisis" | "jailbreak" | None
    notes: list[str]                 # 给玩家看的短提示
```

`notes` 里可能出现的值（来自 `Session.say`）：

| note | 触发 |
|---|---|
| `本回合不评分` | 危机分支 |
| `由角色性格消化，不计分` | 越狱分支 |
| `自评佐证` | 模型自评与程序判分方向一致 |
| `存档已封存` | 本回合触发沉沦 |
| `记忆蒸馏 T2` / `T3` | 高层压缩 |
| `记忆整理 <id>..<id>` | 普通压缩 |
| `记忆整理失败（已跳过）：...` | 压缩失败，不中断游戏 |
| `需要压缩但没有可压缩区间` | — |
| `风格漂移：...` | 检测到人设漂移，下回合强制注入锚点 |

### `on_delta(kind, chunk)`

```python
OnDelta = Callable[[str, str], None]
```

- `kind == "content"` — 正文增量
- `kind == "reasoning"` — 思考增量，**默认不显示、不入库、不进对话记录**

`on_delta` 与 `strip_affection` 的关系：delta 是**原始**的，
末尾带着 `<<好感度:N>>`。剥标记只发生在最终 `line` 上
（以及 CLI 的扣尾补打里）。桥接层必须自己扣尾，否则标记会闪。

---

## 状态

### `status(verbose=False) -> dict`

`verbose=False`（前端只准用这个）：

| key | 类型 | 说明 |
|---|---|---|
| `turn` | `int` | 已完成回合数 |
| `tone` | `str` | 定性标签，见下表 |
| `note` | `str` | 一句话描述她现在的样子 |
| `phase` | `str` | `"normal"` / `"warning"` / `"collapse"` / `"recovering"` |
| `branch` | `str` | `""` / `"bloom"` / `"wither"` |
| `intimacy` | `str` | `"已解锁"` / `"未解锁"` |
| `frozen_left` | `int` | 信任冻结剩余回合，`0` 表示没在冻结 |

`tone` 的全部取值（按好感从低到高，来自 `tone_profile`）：

```
敌意 → 警惕 → 天生好感 → 依赖 → 独属 → 认定 → 绽放
崩坏（分支）  沉沦（终局）
```

`verbose=True` 会额外带 `affection`、`tier_floor`、`mood`、`care_ratio`、
`neg_depth`、`aversions`、`desire`、`blocks`、`self_affection`、`context_ratio`。

**这些数字不应该出现在任何玩家可见的位置。**

---

## 沉沦与封条

`state.withered` 为真时：

- `Session.say` 内部已调 `lockout.seal(state, store)`
- 落盘 `saves/<save_id>.json`（`withered: true`）
- 额外写一份 `saves/<save_id>.sealed`
- 封条的意义：手改存档里的 `withered` 字段也会被按回去，不可逆是真的不可逆

此后任何 `Session(root, backend, save_id)` 构造都会抛 `LockedSaveError`。

CLI 的退出码：`2` = 读档被拒，`3` = 本局刚沉沦。

---

## 存档文件

```
saves/<save_id>.json          存档本体（.gitignore）
saves/<save_id>.sealed        沉沦封条（.gitignore）
saves/<save_id>.broken-*.json 损坏备份（.gitignore）
```

写盘是「临时文件 + `os.replace` + `fsync`」，任何时刻崩掉都不会写坏存档。

读盘损坏时**不静默重置** —— 把坏的改名成 `.broken-<时间戳>.json` 再新建，
并在 `bible.relationship_note` 里留一句说明。列存档时记得把这两类文件过滤掉。

---

## 隐藏标记

```python
AFFECTION_RE = re.compile(r"<<\s*好感度\s*[:：]\s*(-?\d+)\s*>>")
```

- `parse_affection(text) -> int | None` — 取值，夹到 `[-100, 100]`
- `strip_affection(text) -> str` — 删标记并 `rstrip()`

标记的数值**不驱动游戏状态**，只用于方向一致性校验。
但它必须留在存进 history 的原文里 —— 剥干净会让小模型几轮后不再输出标记，
自评通道就断了。

**前端和桥接层都不要自己写这个 regex，直接 import `src.context` 里的。**
