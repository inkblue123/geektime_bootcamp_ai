# PostgreSQL MCP 服务器 - 快速入门指南

本指南给出**三种**运行方式，从「零依赖看效果」到「接入真实数据库」。

| 方式 | 需要 PostgreSQL | 需要 OpenAI Key | 用途 |
|------|----------------|-----------------|------|
| [方式 A：离线演示](#方式-a离线演示零外部依赖-30-秒) | ❌ | ❌ | 立刻看到完整链路效果 |
| [方式 B：接入真实数据库](#方式-b接入真实-postgresql--openai) | ✅ | ✅ | 实际使用 |
| [方式 C：Docker Compose](#方式-cdocker-compose) | 容器自带 | ✅ | 一键起 PostgreSQL + 服务 |

---

## 前置条件

- **Python 3.12+**（本项目在 Python 3.14.7 上验证）
- 方式 B/C 还需要：PostgreSQL 12+、OpenAI API Key

---

## 方式 A：离线演示（零外部依赖，30 秒）

不需要数据库、不需要 API Key。演示脚本用内存中的数据库替身和规则化的 LLM 替身，
驱动**真实的生产代码路径**（多库路由、安全校验、重试退避、限流、指标、健康检查）。

```bash
# 1) 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux

pip install -e ".[dev]"

# 2) 跑离线演示（打印 11 个场景的完整链路）
python demo/offline_demo.py

# 3) 跑真实的 MCP stdio 协议冒烟测试（真服务、真工具、假后端）
python demo/mcp_stdio_smoke.py

# 4) 跑测试套件（确定性、全离线）
pytest
```

演示输出见 [`docs/screenshots/`](docs/screenshots/) —— 13 张运行截图，
由 `python demo/render_screenshots.py` 从真实运行结果渲染而成。

---

## 方式 B：接入真实 PostgreSQL + OpenAI

### 步骤 1: 安装依赖

```bash
# 使用 uv（推荐）
uv sync --all-extras

# 或使用 pip
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

### 步骤 2: 配置环境

```bash
cp .env.example .env
```

**最小必需配置**（`.env`）：

```bash
# 数据库
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_NAME=your_database
DATABASE_USER=your_user
DATABASE_PASSWORD=your_password

# OpenAI
OPENAI_API_KEY=sk-your-api-key-here
OPENAI_MODEL=gpt-5.2-mini
```

**推荐再补上安全策略**（这是本次补全的核心能力之一）：

```bash
# 敏感对象保护
SECURITY_BLOCKED_TABLES=secret_data,payment_cards
SECURITY_BLOCKED_COLUMNS=password_hash,api_key
SECURITY_ALLOW_EXPLAIN=false

# 多数据库：额外数据库以 JSON 数组声明，每个库有独立连接池与安全策略
DATABASES=[{"name":"analytics","host":"analytics.internal","user":"pg_mcp_ro","password":"secret"}]

# 第二个库的额外限制（限制只能收紧，不能放宽）
SECURITY_PER_DATABASE={"analytics":{"blocked_tables":["pii_events"],"max_rows":500}}
```

### 步骤 3: 验证数据库连接

```bash
psql -h localhost -U your_user -d your_database
```

### 步骤 4: 运行服务器

```bash
# 任选其一，效果相同（服务器通过 stdio 讲 MCP 协议）
python -m pg_mcp
python main.py
pg-mcp              # 通过 pip install -e . 安装的 console script
```

> **注意**：日志输出到 **stderr**，stdout 只承载 MCP JSON-RPC 数据帧。

### 步骤 5: 接入 MCP 客户端（Claude Desktop 等）

**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
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
        "-m",
        "pg_mcp"
      ],
      "env": {
        "DATABASE_HOST": "localhost",
        "DATABASE_NAME": "your_database",
        "DATABASE_USER": "your_user",
        "DATABASE_PASSWORD": "your_password",
        "OPENAI_API_KEY": "sk-your-api-key",
        "SECURITY_BLOCKED_TABLES": "secret_data",
        "SECURITY_BLOCKED_COLUMNS": "password_hash,api_key"
      }
    }
  }
}
```

**重启客户端**后即可提问：

```
我的数据库中有多少张表？
上个季度销售额最高的 10 个产品是什么？
```

---

## 方式 C：Docker Compose

```bash
docker compose up -d          # 启动 PostgreSQL + pg-mcp
docker compose logs -f pg-mcp # 查看日志
docker compose down           # 停止
```

---

## 三个 MCP 工具

| 工具 | 参数 | 说明 |
|------|------|------|
| `query` | `question`（必填）、`database`（可选）、`return_type`（`result`/`sql`） | 自然语言 → SQL → 校验 → 执行 → 结果验证 |
| `list_databases` | 无 | 列出可查询的数据库名，以及是否需要显式指定 |
| `health_check` | 无 | 返回 `healthy`/`degraded`/`unhealthy` 及每库连接池、熔断器、限流器状态 |

### `query` 响应示例（成功）

```json
{
  "success": true,
  "request_id": "req_6f353482c8a3",
  "database": "analytics",
  "generated_sql": "SELECT COUNT(*) AS user_count FROM users;",
  "validation": { "is_valid": true, "is_select": true, "allows_data_modification": false },
  "data": {
    "columns": ["user_count"],
    "rows": [{"user_count": 1523}],
    "row_count": 1,
    "total_row_count": 1,
    "truncated": false,
    "execution_time_ms": 11.4
  },
  "result_validation": { "confidence": 92, "is_acceptable": true, "explanation": "..." },
  "confidence": 92,
  "low_confidence": false,
  "tokens_used": 128
}
```

### `query` 响应示例（被安全策略拦截）

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

---

## 常见错误码

| code | 含义 | 处理方式 |
|------|------|----------|
| `question_too_long` | 问题超过 `VALIDATION_MAX_QUESTION_LENGTH` | 缩短问题或调大上限 |
| `security_violation` | 命中禁用表/列/函数、EXPLAIN 策略或写操作策略 | 调整 `SECURITY_*` 或改写问题 |
| `sql_parse_error` | 生成的 SQL 无法解析 | 换个说法重试 |
| `database_error` / `database_connection_error` | 数据库不可达、库名不存在 | 检查配置与 `list_databases` |
| `rate_limit_exceeded` | 并发超过 `RESILIENCE_MAX_CONCURRENT_*` | 稍后重试或调大并发上限 |
| `llm_unavailable` | LLM 连续失败触发熔断 | 等待 `CIRCUIT_BREAKER_TIMEOUT` 后自动恢复 |
| `execution_timeout` | 查询超过 `SECURITY_MAX_EXECUTION_TIME` | 优化查询或调大超时 |

---

## 故障排查

### "Connection refused" 连接被拒绝

- 检查 PostgreSQL 是否运行：`pg_isready`
- 校验 `.env` 中的凭据
- 用 `health_check` 看每个库的 `reachable` 与连接池状态

### "OpenAI API error" OpenAI API 错误

- 校验 API Key 与余额：<https://platform.openai.com/usage>
- 调用 `health_check` 查看 `circuit_breaker.state`：若为 `open`，说明已熔断，等待后自动半开重试

### "Port already in use" 端口被占用

- 指标端口 9090 被占用：修改 `OBSERVABILITY_METRICS_PORT`，或 `OBSERVABILITY_METRICS_ENABLED=false`

### 请求总是报 `security_violation`

- 用 `SECURITY_PER_DATABASE` 检查是否为该库额外加了限制
- 确认 `SECURITY_ALLOW_EXPLAIN`：默认禁止 `EXPLAIN`

### 日志里看不到 request_id

- `request_id` 会写入每条日志；JSON 格式下是 `request_id` 字段，文本格式下是 `[request_id=...]`
- 若日志被 MCP 客户端吞掉，请查看 **stderr**

---

## 监控

```bash
curl http://localhost:9090/metrics
```

关键指标：

- `pg_mcp_query_requests_total{status,database}` — 按状态与数据库分的请求数
- `pg_mcp_query_duration_seconds` — 端到端处理耗时
- `pg_mcp_llm_calls_total{operation}` / `pg_mcp_llm_latency_seconds{operation}` — LLM 调用
- `pg_mcp_llm_tokens_used_total{operation}` — token 消耗
- `pg_mcp_sql_rejected_total{reason}` — 安全拦截计数（`blocked_table` / `blocked_column` / `blocked_function` / `explain_denied` / `write_operation` …）
- `pg_mcp_db_query_duration_seconds` — 数据库执行耗时
- `pg_mcp_schema_cache_age_seconds{database}` — schema 缓存新鲜度

---

## 下一步

- 完整文档：[README.md](README.md)
- 全部配置项：[.env.example](.env.example)
- 本次补全的对照说明：[docs/REVIEW_RESOLUTION.md](docs/REVIEW_RESOLUTION.md)
- 运行截图：[docs/screenshots/](docs/screenshots/)
- Claude Desktop 集成：[CLAUDE_DESKTOP_SETUP.md](CLAUDE_DESKTOP_SETUP.md)

## 安全提醒

生产环境使用时：

1. ✅ 使用只读数据库用户，并设置 `SECURITY_READONLY_ROLE`
2. ✅ 保持 `SECURITY_ALLOW_WRITE_OPERATIONS=false`（同时启用只读事务）
3. ✅ 用 `SECURITY_BLOCKED_TABLES` / `SECURITY_BLOCKED_COLUMNS` 保护敏感对象
4. ✅ 保持 `SECURITY_ALLOW_EXPLAIN=false`
5. ✅ 安全存储凭据（生产环境不要用 `.env` 文件）
6. ✅ 启用监控与告警

---

**准备就绪！** 开始用自然语言提问你的数据库吧。
