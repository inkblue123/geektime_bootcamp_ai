# PostgreSQL MCP 服务器

一个生产级的 [Model Context Protocol (MCP)](https://modelcontextprotocol.io) 服务器，使用户能够通过自然语言与 PostgreSQL 数据库进行交互。该服务器基于 FastMCP 构建，将自然语言问题转换为安全的 SQL 查询，执行查询并验证结果。一些参考文档：

- Python Postgres MCP 需求研究
: <https://gemini.google.com/share/c87a73f0969b>
- SQLGlot 深度研究方案
: <https://gemini.google.com/share/cc5e45c76c8f>

## 功能特性

- **自然语言转 SQL**：使用 GPT-5.2-mini 将普通英文问题转换为优化的 PostgreSQL 查询
- **多数据库路由**：每个数据库拥有独立连接池、独立安全策略与独立执行器；请求中的 `database` 会被真正路由，绝不会静默落到别的库
- **安全至上**：只读强制执行、阻止危险函数、禁用表/列黑名单、EXPLAIN 策略、SQL 注入防护、查询超时与行数限制
- **结果验证**：基于 AI 的结果验证，提供置信度评分，低于阈值会在响应中标记 `low_confidence`
- **Schema 智能化**：自动 Schema 缓存，基于 TTL 的刷新机制
- **生产就绪**：连接池管理、熔断器、限流、重试/指数退避、全面的指标与请求链路追踪
- **可观测性**：Prometheus 指标、贯穿全链路的 `request_id`、`health_check` 健康检查工具
- **MCP 兼容**：支持 Claude Desktop 和任何 MCP 兼容客户端
- **完全离线可验证**：内置离线演示与真实 MCP stdio 冒烟测试，无需数据库与 API Key

## 运行截图

`docs/screenshots/` 中的 13 张截图由 `python demo/render_screenshots.py` 从真实运行结果渲染：

| 截图 | 内容 |
|------|------|
| `01` | 启动：多库配置 + 每库生效的安全策略 + 熔断器/限流器/重试策略 |
| `02` | 多库路由：同一个问题打到两个库，返回两份不同结果 |
| `03` | 自然语言分析查询：生成的 SQL、结果集、结果验证置信度 |
| `04` | 仅生成 SQL 模式（`return_type="sql"`） |
| `05` | 安全拦截：命中禁用表 → 带反馈重试 → 最终拒绝 |
| `06` | 安全拦截：EXPLAIN 策略 |
| `07` | 输入保护：`validation.max_question_length`（未调用 LLM） |
| `08` | 重试与指数退避：注入瞬时 LLM 失败后仍成功 |
| `09` | 并发限制：12 个并发请求通过 3 槽限流器 |
| `10` | Prometheus 指标（真实请求路径产生） |
| `11` | `health_check` 健康报告 |
| `12` | 真实 MCP stdio 协议：initialize / tools/list / tools/call |
| `13` | 测试套件结果（356 passed, 46 skipped，全离线） |


## 快速开始

### 前置条件

- Python 3.12+（本项目在 Python 3.14.7 上验证）
- PostgreSQL 12+
- OpenAI API 密钥（用于 GPT-5.2-mini）
- UV 包管理器（推荐）或 pip

> **只想先看效果？** 不需要 PostgreSQL 也不需要 API Key：
>
> ```bash
> python -m venv .venv && .venv\Scripts\activate
> pip install -e ".[dev]"
> python demo/offline_demo.py        # 完整链路的离线演示
> python demo/mcp_stdio_smoke.py     # 真实 MCP stdio 协议冒烟测试
> pytest                             # 确定性测试套件
> ```
>
> 详见 [QUICKSTART.md](QUICKSTART.md) 的「方式 A」。


### 安装

#### 使用 UV（推荐）

```bash
# 克隆仓库
git clone <repository-url>
cd pg-mcp

# 安装依赖
uv sync

# 复制环境配置模板
cp .env.example .env

# 编辑 .env 并配置参数
vi .env
```

#### 使用 pip

```bash
# 克隆仓库
git clone <repository-url>
cd pg-mcp

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Windows 系统: .venv\Scripts\activate

# 安装依赖
pip install -e .

# 复制环境配置模板
cp .env.example .env

# 编辑 .env 并配置参数
vi .env
```

### 配置

编辑 `.env` 文件以配置您的设置：

```bash
# 主数据库配置
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_NAME=your_database
DATABASE_USER=your_user
DATABASE_PASSWORD=your_password

# OpenAI 配置
OPENAI_API_KEY=sk-your-api-key-here
OPENAI_MODEL=gpt-5.2-mini

# 安全设置（可选，显示默认值）
SECURITY_ALLOW_WRITE_OPERATIONS=false
SECURITY_MAX_ROWS=10000
SECURITY_MAX_EXECUTION_TIME=30

# 敏感对象保护（可选，但强烈建议）
SECURITY_BLOCKED_TABLES=secret_data,payment_cards
SECURITY_BLOCKED_COLUMNS=password_hash,api_key
SECURITY_ALLOW_EXPLAIN=false

# 多数据库（可选）：以 JSON 数组声明额外数据库
DATABASES=[{"name":"analytics","host":"analytics.internal","user":"pg_mcp_ro","password":"secret"}]

# 按库追加限制（只能收紧，不能放宽）
SECURITY_PER_DATABASE={"analytics":{"blocked_tables":["pii_events"],"max_rows":500}}
```

完整的配置选项请参考 `.env.example`。

### 运行服务器

#### 离线演示（无需数据库与 API Key）

```bash
# 打印完整请求链路的 11 个场景
python demo/offline_demo.py

# 跑真实 MCP stdio 协议（真服务、真工具、假后端）
python demo/mcp_stdio_smoke.py

# 重新渲染 docs/screenshots 下的运行截图
python demo/render_screenshots.py
```

#### 独立模式

```bash
# 使用 UV
uv run python -m pg_mcp

# 或使用 pip 安装后的 console script
pg-mcp

# 或直接运行仓库根目录的入口
python main.py
```

> 服务器通过 **stdio** 讲 MCP 协议：stdout 只承载 JSON-RPC 数据帧，
> 日志统一写入 **stderr**，因此手动运行时终端上只会看到日志。

#### 与 Claude Desktop 集成

添加以下配置到 Claude Desktop MCP 设置文件：

**macOS/Linux**: `~/Library/Application Support/Claude/claude_desktop_config.json`

**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "postgres": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/pg-mcp",
        "run",
        "python",
        "main.py"
      ],
      "env": {
        "DATABASE_HOST": "localhost",
        "DATABASE_NAME": "your_database",
        "DATABASE_USER": "your_user",
        "DATABASE_PASSWORD": "your_password",
        "OPENAI_API_KEY": "sk-your-api-key-here"
      }
    }
  }
}
```

详细配置说明请参阅 [Claude Desktop 配置](#claude-desktop-配置)。

## 使用方法

### 示例查询

通过 Claude Desktop 或其他 MCP 客户端连接后，您可以提出自然语言问题：

#### 简单查询

```
How many tables are in the database?
→ SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'

