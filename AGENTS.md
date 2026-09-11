# AGENTS.md — AzureSnowBot 开发指南

> 本文件供 AI 编码工具（Copilot / Claude / Cursor 等）在新会话中快速理解项目。
> 最后更新: 2026-04-07

---

## 项目概述

基于 **NapCat + NoneBot2** 的 QQ 智能 Agent Bot。Python 3.14，Conda 环境 `QQBot`（`D:\Anaconda\envs\QQBot`）。

核心能力：多轮对话、人格系统、MCP/本地工具调用、Skill 渐进式披露、定时提醒、心跳 + 主动发言、对话压缩（Compaction）、记忆管理、Web Dashboard。

## 技术栈

| 组件 | 说明 |
|------|------|
| NoneBot2 + OneBot V11 | Bot 框架，WebSocket 连接 NapCat |
| NapCat Shell | QQ 无头 Bot，在 `vendor/NapCat.Shell/` |
| LLM | Gemini / OpenAI / Qwen 三选一，统一走 OpenAI 兼容接口 |
| httpx | 所有 LLM 调用都手写 httpx，**没用 openai SDK** |
| FastAPI | Web Dashboard 后端 API，挂载到 NoneBot2 的 ASGI server |
| Vue 3 + Vite + Element Plus | Web Dashboard 前端 SPA |
| PyJWT + bcrypt | Dashboard JWT 认证 |
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
- **群聊消息渲染**: 群聊用户消息带 `[昵称]:` 前缀（昵称用 QQ 昵称而非群名片）；@ 段由 `group/utils.py` 的 `extract_text` 渲染为 `@昵称(qq号)`（@Bot 简写为 `@Bot`，查不到昵称退化为 `@qq号`）；引用消息带作者 `(引用 作者 的消息: "...")`。私聊消息不带前缀。
- **时间上下文**: 动态时间行（当前时间 + 上次对话时间，`last_message_at` 取自 `config.json`）由 `runtime_context.build_time_context()` 生成，**作为独立的 system 消息追加在 messages 数组最末尾**（私聊/群聊主对话）。绝不能拼进 system prompt——LLM 的 prefix cache 按最长公共前缀匹配，秒级时间每轮都变，放 system 里会让整段对话历史每轮缓存全部 miss（2026-09 修复的性能问题）。心跳/塔罗/取名等一次性请求仍拼在 system 里，无影响。这样 LLM 能感知时间但不会在回复中复述时间戳。
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
├── sessions/                  # 群聊会话数据
│   ├── chatlog.db             # 全量聊天记录 SQLite（chatlog_db.py，365 天保留）
│   └── groups/<gid>/
│       ├── config.json        #   {"active_persona": "...", "last_message_at": "..."}
│       ├── <persona>.jsonl    #   对话历史（按人格隔离）
│       ├── _chatlog.jsonl     #   （已废弃）迁移前旧记录，确认后可删
│       └── personas/          #   群私有人格
├── personas/                  # 通用人格 prompt
├── skills/                    # 技能目录
├── mcp_servers.json
└── reminders.json
```

### Web Dashboard

FastAPI 子应用挂载到 NoneBot2 的 ASGI server（共享进程，直接调用现有插件函数读数据）：

- 后端 REST API：`/api/v1/*`，JWT 认证保护
- 前端 Vue SPA：生产模式 `/dashboard/`，开发模式 `localhost:5173`
- 挂载时机：`driver.on_startup` → `driver.server_app.mount("/api/v1", dashboard_app)`

**Dashboard 页面：**
| 页面 | API 前缀 | 說明 |
|------|----------|------|
| 总览 | `/overview` | Bot 状态、今日 Token、活跃群数、最近工具调用 |
| Token 用量 | `/tokens` | 每日趋势图、来源分布、费用估算 |
| 对话浏览器 | `/conversations` | Admin 私聊 + 群聊历史分页浏览 |
| 记忆管理 | `/memory` | 编辑 MEMORY.md（Admin + 各群）、结构化记忆浏览；语义搜索与索引状态需配置 embedding 模型 |
| 人格管理 | `/personas` | 查看/创建/编辑/删除人格（通用 + 群私有） |
| 提醒管理 | `/reminders` | 查看/取消提醒 |
| 技能管理 | `/skills` | 查看/创建/编辑/删除技能 |
| 配置编辑 | `/config` | 编辑 .env 和 Admin 上下文文件 |

**认证配置（.env）：**
```env
DASHBOARD_USER=admin
DASHBOARD_PASSWORD_HASH=       # bcrypt 哈希，留空则默认密码 admin
DASHBOARD_SECRET_KEY=           # JWT 密钥，留空则每次重启随机生成
```

**前端项目位置**：`web/`（Vue 3 + Vite + Element Plus + ECharts），构建产物 `web/dist/`。

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
├── token_stats.py      # Token 用量统计 + 费用估算
├── tool_log.py         # 工具调用日志（JSONL 持久化）
├── chunker.py          # 分条发送
├── ping.py             # 存活检测
├── __init__.py          # 空文件
├── chat/               # 私聊（仅 Admin）
│   ├── handler.py      #   对话处理 + Agentic Loop
│   └── compaction.py   #   对话压缩 + 记忆提取
├── proactive.py        # 心跳 + 主动发言引擎（私聊 & 群聊，keyed 计时器）
├── group/              # 群聊
│   ├── handler.py      #   对话处理 + Agentic Loop
│   ├── chatlog.py      #   全量消息旁路记录器（nonebot 层，写入 SQLite）
│   ├── chatlog_db.py   #   聊天记录 SQLite 存储层（无 nonebot 依赖，可独立加载）
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
├── memory/             # 记忆（向量索引 + 结构化蒸馏）
│   ├── indexer.py      #   Embedding + BM25 混合搜索 + MMR + 时间衰减（当前未启用，见下）
│   └── structured.py   #   小模型蒸馏结构化记忆 → memories.jsonl
├── dashboard/          # Web Dashboard
│   ├── __init__.py     #   NoneBot 启动钩子，挂载 FastAPI 子应用
│   ├── app.py          #   FastAPI 实例 + CORS + Rate Limiting
│   ├── auth.py         #   JWT 认证（PyJWT + bcrypt）
│   ├── config.py       #   Dashboard 配置
│   └── routes/         #   API 路由模块
│       ├── auth.py     #     登录/刷新
│       ├── overview.py #     总览聚合
│       ├── tokens.py   #     Token 用量统计
│       ├── conversations.py  # 对话历史
│       ├── memory.py   #     记忆管理
│       ├── personas.py #     人格 CRUD
│       ├── reminders.py #    提醒管理
│       ├── skills.py   #     技能管理
│       └── config_routes.py  # 配置编辑
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

### 1. 群聊全量记录检索（已完成）

**状态**: 记录器、检索工具、SQLite 落库均已完成。

- **记录器**: `plugins/group/chatlog.py`（nonebot 旁路，白名单群所有消息）
- **存储**: `plugins/group/chatlog_db.py` → `data/sessions/chatlog.db`（SQLite，
  `(group_id, ts)` 索引；`UNIQUE(group_id, ts, uid, text)` 保证迁移幂等）。
  保留期 `RETENTION_DAYS = 365`，bot 启动时自动清理（`@driver.on_startup`）
- **检索**: `get_group_chat_log` 工具（`tools.py`，按发送者/QQ号/关键词/时间窗）；
  chatter、`/查记录` 指令、Dashboard 共用 `chatlog_db.load_chatlog()`，
  返回 `{ts, uid, name, text}` 字典列表，按时间正序取最新 limit 条
- **旧 JSONL 迁移**: `python scripts/migrate_chatlog_to_db.py`（幂等，可重复跑；
  原 `_chatlog.jsonl` 保留不动，确认后手动删除）
- **注意**: `chatlog_db.py` 不依赖 nonebot（迁移脚本直接按文件路径加载）；
  `tools.py` 中不需要 `import time`，`load_chatlog()` 内部自己处理时间过滤

### 2. 电脑操控工具（已完成，仅 Admin 私聊）

**已实现**:
- `run_command` — 执行本地 shell 命令（PowerShell/sh），超时 30s，输出截断 4000 字符
- `read_file` / `write_file` / `list_files` — 文件系统操作。私聊（Admin）限 `data/admin/`、`data/skills/`、`data/personas/`；群聊仅限本群 `data/groups/<群号>/` 目录（传相对路径如 `MEMORY.md` 自动定位；`..`/绝对路径/`data/` 开头越界路径一律拒绝；群聊单次写入上限 20000 字符）
- 安全约束：`run_command` 等 `admin_only=True` 的工具群聊 LLM 不可见；文件工具群聊可见但被路径 jail 限制在本群目录内
- `runtime_context.py` 为私聊注入完整环境信息（OS、Shell、Workspace、Git）

### 3. 长期记忆 RAG（代码在，当前未启用）

**架构**（代码保留，随时可启用）:
- `memory_search` 工具：Embedding + BM25 混合搜索 + MMR 去重 + 时间衰减
- 向量索引存储为 `.memory_index.json`，同时索引 MEMORY.md 和 history.jsonl
- Compaction 后自动刷新索引（`compaction.py` Step 5）
- 启动时预热索引（`chat/__init__.py`）

**当前状态：整条链路实际不生效，且这是设计上可接受的**

- `embed_texts()` 走 `{BASE_URL}/embeddings`，而 `.env` 配的是 `LLM_PROVIDER=deepseek`，
  deepseek 不提供该端点 → 调用失败，被 `ensure_index()` 静默吞掉（返回旧索引）。
  `token_stats.json` 里 `embedding` 来源从未出现过，可作为佐证。
- 未配置 embedding 模型时行为是安全的：`_load_index()` 返回空 → `search()` 的
  `if not chunks: return []`。**不会返回过期内容。**
- **记忆实际靠注入而非检索**：私聊 `MEMORY.md` 全文进 system prompt
  （`chat/handler.py` 的 `_ADMIN_CONTEXT_FILES`，无截断）；群聊注入前 3000 字。
  近期历史在 messages 里，远期历史已被 compaction 压成摘要。**所以不检索也不缺记忆。**
- 因此 `data/admin/AGENTS.md` 已改为「直接查看 prompt 里的 MEMORY.md，不调用检索工具」，
  `memory_search` 处于自然废弃状态（代码未删）。

**要启用时**：配一个支持 `/embeddings` 的 provider（如 gemini），
在 `indexer.py` 的 `_EMBEDDING_MODELS` 里补上对应模型，再把 `data/admin/AGENTS.md`
的检索指令改回来即可。

### 4. 图片理解 / 多模态（部分完成）

**已完成**: 群聊 + 私聊引用消息图片识别（`fetch_quoted_image_urls` + 多模态 content）。

**未完成**: 直接发送图片识别（私聊直接发图不响应，群聊直接发图不响应——均为设计决策，非缺失）。

### 5. 结构化记忆蒸馏（优先级：高）

**目标**: Compaction 和心跳时用小模型将对话中的事实性知识蒸馏为结构化条目，只追加到 `memories.jsonl`，不去重（条目体积小、膨胀可控），写入前严格校验质量。

**架构**:

```
写入侧（两个触发点）：

  Compaction 触发时：
    大模型 ──→ 对话摘要（已有）
    大模型 ──→ MEMORY.md 自由文本追加（已有）
    小模型 ──→ 从摘要中蒸馏 → 追加 memories.jsonl（新增）
    重置水位线 last_distill_line = len(压缩后 history)

  心跳触发时：
    取增量消息 history[last_distill_line:]
    如果有新消息 → 小模型蒸馏 → 追加 memories.jsonl
    更新水位线 last_distill_line = len(history)

读取侧（memory_search 工具）：
  向量索引 ──→ 同时索引 MEMORY.md + history.jsonl（代码已有，当前因未配 embedding 模型不生效）
  结构化检索 ──→ 加载 memories.jsonl，按 type/关键词过滤（依赖蒸馏，memories.jsonl 目前尚未生成）

System prompt 常驻注入：
  ──→ memories.jsonl 中 type=identity 的少量核心条目
```

**水位线机制**:
- `config.json` 新增 `last_distill_line` 字段，记录上次蒸馏时 history.jsonl 的行数
- 心跳蒸馏时只取 `history[last_distill_line:]` 作为增量输入，避免重复蒸馏
- Compaction 会重写 history.jsonl（摘要 + 保留尾部），必须重置水位线到压缩后的新行数

**写入质量控制（宁缺毋滥）**:
- 蒸馏 prompt 要求只提取**高置信度的事实性信息**，闲聊/模棱两可的内容不提取
- 小模型输出后严格 JSON 校验：缺 type/subject/value 的丢弃
- `confidence=low` 的条目直接丢弃，不写入
- 每条加 `updated` 时间戳

**不去重的理由**:
- 每条 ~100 字，一个月几百条，几十 KB，膨胀可控
- 避免去重合并逻辑误删有效信息
- 读取时按时间排序，最新的自然排前面
- 实现极简，不需要额外 API 调用

**数据格式** (`data/admin/memories.jsonl`，每行一条):
```json
{"type": "preference", "subject": "编程语言", "value": "偏好 Python，常用 async/await", "confidence": "high", "updated": "2026-03-28"}
{"type": "fact", "subject": "当前任务", "value": "正在开发 QQ Bot 项目", "expires": "2026-06", "updated": "2026-03-28"}
{"type": "identity", "subject": "基本信息", "value": "Bot 管理员，常在晚上活跃", "updated": "2026-04-01"}
```

**字段说明**:
- `type`: `identity`（身份信息）、`preference`（偏好）、`fact`（事实）、`task`（进行中的任务）、`emotion`（情感记录）
- `subject`: 知识主题（自由文本，不做枚举约束）
- `value`: 知识内容
- `confidence`: `high` / `medium`（low 不写入）
- `expires`: 可选，过期日期（过期后检索时自动跳过）
- `updated`: 最后更新时间

**实现步骤**:
1. 新建 `plugins/memory/structured.py`：`distill_memories(text, memories_path)` 核心函数 — 调用小模型蒸馏 + 校验 + 追加写入
2. 修改 `plugins/chat/compaction.py`：`compact_history()` 末尾调用 `distill_memories(摘要文本, ...)`，重置水位线
3. 修改 `plugins/chat/proactive.py`：心跳末尾取增量消息，调用 `distill_memories(增量消息, ...)`，更新水位线
4. 修改 `load_admin_prompt()`：从 `memories.jsonl` 中提取 `type=identity` 的条目注入 system prompt
5. 修改 `memory_search` 工具：增加结构化过滤模式（按 type/关键词查询 memories.jsonl）

**小模型选择**: `gemini-2.0-flash-lite`（或同级别小模型），蒸馏任务本质是信息提取，不需要强推理，小模型够用且省成本。

**Compaction 蒸馏输入**: 复用 Compaction 第 1 步已经生成的对话摘要（~1K token），不传原始被压缩消息，避免小模型输入过长。

**适用**: 私聊 + 群聊共享。

### 6. 主动发言扩展到群聊（已完成）

**状态**: 已实现。统一引擎 `plugins/proactive.py`（根级模块，keyed 计时器 + 私聊/群聊心跳）：

- key 格式: `private` / `group:<gid>`，`reset_idle_timer(key)` / `cancel_idle_timer(key)`
- 心跳执行 `run_heartbeat(chat_type, target_id)`：私聊加载 Admin 上下文，群聊加载人格 prompt，共用 `HEARTBEAT.md`
- 开关：私聊 `data/admin/config.json` 的 `proactive_enabled`（默认开）；群聊群 config.json 的 `proactive_enabled`（默认关）
- 指令：私聊 `/主动对话 enable|disable`；群聊 `/主动对话 [群号] enable|disable`（仅管理员）
- 心跳完成后检查开关再决定是否重启计时器（防止禁用后空转循环）

**已知取舍**:
- 群聊触发条件为"Bot 回复后一段时间"，与私聊相同
- 防骚扰：回复 `HEARTBEAT_OK` 静默；短回复（≤10 字）不发送
- 成本控制：心跳只保留最近 30 条消息（`HEARTBEAT_MAX_MESSAGES`）

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
- 时间上下文通过 messages 末尾的独立 system 消息动态注入，不存储在消息中、也不进 system prompt（保 LLM prefix cache 命中）

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
- `.vscode/` — 看团队约定

已在 .gitignore 中的：
- `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.pytest_tmp*/`, `.env`, `vendor/`, `reference/`
- `good_persona/` — 人格草稿/备份目录，含敏感 prompt 内容
- `data/sessions`, `data/groups`, `data/reminders.json`, `data/tool_calls.jsonl`
- `data/admin/config.json`, `data/admin/history.jsonl`, `data/admin/SOUL.md`,
  `data/admin/token_stats.json`, `data/admin/.memory_index.json`, `data/admin/memories.jsonl`
- `web/node_modules/`, `web/dist/`, `docs/`

> 注：`docs/` 是被忽略的，本 AGENTS.md 因位于仓库根目录才纳入版本控制。
