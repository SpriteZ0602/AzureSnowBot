# AzureSnowBot

基于 NapCat + NoneBot2 的 QQ 智能 Agent Bot。

## 项目状态

🚧 **开发中** — 当前为 MVP 阶段，后续将接入 AI Agent 能力。

## 技术栈

- **NapCat** — 基于 NTQQ 的无头 Bot 框架，提供 OneBot 11 协议支持
- **NoneBot2** — Python 异步 Bot 框架，负责消息处理与插件管理

## 项目结构

```
AzureSnowBot/
├── main.py            # Bot 入口
├── pyproject.toml     # 项目配置
├── .env               # 运行时环境变量
└── plugins/           # NoneBot2 插件目录
    └── hello.py       # 示例插件
```

## 快速开始

### 环境要求

- Python >= 3.10
- NapCat Shell 已安装并登录 QQ

### 安装依赖

```bash
pip install "nonebot2[fastapi]" nonebot-adapter-onebot
```

### NapCat 配置

在 NapCat WebUI 中添加 **WebSocket 客户端**，地址设为：

```
ws://localhost:8082/onebot/v11/ws
```

### 启动

```bash
python main.py
```

## Roadmap

- [x] NapCat + NoneBot2 基础通信
- [ ] 接入 LLM，实现智能对话 Agent
- [ ] 多轮对话上下文管理
- [ ] 自定义工具调用能力