Show me all users
→ SELECT * FROM users LIMIT 10000

What are the column names in the products table?
→ SELECT column_name, data_type FROM information_schema.columns
  WHERE table_name = 'products'
```

#### 分析查询

```
What are the top 10 products by sales?
→ SELECT product_name, SUM(quantity * price) as total_sales
  FROM orders
  GROUP BY product_name
  ORDER BY total_sales DESC
  LIMIT 10

How many users registered in the last 30 days?
→ SELECT COUNT(*) FROM users
  WHERE created_at > CURRENT_DATE - INTERVAL '30 days'
```

#### 仅 SQL 模式

您也可以只请求 SQL 而不执行：

```
Generate SQL to find duplicate emails
Return Type: sql
→ Returns: SELECT email, COUNT(*) FROM users GROUP BY email HAVING COUNT(*) > 1
```

### 返回类型

服务器支持两种返回类型：

- **`result`**（默认）：执行查询并返回结果
- **`sql`**：生成并验证 SQL，但不执行

### MCP 工具

| 工具 | 参数 | 说明 |
|------|------|------|
| `query` | `question`（必填）、`database`（可选）、`return_type`（`result`/`sql`） | 自然语言 → SQL → 安全校验 → 执行 → 结果验证 |
| `list_databases` | 无 | 列出所有可查询数据库名与默认选中项 |
| `health_check` | 无 | 返回整体健康状态、每库连接池与缓存、熔断器与限流器状态 |

### 响应格式

#### 成功查询响应

```json
{
  "success": true,
  "request_id": "req_6f353482c8a3",
  "database": "analytics",
  "generated_sql": "SELECT COUNT(*) AS user_count FROM users;",
  "validation": {
    "is_valid": true,
    "is_select": true,
    "allows_data_modification": false,
    "uses_blocked_functions": [],
    "error_message": null,
    "error_code": null
  },
  "data": {
    "columns": ["user_count"],
    "rows": [{"user_count": 1523}],
    "row_count": 1,
    "total_row_count": 1,
    "truncated": false,
    "execution_time_ms": 23.4
  },
  "result_validation": {
    "confidence": 92,
    "explanation": "The result set (1 row(s)) directly answers the question",
    "suggestion": null,
    "is_acceptable": true
  },
  "confidence": 92,
  "low_confidence": false,
  "tokens_used": 128
}
```

#### 仅 SQL 响应

```json
{
  "success": true,
  "request_id": "req_057d40982b33",
  "database": "sales",
  "generated_sql": "SELECT SUM(total_amount) AS revenue FROM orders;",
  "validation": { "is_valid": true, "is_select": true },
  "data": null,
  "confidence": 100,
  "tokens_used": 141
}
```

#### 错误响应

```json
{
  "success": false,
  "request_id": "req_4e7daf16a8f9",
  "database": "analytics",
  "error": {
    "code": "security_violation",
    "message": "Access to table 'secret_data' is not allowed",
    "details": {}
  },
  "confidence": 0,
  "tokens_used": 0
}
```

## 架构

### 核心组件

```
┌─────────────────────────────────────────────────────────────┐
│                      MCP Server (FastMCP)                   │
│        query  ·  list_databases  ·  health_check            │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    Query Orchestrator                       │
│  - Resolves the target database and routes to ITS components│
│  - Enforces question length / security policy / rate limits │
│  - Retry + backoff, circuit breaker, metrics, request_id    │
└─────────────────────────────────────────────────────────────┘
     │                    │                    │
     ▼                    ▼                    ▼
