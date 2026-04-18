# Chain-AI-Gateway 部署指南

## 项目概述

**Chain-AI-Gateway** 是一个高可用性的 OpenAI 兼容式代理网关，提供如下功能：
- 多提供商路由：自动故障转移和负载均衡
- 健康检查：内置健康检查和熔断器机制
- 请求记录：详细的请求日志和性能统计
- 热重载配置：支持配置文件实时更新
- API 兼容：完全兼容 OpenAI API

## 部署环境要求

### 基础环境
- **Ubuntu 20.04/22.04** (或类似 Linux 发行版)
- **Python 3.8+** 
- **2GB+ 内存**
- **10GB+ 存储空间**

### 网络要求
- **80端口**（如果使用默认配置）
- 所有提供商的网络访问权限

## 部署方案

### 方案 1：使用插件安装（推荐）

#### 1.1 安装插件

```bash
# 下载并安装插件
wget https://github.com/your-repo/chain-ai-gateway/releases/latest/download/chain-ai-gateway-plugin.zip
unzip chain-ai-gateway-plugin.zip
cd chain-ai-gateway-plugin

# 运行安装脚本
./install.sh
```

#### 1.2 配置插件

```bash
# 编辑配置文件
nano /opt/chain-ai-gateway/config.yaml
```

关键配置项：
- `providers`: 设置提供商 API 密钥
- `default_model`: 默认模型
- `circuit_breaker.cooldown_seconds`: 熔断器恢复时间

#### 1.3 启动服务

```bash
systemctl start chain-ai-gateway
systemctl enable chain-ai-gateway
```

#### 1.4 验证部署

```bash
# 检查服务状态
systemctl status chain-ai-gateway

# 测试 API 连接
curl http://localhost:8000/v1/models
```

### 方案 2：手动部署

#### 2.1 安装依赖

```bash
# 更新系统包
apt-get update -y

# 安装 Python 环境
apt-get install -y python3 python3-pip python3-venv

# 创建并激活虚拟环境
python3 -m venv chain-ai-gateway-env
source chain-ai-gateway-env/bin/activate

# 安装 Python 依赖
pip install -r requirements.txt
```

#### 2.2 下载项目

```bash
# 克隆项目
git clone https://github.com/your-repo/chain-ai-gateway.git
cd chain-ai-gateway

# 备份配置文件
cp config.yaml config.yaml.backup
```

#### 2.3 配置环境变量

```bash
# 设置配置文件路径
export GATEWAY_CONFIG=/path/to/config.yaml
```

#### 2.4 启动服务

```bash
# 使用 uvicorn 启动
uvicorn main:app --host 0.0.0.0 --port 8000

# 或使用后台运行
nohup uvicorn main:app --host 0.0.0.0 --port 8000 > gateway.log 2>&1 &
```

## 配置说明

### 核心配置

```yaml
# 电路断器配置
circuit_breaker:
  cooldown_seconds: 300  # 恢复时间（秒）

# 默认模型
default_model: free

# 排除的模型列表
exclude_from_pool:
  - nvidia/nemotron-nano-12b-v2-vl:free
  - z-ai/glm-4.5-air:free

# 提供商配置
providers:
  openrouter_1:
    api_key: your-api-key
    base_url: https://openrouter.ai/api/v1
    proxy: ''  # 代理服务器（可选）
```

### 常用配置示例

```yaml
circuit_breaker:
  cooldown_seconds: 300

default_model: free
exclude_from_pool:
  - nvidia/nemotron-nano-12b-v2-vl:free
  - z-ai/glm-4.5-air:free
providers:
  openrouter_1:
    api_key: sk-or-v1-your-key-here
    base_url: https://openrouter.ai/api/v1
    proxy: ''
  openrouter_2:
    api_key: sk-or-v1-another-key
    base_url: https://openrouter.ai/api/v1
    proxy: ''
```

## API 端点

### OpenAI 兼容端点

```
POST /v1/chat/completions  # 聊天完成
GET /v1/models             # 获取模型列表
```

### 管理端点

```
GET /admin/logs            # 请求日志
GET /admin/stats           # 性能统计
GET /admin/models          # 提供商模型
```

## 监控与故障排除

### 日志查看

```bash
# 查看服务日志
journalctl -u chain-ai-gateway -f

# 查看应用日志
tail -f /opt/chain-ai-gateway/gateway.log
```

### 性能监控

```bash
# 检查服务状态
systemctl status chain-ai-gateway

# 查看资源使用
htop
```

### 常见问题

#### 问题 1：服务启动失败
```bash
# 检查配置文件
cat /opt/chain-ai-gateway/config.yaml

# 检查权限
ls -la /opt/chain-ai-gateway/
```

#### 问题 2：API 连接失败
```bash
# 检查网络连接
curl -I https://openrouter.ai

# 检查 API 密钥
cat /opt/chain-ai-gateway/config.yaml | grep api_key
```

## 说明文档

- 官方文档：https://github.com/your-repo/chain-ai-gateway
- API 参考：https://platform.openai.com/docs/api-reference
- 问题追踪：https://github.com/your-repo/chain-ai-gateway/issues

## 许可证

本项目采用 MIT 许可证，详见 LICENSE 文件。