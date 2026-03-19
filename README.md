# AzureSnowBot

基于 NapCat + NoneBot2 的 QQ 智能 Agent Bot。

## 项目状态

🚧 **开发中** — 已接入 LLM 实现多轮对话，后续将扩展 Agent 能力。

## 技术栈

- **NapCat** — 基于 NTQQ 的无头 Bot 框架，提供 OneBot 11 协议支持
- **NoneBot2** — Python 异步 Bot 框架，负责消息处理与插件管理
- **ChatGPT** — 大模型对话能力，支持多轮上下文

## 功能

| 指令 | 说明 |
|------|------|
| `ping` | 回复 `pong~`，用于检测 Bot 是否在线 |
| 私聊任意消息 | 调用 ChatGPT 进行多轮对话 |
| `清除对话` | 清空当前用户的对话历史 |

## 项目结构

```
AzureSnowBot/
├── main.py               # Bot 入口
├── pyproject.toml         # 项目配置
├── .env                   # 运行时环境变量（API Key 等，不提交）
├── plugins/               # NoneBot2 插件目录
│   ├── ping.py            # 存活检测插件
│   └── chat.py            # ChatGPT 多轮对话插件
└── data/
    └── sessions/          # 对话历史（JSONL，按用户存储）
```

## 快速开始

### 环境要求

- Python >= 3.10
- NapCat Shell 已安装并登录 QQ

### 安装依赖

```bash
pip install "nonebot2[fastapi]" nonebot-adapter-onebot httpx
```

### 配置

1. 在项目根目录创建 `.env` 文件：

```env
HOST=127.0.0.1
PORT=8082
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxx
# 可选
# OPENAI_BASE_URL=https://api.openai.com/v1
# OPENAI_MODEL=gpt-5.4
```

2. 在 NapCat WebUI 中添加 **WebSocket 客户端**，地址设为：

```
ws://localhost:8082/onebot/v11/ws
```

### 启动

```bash
python main.py
```

## Roadmap

- [x] NapCat + NoneBot2 基础通信
- [x] 接入 LLM，实现智能对话
- [x] 多轮对话上下文管理（JSONL 持久化 + Token 截断）
- [ ] 群聊对话支持
- [ ] 自定义 System Prompt
- [ ] 工具调用 / Agent 能力