┌───────────┐     ┌────────────┐     ┌──────────────┐
│   SQL     │     │    SQL     │     │     SQL      │
│ Generator │────▶│ Validator  │────▶│  Executor    │
│ (LLM)     │     │ (per DB)   │     │ (per DB)     │
└───────────┘     └────────────┘     └──────────────┘
     │                                      │
     ▼                                      ▼
┌───────────┐                          ┌──────────────┐
│  Schema   │                          │   Result     │
│  Cache    │                          │  Validator   │
└───────────┘                          │  (LLM)       │
                                       └──────────────┘

每个数据库一份：连接池 / SecurityConfig（effective_for）/ SQLValidator / SQLExecutor
全服务共享一份：SQLGenerator、ResultValidator、MetricsCollector、
                CircuitBreaker、MultiRateLimiter（限流 + 重试策略从中读取）
```

### 安全特性

1. **只读强制执行**：默认仅允许 SELECT；`SECURITY_ALLOW_WRITE_OPERATIONS=false` 时查询还会运行在只读事务中
2. **阻止危险函数**：内置黑名单（pg_sleep、pg_read_file、dblink 等）与 `SECURITY_BLOCKED_FUNCTIONS` 合并
3. **禁用表**：`SECURITY_BLOCKED_TABLES` 支持裸名（`secret_data`）与 schema 限定名（`public.secret_data`），
   同时检查 JOIN、子查询与 CTE 中的引用
4. **禁用列**：`SECURITY_BLOCKED_COLUMNS` 支持裸名、`table.column` 与 `schema.table.column`
5. **EXPLAIN 策略**：默认禁止（会泄露执行计划），通过 `SECURITY_ALLOW_EXPLAIN=true` 显式开启
6. **按库策略**：`SECURITY_PER_DATABASE` 为单个库追加限制；限制只能收紧不能放宽
7. **SQL 解析**：使用 sqlglot 进行准确的结构校验，拒绝多语句与嵌套写操作
8. **输入保护**：`VALIDATION_MAX_QUESTION_LENGTH` 在任何 LLM 调用之前拦截超长输入
9. **资源限制**：
   - 行数限制（默认：10,000）
   - 查询超时（默认：30 秒，`statement_timeout`）
   - 强制 `search_path`
   - `SET ROLE` 切换到只读角色
   - 连接池管理
10. **事务隔离**：所有查询在只读事务中运行

### 弹性特性

- **重试与指数退避**：`resilience/retry.py` 提供 `RetryPolicy` / `retry_async` / `with_retry`；
  第 N 次重试等待 `min(RETRY_DELAY * BACKOFF_FACTOR^N, MAX_RETRY_DELAY)`，叠加 `RETRY_JITTER` 抖动；
  只对瞬时错误（超时、连接类错误）重试，安全违规与语法错误立即失败
- **熔断器**：LLM 连续失败达到 `CIRCUIT_BREAKER_THRESHOLD` 后快速失败，
  `CIRCUIT_BREAKER_TIMEOUT` 后半开重试；全服务共享同一个实例
- **限流**：`MultiRateLimiter` 分别限制并发数据库操作与并发 LLM 调用，
  拿不到槽位的请求快速返回 `rate_limit_exceeded`
- **连接池**：所有数据库的连接池并发创建、统一优雅关闭
- **Schema 缓存**：基于 TTL 的缓存减少数据库元数据查询


## 配置参考

### 数据库设置

| 变量                       | 描述            | 默认值      |
|----------------------------|-----------------|-------------|
| `DATABASE_HOST`            | PostgreSQL 主机 | `localhost` |
| `DATABASE_PORT`            | PostgreSQL 端口 | `5432`      |
| `DATABASE_NAME`            | 数据库名称      | 必需        |
| `DATABASE_USER`            | 数据库用户      | 必需        |
| `DATABASE_PASSWORD`        | 数据库密码      | 必需        |
| `DATABASE_MIN_POOL_SIZE`   | 池中最小连接数  | `5`         |
| `DATABASE_MAX_POOL_SIZE`   | 池中最大连接数  | `20`        |
| `DATABASE_COMMAND_TIMEOUT` | 查询超时（秒）    | `30`        |

### 多数据库设置

| 变量         | 描述                                                                 | 默认值 |
|--------------|----------------------------------------------------------------------|--------|
| `DATABASES`  | JSON 数组，声明额外数据库；每项包含 `name`/`host`/`port`/`user`/`password`/`min_pool_size`/`max_pool_size`/`pool_timeout`/`command_timeout`。名称必须唯一 | `[]`（单库模式） |

数据库名称即 `query` 工具的 `database` 参数；用 `list_databases` 工具查询可用名称。
未指定且只有一个库时自动选中，多个库时返回 `database_error` 并列出可选名称。

### OpenAI 设置

| 变量                 | 描述                    | 默认值         |
|----------------------|-------------------------|----------------|
| `OPENAI_API_KEY`     | OpenAI API 密钥         | 必需           |
| `OPENAI_MODEL`       | 使用的模型              | `gpt-5.2-mini` |
| `OPENAI_MAX_TOKENS`  | 每次请求的最大 token 数 | `32000`        |
| `OPENAI_TEMPERATURE` | 模型温度                | `0.0`          |
| `OPENAI_TIMEOUT`     | API 超时（秒）            | `30`           |
| `OPENAI_BASE_URL`    | 兼容 OpenAI 的代理地址  | 未设置         |

### 安全设置

| 变量                              | 描述                                        | 默认值            |
|-----------------------------------|---------------------------------------------|-------------------|
| `SECURITY_ALLOW_WRITE_OPERATIONS` | 允许 INSERT/UPDATE/DELETE/MERGE             | `false`           |
| `SECURITY_BLOCKED_FUNCTIONS`      | 逗号分隔或 JSON 数组的函数黑名单            | 参考 `.env.example` |
| `SECURITY_BLOCKED_TABLES`         | 禁用表（裸名或 schema 限定名）              | `[]`              |
| `SECURITY_BLOCKED_COLUMNS`        | 禁用列（裸名 / `table.col` / `schema.table.col`） | `[]`          |
| `SECURITY_ALLOW_EXPLAIN`          | 允许 EXPLAIN                                | `false`           |
| `SECURITY_MAX_ROWS`               | 每个查询的最大返回行数                      | `10000`           |
| `SECURITY_MAX_EXECUTION_TIME`     | 查询超时（秒）                                | `30`              |
| `SECURITY_READONLY_ROLE`          | 强制 `SET ROLE` 的只读角色                  | 未设置            |
| `SECURITY_SAFE_SEARCH_PATH`       | 强制的 `search_path`                        | `public`          |
| `SECURITY_PER_DATABASE`           | 按库追加的限制（JSON 对象）                 | `{}`              |

### 缓存设置

| 变量               | 描述                | 默认值 |
|--------------------|---------------------|--------|
| `CACHE_ENABLED`    | 启用 Schema 缓存    | `true` |
| `CACHE_SCHEMA_TTL` | Schema 缓存 TTL（秒） | `3600` |
| `CACHE_MAX_SIZE`   | 最大缓存 Schema 数  | `100`  |

### 弹性设置

| 变量                                   | 描述                          | 默认值 |
|----------------------------------------|-------------------------------|--------|
| `RESILIENCE_MAX_RETRIES`               | 最大重试次数                  | `3`    |
| `RESILIENCE_RETRY_DELAY`               | 初始重试延迟（秒）              | `1.0`  |
| `RESILIENCE_BACKOFF_FACTOR`            | 指数退避倍数                  | `2.0`  |
| `RESILIENCE_MAX_RETRY_DELAY`           | 单次重试延迟上限（秒）          | `30.0` |
| `RESILIENCE_RETRY_JITTER`              | 抖动比例（0~1）               | `0.1`  |
| `RESILIENCE_CIRCUIT_BREAKER_THRESHOLD` | 熔断前的失败数                | `5`    |
| `RESILIENCE_CIRCUIT_BREAKER_TIMEOUT`   | 熔断器超时（秒）                | `60`   |
| `RESILIENCE_RATE_LIMIT_ENABLED`        | 启用并发限流                  | `true` |
| `RESILIENCE_MAX_CONCURRENT_QUERIES`    | 并发数据库操作上限            | `10`   |
| `RESILIENCE_MAX_CONCURRENT_LLM_CALLS`  | 并发 LLM 调用上限             | `5`    |
| `RESILIENCE_RATE_LIMIT_TIMEOUT`        | 等待限流槽位的超时（秒）        | `10.0` |

### 验证设置

| 变量                               | 描述                                     | 默认值 |
|------------------------------------|------------------------------------------|--------|
| `VALIDATION_MAX_QUESTION_LENGTH`   | 问题最大长度（LLM 调用前校验）           | `10000` |
| `VALIDATION_MIN_CONFIDENCE_SCORE`  | 低于该置信度时标记 `low_confidence=true` | `70`   |
| `VALIDATION_ENABLED`               | 启用 LLM 结果验证                        | `true` |
| `VALIDATION_SAMPLE_ROWS`           | 送给验证器的样本行数                     | `5`    |
| `VALIDATION_TIMEOUT_SECONDS`       | 结果验证超时（秒）                       | `10`   |
| `VALIDATION_CONFIDENCE_THRESHOLD`  | `is_acceptable` 的最低置信度             | `70`   |

### 可观测性设置

| 变量                            | 描述                 | 默认值 |
|---------------------------------|----------------------|--------|
| `OBSERVABILITY_METRICS_ENABLED` | 启用 Prometheus 指标 | `true` |
| `OBSERVABILITY_METRICS_PORT`    | 指标 HTTP 端口       | `9090` |
| `OBSERVABILITY_LOG_LEVEL`       | 日志级别             | `INFO` |
| `OBSERVABILITY_LOG_FORMAT`      | 日志格式（json/text）  | `json` |

## 开发

### 设置开发环境

```bash
# 安装开发依赖
uv sync --all-extras

