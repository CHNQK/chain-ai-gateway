# Use Cases / 使用场景

这份文档的目标很直接：  
帮助第一次接触这个仓库的人，用他们真实会搜索的词，快速判断这是不是他们要找的项目。

This document is intentionally written in a search-friendly way so people looking for an OpenAI-compatible gateway, OpenRouter proxy, or tool-call-safe streaming proxy can quickly see whether this repository fits.

## 1. OpenAI-Compatible Gateway

如果你在找这些词：

- `OpenAI-compatible gateway`
- `OpenAI API gateway`
- `OpenAI compatible proxy`
- `OpenAI compatible admin panel`

这个项目适合你，因为它提供：

- `POST /v1/chat/completions`
- `GET /v1/models`
- OpenAI SDK 兼容接入方式
- 统一下游入口

适合场景：

- 已有应用、Agent、工具链都是按 OpenAI 协议接入
- 不想改业务代码，只想改 `base_url`

## 2. OpenRouter Proxy / OpenRouter Gateway

如果你在找这些词：

- `OpenRouter proxy`
- `OpenRouter gateway`
- `OpenRouter failover`
- `multiple OpenRouter keys`

这个项目适合你，因为它支持：

- 多个 OpenRouter API key
- 上游模型同步
- 模型不存在时回退到 `openrouter/free`
- 只对 `429` 进行 key 级别 failover

适合场景：

- 免费模型经常 `429`
- 希望把多个 key 收敛成一个统一地址

## 3. Model Router / Dynamic Model Routing

如果你在找这些词：

- `model router`
- `dynamic model routing`
- `route by model name`
- `OpenAI model gateway`

这个项目适合你，因为它的核心策略就是：

- 下游传什么模型，就按什么模型请求上游
- 不再固定指定上游模型
- 模型不存在时自动回退到 `openrouter/free`

适合场景：

- 下游客户端模型名很多，不想维护复杂映射
- 想让网关更像“统一入口”，而不是“固定转发器”

## 4. Tool Call Streaming Proxy / SSE Proxy

如果你在找这些词：

- `tool call proxy`
- `tool call streaming proxy`
- `SSE tool call proxy`
- `OpenAI tool_calls proxy`

这个项目适合你，因为它重点处理：

- 流式转发
- tool call 结构保留
- 不把结构化 `tool_calls` 降级塞到 `content` 字符串
- 不在中转层乱改响应内容

适合场景：

- Agent 框架需要可靠的工具调用
- 你遇到过中间层把工具调用搞坏的问题

## 5. Self-Hosted AI Gateway With Admin UI

如果你在找这些词：

- `self-hosted AI gateway`
- `LLM gateway with dashboard`
- `OpenAI proxy with admin panel`
- `AI gateway logs and metrics`

这个项目适合你，因为它自带：

- 首页概览
- 请求日志
- 上游状态
- 模型指标评估
- 上游模型目录
- 全量模型测试
- 测试报告

适合场景：

- 想观察真实请求表现
- 想用后台测试模型可用性
- 想知道哪些模型最近真的能用

## 6. BYOK Gateway / Multi-Provider Gateway

如果你在找这些词：

- `BYOK gateway`
- `multi-provider LLM gateway`
- `multi-upstream OpenAI proxy`
- `AI provider failover`

这个项目适合你，因为它支持：

- 多个上游 provider / API key
- 按规则进行失败转移
- 统一出口给下游使用

适合场景：

- 想减少客户端逐个适配不同 provider 的成本
- 想把 provider 切换逻辑收口在服务端

## 最后一句 / One-Line Positioning

Chain-AI-Gateway is a self-hosted OpenAI-compatible LLM gateway and OpenRouter proxy with dynamic model routing, `429`-only failover, upstream model sync, tool-call-safe streaming, and admin UI.
