# AGENTS.md — AzureSnowBot 开发指南

> 本文件供 AI 编码工具（Copilot / Claude / Cursor 等）在新会话中快速理解项目。
> 最后更新: 2026-03-31

---

## 项目概述

基于 **NapCat + NoneBot2** 的 QQ 智能 Agent Bot。Python 3.14，Conda 环境 `QQBot`（`D:\Anaconda\envs\QQBot`）。

核心能力：多轮对话、人格系统、MCP/本地工具调用、Skill 渐进式披露、定时提醒、心跳 + 主动发言、对话压缩（Compaction）、记忆管理。

## 技术栈

| 组件 | 说明 |
|------|------|
| NoneBot2 + OneBot V11 | Bot 框架，WebSocket 连接 NapCat |
| NapCat Shell | QQ 无头 Bot，在 `vendor/NapCat.Shell/` |
| LLM | Gemini / OpenAI / Qwen 三选一，统一走 OpenAI 兼容接口 |
| httpx | 所有 LLM 调用都手写 httpx，**没用 openai SDK** |
| pytest + pytest-asyncio | 测试框架，`asyncio_mode = auto` |

## 关键架构决策

### 插件加载方式
`pyproject.toml` 配置 `plugin_dirs = ["plugins"]`，NoneBot2 会递归扫描 `plugins/` 下所有包。每个子包的 `__init__.py` 负责导入自己的子模块（handler、commands 等）。

### LLM 调用
`plugins/llm.py` 是唯一的 LLM 配置中心，导出 `API_KEY, BASE_URL, MODEL`。所有调用方 `from ..llm import ...`，**不要**在其他模块硬编码 API key 或 base URL。

### 对话历史
- **Admin 私聊**: `data/admin/history.jsonl`，人格在 `data/admin/SOUL.md`，配置在 `data/admin/config.json`。
- **私聊仅限 Admin**：非 Admin 用户私聊会收到“请在群里跟我聊天哦~”提示。
- **群聊**: `data/sessions/groups/<gid>/<persona>.jsonl`，按人格隔离。配置在 `data/sessions/groups/<gid>/config.json`（含 `active_persona` + `last_message_at`）。
- **消息格式**: `{"role": "user", "content": "你好"}`，纯净的 role/content 格式，不嵌入时间戳。
- **时间上下文**: 每次组装 LLM 请求时，在 system prompt 末尾追加当前时间和上次对话时间（从 `config.json` 的 `last_message_at` 读取），如 `"\n当前时间: 2026-03-26 14:30:00（星期四），上次对话: 2026-03-26 12:00:00"`。这样 LLM 能感知时间但不会在回复中复述时间戳。
- **config.json**: 每次 `append_message()` 同时更新 `last_message_at` 字段，用于时间上下文和主动发言功能。
- 私聊由 `plugins/chat/handler.py` 管理 `load_history` / `append_message` / `trim_history`（仅 Admin）。
- 运行时上下文由 `plugins/runtime_context.py` 统一构建，私聊/群聊共用。
- 群聊由 `plugins/persona/manager.py` 管理 `load_history` / `append_message`，`plugins/group/utils.py` 提供 `trim_history`。
- **两套接口不同**，私聊是 `load_history(uid)`（仅 Admin），群聊是 `load_history(gid, persona_name)`。这是已知的架构分裂，未来如果统一需要做 adapter 层。

### Admin 上下文文件（私聊）
Admin 私聊每次请求动态从磁盘读取以下文件组装 system prompt（支持热更新）：

| 文件 | 用途 |
|------|------|
| `SOUL.md` | 人格灵魂（角色设定） |
| `AGENTS.md` | 操作手册 — 核心原则、记忆规则、工具使用指南 |
| `USER.md` | 用户档案 — Admin 的个人信息和偏好 |
| `MEMORY.md` | 长期记忆 — 跨会话事实/情感记录 |
| `HEARTBEAT.md` | 心跳任务 — 定时唤醒时的任务清单 |

加载函数 `load_admin_prompt()` 在 `plugins/chat/handler.py` 中，按上述顺序拼接各文件内容。
`HEARTBEAT.md` 不在此加载，而是在心跳触发时由 `proactive.py` 单独读取。
非 Admin 用户私聊会收到"请在群里跟我聊天哦~"提示。