# 安装 pre-commit 钩子（可选）
pre-commit install
```

### 运行测试

```bash
# 运行所有测试（集成/E2E 用例在没有真实 PostgreSQL + OpenAI 时会自动跳过）
uv run pytest

# 需要真实环境时显式开启
PG_MCP_LIVE_TESTS=1 uv run pytest -m integration

# 运行并生成覆盖率报告
uv run pytest --cov=src --cov-report=html

# 运行特定测试类别
uv run pytest tests/unit/          # 仅单元测试
uv run pytest tests/integration/   # 集成测试
uv run pytest tests/e2e/           # 端到端测试
uv run pytest -m integration       # 标记为集成的测试
```

### 代码质量

```bash
# 类型检查
uv run mypy src

# Lint 和格式化
uv run ruff check --fix .
uv run ruff format .

# 运行所有质量检查
uv run pytest --cov=src --cov-fail-under=80
uv run mypy src
uv run ruff check .
```

### 项目结构

```
pg-mcp/
├── src/pg_mcp/
│   ├── cache/              # Schema 缓存
│   ├── config/             # 配置管理（多库 + 安全策略 + 弹性 + 可观测性）
│   ├── db/                 # 数据库连接池（并发创建、优雅关闭）
│   ├── models/             # 数据模型
│   ├── observability/      # 日志、指标、追踪、健康检查
│   ├── prompts/            # LLM Prompt 模板
│   ├── resilience/         # 熔断器、限流器、重试/退避
│   ├── services/           # 核心业务逻辑
│   │   ├── orchestrator.py      # 查询协调（多库路由 + 安全 + 弹性 + 指标）
│   │   ├── sql_generator.py     # 基于 LLM 的 SQL 生成
│   │   ├── sql_validator.py     # 安全验证（analyze / validate_or_raise）
│   │   ├── sql_executor.py      # 查询执行（会话加固 + 序列化 + 指标）
│   │   └── result_validator.py  # 结果验证
│   └── server.py           # FastMCP 服务器（query / list_databases / health_check）
├── tests/
│   ├── conftest.py         # 共享 fixtures（未开 PG_MCP_LIVE_TESTS 时跳过联外测试）
│   ├── unit/               # 单元测试
│   ├── integration/        # 集成测试（需要真实 PostgreSQL）
│   └── e2e/                # 端到端测试（需要真实环境）
├── demo/                   # 离线演示、MCP stdio 冒烟测试、截图渲染
├── docs/screenshots/       # 运行截图（PNG）
├── fixtures/               # 测试数据库 fixture
├── .env.example            # 环境模板
├── pyproject.toml          # 项目配置
└── main.py                 # 入口点
```

## Docker 部署

### 构建镜像

```bash
docker build -t pg-mcp:latest .
```

### 运行容器

```bash
docker run -d \
  --name pg-mcp \
  -e DATABASE_HOST=your-db-host \
  -e DATABASE_NAME=your-db \
  -e DATABASE_USER=your-user \
  -e DATABASE_PASSWORD=your-password \
  -e OPENAI_API_KEY=sk-your-key \
  -p 9090:9090 \
  pg-mcp:latest
