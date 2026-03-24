---
name: web-search
description: 搜索互联网获取实时信息。当用户询问最新新闻、实时数据、具体事实查询、或你知识截止日期之后的事件时使用此技能。也适用于用户明确要求"搜一下"、"查一查"、"帮我查"的场景。
---

# Web Search

通过 Playwright MCP 工具进行网页搜索和信息提取。

## 工作流程

1. 使用 `playwright__browser_navigate` 打开搜索引擎
2. 在搜索框中输入查询关键词
3. 从搜索结果中提取关键信息
4. 如需详细内容，点击进入具体页面

## 搜索引擎

优先使用 Bing (`https://www.bing.com`)，备选 Google (`https://www.google.com`)。

## 搜索步骤

```
1. browser_navigate → https://www.bing.com
2. browser_type → 在搜索框中输入关键词
3. browser_press_key → Enter
4. browser_snapshot → 读取搜索结果
5. (可选) browser_click → 进入具体页面获取详细信息
```

## 注意事项

- 搜索关键词应简洁精准，中文搜索用中文关键词
- 优先从搜索结果摘要中提取答案，避免不必要的页面跳转
- 如果搜索结果不理想，尝试调整关键词重新搜索
- 汇总信息时注明来源
