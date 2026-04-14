# OpenClaw Memory System

> 基于 AWS OpenSearch Serverless + Bedrock 的 AI Agent 记忆系统，作为 [OpenClaw](https://github.com/openclaw/openclaw) MCP Server 运行。

## 目录

- [功能特性](#功能特性)
- [架构](#架构)
- [快速开始](#快速开始)
- [部署](#部署)
  - [前提条件](#前提条件)
  - [方式一：一键部署（推荐）](#方式一一键部署推荐)
  - [方式二：手动部署](#方式二手动部署)
- [配置](#配置)
  - [OpenClaw MCP 配置](#openclaw-mcp-配置)
  - [环境变量](#环境变量)
  - [Agent Workspace 配置](#agent-workspace-配置)
- [自动化维护](#自动化维护)
  - [Dreaming 记忆整理](#dreaming-记忆整理)
  - [复盘 Weekly Review](#复盘-weekly-review)
  - [灾难恢复](#灾难恢复)
- [MCP Tools 参考](#mcp-tools-参考)
- [成本](#成本)
- [目录结构](#目录结构)
- [删除](#删除)
- [License](#license)

---

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

## 快速开始

```bash
# 克隆 → 部署 → 配置 → 测试（约 10 分钟）
git clone https://github.com/XHe-AWS/OpenClaw-Memory-on-AWS-OpenSearch.git
cd OpenClaw-Memory-on-AWS-OpenSearch
chmod +x deploy.sh && ./deploy.sh us-west-2 openclaw-memory
# 按输出提示将 MCP 配置添加到 openclaw.json，然后：
openclaw gateway restart
# 测试：对话中说 "记住我喜欢吃火锅"，再问 "我喜欢吃什么？"
```

---

## 部署

### 前提条件

- AWS 账号，已开通 Bedrock 模型访问：**Amazon Titan Embed Text V2** + **Anthropic Claude Sonnet**
- 运行 OpenClaw 的 EC2 实例（t4g.medium 或以上），Python 3.9+

**IAM 权限（部署完成后，最小权限）：**

<details>
<summary>点击展开 IAM Policy</summary>

> 💡 部署时可临时给 EC2 Role 加 `AdministratorAccess`，部署完成后收窄为以下最小权限。

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "aoss:APIAccessAll",
      "Resource": "arn:aws:aoss:*:*:collection/*"
    },
    {
      "Effect": "Allow",
      "Action": "bedrock:InvokeModel",
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/amazon.titan-embed-text-v2:0",
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
        "arn:aws:bedrock:*:*:inference-profile/us.anthropic.*"
      ]
    }
  ]
}
```

</details>

### 方式一：一键部署（推荐）

```bash
git clone https://github.com/XHe-AWS/OpenClaw-Memory-on-AWS-OpenSearch.git
cd OpenClaw-Memory-on-AWS-OpenSearch
chmod +x deploy.sh
./deploy.sh us-west-2 openclaw-memory
```

deploy.sh 自动完成：CloudFormation 部署 → 等待 AOSS ACTIVE → 创建索引 → 创建搜索 pipeline → 输出 MCP 配置。

按输出提示将配置添加到 `openclaw.json`（见下方 [OpenClaw MCP 配置](#openclaw-mcp-配置)），然后重启 gateway。

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

#### 6. 配置 OpenClaw 并重启

见下方 [OpenClaw MCP 配置](#openclaw-mcp-配置)。

---

## 配置

### OpenClaw MCP 配置

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

### 环境变量

通过环境变量覆盖默认配置（在 openclaw.json 的 `env` 中设置）：

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `OPENCLAW_MEMORY_OPENSEARCH_ENDPOINT` | （必填） | AOSS endpoint URL |
| `OPENCLAW_MEMORY_OPENSEARCH_REGION` | `us-west-2` | AWS region |
| `OPENCLAW_MEMORY_EMBED_MODEL` | `amazon.titan-embed-text-v2:0` | 嵌入模型 |
| `OPENCLAW_MEMORY_EXTRACT_MODEL` | `us.anthropic.claude-sonnet-4-6` | Dreaming 用的 LLM |
| `OPENCLAW_MEMORY_EXCEPTION_AGENTS` | `xiaoxiami` | 跨 agent 搜索白名单（逗号分隔） |

### Agent Workspace 配置

部署 MCP Server 后，还需要在 agent 的 workspace 文件中添加记忆系统的使用规则，否则 agent 有工具但不知道要用。

在 `IDENTITY.md`、`AGENTS.md` 或其他 agent 会读取的系统文件中，添加以下规则：

```markdown
## MCP Memory 使用规则

如果 `openclaw-memory` MCP server 可用（`aws_memory_write`、`aws_memory_search`），遵守以下规则：

1. **回复前**：如果用户询问过去的事件/偏好/决策，先调用 `aws_memory_search` 搜索相关记忆。
2. **收到用户消息后**：调用 `aws_memory_write`，记录用户消息摘要。
   ```
   aws_memory_write(agent_id="<your-agent-id>", role="user", session_id=<session>, content=<摘要>)
   ```
3. **生成回复后**：调用 `aws_memory_write`，记录回复摘要。
   ```
   aws_memory_write(agent_id="<your-agent-id>", role="assistant", session_id=<session>, content=<摘要>)
   ```
4. **每个 turn 都必须执行**，不能因为"正在专注回答问题"而跳过写入。
5. 不需要手动写入 MEMORY.md 等文件，除非用户明确要求——MCP Memory + Dreaming 会自动处理长期记忆。
```

> 💡 将 `<your-agent-id>` 替换为 agent 名称。这段规则确保 agent 的每次对话都被记录到 OpenSearch，为后续 Dreaming 和复盘提供原始数据。没有写入，就没有记忆。

---

## 自动化维护

部署完成后，建议配置两个自动化任务来持续维护记忆质量：

| 任务 | 调度方式 | 频率 | 作用 |
|------|----------|------|------|
| **Dreaming** | 系统 crontab | 每天 | 三阶段自动提炼原始记忆（Light 提取 → REM 聚类 → Deep 评分+晋升） |
| **复盘** | OpenClaw cron | 每周 | Agent 回顾近期记忆，提炼教训，更新人格/记忆文件 |

### Dreaming 记忆整理

Dreaming 是 Python 脚本，对 OpenSearch 中的原始记忆做三阶段处理。通过系统 crontab 调度。

**添加 crontab：**

```bash
(crontab -l 2>/dev/null; echo "0 19 * * * cd /path/to/OpenClaw-Memory-on-AWS-OpenSearch && /path/to/OpenClaw-Memory-on-AWS-OpenSearch/.venv/bin/python -m dreaming.runner --agent <your-agent-id> >> /path/to/OpenClaw-Memory-on-AWS-OpenSearch/dreaming.log 2>&1") | crontab -
```

> 将 `/path/to/` 替换为实际安装路径，`<your-agent-id>` 替换为 agent 名称。`0 19 * * *` = 每天 UTC 19:00，按需调整。

**验证 & 手动测试：**

```bash
crontab -l                                              # 确认已添加
cd /path/to/OpenClaw-Memory-on-AWS-OpenSearch
.venv/bin/python -m dreaming.runner --agent <your-agent-id>  # 手动跑一次
tail -f dreaming.log                                    # 查看日志
```

### 复盘 Weekly Review

复盘运行在 OpenClaw 内部的 cron 系统中。Agent 在隔离 session 中自动执行——用自然语言 prompt 驱动，无需额外代码。

**OpenClaw cron vs 系统 crontab：**

| | OpenClaw Cron | 系统 Crontab |
|---|---|---|
| **执行者** | OpenClaw Agent | 操作系统 shell |
| **能力** | 调用所有 agent 工具（搜记忆、读写文件、发消息等） | 只能跑 shell 命令 |
| **上下文** | 知道自己是谁，能读 SOUL.md / MEMORY.md | 无上下文 |
| **输出** | 自动发消息通知用户 | 写日志 |

**创建方法：** 在与 agent 的对话中说"创建复盘 cron job"即可。也可手动配置：

```json
{
  "name": "weekly-review",
  "description": "每周复盘，回顾近期记忆，提炼教训更新系统文件",
  "schedule": {
    "kind": "cron",
    "expr": "0 20 * * 0",
    "tz": "UTC"
  },
  "sessionTarget": "isolated",
  "payload": {
    "kind": "agentTurn",
    "message": "<见下方 prompt>",
    "timeoutSeconds": 300
  },
  "delivery": {
    "mode": "announce"
  }
}
```

**参数说明：**
- `0 20 * * 0` = 每周日 UTC 20:00（按需调整）
- `sessionTarget: "isolated"` = 独立 session，不污染主对话
- `delivery.mode: "announce"` = 执行完自动通知结果
- `timeoutSeconds: 300` = 5 分钟超时

<details>
<summary><b>复盘 Prompt 参考（点击展开）</b></summary>

```text
你是 <agent-name>，正在执行每周复盘任务。

## 步骤

1. 用 aws_memory_search 搜索过去 7 天的记忆，分以下维度搜索：
   - query: "错误 失败 error failed" (days_back: 7)
   - query: "纠正 不对 actually wrong" (days_back: 7)
   - query: "教训 发现 学到 best practice" (days_back: 7)
   - query: "偏好 喜欢 不喜欢 preference" (days_back: 7)
   - query: "工具 tool 配置 config" (days_back: 7)

2. 读取当前系统文件：
   - SOUL.md
   - AGENTS.md
   - TOOLS.md
   - MEMORY.md

3. 分析并归类：
   - 反复出现的错误/被纠正的行为 → 提炼成规则
   - 新发现的用户偏好/事实 → 记录
   - 工具使用的新发现/坑 → 记录

4. 写入规则（严格遵守）：
   - 写之前先 read 最新文件内容
   - 用 edit 精确替换，不要用 write 全量覆盖
   - 检查是否已有类似条目：有则更新，无则追加
   - 如发现新结论与已有规则矛盾 → 不自动修改，记录到 memory/review-needed.md 等用户裁决
   - 行为规范 → SOUL.md
   - 工作流程改进 → AGENTS.md
   - 工具使用经验 → TOOLS.md
   - 事实/偏好/人物信息 → MEMORY.md

5. 输出本次复盘摘要，包括：
   - 搜索到多少条相关记忆
   - 做了哪些更新（文件名 + 改了什么）
   - 有无矛盾需要用户裁决
   - 如果没有值得更新的内容，说明原因
```

</details>

> 💡 核心思路：agent 按步骤搜索记忆 → 读取现有文件 → 对比分析 → 精确更新。自然语言描述即可，agent 自己编排工具调用。

### 灾难恢复

如果需要在新实例上恢复：

1. **MCP Server** — 重新 `git clone` + `pip install` + 配置 `openclaw.json`
2. **Dreaming** — 重新添加 crontab
3. **复盘** — 对 agent 说"创建复盘 cron job"，或将 JSON 写入 `~/.openclaw/cron/jobs.json`

**备份清单：**

| 资产 | 位置 | 重要性 |
|------|------|--------|
| Agent workspace | `~/.openclaw/workspace-*/` | ⭐⭐⭐ 不可替代 |
| OpenClaw 配置 | `~/.openclaw/openclaw.json` | ⭐⭐ 可重建但费时 |
| Cron 任务 | `~/.openclaw/cron/jobs.json` | ⭐ 可重建 |
| OpenSearch 数据 | AWS 云上 | 不受本地影响 |
| Session 历史 | `~/.openclaw/agents/*/sessions/` | 丢了无所谓 |

---

## MCP Tools 参考

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

---

## 成本

~$25/月（AOSS 2 OCU 最低消费 $24 + Bedrock 调用 ~$1）

---

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

---

## 删除

```bash
# 删除 AWS 资源
aws cloudformation delete-stack --stack-name openclaw-memory --region us-west-2

# 清理本地
rm -rf ~/.openclaw/memory/

# 移除 MCP 配置并重启
openclaw gateway restart
```

---

## License

MIT