```

### Docker Compose

```bash
# 启动所有服务（PostgreSQL + pg-mcp）
docker-compose up -d

# 查看日志
docker-compose logs -f pg-mcp

# 停止服务
docker-compose down
```

详细配置参考 `docker-compose.yml`。

## 监控

### 指标

服务器在端口 9090（可配置）上暴露 Prometheus 指标：

```bash
curl http://localhost:9090/metrics
```

**可用指标：**

- `pg_mcp_query_requests_total{status,database}` - 已处理的总查询数（按状态与数据库）
- `pg_mcp_query_duration_seconds` - 端到端查询处理时间直方图
- `pg_mcp_llm_calls_total{operation}` - LLM 调用次数（`generate` / `validate`）
- `pg_mcp_llm_latency_seconds{operation}` - LLM 调用延迟
- `pg_mcp_llm_tokens_used_total{operation}` - LLM token 使用总数
- `pg_mcp_sql_rejected_total{reason}` - 安全拦截计数
  （`blocked_table` / `blocked_column` / `blocked_function` / `explain_denied` /
  `write_operation` / `statement_type` / `parse_error`）
- `pg_mcp_db_query_duration_seconds` - 数据库执行时间直方图
- `pg_mcp_db_connections_active{database}` - 每库活跃连接数
- `pg_mcp_schema_cache_age_seconds{database}` - Schema 缓存新鲜度

每个 `MetricsCollector` 拥有独立的 `CollectorRegistry`，因此 `reset_all_metrics()`
可以真正清零（默认全局 registry 无法做到），测试之间也不会互相污染。

### 日志

结构化 JSON 日志（或文本格式）输出到 **标准错误**（stdout 保留给 MCP 数据帧）：

```json
{
  "timestamp": "2025-12-20T10:30:00.123Z",
  "level": "INFO",
  "message": "SQL executed successfully",
  "request_id": "req_6f353482c8a3",
  "database": "mydb",
  "execution_time_ms": 23.0,
  "row_count": 42
}
```

`request_id` 由 `RequestContextFilter` 从 contextvars 自动注入到**每一条**日志，
无需调用方手动传递；同一个 `request_id` 也会出现在 `query` 工具的响应中，
便于把 MCP 客户端看到的结果与服务端日志对上。

### 健康检查

`health_check` 工具返回：

```json
{
  "status": "healthy",
  "service": "pg-mcp",
  "environment": "development",
  "uptime_seconds": 12.5,
  "metrics_enabled": true,
  "databases": [
    {
      "name": "analytics",
      "host": "analytics.internal",
      "reachable": true,
      "pool_size": 4,
      "pool_idle": 3,
      "schema_cached": true,
      "schema_cache_age_seconds": 12.5
    }
  ],
  "circuit_breaker": { "state": "closed", "failure_count": 0 },
  "rate_limiter": { "queries": { "max_concurrent": 10 }, "llm": { "max_concurrent": 5 } }
}
```

`status` 语义：所有库可达为 `healthy`；部分库不可达或熔断器打开为 `degraded`；
没有任何库可达为 `unhealthy`。

## 故障排查

### 常见问题

#### 连接被拒绝

```
Error: Connection to database failed
```

**解决方案**：验证 PostgreSQL 正在运行且凭证正确：

```bash
psql -h $DATABASE_HOST -U $DATABASE_USER -d $DATABASE_NAME
```

#### OpenAI API 错误

```
Error: OpenAI API request failed
```

**解决方案**：

1. 检查 API 密钥是否有效且有额度
2. 验证网络连接
3. 如果请求超时，检查 `OPENAI_TIMEOUT` 设置

#### 查询超时

```
Error: Query execution timeout exceeded
```

**解决方案**：

1. 增加 `SECURITY_MAX_EXECUTION_TIME`
2. 优化数据库（添加索引、VACUUM）
3. 简化查询或添加过滤条件

#### Schema 缓存问题

```
Error: Schema not found in cache
```

**解决方案**：

1. 重启服务器以重新加载 Schema
2. 验证数据库用户有 Schema 读取权限
3. 检查 `CACHE_ENABLED` 是否设置为 `true`

### 调试模式

启用调试日志：

```bash
export OBSERVABILITY_LOG_LEVEL=DEBUG
uv run python main.py
```

## Claude Desktop 配置

### macOS/Linux 配置

编辑 `~/Library/Application Support/Claude/claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "postgres": {
      "command": "uv",
      "args": [
        "--directory",
        "/Users/yourname/projects/pg-mcp",
        "run",
        "python",
        "main.py"
      ],
      "env": {
        "DATABASE_HOST": "localhost",
        "DATABASE_PORT": "5432",
        "DATABASE_NAME": "mydb",
        "DATABASE_USER": "postgres",
        "DATABASE_PASSWORD": "your-password",
        "OPENAI_API_KEY": "sk-your-api-key-here",
        "OPENAI_MODEL": "gpt-5.2-mini",
        "SECURITY_MAX_ROWS": "10000",
        "CACHE_ENABLED": "true",
        "OBSERVABILITY_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

### Windows 配置

编辑 `%APPDATA%\Claude\claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "postgres": {
      "command": "uv",
      "args": [
        "--directory",
        "C:\\Users\\YourName\\projects\\pg-mcp",
        "run",
        "python",
        "main.py"
      ],
      "env": {
        "DATABASE_HOST": "localhost",
        "DATABASE_NAME": "mydb",
        "DATABASE_USER": "postgres",
        "DATABASE_PASSWORD": "your-password",
        "OPENAI_API_KEY": "sk-your-api-key-here"
      }
    }
  }
}
```

### 使用 Python Virtualenv

如果不使用 UV，请直接配置 Python：

```json
{
  "mcpServers": {
    "postgres": {
      "command": "/absolute/path/to/pg-mcp/.venv/bin/python",
      "args": ["main.py"],
      "cwd": "/absolute/path/to/pg-mcp",
      "env": {
        "DATABASE_HOST": "localhost",
        ...
      }
    }
  }
}
```

### 重启 Claude Desktop

编辑配置后：

1. 完全退出 Claude Desktop
2. 重启 Claude Desktop
3. PostgreSQL MCP 服务器将可用

## 安全考虑

### 生产环境部署

1. **使用只读数据库用户**：创建专用 PostgreSQL 用户，仅具有 SELECT 权限：

```sql
CREATE USER pg_mcp_readonly WITH PASSWORD 'secure-password';
GRANT CONNECT ON DATABASE your_database TO pg_mcp_readonly;
GRANT USAGE ON SCHEMA public TO pg_mcp_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO pg_mcp_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO pg_mcp_readonly;
```

2. **保护 API 密钥**：使用环境变量或秘密管理系统，切勿提交到版本控制

3. **网络隔离**：在隔离网络中运行服务器，通过 IP 限制数据库访问

4. **监控使用**：启用指标并为异常模式设置告警

5. **限流**：配置合适的限流参数以防止滥用

6. **日志清理**：敏感数据会自动从日志中过滤

## 许可证

[您的许可证信息]

## 贡献

欢迎贡献！请参阅 CONTRIBUTING.md 了解指南。

## 支持

如有问题和疑问：

- GitHub Issues：[repository-url]/issues
- 文档：查看 `specs/w5/` 目录获取详细设计文档

## 致谢

- 基于 [FastMCP](https://github.com/jlowin/fastmcp) 构建
- SQL 解析由 [sqlglot](https://github.com/tobymao/sqlglot) 提供
- 数据库驱动：[asyncpg](https://github.com/MagicStack/asyncpg)
