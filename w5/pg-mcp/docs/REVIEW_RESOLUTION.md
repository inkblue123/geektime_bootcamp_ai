# 代码评审问题闭环说明

本文逐条对照 `specs/w5/0006-pg-mcp-code-review.md` 中列出的问题，说明**改了什么、在哪、怎么验证**。

验证方式（全部离线可复现）：

```bash
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"

pytest -q          # 356 passed, 46 skipped
ruff check .
ruff format --check .
mypy src           # Success: no issues found in 32 source files

python demo/offline_demo.py       # 完整链路演示
python demo/mcp_stdio_smoke.py    # 真实 MCP stdio 协议
python demo/render_screenshots.py # 生成 docs/screenshots/*.png
```

---

## 一、多数据库与安全控制（评审 Priority 1）

### 1.1 请求可能落到错误的数据库

**评审原文**：`_resolve_database` 可能返回另一个库名，但 `QueryOrchestrator` 始终使用绑定默认库的单一执行器。

**修复**

| 位置 | 变更 |
|------|------|
| `src/pg_mcp/config/settings.py` | 新增 `Settings.databases: list[DatabaseConfig]`、`resolved_databases()`、`database_names()`、`get_database()`；校验名称唯一 |
| `src/pg_mcp/db/pool.py` | `create_pools()` 并发建池，任一失败则关闭已建池再抛出 `DatabaseConnectionError`；`create_pool()` 包裹为结构化错误 |
| `src/pg_mcp/services/orchestrator.py` | 构造参数改为 `sql_validators: Mapping[str, SQLValidator]` + `sql_executors: Mapping[str, SQLExecutor]`；`_get_validator()` / `_get_executor()` 按**解析后的库名**取组件，缺失即 `database_connection_error`，**不做任何回退** |
| `src/pg_mcp/server.py` | lifespan 为每个库建立独立连接池、独立 `SecurityConfig`、独立 `SQLValidator`、独立 `SQLExecutor`，一起注入 orchestrator |

**验证**

- `tests/unit/test_orchestrator.py::TestPerDatabaseRouting`
  - `test_executor_selected_by_database`：请求 `database="b"` 时只有 `b` 的执行器被调用
  - `test_validator_selected_by_database`：`open` 库放行、`locked` 库被安全策略拒绝
  - `test_missing_executor_is_reported`：没有执行器的库返回 `database_connection_error`，绝不回退
- `tests/unit/test_config_multidb.py`：多库解析、JSON 环境变量、重名拒绝
- 截图 `docs/screenshots/02-*.png`：同一个问题在两个库返回两份不同结果

### 1.2 敏感对象无法保护（blocked tables / columns / EXPLAIN）

**评审原文**：`SecurityConfig` 缺少 blocked table/column 与 allow_explain 字段；`SQLValidator` 以 `None, None, False` 构造。

**修复**

| 位置 | 变更 |
|------|------|
| `SecurityConfig` | 新增 `blocked_tables`、`blocked_columns`、`allow_explain`、`per_database: dict[str, DatabaseSecurityConfig]`；`effective_for(db)` 合并全局与按库策略（黑名单**只增不减**） |
| `new DatabaseSecurityConfig` | 按库覆盖：blocked_functions/tables/columns、allow_explain、max_rows、max_execution_time、readonly_role、safe_search_path |
| `SQLValidator` | 从 `SecurityConfig` 读取全部策略；表支持裸名与 `schema.table`；列支持裸名、`table.column`、`schema.table.column`；EXPLAIN 受 `allow_explain` 控制；`allow_write_operations` 生效（DDL 始终禁止） |
| `SQLExecutor` | 只读事务由 `allow_write_operations` 决定，不再硬编码 `readonly=True` |
| `server.py` | 每个库用 `settings.security.effective_for(db.name)` 构造 validator 与 executor，并记录生效策略 |

**验证**：`tests/unit/test_security_policy.py`（39 个用例全覆盖 disabled/enabled、JOIN/子查询/CTE 引用、schema 限定、函数名去 schema 前缀、EXPLAIN 策略、写策略、DDL 恒禁）。
截图 `docs/screenshots/05-*.png`、`06-*.png`。

### 1.3 输入长度与置信度阈值未生效

**修复**

- `orchestrator._check_question_length()` 在任何 LLM 调用**之前**校验 `ValidationConfig.max_question_length`，抛出新的 `QuestionTooLongError`（`ErrorCode.QUESTION_TOO_LONG`）。
- `ValidationConfig.min_confidence_score` 生效：结果置信度低于阈值时在响应中标记 `low_confidence: true`，并记一条 warning 日志（按设计"标记但仍返回"）。
- `QueryResponse` 新增 `result_validation` 与 `low_confidence` 字段，把 LLM 校验结论真正暴露给调用方。

**验证**：`tests/unit/test_orchestrator.py::TestQuestionLengthGuard`、`test_low_confidence_results_are_flagged`。
截图 `docs/screenshots/07-*.png`。

---

