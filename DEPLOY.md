# OpenClaw 记忆系统 v2 — 部署手册

> 从零到生产环境的完整部署指南，包含数据迁移。
> 基于 2026-04-08 的实际验证。

---

## 目录

1. [前提条件](#1-前提条件)
2. [架构概览](#2-架构概览)
3. [一键部署](#3-一键部署)
4. [手动部署（详细步骤）](#4-手动部署详细步骤)
5. [配置 OpenClaw](#5-配置-openclaw)
6. [数据迁移](#6-数据迁移)
7. [验证部署](#7-验证部署)
8. [配置参数说明](#8-配置参数说明)
9. [运维与监控](#9-运维与监控)
10. [故障排除](#10-故障排除)
11. [资源清理](#11-资源清理)
12. [成本估算](#12-成本估算)

---

## 1. 前提条件

### 1.1 AWS 账号要求

| 项目 | 要求 |
|------|------|
| AWS 账号 | 一个可用的 AWS 账号 |
| Bedrock 模型访问 | 开通 **Amazon Titan Embed Text V2** 和 **Anthropic Claude Sonnet** |
| EC2 实例 | 运行 OpenClaw 的 EC2（t4g.medium 或以上，Amazon Linux 2023 / Ubuntu） |
| IAM 权限 | 能创建 CloudFormation stack、IAM Role、OpenSearch Serverless |
| Python | 3.9+（推荐 3.12） |

### 1.2 开通 Bedrock 模型

在 AWS 控制台 → Bedrock → Model access 中申请以下模型：

- **Amazon Titan Embed Text V2**（嵌入向量生成）
- **Anthropic Claude Sonnet**（Dreaming 记忆提炼）

> ⚠️ 模型申请通常即时生效，但部分模型可能需要几分钟。

### 1.3 确认 EC2 环境

```bash
# 检查 Python 版本
python3 --version  # 需要 3.9+

# 检查 AWS CLI
aws --version
aws sts get-caller-identity

# 检查 OpenClaw
openclaw --version
```

---

## 2. 架构概览

```
┌─────────────────────────────────────────────┐
│              EC2 Instance                    │
│                                              │
│  ┌──────────────┐  ┌─────────────────────┐  │
│  │   OpenClaw    │  │  Memory MCP Server  │  │
│  │   Gateway     │──│  (Python, stdio)    │  │
│  └──────────────┘  └──────────┬──────────┘  │
│                               │              │
└───────────────────────────────┼──────────────┘
                                │
              ┌─────────────────┼───────────────┐
              │                 │               │
    ┌─────────▼──────┐  ┌──────▼──────┐  ┌─────▼─────┐
    │   OpenSearch    │  │   Bedrock   │  │  Bedrock  │
    │   Serverless    │  │  Titan V2   │  │  Claude   │
    │   (AOSS)        │  │  (Embed)    │  │ (Dreaming)│
    └────────────────┘  └─────────────┘  └───────────┘
```

**月成本约 $25**：AOSS 最低消费 ~$24（2 OCU） + Bedrock 调用 ~$1。

---

## 3. 一键部署

最简路径，6 步 10 分钟：

```bash
# 1. 克隆代码
git clone <your-repo> openclaw-memory-v2
cd openclaw-memory-v2

# 2. 确保 EC2 已挂载 Instance Profile（有 Bedrock + OpenSearch 权限）
#    如果还没有，deploy.sh 会通过 CloudFormation 创建

# 3. 运行部署脚本
chmod +x deploy.sh
./deploy.sh us-west-2 openclaw-memory

# 4. 按输出提示，把 MCP 配置加到 openclaw.json
#    （deploy.sh 最后会打印具体配置内容）

# 5. 重启 gateway
openclaw gateway restart

# 6. 测试
#    在对话中输入："记住我喜欢吃火锅"
#    然后输入："我喜欢吃什么？"
```

> deploy.sh 会自动完成：CloudFormation 部署 → 等待 AOSS ACTIVE → 创建索引 → 创建搜索 pipeline → 输出配置。

---

## 4. 手动部署（详细步骤）

### 4.1 部署 CloudFormation

```bash
cd openclaw-memory-v2

aws cloudformation deploy \
  --template-file cloudformation/memory-system.yaml \
  --stack-name openclaw-memory \
  --parameter-overrides CollectionName=openclaw-memory \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-west-2
```

CloudFormation 创建以下资源：

| 资源 | 说明 |
|------|------|
| OpenSearch Serverless Collection | VECTORSEARCH 类型，支持 kNN |
| 加密策略 | AWS 托管密钥加密 |
| 网络策略 | 公网访问（可按需改为 VPC 限制） |
| 数据访问策略 | IAM Role 有索引读写权限 |
| IAM Role + Instance Profile | Bedrock + AOSS 权限 |

### 4.2 等待 Collection ACTIVE

AOSS Collection 创建后需要 **5-10 分钟**变为 ACTIVE：

```bash
# 查看状态
aws opensearchserverless batch-get-collection \
  --names openclaw-memory \
  --query 'collectionDetails[0].status' \
  --output text --region us-west-2
```

等到输出 `ACTIVE` 再继续。

### 4.3 获取 Endpoint

```bash
ENDPOINT=$(aws cloudformation describe-stacks \
  --stack-name openclaw-memory \
  --query 'Stacks[0].Outputs[?OutputKey==`CollectionEndpoint`].OutputValue' \
  --output text --region us-west-2)

echo $ENDPOINT
# 输出类似: https://xxxxx.us-west-2.aoss.amazonaws.com
```

### 4.4 挂载 Instance Profile

```bash
# 获取 Instance Profile 名称
PROFILE=$(aws cloudformation describe-stacks \
  --stack-name openclaw-memory \
  --query 'Stacks[0].Outputs[?OutputKey==`InstanceProfileArn`].OutputValue' \
  --output text --region us-west-2)

# 挂载到当前 EC2（替换为你的 instance ID）
aws ec2 associate-iam-instance-profile \
  --instance-id i-xxxxxxxxx \
  --iam-instance-profile Arn=$PROFILE \
  --region us-west-2
```

> 如果 EC2 已经有 Instance Profile，需要先 disassociate 旧的，或者手动把 Bedrock 和 AOSS 权限加到现有 Role 上。

### 4.5 安装 Python 依赖

```bash
cd openclaw-memory-v2
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4.6 创建索引和搜索 Pipeline

```bash
export OPENCLAW_MEMORY_OPENSEARCH_ENDPOINT="$ENDPOINT"
export OPENCLAW_MEMORY_OPENSEARCH_REGION="us-west-2"

python setup_opensearch.py
```

成功输出：
```
Created index 'openclaw-memory': {'acknowledged': True}
Created search pipeline 'memory-search-pipeline': {'acknowledged': True}
```

---

## 5. 配置 OpenClaw

### 5.1 添加 MCP Server 配置

编辑 `openclaw.json`（通常在 `~/.openclaw/openclaw.json`），添加 MCP server：

```json
{
  "mcp": {
    "servers": {
      "openclaw-memory": {
        "command": "/path/to/openclaw-memory-v2/.venv/bin/python",
        "args": ["/path/to/openclaw-memory-v2/mcp_server.py"],
        "env": {
          "OPENCLAW_MEMORY_OPENSEARCH_ENDPOINT": "https://xxxxx.us-west-2.aoss.amazonaws.com",
          "OPENCLAW_MEMORY_OPENSEARCH_REGION": "us-west-2"
        }
      }
    }
  }
}
```

### 5.2 配置环境变量（可选）

可以通过 MCP env 或系统环境变量覆盖默认配置：

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `OPENCLAW_MEMORY_OPENSEARCH_ENDPOINT` | （必填） | AOSS endpoint URL |
| `OPENCLAW_MEMORY_OPENSEARCH_REGION` | `us-west-2` | AWS region |
| `OPENCLAW_MEMORY_EMBED_MODEL` | `amazon.titan-embed-text-v2:0` | 嵌入模型 |
| `OPENCLAW_MEMORY_EXTRACT_MODEL` | `us.anthropic.claude-sonnet-4-6` | Dreaming 用的 LLM |
| `OPENCLAW_MEMORY_EXCEPTION_AGENTS` | `xiaoxiami` | 可跨 agent 搜索的白名单（逗号分隔） |
| `OPENCLAW_MEMORY_WAL_PATH` | `~/.openclaw/memory/wal.jsonl` | WAL 文件路径 |
| `OPENCLAW_MEMORY_LOG_LEVEL` | `INFO` | 日志级别 |

### 5.3 重启 Gateway

```bash
openclaw gateway restart
```

### 5.4 配置 Dreaming 定时任务（推荐）

Dreaming 每天自动从对话中提炼有价值的记忆：

```bash
openclaw cron add \
  --name memory-dreaming \
  --schedule "0 3 * * *" \
  --command "/path/to/openclaw-memory-v2/.venv/bin/python -m dreaming.runner --agent xiaoxiami"
```

> 默认 UTC 03:00（北京时间 11:00）运行。可调整为适合你的时间。

---

## 6. 数据迁移

### 6.1 适用场景

如果你之前使用了以下任一存储，需要迁移：

| 旧存储 | 数据类型 | 迁移方式 |
|--------|----------|----------|
| DynamoDB (`OpenClaw-memory-short-term`) | 短期对话消息 | `migrate.py` 自动迁移 |
| S3 Vectors (`openclaw-memory-long-term`) | 长期记忆向量 | `migrate.py` 自动迁移 |
| 仅 MEMORY.md + memory/*.md 文件 | 文件记忆 | **无需手动迁移**，Indexer 启动后自动索引 |

### 6.2 迁移前检查

```bash
# 检查 DynamoDB 数据量
aws dynamodb describe-table \
  --table-name OpenClaw-memory-short-term \
  --query 'Table.ItemCount' --output text

# 检查 S3 向量数据
aws s3 ls s3://openclaw-memory-long-term/memory-index/ --summarize | tail -2
```

### 6.3 执行迁移

```bash
cd openclaw-memory-v2
source .venv/bin/activate

export OPENCLAW_MEMORY_OPENSEARCH_ENDPOINT="https://xxxxx.us-west-2.aoss.amazonaws.com"
export OPENCLAW_MEMORY_OPENSEARCH_REGION="us-west-2"

# 先试运行（不写入，只检查）
python migrate.py --dry-run

# 确认没问题后正式迁移
python migrate.py
```

### 6.4 迁移输出示例

```
Starting migration (dry_run=False)...
Migrating DynamoDB table: OpenClaw-memory-short-term
Found 105 DynamoDB items
Migrated DynamoDB batch 0-10 (10 docs)
Migrated DynamoDB batch 10-20 (10 docs)
...
Migrating S3 Vectors: openclaw-memory-long-term/memory-index
Found 50 S3 Vectors
Migrated S3V batch 0-10 (10 docs)
...
Migration complete: {
  "dynamo_scanned": 105,
  "dynamo_migrated": 105,
  "s3v_scanned": 50,
  "s3v_migrated": 50,
  "errors": 0
}
```

### 6.5 迁移验证

等待 **80 秒**（AOSS 索引延迟），然后搜索验证：

```bash
python -c "
from opensearch_client import OpenSearchClient
c = OpenSearchClient()
# 统计各类型文档数
for t in ['message', 'extracted', 'file_chunk']:
    r = c.client.search(index='openclaw-memory', body={'size':0, 'query':{'term':{'doc_type':t}}})
    print(f'{t}: {r[\"hits\"][\"total\"][\"value\"]} docs')
"
```

### 6.6 迁移注意事项

- **幂等**：重复运行 migrate.py 会产生重复数据（doc_id 相同但 AOSS 自动生成的 _id 不同）。如需重跑，建议先清空索引。
- **脏数据处理**：`day` 字段格式不符合 `yyyy-MM-dd` 的记录会被自动修正为 `created_at` 的日期。
- **S3 Vectors embedding 复用**：S3 中的向量直接复用，不重新调用 Bedrock 生成，节省成本。
- **DynamoDB embedding 重生成**：DynamoDB 的消息没有原始 embedding，迁移时会调 Bedrock Titan V2 生成。105 条约需 30 秒。

### 6.7 文件记忆自动迁移

如果你只有 `MEMORY.md` 和 `memory/*.md` 文件（没用 DynamoDB/S3），**无需运行 migrate.py**。MCP Server 启动后，Indexer 会在 30 秒内自动扫描并索引这些文件。

---

## 7. 验证部署

### 7.1 快速验证

在 OpenClaw 对话中测试：

```
你：记住我喜欢吃火锅
AI：好的，记住了。

你：我喜欢吃什么？
AI：你喜欢吃火锅。
```

### 7.2 完整验证清单

| # | 测试项 | 操作 | 预期 |
|---|--------|------|------|
| 1 | 消息写入 | 说任意内容 | `memory_write` 返回 `status: queued` |
| 2 | 即时搜索 | 说完立刻追问 | pending queue 兜底，能立即搜到 |
| 3 | AOSS 搜索 | 等 80 秒后搜索 | OpenSearch 命中 |
| 4 | 中文搜索 | 搜索中文关键词 | ICU 分词正确命中 |
| 5 | 语义搜索 | 用近义词搜索 | kNN 向量匹配 |
| 6 | 文件索引 | 编辑 MEMORY.md | 30 秒后可搜到新内容 |
| 7 | 系统状态 | 调用 `memory_stats` | 返回文档数量和健康状态 |

### 7.3 检查 MCP Server 日志

```bash
tail -f ~/.openclaw/memory/memory-v2.log
```

---

## 8. 配置参数说明

### 8.1 生产环境推荐配置

部署后修改 `config.py` 中的以下参数（从测试值改为生产值）：

| 参数 | 测试值 | 生产值 | 说明 |
|------|--------|--------|------|
| `BATCH_MAX_WAIT_SECS` | 1.0 | **2.0** | 写入 batch 间隔 |
| `INDEX_POLL_INTERVAL_SECS` | 5 | **30** | 文件轮询间隔 |
| `TTL_DAYS[message]` | 1 | **7** | 短期消息保留天数 |
| `TTL_DAYS[session_summary]` | 3 | **30** | 会话摘要保留天数 |
| `TTL_DAYS[daily_note]` | 2 | **14** | 日常笔记保留天数 |
| `SEARCH_OVERSAMPLE_FACTOR` | 10 | **4** | 搜索过采样（测试需高值因 AOSS 重复） |

### 8.2 多 Agent 配置

如果有多个 agent，配置跨 agent 搜索白名单：

```bash
# 环境变量方式
OPENCLAW_MEMORY_EXCEPTION_AGENTS="xiaoxiami,dragonborn"

# 或在 config.py 中直接修改
EXCEPTION_AGENT_LIST = ["xiaoxiami", "dragonborn"]
```

白名单内的 agent 可以搜索所有 agent 的记忆；不在白名单内的只能搜自己的。

---

## 9. 运维与监控

### 9.1 三级告警

记忆系统通过 MCP tool 返回值传递告警，agent 会在回复中提醒用户：

| 级别 | 触发条件 | 说明 |
|------|----------|------|
| ℹ️ INFO | pending queue > 10 | 仅日志，不打扰 |
| ⚠️ WARNING | queue > 50 或 embed 降级 | 回复末尾附带提醒 |
| 🔴 CRITICAL | queue > 200 或 AOSS 持续不可用 >5 分钟 | 主动告知用户 |

### 9.2 降级策略

| 故障 | 降级行为 |
|------|----------|
| AOSS 不可用 | 消息写入 pending queue + WAL，搜索降级为 pending queue 文本匹配 |
| Bedrock Embed 不可用 | 纯文本写入（needs_embed=true），BM25 仍可搜，后台 5 分钟重试补刷 |
| MCP Server 崩溃 | 重启后 WAL 自动 replay，已写入消息不丢失 |

### 9.3 日志位置

| 日志 | 路径 |
|------|------|
| Memory MCP Server | `~/.openclaw/memory/memory-v2.log` |
| WAL 文件 | `~/.openclaw/memory/wal.jsonl` |
| Index 状态 | `~/.openclaw/memory/index-state.json` |
| Dreaming 报告 | workspace 中的 `DREAMS.md` |

### 9.4 常用检查命令

```bash
# 检查 AOSS 连接
python -c "from opensearch_client import OpenSearchClient; c = OpenSearchClient(); print(c.client.cat.indices(format='json'))"

# 检查文档数量
python -c "
from opensearch_client import OpenSearchClient
c = OpenSearchClient()
r = c.client.count(index='openclaw-memory')
print(f'Total docs: {r[\"count\"]}')
"

# 检查 WAL 积压
wc -l ~/.openclaw/memory/wal.jsonl

# 手动触发 Dreaming
python -m dreaming.runner --agent xiaoxiami
```

---

## 10. 故障排除

### 10.1 AOSS Collection 创建失败

**症状**：CloudFormation stack CREATE_FAILED

**排查**：
```bash
aws cloudformation describe-stack-events \
  --stack-name openclaw-memory \
  --query 'StackEvents[?ResourceStatus==`CREATE_FAILED`].[LogicalResourceId,ResourceStatusReason]' \
  --output table --region us-west-2
```

**常见原因**：
- 加密策略名称冲突（已有同名策略）→ 改 CollectionName 参数
- 服务限额（每账号最多 50 个 AOSS collection）

### 10.2 写入返回 400

**症状**：`mapper_parsing_exception`

**原因**：文档字段类型与 mapping 不匹配（如 `day` 字段不是 `yyyy-MM-dd` 格式）

**修复**：检查写入的文档数据，确保字段格式正确。

### 10.3 搜索返回 0 结果

**排查顺序**：
1. 确认文档已写入：`curl -XPOST $ENDPOINT/openclaw-memory/_count`
2. 确认 AOSS 索引完成：写入后等 **80 秒**
3. 检查 agent_id 过滤：非白名单 agent 只能搜自己的
4. 检查 search pipeline：`GET _search/pipeline/memory-search-pipeline`

### 10.4 Bedrock 调用失败

**症状**：`AccessDeniedException` 或 `ThrottlingException`

**排查**：
- 确认 Bedrock 模型已开通（控制台 → Bedrock → Model access）
- 确认 IAM Role 有 `bedrock:InvokeModel` 权限
- Throttle 情况下系统会自动降级并重试

### 10.5 AOSS 最终一致性

**须知**：AOSS 写入后 **60-75 秒**才能通过搜索 API 查到。这是 AOSS 固有特性，不可配置。

系统通过以下机制消除用户感知：
- **pending queue**：刚写入的消息立即可通过内存匹配搜到
- **forgotten_ids**：forget 操作通过内存黑名单即时生效
- **WAL**：崩溃恢复时 replay 到 pending queue

---

## 11. 资源清理

如需完全卸载：

```bash
# 1. 删除 CloudFormation stack（会删除 AOSS collection + IAM role）
aws cloudformation delete-stack --stack-name openclaw-memory --region us-west-2
aws cloudformation wait stack-delete-complete --stack-name openclaw-memory --region us-west-2

# 2. 删除本地文件
rm -rf ~/.openclaw/memory/
rm -rf openclaw-memory-v2/

# 3. 从 openclaw.json 移除 MCP 配置
# 手动编辑 openclaw.json，删除 "openclaw-memory" 段

# 4. 重启 gateway
openclaw gateway restart
```

---

## 12. 成本估算

| 资源 | 配置 | 月成本 |
|------|------|--------|
| OpenSearch Serverless | 2 OCU 最低消费 | ~$24 |
| Bedrock Titan Embed V2 | ~200 次/天 embedding | ~$0.50 |
| Bedrock Claude Sonnet | Dreaming 1 次/天 | ~$0.30 |
| **合计** | | **~$25/月** |

> AOSS 的 2 OCU 最低消费是主要成本。如果已有 AOSS collection 用于其他用途，可以共享 OCU，不额外增加。

---

## 附录：deploy.sh 做了什么

```
deploy.sh
  ├─ 1. aws cloudformation deploy       → 创建 AOSS + IAM
  ├─ 2. python3 -m venv + pip install   → 安装 Python 依赖
  ├─ 3. 等待 collection ACTIVE           → 最多 10 分钟
  ├─ 4. python setup_opensearch.py       → 创建索引 + 搜索 pipeline
  └─ 5. 输出 openclaw.json 配置片段      → 复制粘贴即可
```

---

## 附录：已验证的功能清单

| 类别 | 功能数 | 通过 | 说明 |
|------|--------|------|------|
| 基础功能 (F1-F9) | 9 | 9 ✅ | 写入、搜索、读取、索引、遗忘、更新、固定、状态、手动索引 |
| 搜索特性 (F10-F16) | 7 | 7 ✅ | BM25、kNN、中文分词、temporal decay、MMR、跨 agent、pending 一致性 |
| 写入特性 (F17-F21) | 5 | 5 ✅ | batch、幂等、embed 降级、WAL 持久化、WAL replay |
| Dreaming (F22-F26) | 5 | 5 ✅ | Light 提取、去重、Deep 评分、promote、冷启动 |
| 告警降级 (F27-F31) | 5 | 5 ✅ | 三级告警、搜索降级、embed 降级 |
| 文件同步 (F32-F34) | 3 | 3 ✅ | 白名单索引、增量索引、chunk 结构保持 |
| 数据迁移 (F35-F36) | 2 | 2 ✅ | DynamoDB 迁移、S3 Vectors 迁移 |
| 极端场景 (E1-E8) | 8 | 8 ✅ | 并发、链路、溢出、冲突、崩溃、故障、竞争、隔离 |

**总计：44/44 全部通过**

---

*文档版本: v1.0 | 日期: 2026-04-08 | 验证环境: us-west-2*