### Admin 工具链
Admin 私聊拥有完整工具链（与群聊一致）：
- **Skill 工具**（渐进式加载） + **本地工具**（@register_tool） + **MCP 工具**（外部服务）
- 分发优先级：Skill → 本地 → MCP

### 数据目录结构
```
data/
├── admin/                     # Admin 私聊专用
│   ├── SOUL.md                #   人格灵魂（角色设定）
│   ├── AGENTS.md              #   操作手册
│   ├── USER.md                #   用户档案
│   ├── MEMORY.md              #   长期记忆
│   ├── config.json            #   {"last_message_at": "..."}
│   └── history.jsonl          #   对话历史
├── sessions/groups/<gid>/     # 群聊
│   ├── config.json            #   {"active_persona": "...", "last_message_at": "..."}
│   ├── <persona>.jsonl        #   对话历史（按人格隔离）
│   ├── _chatlog.jsonl         #   全量群聊记录
│   └── personas/              #   群私有人格
├── personas/                  # 通用人格 prompt
├── skills/                    # 技能目录
├── mcp_servers.json
└── reminders.json
```

### Agentic Loop（工具调用）
群聊和 Admin 私聊都有完整 Agentic Loop：
1. 发 LLM 请求（带 tools）
2. 如果 LLM 返回 `tool_calls` → 执行工具 → 把结果塞回 messages → 回到 1
3. 最多 10 轮（`MAX_TOOL_ROUNDS`）
4. 工具优先级：Skill 工具 → 本地工具 → MCP 工具

### 消息分条发送（Chunker）
`plugins/chunker.py` 提供两个发送函数：
- `send_chunked(bot, event, chunks)` — 需要 event 对象，用于普通回复
- `send_chunked_raw(bot, chat_type, target_id, text)` — 不需要 event，用于主动推送（提醒 / 主动发言）

## 目录结构速查

```
plugins/
├── llm.py              # LLM 配置中心
├── runtime_context.py  # 运行时上下文（时间、Runtime、渠道、工具摘要）
├── chunker.py          # 分条发送
├── ping.py             # 存活检测
├── __init__.py          # 空文件
├── chat/               # 私聊（仅 Admin）
│   ├── handler.py      #   对话处理 + Agentic Loop
│   ├── compaction.py   #   对话压缩 + 记忆提取
│   └── proactive.py    #   心跳 + 主动发言
├── group/              # 群聊
│   ├── handler.py      #   对话处理 + Agentic Loop
│   ├── chatlog.py      #   全量消息记录
│   ├── commands.py     #   /reset, /compact, /取名, /help
│   └── utils.py        #   白名单、工具函数
├── persona/            # 人格系统
│   ├── manager.py      #   人格 CRUD + 会话持久化
│   └── commands.py     #   /persona 指令
├── skill/              # Skill 系统
│   ├── manager.py      #   技能扫描、渐进式加载
│   └── commands.py     #   /skill 指令
├── local_tools/        # 本地工具
│   ├── manager.py      #   @register_tool 装饰器
│   └── tools.py        #   内置工具实现
├── reminder/           # 定时提醒
│   ├── __init__.py     #   启动时重载
│   └── scheduler.py    #   asyncio 调度 + JSON 持久化
└── mcp/                # MCP 工具
    └── manager.py      #   MCP 服务器连接 + 工具调用
├── memory/             # 记忆向量索引
│   └── indexer.py      #   Embedding + BM25 混合搜索 + MMR + 时间衰减
```

## 测试约定

### 运行
```bash
conda activate QQBot
python -m pytest tests/ -v
```

### NoneBot 隔离模式
测试无法直接 `import plugins.xxx`，因为会触发 NoneBot2 的 `get_driver()` 等调用链。所有测试文件使用 `importlib.util.spec_from_file_location()` 直接加载目标 `.py` 文件，并在 `sys.modules` 中预设 mock：

