# Chain-AI-Gateway

一个面向 OpenAI 兼容客户端的多上游网关，重点解决以下问题：

- 下游传什么模型，就按什么模型向上游请求
- 上游找不到该模型时，自动回退到 `openrouter/free`
- 只对 `429` 做 API key 级别 failover
- 下游 `/v1/models` 自动同步上游模型目录
- 内置后台管理页，可查看请求日志、上游状态、模型指标、全量模型测试和测试报告

这个项目适合自建一个统一入口，把多个 OpenRouter / OpenAI-compatible 上游收敛成一个稳定的下游地址。

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
