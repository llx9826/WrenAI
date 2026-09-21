# Wren HTTP Service

这是独立的通用问数服务。它不包含大会或 Excel 专用规则，通过 `workspace_id + publication_id` 选择数据发布，并对平台暴露稳定 HTTP 契约。

## 阅读顺序

1. `src/wren_http/main.py`：读取一次环境配置。
2. `src/wren_http/bootstrap.py`：显式创建注册表、Wren Project 运行时和问题服务。
3. `src/wren_http/api.py`：HTTP 鉴权、请求解析和错误映射。
4. `src/wren_http/registry.py`：空间、发布、令牌和数据路径隔离。
5. `src/wren_http/service.py`：官方 `WrenToolkit.from_project()`、项目 Memory 和查询运行时。
6. `src/wren_http/question.py`：一次 Wren Agent 调用及其工具结果映射。

## HTTP 接口

- `GET /health`
- `POST /v1/admin/publications`：注册不可变发布；需要管理员令牌。
- `POST /v1/models`：列出发布中的 Wren 模型。
- `POST /v1/sql-plans`：返回 Wren 规划结果；需要对应空间令牌。
- `POST /v1/sql-dry-runs`：由 Wren 对数据源做只读预检。
- `POST /v1/sql-queries`：由官方 Wren Toolkit 规划并查询。
- `POST /v1/cube-queries`：查询标准 Wren Cube 的维度与指标。
- `POST /v1/questions`：由 Pydantic AI Agent 调用 Wren 官方工具完成自然语言问数。

自然语言请求只走一条路径：Agent 使用 `wren_list_models`、`wren_recall_queries`、`wren_fetch_context`、`wren_dry_plan`、`wren_query` 和按项目启用的 `wren_cube_query`，最终返回答案、SQL、数据行、来源及完整工具轨迹。只有一次且成功的查询会通过 Wren Memory API 受控写入已确认的 NL→SQL；失败 SQL、多次探索、澄清请求和不支持的问题不会写入。Wren 服务不保存请求幂等结果，幂等与会话状态由调用平台负责。

SQL、预检、Cube 和自然语言接口都接受 `session_properties`。调用平台把用户和空间上下文作为属性传入，Wren 在规划与执行阶段统一应用项目中的 RLAC/CLAC；HTTP 层不复制权限表达式，也不在结果返回后补做过滤。

动态注册仅接受位于 `WREN_HTTP_DUCKDB_ROOT` 下的标准 Wren Project 目录。注册时执行项目校验和 `target/mdl.json` 构建，并为每个 `workspace_id + publication_id` 创建独立 Wren profile。空间令牌通过环境变量提供，注册表只保存令牌环境变量名或哈希，不保存明文。

注册请求由管理端发送，`workspaces.json` 是服务生成的状态文件，不手工维护：

```json
{
  "workspace_id": "conference-2026",
  "publication_id": "p_20260920",
  "access_token": "由管理端配置提供",
  "project_path": "conference-2026/p_20260920",
  "backend": {"type": "duckdb"},
  "sources": []
}
```

S2 注册表格式是破坏性升级。旧格式文件应替换为 `workspaces.example.json` 的空注册表，再从管理端重新上传原 Excel；相同内容的上传也会执行幂等注册，因此不会生成新的数据发布版本。

## 配置与运行

必需配置：

```text
WREN_HTTP_WORKSPACES_FILE=/srv/config/workspaces.json
WREN_HTTP_DUCKDB_ROOT=/srv/data
WREN_HTTP_WREN_HOME=/srv/config/wren-home
WREN_HTTP_ADMIN_TOKEN=<admin token>
WREN_HTTP_MODEL_API_KEY=<Bailian API key>
WREN_HTTP_MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
WREN_HTTP_MODEL_NAME=qwen3.7-plus
WREN_HTTP_MODEL_TIMEOUT_SECONDS=60
WREN_HTTP_MODEL_REQUEST_LIMIT=12
WREN_HTTP_MEMORY_ENABLED=true
WREN_EMBEDDING_BACKEND=onnx
CONF_2026_WREN_TOKEN=<workspace token>
```

每个不可变发布使用自己的 `<project>/.wren/memory`，因此 schema 索引、召回示例和 Agent Toolkit cache 都按 `workspace_id + publication_id` 隔离。Windows 环境固定使用兼容版本的 ONNX Runtime，依赖约束已写入 `pyproject.toml`。

启动：

```powershell
python -m wren_http
```

默认监听 `8001`。注册表示例见 `workspaces.example.json`。

## Docker 镜像

Dockerfile 的构建上下文是 WrenAI 仓库根目录，以便安装同一版本中的 Wren SDK：

```powershell
docker build -f services/wren-http/Dockerfile -t wren-http:0.1.0 .
```

容器使用两个持久化目录：`/srv/config` 保存注册表、profile 和 Wren Home，
`/srv/data` 保存平台发布的标准 Wren Project 与 DuckDB。服务启动时会创建这两个
目录；密钥只通过环境变量注入，不写入镜像。

## 验证

```powershell
python -m ruff check src tests
python -m pytest -q
```

服务已改用标准 Wren Project、官方 Agent Toolkit 和 Wren Memory。手工 manifest 注册、手写 DuckDB 执行器、规划转译、自定义 NL2SQL prompt、模型网关与 Wren 侧请求结果库已经删除。Wren 原生 `core/`、`sdk/`、`skills/` 与 `docs/core/` 不在本服务改造范围内。

PostgreSQL 字段仅作为未来部署入口保留，当前放行路径使用 DuckDB。