```python
# 标准 mock 模板
sys.modules.setdefault("nonebot", MagicMock())
sys.modules.setdefault("nonebot.log", MagicMock(logger=MagicMock()))
sys.modules.setdefault("nonebot.exception", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot.v11", MagicMock())
# ... 等等

# 构造父包（防止 __init__.py 触发 handler 导入）
_pkg = types.ModuleType("plugins.xxx")
_pkg.__path__ = [str(ROOT / "plugins" / "xxx")]
sys.modules["plugins.xxx"] = _pkg

# 加载目标模块
spec = importlib.util.spec_from_file_location("plugins.xxx.target", target_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
```

### 已知坑
- **中文引号**: Python 字符串中不能嵌套相同引号。`"总结小明的发言"` 在 `"..."` 内会 SyntaxError，改用 `'总结小明的发言'`。
- **`asyncio.Task.cancelled()`**: 调用 `task.cancel()` 后需要 `await asyncio.sleep(0)` 才能让 task 真正进入 cancelled 状态，否则 `task.cancelled()` 还是 False。
- **NoneBot `.env` 整数解析**: `.env` 中纯数字值（如 `ADMIN_NUMBER=373900859`）会被 NoneBot 解析为 `int` 而非 `str`。读取时必须 `str(getattr(config, "admin_number", ""))`，否则 `Path / int` 会 TypeError。
- **LLM 回复时间戳**: 即使 prompt 里说了不要加时间戳，LLM 仍可能在回复开头加 `[2026-03-25 16:55:46]`。`_base.txt` 里有提示约束，但不能完全保证。

## 待实现功能 & 重构路线

### 1. 群聊全量记录检索工具（优先级：高）

**状态**: 记录器 (`plugins/group/chatlog.py`) 已完成，工具尚未实现。

**实现方案**: 在 `plugins/local_tools/tools.py` 添加 `get_group_chat_log` 工具：
```python
@register_tool(
    name="get_group_chat_log",
    description="检索群聊的历史消息记录，可按发送者/关键词/时间筛选。",
    parameters={
        "type": "object",
        "properties": {
            "user_name": {"type": "string", "description": "发送者昵称（模糊匹配）"},
            "keyword":   {"type": "string", "description": "消息内容关键词"},
            "hours":     {"type": "number", "description": "查看最近 N 小时（默认 24）"},
            "limit":     {"type": "integer", "description": "最多返回条数（默认 50）"},
        },
    },
)
```
- 调用 `chatlog.load_chatlog(group_id, ...)` 获取数据
- `group_id` 从 `kwargs["_target_id"]` 获取（handler 自动传入的工具上下文）
- **注意**: `tools.py` 中不需要 `import time`，`load_chatlog()` 内部自己处理时间过滤

### 2. 电脑操控工具 & Skill（优先级：高，仅 Admin 私聊）

**目标**: Admin 私聊中 Bot 可以操控本地电脑（文件读写、命令执行、浏览器等）。

**实现方向**:
- 在 `plugins/local_tools/tools.py` 或新文件中添加系统操作工具（如 `run_command`, `read_file`, `write_file`）
- 安全约束：仅 Admin 私聊可触发（handler 已通过 `_tool_context["_chat_type"]` 传入上下文，工具内部可校验）
- 配套 Skill：在 `data/skills/` 中添加指导 LLM 如何安全操作电脑的 Skill
- `runtime_context.py` 已为私聊注入完整环境信息（OS、Shell、Workspace、Git），群聊不注入这些

### 3. 长期记忆 RAG（优先级：高）

**目标**: MEMORY.md 增长后，通过向量语义搜索按需检索记忆，而不是全量注入 system prompt。

**实现方向**:
- 添加 `memory_search` 工具（基于 Embedding 语义搜索）
- 添加 `memory_get` 工具（精确行读取）
- 当前 `read_file`/`write_file` 工具保留作为手动读写记忆的备选
- System prompt 中 MEMORY.md 改为摘要注入或完全去掉，改由工具检索
- 参考 OpenClaw 的 memory_search + memory_get 两步走架构

### 4. 图片理解 / 多模态（优先级：中）

**状态**: 群聊引用消息图片识别已完成（`fetch_quoted_image_urls` + 多模态 content）。私聊 + 直接发送图片尚未支持。

**适用**: 私聊 + 群聊均需要。

