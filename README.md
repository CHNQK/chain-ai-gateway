# Chain-AI-Gateway

> OpenAI-compatible LLM gateway, OpenRouter proxy, model router, and self-hosted AI API gateway with 429-only failover, upstream model sync, tool-call-safe streaming, and admin UI.
>
> 面向 OpenAI 兼容客户端的多上游 AI 网关，支持按下游模型直连上游、`openrouter/free` 回退、`429` 级别 API key failover、上游模型同步和管理后台。

[![GitHub tag](https://img.shields.io/github/v/tag/CHNQK/chain-ai-gateway?label=version)](https://github.com/CHNQK/chain-ai-gateway/tags)
![OpenAI Compatible](https://img.shields.io/badge/OpenAI-Compatible-0f766e)
![OpenRouter Proxy](https://img.shields.io/badge/OpenRouter-Proxy-c65a11)
![FastAPI](https://img.shields.io/badge/FastAPI-Gateway-2563eb)
![Tool Call Safe](https://img.shields.io/badge/Tool--Call-Safe%20Streaming-7c3aed)

一个面向 OpenAI 兼容客户端的多上游网关，重点解决以下问题：

- 下游传什么模型，就按什么模型向上游请求
- 上游找不到该模型时，自动回退到 `openrouter/free`
- 只对 `429` 做 API key 级别 failover
- 下游 `/v1/models` 自动同步上游模型目录
- 内置后台管理页，可查看请求日志、上游状态、模型指标、全量模型测试和测试报告

这个项目适合自建一个统一入口，把多个 OpenRouter / OpenAI-compatible 上游收敛成一个稳定的下游地址。

当前版本：`v0.1.0`  
Release Notes：[`CHANGELOG.md`](./CHANGELOG.md)

## 关键词 / Keywords

如果有人在搜下面这些词，这个项目应该能被看见：

- `OpenAI-compatible gateway`
- `OpenAI API proxy`
- `OpenRouter proxy`
- `OpenRouter gateway`
- `LLM gateway`
- `AI gateway`
- `model router`
- `chat completions proxy`
- `tool call streaming proxy`
- `SSE tool call proxy`
- `BYOK AI gateway`
- `multi-provider LLM proxy`
- `429 failover OpenRouter`
- `OpenAI compatible admin panel`

## 这项目适合谁 / Who Is This For

- 想把多个 OpenRouter / OpenAI-compatible 上游收敛到一个统一入口的人
- 想继续沿用 OpenAI SDK，只改 `base_url` 就接入的人
- 想让下游模型名直接决定上游请求模型，而不是维护固定映射表的人
- 想在免费模型或共享额度环境里做 `429` 级别 failover 的人
- 想保留 tool call / SSE 结构，不希望中转层把工具调用降级成纯文本的人
- 想要一个自带日志、状态页、模型测试和可用模型列表的 self-hosted gateway 的人

## 用什么词可以找到它 / Search-Friendly Summary

Chain-AI-Gateway is a self-hosted OpenAI-compatible API gateway for OpenRouter and other OpenAI-compatible upstreams.  
It works as an OpenAI proxy, OpenRouter proxy, model router, and multi-provider LLM gateway, while preserving tool calls, handling `429` failover across upstream API keys, syncing upstream models to downstream clients, and exposing an admin dashboard for logs, status, testing, and reports.

这不是一个只做“简单转发”的反向代理。  
它更接近一个面向 Agent、工具调用、模型测试和多上游调度的 OpenAI-compatible AI gateway。

## 系统架构

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│                                  下游客户端                                  │
│                     OpenAI SDK / 应用 / Agent / 工具                         │
└───────────────────────────────────┬──────────────────────────────────────────┘
                                    │
                                    │  /v1/chat/completions
                                    │  /v1/models
                                    ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                             Chain-AI-Gateway                                │
│                                                                              │
│  请求路由                   故障转移策略                管理与控制             │
│  - 模型名直接透传          - 仅重试 429               - 上游模型同步          │
│  - 不固定上游模型          - 切换到下一个 API key     - 全量模型测试          │
│  - 模型不存在时回退        - 其他错误直接返回         - 测试报告汇总          │
│    到 openrouter/free         给下游客户端             - 实时管理后台          │
│                                                                              │
│  响应路径                   持久化存储                 可观测性               │
│  - OpenAI 兼容              - SQLite                  - 请求日志             │
│  - 流式 / 文本              - 测试快照                - Provider 状态        │
│  - 保持 tool-call 结构      - 统计指标                - 模型指标评估         │
└───────────────┬──────────────────────────┬──────────────────────┬────────────┘
                │                          │                      │
                │                          │                      │
                ▼                          ▼                      ▼
     ┌───────────────────┐      ┌───────────────────┐   ┌────────────────────┐
     │ 上游 Key 池        │      │   gateway.db      │   │    管理控制台       │
     │ provider A / B... │      │ 日志 / 指标 /     │   │ static/index.html  │
     │ 429 => 下一个 key │      │ 报告 / 运行状态   │   │ 状态 / 测试 / 报告 │
     └─────────┬─────────┘      └───────────────────┘   └────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                        OpenRouter / OpenAI 兼容上游                          │
│                                                                              │
│   请求模型存在        => 直接请求同名模型                                    │
│   请求模型不存在      => 自动路由到 openrouter/free                          │
│   上游返回 429        => 切换到下一个 API key                                │
│   上游返回其他错误    => 直接返回给下游客户端                                 │
└──────────────────────────────────────────────────────────────────────────────┘
```

## 请求流转示例

```text
下游请求
  │
  │  model = some/model
  ▼
┌──────────────────────┐
│  Chain-AI-Gateway    │
└──────────┬───────────┘
           │
           ├─ 上游存在该模型
           │    └─> 直接请求同名模型
           │
           ├─ 上游不存在该模型
           │    └─> 回退到 openrouter/free
           │
           ├─ 返回 429
           │    └─> 切换到下一个 API key
           │
           └─ 返回其他错误
                └─> 直接返回给下游客户端
```

## Quick Start 摘要

```text
┌─────────────────────────────────────────────────────────────┐
│  1. 复制配置                                                │
│     cp config.example.yaml config.yaml                      │
│                                                             │
│  2. 填入上游 API key                                        │
│     providers.openrouter_x.api_key                          │
│                                                             │
│  3. 安装依赖                                                │
│     python3 -m venv .venv                                   │
│     source .venv/bin/activate                               │
│     pip install -r requirements.txt                         │
│                                                             │
│  4. 启动网关                                                │
│     python main.py                                          │
│                                                             │
│  5. 访问地址                                                │
│     API   -> http://127.0.0.1:8088/v1                       │
│     后台  -> http://127.0.0.1:8088/admin                    │
└─────────────────────────────────────────────────────────────┘
```

## 核心特性

- OpenAI 兼容接口
  - `POST /v1/chat/completions`
  - `GET /v1/models`
- 动态模型路由
  - 不再固定指定上游模型
  - 由下游请求模型名直接决定上游定位
- 模型不存在自动回退
  - 上游无该模型时，直接请求 `openrouter/free`
  - 不做 `原模型名 + :free` 这种拼接
- 429-only failover
  - 只对 `429` 在不同上游 API key 之间转移
  - 其他错误直接返回给客户端
- 上游模型目录同步
  - 后台支持手动刷新
  - 下游查看模型列表时自动取上游聚合结果
- 后台全量模型测试
  - 通过下游真实链路逐个测试上游模型
  - 保留历史测试报告
  - 支持可用模型列表展示
- 管理后台
  - Provider 实时状态
  - 请求日志
  - 模型指标评估
  - 上游模型目录
  - 可用模型列表
  - 批量测试与报告

## 典型使用场景 / Common Use Cases

- 作为 `OpenAI API proxy`
  - 现有应用继续使用 OpenAI SDK，只替换 `base_url`
- 作为 `OpenRouter proxy`
  - 统一多个 OpenRouter key，对 `429` 做 key 级别 failover
- 作为 `model router`
  - 下游传什么模型，就请求什么模型；不存在时自动回退到 `openrouter/free`
- 作为 `tool call safe streaming proxy`
  - 保持流式响应中的 tool call 结构，不把结构化调用错误塞进 `content`
- 作为 `self-hosted AI gateway`
  - 带后台、日志、模型测试、可用模型列表和测试报告

更多长尾场景和搜索词说明见：[docs/USE_CASES.md](./docs/USE_CASES.md)

## 为什么不是简单反向代理 / Why Not Just A Reverse Proxy

- 不是只转 HTTP
  - 它会做模型存在性判断和 `openrouter/free` 兜底
- 不是无脑重试
  - 只对 `429` 在不同上游 API key/provider 间转移
- 不是只看静态配置
  - `/v1/models` 和后台模型目录会同步上游模型列表
- 不是只做请求转发
  - 它还包含日志、指标、批量模型测试、可用模型列表和测试报告
- 不是把流式响应当纯文本
  - 它关注 tool call / SSE 的结构完整性

## 当前路由规则

### 1. 正常请求

下游请求：

```json
{
  "model": "openrouter/auto",
  "messages": [
    { "role": "user", "content": "hello" }
  ]
}
```

网关行为：

- 直接按下游传入的 `model` 去上游查找并请求
- 不再使用固定上游模型映射

### 2. 模型不存在

如果该模型在当前上游不可用：

- 直接回退到 `openrouter/free`

### 3. Failover

如果某个上游 key 返回：

- `429`：切到下一个上游 key 重试
- 其他错误：直接返回给客户端

## 安装方式

### 方式一：快速启动

适合本机直接跑。

```bash
git clone <your-repo-url>
cd chain-ai-gateway

cp config.example.yaml config.yaml
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python main.py
```

启动后默认可访问：

- API: `http://127.0.0.1:8088/v1`
- 后台: `http://127.0.0.1:8088/admin`

### 方式二：用安装脚本部署

适合部署到新的 Linux 机器。

```bash
sudo ./install.sh
```

安装脚本会：

- 复制项目到 `/opt/chain-ai-gateway`
- 创建虚拟环境
- 安装依赖
- 写入 systemd 服务
- 启动服务

安装完成后常用命令：

```bash
sudo systemctl status chain-ai-gateway
sudo systemctl restart chain-ai-gateway
journalctl -u chain-ai-gateway -f
```

## 配置

复制示例配置：

```bash
cp config.example.yaml config.yaml
```

最小配置示例：

```yaml
circuit_breaker:
  cooldown_seconds: 300

default_model: free

exclude_from_pool:
  - nvidia/nemotron-nano-12b-v2-vl:free

providers:
  openrouter_1:
    api_key: sk-or-v1-your-key-here
    base_url: https://openrouter.ai/api/v1
    proxy: ''
```

说明：

- `providers`
  - 配置多个上游 key
  - `429` 时会在这些 key 之间切换
- `exclude_from_pool`
  - 用来排除不希望参与路由的一些模型
- `default_model`
  - 用于兼容兜底，但实际请求优先以下游传入模型为准

## 下游如何接入

下游把网关当成 OpenAI 兼容服务即可。

### cURL 示例

```bash
curl http://127.0.0.1:8088/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openrouter/auto",
    "messages": [
      { "role": "user", "content": "请用一句话介绍这个网关。" }
    ],
    "stream": false
  }'
```

### Python 示例

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8088/v1",
    api_key="dummy"
)

resp = client.chat.completions.create(
    model="openrouter/auto",
    messages=[{"role": "user", "content": "hello"}],
)

print(resp.choices[0].message)
```

## 后台页面

后台地址：

```text
http://127.0.0.1:8088/admin
```

主要页面：

- 概览
  - 今日请求
  - Provider 实时状态
  - 热度和异常
- 请求日志
  - 一次下游请求聚合成一条
  - 可查看上下游原文
- 上游状态
  - 模型指标评估
  - 上游模型目录
  - 最近一次测试可用模型
  - 全量测试报告
- Chat
  - 直接从后台发测试请求
  - 模型列表来自最近一次全量测试确认可用的模型

## 全量模型测试

支持从后台发起“全部模型测试”：

- 真实通过下游链路测试，不是直连上游
- 每个模型有超时控制
- 成功模型会进入“可用模型”列表
- 每次测试都会保存报告
- 报告会汇总：
  - 可用模型
  - 失败分类
  - 失败原因统计
  - 建议

## 项目结构

```text
main.py              FastAPI 主入口、转发逻辑、后台接口
db.py                SQLite 持久化
scheduler.py         上游调度、健康状态和 failover
static/index.html    管理后台前端
config.example.yaml  示例配置
install.sh           Linux 安装脚本
DEPLOYMENT_GUIDE.md  额外部署说明
```

## 安全说明

仓库默认不会提交以下内容：

- 真实 `config.yaml`
- `gateway.db`
- 日志文件
- 虚拟环境
- 本地备份文件

如果你要公开发布，请确认：

- 不要上传真实 API key
- 不要上传生产数据库
- 不要上传包含敏感请求内容的日志

## License

如果你准备公开分发，建议补一个明确的 License。