## 二、弹性与可观测性未接入请求路径（评审 Priority 2）

### 2.1 新增 `resilience/retry.py`

- `RetryPolicy`（`max_retries` / `initial_delay` / `backoff_factor` / `max_delay` / `jitter`），`from_config()` 从 `ResilienceConfig` 构建。
- `retry_async()` 与 `with_retry()` 装饰器；延迟 = `min(RETRY_DELAY * BACKOFF_FACTOR^N, MAX_RETRY_DELAY)` ± 抖动。
- `RETRYABLE_EXCEPTIONS` **只包含瞬时错误**（LLM 超时/不可用、数据库连接、执行超时、限流、`TimeoutError`、`ConnectionError`）；安全违规、SQL 语法错误、校验错误**立即失败**，不做无意义重试。

**验证**：`tests/unit/test_retry.py`（29 个用例：退避序列、上限、抖动边界、非法参数、不可重试错误、hook、装饰器）。
截图 `docs/screenshots/08-*.png`（注入 2 次瞬时失败后仍成功）。

### 2.2 限流真正生效

- `ResilienceConfig` 新增 `rate_limit_enabled`、`max_concurrent_queries`、`max_concurrent_llm_calls`、`rate_limit_timeout`。
- `server.py` 用配置值构造 `MultiRateLimiter` 并注入 orchestrator（此前是硬编码且被丢弃）。
- orchestrator：LLM 调用走 `for_llm()`，数据库操作走 `for_queries()`；拿不到槽位抛出 `RateLimitExceededError`（`rate_limit_exceeded`），沿用既有错误码。
- `RateLimiter` 支持 `async with limiter:`（并导出设计文档中的别名 `SemaphoreRateLimiter`）。

**验证**：`tests/unit/test_orchestrator.py::TestRateLimitingIntegration::test_db_concurrency_is_bounded`。
截图 `docs/screenshots/09-*.png`（12 并发 / 3 槽位全部完成且并发受限）。

### 2.3 熔断器只实例化一次

- 删除 server 中"创建后从未使用"的熔断器；现在 **一个** `CircuitBreaker` 在 lifespan 中创建，注入 orchestrator 并复用到 `health_snapshot()`。
- 生成阶段的瞬时失败会 `record_failure()`；本地限流拒绝**不**计为熔断失败。

### 2.4 指标与追踪接入请求路径

| 指标 | 产生位置 |
|------|----------|
| `pg_mcp_query_requests_total{status,database}` | `orchestrator.execute_query()` finally |
| `pg_mcp_query_duration_seconds` | 同上 |
| `pg_mcp_llm_calls_total{operation}` / `pg_mcp_llm_latency_seconds{operation}` | `orchestrator._call_llm()`（`generate` / `validate`） |
| `pg_mcp_llm_tokens_used_total{operation}` | `SQLGenerator.generate_with_usage()` 返回的 `usage.total_tokens` |
| `pg_mcp_sql_rejected_total{reason}` | 安全校验失败时（reason 见下表） |
| `pg_mcp_db_query_duration_seconds` | `SQLExecutor` |
| `pg_mcp_db_connections_active{database}` | lifespan |
| `pg_mcp_schema_cache_age_seconds{database}` | 每次取 schema 时 |

`reason` 取值：`parse_error`、`explain_denied`、`blocked_function`、`blocked_table`、
`blocked_column`、`write_operation`、`statement_type`、`security_violation`。

**修复的隐藏缺陷**：`MetricsCollector.reset_all_metrics()` 原本会重建全局 registry 中的同名指标而抛 `Duplicated timeseries`。现在每个 collector 拥有**私有 `CollectorRegistry`**，重置是真正可用的，测试之间也不会互相污染。

**追踪**：`request_id` 改为设计文档的 `req_<12 hex>` 格式，贯穿 orchestrator 与工具响应；
新增 `observability/logging.py::RequestContextFilter`，从 contextvars 自动把
`request_id` / `operation` 注入**每一条**日志记录（替换掉原先会篡改进程全局
`logging.setLogRecordFactory` 的并发不安全实现）。

**验证**：`tests/unit/test_metrics.py`（含 reset 可重复执行）、`test_orchestrator.py::TestObservabilityIntegration`。
截图 `docs/screenshots/10-*.png`。

### 2.5 健康检查端点

- 新增 `observability/health.py`：`HealthChecker` / `HealthReport` / `HealthStatus`，
  逐库报告 `reachable`、连接池大小与空闲数、schema 缓存命中与年龄，并附带熔断器与限流器状态。
- 新增 MCP 工具 `health_check`（以及便于发现库名的 `list_databases`）。

**验证**：`tests/unit/test_health.py`（7 个用例）。截图 `docs/screenshots/11-*.png`。

### 2.6 校验结果不再硬编码为 valid

- 新增 `SQLValidator.analyze()`：**不抛异常**地返回结构化 `ValidationResult`
  （`is_valid` / `is_select` / `allows_data_modification` / `uses_blocked_functions` /
  `error_message` / 新增 `error_code`）。