**实现方向**:
- 在消息事件中提取图片 URL / base64
- 构造 multimodal content 格式（OpenAI vision API 格式）
- Gemini 的 multimodal 接口略有不同，可能需要在 `llm.py` 加适配层
- `runtime_context.py` 的 Channel capabilities 已包含 "图片"

### 5. 各类 Skill 扩展（优先级：中）

**适用**: 私聊 + 群聊共享。

在 `data/skills/` 中持续添加新技能。Skill 系统已完善（三层渐进式披露），只需写 `SKILL.md` + 可选 `references/` 即可。

### 6. 主动发言扩展到群聊（优先级：低）

**状态**: Admin 私聊版已完成，合并为心跳机制 (`plugins/chat/proactive.py`)。
启动时自动开启心跳计时器，HEARTBEAT.md 文件驱动，带完整工具链。

**推荐重构**: 提升为 `plugins/proactive.py`（根级模块），抽象为引擎 + 回调模式：

```python
# plugins/proactive.py
_idle_tasks: dict[str, asyncio.Task] = {}

def reset_idle_timer(key: str, callback: Callable[[], Awaitable]) -> None: ...
def cancel_idle_timer(key: str) -> None: ...

async def try_proactive(
    *,
    history: list[dict],
    system_prompt: str,
    send_fn: Callable[[str], Awaitable],
    save_fn: Callable[[dict], None],
) -> None: ...
```

**群聊额外考虑**:
- 触发条件更复杂：应该是"Bot 参与过对话后一段时间无人 @Bot"，而非"群内任何消息后"
- 需要防骚扰：主动发言后无人理，不应再次触发循环
- 成本控制：对话太短（1-2 轮）时可跳过

### 7. 私聊 / 群聊对话历史接口统一（优先级：低）

两套历史管理接口是历史遗留。最理想的做法是抽出一个通用的 `SessionStore`：

```python
class SessionStore:
    def load(self, key: str) -> list[dict]: ...
    def append(self, key: str, msg: dict) -> None: ...
    def clear(self, key: str) -> None: ...
    def trim(self, messages: list[dict], budget: int) -> list[dict]: ...
```

- 私聊 key = `f"private:{uid}"`
- 群聊 key = `f"group:{gid}:{persona}"`

但工作量不小且涉及多个模块，等有明确需求再做。

### 8. 运行时上下文差异

`plugins/runtime_context.py` 按 `chat_type` 区分注入内容：

| 信息 | 私聊（Admin） | 群聊 |
|------|-------------|------|
| 当前时间 | ✓ | ✓ |
| 上次对话时间 | ✓ | ✓ |
| 模型名 | ✓ | ✓ |
| OS / 机器名 / Python / Shell | ✓ | ✗（群聊无电脑操控需求） |
| Workspace / Git root | ✓ | ✗ |
| 消息渠道 + 能力 | ✓ | ✓ |
| 可用工具摘要 | ✓ | ✓ |

## 代码风格约定

- 使用 `from nonebot.log import logger` 做日志，不用 `print`
- 工具函数签名统一 `async def tool_fn(param=default, **kwargs) -> str`，`kwargs` 中有 handler 注入的上下文（`_chat_type`, `_target_id`, `_user_id`, `_sender_name`）
- 环境变量在 `.env` 中定义，通过 `get_driver().config` 访问（NoneBot2 会自动加载 `.env`）
- 路径用 `pathlib.Path`，不用字符串拼接
- 对话历史格式：JSONL，每行 `{"role": "...", "content": "..."}`
- 时间上下文通过 system prompt 末尾动态注入，不存储在消息中

## 常用操作

```bash
# 启动 Bot
python main.py

# 运行测试
python -m pytest tests/ -v

# 单文件测试
python -m pytest tests/test_proactive.py -v

# 语法检查
python -c "import ast; ast.parse(open('plugins/chat/proactive.py', encoding='utf-8').read()); print('OK')"
```

## .gitignore 注意事项

当前未忽略的文件：
- `.pytest_cache/` — 应该加到 .gitignore
- `.vscode/` — 看团队约定
- `good_persona/` — 人格草稿/备份目录，含敏感 prompt 内容

已在 .gitignore 中的：
- `__pycache__/`, `*.pyc`, `.env`, `vendor/`, `reference/`, `data/sessions`, `data/admin`, `data/private`, `data/reminders.json`
