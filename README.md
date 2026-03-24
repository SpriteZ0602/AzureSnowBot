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
| 任意消息 | 调用 LLM 进行多轮对话（支持引用消息） |
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

添加新工具只需在 `plugins/local_tools/tools.py` 中编写函数并加上 `@register_tool` 装饰器，重启即可。

工具调用优先级：Skill 工具 → 本地工具 → MCP 工具。

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
- **Gemini API** — 通过 OpenAI 兼容接口调用（默认模型 `gemini-3-flash-preview`）
- **MCP SDK** — Model Context Protocol 客户端，连接外部工具服务器

## 项目结构

```
AzureSnowBot/
├── main.py                        # Bot 入口
├── pyproject.toml                 # 项目配置
├── .env                           # 运行时环境变量（不提交）
├── plugins/                       # NoneBot2 插件目录
│   ├── __init__.py
│   ├── ping.py                    #   存活检测
│   ├── chunker.py                 #   消息分条发送 + 人类节奏模拟
│   ├── chat/                      #   私聊对话
│   │   └── handler.py
│   ├── group/                     #   群聊对话
│   │   ├── handler.py             #     消息处理 + Agentic Loop
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
│   └── sessions/                  # 对话历史（JSONL）
│       ├── admin_persona.txt      #   管理员私聊专属人格
│       ├── <user_id>.jsonl        #   私聊会话
│       └── groups/
│           └── <group_id>/
│               ├── _active.json   #   当前激活人格
│               ├── <persona>.jsonl #  对话历史（按人格隔离）
│               └── personas/      #   群私有人格
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
gemini_api_key=AIzaSyXXXXXXXXXXXXX
gemini_base_url=https://generativelanguage.googleapis.com/v1beta/openai
gemini_model=gemini-3-flash-preview
GROUP_WHITELIST=["群号1", "群号2"]
ADMIN_NUMBER=你的QQ号
```

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `gemini_api_key` | Gemini API 密钥 | （必填） |
| `gemini_base_url` | Gemini API 基地址（OpenAI 兼容） | `https://generativelanguage.googleapis.com/v1beta/openai` |
| `gemini_model` | 模型名称 | `gemini-3-flash-preview` |
| `GROUP_WHITELIST` | 允许使用的群号列表（JSON 数组） | `[]`（空 = 不响应任何群） |
| `ADMIN_NUMBER` | 管理员 QQ 号，该用户私聊时读取专属人格 | 空 |

2. 如果需要管理员私聊专属人格，创建文件：

```text
data/sessions/admin_persona.txt
```

当私聊用户 QQ 号与 `ADMIN_NUMBER` 一致时，会优先使用这个文件作为 system prompt。

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
- [ ] 图片理解 / 多模态