- orchestrator 用 `analyze()` 驱动重试与指标标签，失败时才按错误类型抛出
  `SecurityViolationError` 或 `SQLParseError`，保留原有错误码语义。

---

## 三、响应/模型缺陷与死配置（评审 Priority 3）

| 问题 | 修复 |
|------|------|
| `QueryResponse.to_dict` 重复定义（第二个覆盖第一个，"tokens_used 恒存在"成为死代码） | 删除重复定义，只保留会补 `tokens_used = 0` 的实现 |
| 两个 `ErrorDetail`（Pydantic vs 普通类） | 只保留 `models/query.py` 的 Pydantic `ErrorDetail`，`models/errors.py` 改为复用并补充 `to_dict()`；`PgMcpError.to_error_detail()` 返回同一类型 |
| `retry_delay` / `backoff_factor` 从未使用 | 由 `RetryPolicy.from_config()` 消费，接入 LLM 与数据库调用 |
| `circuit_breaker_*` 在 server 与 orchestrator 各建一份 | 只建一份并共享 |
| `max_question_length` / `min_confidence_score` 未使用 | 见 1.3 |
| `create_pools()` 未被使用 | 见 1.1 |
| `tracing.py` 未被使用 | 见 2.4 |
| `scripts` 入口指向的 `main.py` 实际是 "add two numbers" 占位实现 | `main.py` 改为调用 `pg_mcp.__main__:main` |
| 日志写 stdout，会污染 MCP stdio 数据帧 | `configure_logging()` 改为写 **stderr**（并降低 sqlglot 噪音日志） |
| `OPENAI_MAX_TOKENS` 上限 4096，与 `.env.example` 的 32000 冲突 | 上限放宽到 128000 |
| `OBSERVABILITY_LOG_FORMAT` 默认值与文档/测试不一致 | 默认改为 `json`，与 README 和 `.env.example` 一致 |
| 新增未被消费的配置 | 新增的每个字段都有消费点，可对照 `.env.example` 与本文件 |

---

## 四、测试覆盖（评审 Priority 4）

### 4.1 测试必须能在没有外部服务时通过

- `tests/conftest.py` 新增 `pytest_collection_modifyitems`：未设置 `PG_MCP_LIVE_TESTS=1` 时，
  `tests/integration/**` 与 `tests/e2e/**` 自动 skip，默认 `pytest` 全离线、确定性。
- 需要真实环境的用例可显式开启：`PG_MCP_LIVE_TESTS=1 pytest -m integration`。

### 4.2 新增/重写的测试

| 文件 | 覆盖 |
|------|------|
| `tests/unit/test_orchestrator.py`（重写） | 多库路由、按库安全策略、缺失执行器、问题长度、重试、熔断、结果校验、低置信度、行截断、指标、健康快照、限流 |
| `tests/unit/test_retry.py`（新） | 退避策略与重试语义 |
| `tests/unit/test_security_policy.py`（新） | 禁用表/列、EXPLAIN、写策略、DDL、`analyze()` |
| `tests/unit/test_config_multidb.py`（新） | `databases` 解析、JSON 环境变量、重名拒绝、`effective_for()` 合并语义 |
| `tests/unit/test_health.py`（新） | healthy/degraded/unhealthy、熔断打开、缓存信息、序列化 |
| `tests/unit/test_metrics.py`（新） | 指标标签、渲染、重置、别名 |
| `tests/unit/test_config.py`（调整） | 与新上限/新默认值对齐 |

当前结果：**356 passed, 46 skipped**（skip 的全是需要真实 PostgreSQL/OpenAI 的联外用例）。

---

## 五、离线可验证性

- `demo/offline_demo.py`：用内存数据库替身与规则化 LLM 替身驱动**真实生产代码路径**，
  输出 11 个场景的完整链路（多库路由、安全拦截、EXPLAIN 策略、长度保护、重试退避、
  并发限流、Prometheus 指标、健康报告）。
- `demo/offline_server.py` + `demo/mcp_stdio_smoke.py`：启动**真实 MCP stdio 服务器**
  （真 FastMCP、真工具、真 JSON-RPC 握手），用官方 `mcp` 客户端完成
  `initialize` → `tools/list` → `tools/call`。
- `demo/render_screenshots.py`：把上述输出连同 `pytest` 结果渲染为
  `docs/screenshots/*.png`（13 张）。

---

## 六、仍未覆盖 / 有意保留

- 多库支持保持"一次请求只查一个库"（与设计一致）；跨库 JOIN 不在范围内。
- `CacheConfig` 依旧使用 `schema_ttl` / `max_size` / `enabled`（设计文档中另有命名），
  因为 README、`.env.example` 与既有测试都基于当前命名；自动刷新仍默认关闭。
- `tests/integration` 与 `tests/e2e` 仍需要真实环境，只做了 skip 处理，未改写成 mock 测试
  （mock 版本的行为已由 `tests/unit` 与离线演示覆盖）。
- `pglast` 未引入（实际使用 sqlglot），与设计文档差异保留。
