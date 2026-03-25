# AzureSnowBot

基于 NapCat + NoneBot2 的 QQ 智能 Agent Bot。支持多轮对话、人格切换、MCP 工具调用、渐进式披露 Skill 系统、本地工具注册、仿真人分条发送。

## 功能

### 群聊（需要 @Bot）

| 指令 | 说明 |
|------|------|
| @Bot `任意消息` | 调用 LLM 进行多轮对话（支持引用消息） |
| @Bot `/withoutChunking 消息` | 不分条发送，整条回复 |
| @Bot `/persona` | 列出所有可用人格 |
| @Bot `/persona <名称>` | 切换到指定人格 |
| @Bot `/persona info` | 查看当前人格的 prompt 摘要 |
| @Bot `/persona reset` | 清除当前人格的对话历史 |
| @Bot `/persona create <名称> <prompt>` | 创建本群私有人格 |
| @Bot `/persona delete <名称>` | 删除本群私有人格 |
| @Bot `/reset` | 清除当前对话历史 |
| @Bot `/skill` | 列出所有已加载技能 |
| @Bot `/skill <名称>` | 查看技能详情 |
| @Bot `/skill reload` | 重新扫描技能目录 |
| @Bot `/help` | 显示帮助信息 |

### 私聊

| 指令 | 说明 |
|------|------|
| 任意消息 | 调用 LLM 进行多轮对话（支持引用消息 + 工具调用） |
| `/reset` | 清空当前用户的对话历史 |
| `ping` | 回复 `pong~`，检测 Bot 是否在线 |

### 人格系统

双层人格体系，每个人格有独立的 system prompt 和对话历史：

- **通用人格**：`data/personas/<name>.txt`，所有群共享
- **群私有人格**：`data/sessions/groups/<gid>/personas/<name>.txt`，仅该群可见
- **管理员私聊人格**：`data/sessions/admin_persona.txt`，仅 `ADMIN_NUMBER` 对应的私聊用户使用
- **共享基础指令**：`data/personas/_base.txt`，自动追加到所有人格 prompt 末尾（回复风格、格式约束等）

查找优先级：群私有 > 通用（同名时群的覆盖通用）

内置人格：`default`（默认）、`catgirl`（猫娘）、`philosopher`（哲学家）、`roaster`（毒舌）

### 消息分条发送（Chunker）

模仿真人聊天节奏，将长回复拆成多条消息依次发送：

- 按换行符 `\n` 拆分成独立消息气泡
- 单条超过 200 字自动按句子再拆
- 每条间随机延迟 3-5 秒，模拟打字节奏
- 第一条消息引用原消息，后续直接发送
- 同一会话加锁，防止并发请求交错混乱
- 发送 `/withoutChunking` 前缀可跳过分条，整条返回
- 自动去除 LLM 回复开头的时间戳（如 `[2026-03-25 16:55:46]`），防止泄漏给用户

### MCP 工具调用

