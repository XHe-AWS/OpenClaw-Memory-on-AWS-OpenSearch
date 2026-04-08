# OpenClaw Memory System

> 基于 AWS OpenSearch Serverless + Bedrock 的 AI Agent 记忆系统，作为 OpenClaw MCP Server 运行。

## 功能特性

- **混合搜索** — BM25 关键字 + kNN 向量语义搜索，OpenSearch 原生 search pipeline 归一化
- **中文分词** — ICU analyzer，不是 trigram 模糊匹配
- **即写即搜** — 异步写入 + pending queue 兜底，写入延迟 <3ms
- **Dreaming 记忆整理** — 三阶段自动提炼：Light（提取）→ REM（聚类）→ Deep（评分+晋升）
- **WAL 崩溃恢复** — Write-Ahead Log 保证消息不丢失
- **多 Agent 隔离** — agent_id 过滤 + 白名单跨 agent 查询
- **三级告警** — INFO / WARNING / CRITICAL，通过 tool 返回值传递给 agent
- **降级策略** — AOSS 不可用时降级到 pending queue 文本匹配，Bedrock 不可用时降级到纯文本存储
- **记忆遗忘/更新** — soft/hard forget + 原子更新，内存黑名单即时生效
- **文件自动索引** — 监控 MEMORY.md + memory/*.md，Markdown-aware 分块（代码块/表格不切碎）

## 架构

```
EC2 Instance
├── OpenClaw Gateway
└── Memory MCP Server (Python, stdio)
    ├── Ingester (async write + WAL)
    ├── Searcher (hybrid search + MMR + temporal decay)
    ├── Indexer  (file watcher + chunker)
    └── Dreaming (Light → REM → Deep)
        │
        ▼
    AWS Services
    ├── OpenSearch Serverless (VECTORSEARCH)
    ├── Bedrock Titan Embed V2 (1024d)
    └── Bedrock Claude Sonnet (Dreaming)
```

## 前提条件

- 一个 AWS 账号
- 已开通 Bedrock 模型访问：**Amazon Titan Embed Text V2** + **Anthropic Claude Sonnet**
- 运行 OpenClaw 的 EC2 实例（t4g.medium 或以上）
- Python 3.9+

## 部署

### 方式一：一键部署（推荐）

```bash
# 1. 克隆代码
git clone https://github.com/XHe-AWS/OpenClaw-Memory-on-AWS-OpenSearch.git
cd OpenClaw-Memory-on-AWS-OpenSearch

# 2. 运行部署脚本（约 10 分钟）
chmod +x deploy.sh
./deploy.sh us-west-2 openclaw-memory

# 3. 按输出提示，将 MCP 配置添加到 openclaw.json

# 4. 重启 gateway
openclaw gateway restart

# 5. 测试：对话中输入 "记住我喜欢吃火锅"，然后问 "我喜欢吃什么？"
```

deploy.sh 自动完成：CloudFormation 部署 → 等待 AOSS ACTIVE → 创建索引 → 创建搜索 pipeline → 输出配置。

### 方式二：手动部署

#### 1. 部署 CloudFormation

```bash
aws cloudformation deploy \
  --template-file cloudformation/memory-system.yaml \
  --stack-name openclaw-memory \
  --parameter-overrides CollectionName=openclaw-memory \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-west-2
```

或在 AWS 控制台 → CloudFormation → Create Stack → Upload [`cloudformation/memory-system.yaml`](cloudformation/memory-system.yaml)（[直接下载](https://raw.githubusercontent.com/XHe-AWS/OpenClaw-Memory-on-AWS-OpenSearch/main/cloudformation/memory-system.yaml)）。

CloudFormation 创建：
- OpenSearch Serverless Collection（VECTORSEARCH）
- 加密 / 网络 / 数据访问策略
- IAM Role + Instance Profile（Bedrock + AOSS 权限）

#### 2. 等待 Collection ACTIVE（5-10 分钟）

```bash
aws opensearchserverless batch-get-collection \
  --names openclaw-memory \
  --query 'collectionDetails[0].status' \
  --output text --region us-west-2
```

#### 3. 获取 Endpoint

```bash
ENDPOINT=$(aws cloudformation describe-stacks \
  --stack-name openclaw-memory \
  --query 'Stacks[0].Outputs[?OutputKey==`CollectionEndpoint`].OutputValue' \
  --output text --region us-west-2)
```

#### 4. 挂载 Instance Profile 到 EC2

```bash
PROFILE=$(aws cloudformation describe-stacks \
  --stack-name openclaw-memory \
  --query 'Stacks[0].Outputs[?OutputKey==`InstanceProfileArn`].OutputValue' \
  --output text --region us-west-2)

aws ec2 associate-iam-instance-profile \
  --instance-id <your-instance-id> \
  --iam-instance-profile Arn=$PROFILE \
  --region us-west-2
```

#### 5. 安装依赖 + 创建索引

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export OPENCLAW_MEMORY_OPENSEARCH_ENDPOINT="$ENDPOINT"
export OPENCLAW_MEMORY_OPENSEARCH_REGION="us-west-2"
python setup_opensearch.py
```

#### 6. 配置 OpenClaw

在 `openclaw.json` 中添加：

```json
{
  "mcp": {
    "servers": {
      "openclaw-memory": {
        "command": "/path/to/.venv/bin/python",
        "args": ["/path/to/mcp_server.py"],
        "env": {
          "OPENCLAW_MEMORY_OPENSEARCH_ENDPOINT": "https://xxxxx.us-west-2.aoss.amazonaws.com",
          "OPENCLAW_MEMORY_OPENSEARCH_REGION": "us-west-2"
        }
      }
    }
  }
}
```

```bash
openclaw gateway restart
```

## 删除

```bash
# 一键删除所有 AWS 资源（AOSS + IAM + 数据）
aws cloudformation delete-stack --stack-name openclaw-memory --region us-west-2

# 清理本地文件
rm -rf ~/.openclaw/memory/

# 从 openclaw.json 移除 MCP 配置，重启 gateway
openclaw gateway restart
```

## 配置

通过环境变量覆盖默认配置（在 openclaw.json 的 `env` 中设置）：

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `OPENCLAW_MEMORY_OPENSEARCH_ENDPOINT` | （必填） | AOSS endpoint URL |
| `OPENCLAW_MEMORY_OPENSEARCH_REGION` | `us-west-2` | AWS region |
| `OPENCLAW_MEMORY_EMBED_MODEL` | `amazon.titan-embed-text-v2:0` | 嵌入模型 |
| `OPENCLAW_MEMORY_EXTRACT_MODEL` | `us.anthropic.claude-sonnet-4-6` | Dreaming 用的 LLM |
| `OPENCLAW_MEMORY_EXCEPTION_AGENTS` | `xiaoxiami` | 跨 agent 搜索白名单（逗号分隔） |

## MCP Tools

| Tool | 说明 |
|------|------|
| `aws_memory_write` | 写入消息（异步，<3ms） |
| `aws_memory_search` | 混合搜索（BM25 + kNN） |
| `aws_memory_get` | 按 doc_id 或文件路径读取 |
| `aws_memory_pin` | 固定重要记忆（importance=1.0） |
| `aws_memory_forget` | 遗忘（soft/hard） |
| `aws_memory_update` | 原子更新内容 |
| `aws_memory_index` | 手动触发文件索引 |
| `aws_memory_stats` | 系统状态查询 |

## 成本

~$25/月（AOSS 2 OCU 最低消费 $24 + Bedrock 调用 ~$1）

## 目录结构

```
├── mcp_server.py          # MCP 入口（stdio JSON-RPC）
├── config.py              # 配置常量（环境变量覆盖）
├── opensearch_client.py   # AOSS 客户端（SigV4 签名）
├── embedding.py           # Bedrock Titan Embed V2
├── ingester.py            # 异步写入（WAL + pending queue + batch）
├── searcher.py            # 混合搜索（hybrid + MMR + decay）
├── chunker.py             # Markdown-aware 分块
├── indexer.py             # 文件监控 + 自动索引
├── tools.py               # 8 个 MCP tool 定义
├── migrate.py             # 数据迁移脚本
├── setup_opensearch.py    # 创建 index + search pipeline
├── deploy.sh              # 一键部署脚本
├── dreaming/
│   ├── light.py           # Light phase（LLM 提取）
│   ├── rem.py             # REM phase（聚类+主题）
│   ├── deep.py            # Deep phase（7维评分+晋升）
│   └── runner.py          # Dreaming 调度器
├── cloudformation/
│   └── memory-system.yaml # CloudFormation 模板
├── tests/                 # 单元测试
├── DEPLOY.md              # 完整部署手册（含迁移）
└── requirements.txt       # Python 依赖
```

## License

MIT