集成 [Model Context Protocol](https://modelcontextprotocol.io) 客户端，LLM 可在对话中自动调用外部工具（Agentic Loop，最多 10 轮）。

工具通过 `data/mcp_servers.json` 配置，支持任意 MCP 服务器：

```json
{
  "servers": {
    "playwright": {
      "command": "cmd.exe",
      "args": ["/c", "npx", "-y", "@playwright/mcp", "--headless"]
    }
  }
}
```

### 本地工具（Local Tools）

轻量级本地工具注册系统，使用 `@register_tool` 装饰器即可添加新工具，模型会自动调用：

| 工具 | 说明 |
|------|------|
| `current_time` | 获取当前日期、时间和星期 |
| `calculate` | 安全的数学表达式求值 |
| `random_number` | 生成指定范围的随机整数 |
| `set_reminder` | 一次性定时提醒（"X分钟后提醒我做Y"） |
| `set_daily_reminder` | 每日定时提醒（"每天9点提醒我签到"） |
| `cancel_reminder` | 取消已设置的提醒（一次性/每日） |
| `list_reminders` | 查看当前对话的待触发提醒 |

添加新工具只需在 `plugins/local_tools/tools.py` 中编写函数并加上 `@register_tool` 装饰器，重启即可。

工具调用优先级：Skill 工具 → 本地工具 → MCP 工具。

### 定时提醒

参考 OpenClaw 的 cron 调度器设计，内置定时提醒功能：

- **一次性提醒**："30分钟后提醒我开会" → `set_reminder`
- **每日定时**："每天早上9点提醒我签到" → `set_daily_reminder`（持续触发直到取消）
- **AI 生成消息**：提醒触发时自动加载群聊/私聊上下文，调用 LLM 生成自然的提醒消息（而非固定模板）
- **持久化**：`data/reminders.json`，Bot 重启不丢失
- 支持查看和取消已设置的提醒

### 群聊全量记录

旁路记录白名单群内所有消息（不仅限 @Bot），供后续工具按需检索：

- 存储位置：`data/sessions/groups/<gid>/_chatlog.jsonl`
- 每行格式：`{"ts": 1711000000, "uid": "123", "name": "昵称", "text": "消息内容"}`
- 自动清理超过 7 天的旧记录
- 支持按时间、发送者、关键词过滤查询
- 不会传入 LLM 的普通请求，仅在工具调用时按需加载

### 主动发言（Proactive Messaging）

Admin 私聊专属功能 —— Bot 在对话结束一段时间后，自主决定是否主动找你聊天：

- Bot 回复 admin 后启动空闲计时器（默认 1 小时，可通过 `PROACTIVE_IDLE_SECONDS` 配置）
- admin 再次对话会重置计时器
- 计时器到期后，携带完整对话历史询问 LLM 是否想主动说点什么
- LLM 有话说 → 直接发送并写入对话历史；回复 NO → 无事发生
- 主动发言后不重置计时器，避免自言自语循环；下次 admin 发消息并得到回复后才重新启动
- Admin 执行 `/reset` 清空对话历史时自动取消空闲计时器

### Skill 技能系统（渐进式披露）

借鉴 [OpenClaw AgentSkills](https://github.com/nicepkg/openclaw) 的设计理念，三层加载体系，最大限度节省上下文窗口：

| 层级 | 内容 | 加载时机 | Token 开销 |
|------|------|----------|------------|
| Level 1 | 技能名称 + 描述 | 始终在 system prompt 中 | ~100 词/技能 |
| Level 2 | SKILL.md 正文 | LLM 调用 `load_skill` 工具时 | 按需加载 |
| Level 3 | references/ 参考文档 | LLM 调用 `load_reference` 工具时 | 按需加载 |

Skill 目录结构：

```
data/skills/<skill-name>/
├── SKILL.md              (必需) YAML frontmatter + Markdown 正文
└── references/           (可选) 详细参考文档
    └── example.md
```

内置技能：`web-search`、`translator`、`code-reviewer`、`moegirl-wiki`

## 技术栈

- **NapCat** — 基于 NTQQ 的无头 Bot 框架，提供 OneBot 11 协议支持
- **NoneBot2** — Python 异步 Bot 框架，负责消息处理与插件管理
- **LLM 多服务商支持** — Gemini / OpenAI / Qwen 三选一，通过 `.env` 一键切换
- **MCP SDK** — Model Context Protocol 客户端，连接外部工具服务器

## 项目结构

```
AzureSnowBot/
├── main.py                        # Bot 入口
├── pyproject.toml                 # 项目配置
├── pytest.ini                     # pytest 配置
├── .env                           # 运行时环境变量（不提交）
├── plugins/                       # NoneBot2 插件目录
│   ├── __init__.py
│   ├── llm.py                     #   LLM 统一配置（多 Provider 切换）
│   ├── ping.py                    #   存活检测
│   ├── chunker.py                 #   消息分条发送 + 人类节奏模拟
│   ├── chat/                      #   私聊对话
│   │   ├── handler.py             #     消息处理 + Agentic Loop
│   │   └── proactive.py           #     Admin 主动发言（空闲计时器）
│   ├── group/                     #   群聊对话
│   │   ├── handler.py             #     消息处理 + Agentic Loop
│   │   ├── chatlog.py             #     全量群聊记录（旁路存储）
│   │   ├── commands.py            #     /reset, /help
│   │   └── utils.py               #     白名单、工具函数
│   ├── persona/                   #   人格管理
│   │   ├── manager.py             #     人格增删查改 + 会话持久化
│   │   └── commands.py            #     /persona 指令
│   ├── skill/                     #   Skill 技能系统
│   │   ├── manager.py             #     技能扫描、解析、渐进式加载
│   │   └── commands.py            #     /skill 指令
│   ├── local_tools/               #   本地工具注册系统
│   │   ├── manager.py             #     @register_tool 装饰器 + 调度
│   │   └── tools.py               #     内置工具实现
│   ├── reminder/                  #   定时提醒调度器
│   │   ├── __init__.py            #     启动时重载持久化提醒
│   │   └── scheduler.py           #     asyncio 定时任务 + JSON 持久化
│   └── mcp/                       #   MCP 工具集成
│       └── manager.py             #     MCP 服务器连接 + 工具调用
├── data/
│   ├── mcp_servers.json           # MCP 服务器配置
│   ├── skills/                    # Skill 技能目录
│   │   ├── web-search/            #   网络搜索技能
│   │   ├── translator/            #   翻译技能
│   │   ├── code-reviewer/         #   代码审查技能 (+ references/)
│   │   └── moegirl-wiki/          #   萌娘百科查询技能
│   ├── personas/                  # 通用人格 prompt 文件
│   │   ├── _base.txt              #   共享基础指令（自动追加到所有人格）
│   │   ├── default.txt            #   默认人格
│   │   ├── catgirl.txt            #   猫娘
│   │   ├── philosopher.txt        #   哲学家
│   │   └── roaster.txt            #   毒舌
│   ├── reminders.json             # 定时提醒持久化数据
│   ├── admin/                     # 管理员私聊
│   │   ├── admin_persona.txt      #   管理员专属人格
│   │   ├── config.json            #   {"last_message_at": "..."}
│   │   └── history.jsonl          #   对话历史
│   ├── private/                   # 普通私聊
│   │   └── <user_id>/
│   │       ├── config.json        #   {"last_message_at": "..."}
│   │       └── history.jsonl      #   对话历史
│   └── sessions/groups/           # 群聊会话
│       └── <group_id>/
│           ├── config.json        #   {"active_persona": "...", "last_message_at": "..."}
│           ├── <persona>.jsonl    #   对话历史（按人格隔离）
│           ├── _chatlog.jsonl     #   全量群聊记录
│           └── personas/          #   群私有人格
├── tests/                         # 单元测试
│   ├── test_calculate.py
│   ├── test_chatlog.py
│   ├── test_chunker.py
│   ├── test_persona.py
│   ├── test_proactive.py
│   ├── test_scheduler.py
│   └── test_skill.py
└── vendor/
    └── NapCat.Shell/              # NapCat 运行时
```

## 快速开始

### 环境要求

- Python >= 3.10
- NapCat Shell 已安装并登录 QQ
- Node.js（如需 MCP 工具，如 Playwright）

### 安装依赖

```bash
pip install "nonebot2[fastapi]" nonebot-adapter-onebot httpx mcp
```

如需 Playwright MCP 服务器：

```bash
npm install -g @playwright/mcp playwright
npx playwright install chromium
```

### 配置

1. 在项目根目录创建 `.env` 文件：

```env
HOST=127.0.0.1
PORT=8082

# LLM Provider 切换：gemini / openai / qwen
LLM_PROVIDER=gemini

# （可选）覆盖默认模型和接口地址:
# LLM_MODEL=gemini-2.5-flash-preview-05-20
# LLM_BASE_URL=

# API Keys（只需填写当前 provider 对应的即可）
gemini_api_key=AIzaSyXXXXXXXXXXXXX
# openai_api_key=sk-XXXX
# qwen_api_key=sk-XXXX

GROUP_WHITELIST=["群号1", "群号2"]
ADMIN_NUMBER=你的QQ号
```

支持三家 LLM 服务商，均通过 OpenAI 兼容接口调用：

| Provider | 默认模型 | 默认 Base URL | 所需 Key 变量 |
|----------|----------|-------------|------------|
| `gemini` | `gemini-2.5-flash-preview-05-20` | `generativelanguage.googleapis.com/v1beta/openai` | `gemini_api_key` |
| `openai` | `gpt-4o` | `api.openai.com/v1` | `openai_api_key` |
| `qwen` | `qwen-plus` | `dashscope.aliyuncs.com/compatible-mode/v1` | `qwen_api_key` |

其他配置项：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_PROVIDER` | LLM 服务商 | `gemini` |
| `LLM_MODEL` | （可选）覆盖默认模型名称 | 按 provider 自动选择 |
| `LLM_BASE_URL` | （可选）覆盖默认接口地址 | 按 provider 自动选择 |
| `GROUP_WHITELIST` | 允许使用的群号列表（JSON 数组） | `[]`（空 = 不响应任何群） |
| `ADMIN_NUMBER` | 管理员 QQ 号，该用户私聊时读取专属人格 | 空 |
| `PROACTIVE_IDLE_SECONDS` | Admin 私聊主动发言空闲等待秒数 | `3600`（1 小时） |

2. 如果需要管理员私聊专属人格，创建文件：

```text
data/admin/admin_persona.txt
```

当私聊用户 QQ 号与 `ADMIN_NUMBER` 一致时，会优先使用这个文件作为 system prompt。（首次启动时会自动从旧路径 `data/sessions/admin_persona.txt` 迁移）

3. 在 NapCat WebUI 中添加 **WebSocket 客户端**，地址设为：

```
ws://localhost:8082/onebot/v11/ws
```

4.（可选）配置 MCP 服务器，编辑 `data/mcp_servers.json`：

```json
{
  "servers": {
    "playwright": {
      "command": "cmd.exe",
      "args": ["/c", "npx", "-y", "@playwright/mcp", "--headless"]
    }
  }
}
```

> Windows 上 MCP manager 会自动补全 Node.js 到子进程 PATH，无需额外配置。

### 启动

```bash
python main.py
```

### 添加人格

在 `data/personas/` 下创建 `<名称>.txt` 文件，内容为该人格的角色设定（system prompt），即可全局使用。

`_base.txt` 中的共享指令会自动追加到所有人格末尾，无需在每个人格文件中重复编写回复风格等通用约束。

也可以在群聊中通过 `/persona create <名称> <prompt>` 创建仅限该群的私有人格。

如果某个人格只想给单个群使用，请放到：

```text
data/sessions/groups/<group_id>/personas/<name>.txt
```

### 添加本地工具

在 `plugins/local_tools/tools.py` 中编写函数并添加装饰器：

```python
from .manager import register_tool

@register_tool(
    name="my_tool",
    description="工具描述，告诉模型什么时候使用。",
)
async def my_tool(param1: str = "", **kwargs) -> str:
    return "工具执行结果"
```

重启 Bot 即可，模型会自动发现并按需调用。

### 创建技能 (Skill)

在 `data/skills/` 下创建目录，写一个 `SKILL.md`：

```
data/skills/my-skill/
├── SKILL.md
└── references/       (可选)
    └── details.md
```

`SKILL.md` 格式：

```markdown
---
name: my-skill
description: 这个技能做什么。当用户问到 XX 时使用。
---

# My Skill

具体的工作流程和指导...
```

**渐进式披露设计原则：**

- `name` + `description`（frontmatter）始终注入上下文，让 LLM 知道有哪些技能
- Markdown 正文只在 LLM 触发时加载，节省 token
- references/ 中的文件按需加载，适合放详细参考料
- SKILL.md 正文建议不超过 500 行，超出部分拆到 references/

## Roadmap

- [x] NapCat + NoneBot2 基础通信
- [x] 接入 LLM，实现多轮对话（私聊 + 群聊）
- [x] 多轮对话上下文管理（JSONL 持久化 + Token 截断）
- [x] 群聊白名单
- [x] 人格系统（通用 + 群私有，双层体系）
- [x] MCP 工具调用（Agentic Loop）
- [x] Playwright 浏览器工具（无头模式）
- [x] Skill 技能系统（渐进式披露）
- [x] 本地自定义工具注册
- [x] 消息分条发送 + 仿真人节奏
- [x] 定时提醒（参考 OpenClaw cron 调度器）
- [x] 私聊工具调用（Agentic Loop）
- [x] Admin 主动发言（空闲计时器 + LLM 自主决策）
- [ ] 图片理解 / 多模态
