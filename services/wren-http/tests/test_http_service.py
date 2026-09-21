import asyncio
import json

import duckdb
import yaml
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from wren_http.bootstrap import create_app
from wren_http.contracts import QuestionRequest
from wren_http.question import WrenQuestionService
from wren_http.registry import open_workspace_registry
from wren_http.service import WrenProjectRuntime
from wren_http.settings import ServiceSettings

ADMIN_HEADERS = {"Authorization": "Bearer test-admin-token"}
TOKENS = {
    "a": "test-service-token-a",
    "b": "test-service-token-b",
}


def write_project(path, rows):
    model = path / "models" / "guests"
    model.mkdir(parents=True)
    (path / "wren_project.yml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 5,
                "name": path.parent.name,
                "version": path.name,
                "catalog": "wren",
                "schema": "main",
                "data_source": "duckdb",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (path / "relationships.yml").write_text("relationships: []\n", encoding="utf-8")
    (model / "metadata.yml").write_text(
        yaml.safe_dump(
            {
                "name": "guests",
                "properties": {"description": "Synthetic guests"},
                "table_reference": {
                    "catalog": "data",
                    "schema": "main",
                    "table": "guests",
                },
                "columns": [
                    {
                        "name": "name",
                        "type": "VARCHAR",
                        "is_calculated": False,
                        "not_null": True,
                        "properties": {},
                    }
                ],
                "cached": False,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    with duckdb.connect(str(path / "data.duckdb")) as connection:
        connection.execute("CREATE TABLE guests(name VARCHAR NOT NULL)")
        connection.executemany("INSERT INTO guests VALUES (?)", [(row,) for row in rows])


def write_relationship_project(path):
    path.mkdir(parents=True)
    (path / "wren_project.yml").write_text(
        "schema_version: 5\nname: sales\ncatalog: wren\nschema: main\n"
        "data_source: duckdb\n",
        encoding="utf-8",
    )
    (path / "relationships.yml").write_text(
        "relationships:\n"
        "  - name: orders_customer\n"
        "    models: [orders, customers]\n"
        "    join_type: MANY_TO_ONE\n"
        "    condition: orders.customer_id = customers.id\n",
        encoding="utf-8",
    )
    models = {
        "customers": {
            "name": "customers",
            "table_reference": {"catalog": "data", "schema": "main", "table": "customers"},
            "primary_key": "id",
            "columns": [
                {"name": "id", "type": "INTEGER", "is_primary_key": True, "not_null": True},
                {"name": "name", "type": "VARCHAR"},
            ],
        },
        "orders": {
            "name": "orders",
            "table_reference": {"catalog": "data", "schema": "main", "table": "orders"},
            "primary_key": "id",
            "columns": [
                {"name": "id", "type": "INTEGER", "is_primary_key": True, "not_null": True},
                {"name": "customer_id", "type": "INTEGER"},
                {"name": "amount", "type": "DOUBLE"},
                {"name": "customer", "type": "customers", "relationship": "orders_customer"},
            ],
        },
    }
    for name, metadata in models.items():
        directory = path / "models" / name
        directory.mkdir(parents=True)
        (directory / "metadata.yml").write_text(
            yaml.safe_dump(metadata, sort_keys=False), encoding="utf-8"
        )
    view = path / "views" / "customer_orders"
    view.mkdir(parents=True)
    (view / "metadata.yml").write_text(
        yaml.safe_dump(
            {
                "name": "customer_orders",
                "statement": (
                    "SELECT customers.name AS customer_name, orders.amount "
                    "FROM orders JOIN customers "
                    "ON orders.customer_id = customers.id"
                ),
                "properties": {"description": "Orders with customer names"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    cube = path / "cubes" / "order_metrics"
    cube.mkdir(parents=True)
    (cube / "metadata.yml").write_text(
        yaml.safe_dump(
            {
                "name": "order_metrics",
                "base_object": "orders",
                "measures": [
                    {"name": "total_revenue", "expression": "SUM(amount)", "type": "DOUBLE"},
                    {"name": "order_count", "expression": "COUNT(*)", "type": "BIGINT"},
                ],
                "dimensions": [
                    {"name": "customer_id", "expression": "customer_id", "type": "INTEGER"}
                ],
                "properties": {"description": "Revenue and order count by customer"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    knowledge = path / "knowledge"
    (knowledge / "rules").mkdir(parents=True)
    (knowledge / "sql").mkdir(parents=True)
    (knowledge / "knowledge.yml").write_text("schema_version: 1\n", encoding="utf-8")
    (knowledge / "rules" / "business.md").write_text(
        "# Revenue\n\nRevenue means the sum of order amount.\n",
        encoding="utf-8",
    )
    (knowledge / "sql" / "revenue.md").write_text(
        "# Revenue by customer\n\n"
        "**Question:** What is revenue by customer?\n\n"
        "```sql\nSELECT customer_id, total_revenue FROM order_metrics\n```\n",
        encoding="utf-8",
    )
    with duckdb.connect(str(path / "data.duckdb")) as connection:
        connection.execute("CREATE TABLE customers(id INTEGER, name VARCHAR)")
        connection.execute("INSERT INTO customers VALUES (1, 'Alpha'), (2, 'Beta')")
        connection.execute("CREATE TABLE orders(id INTEGER, customer_id INTEGER, amount DOUBLE)")
        connection.execute("INSERT INTO orders VALUES (10, 1, 12.5), (11, 1, 7.5), (12, 2, 3.0)")


def write_access_control_project(path):
    model = path / "models" / "guests"
    model.mkdir(parents=True)
    (path / "wren_project.yml").write_text(
        "schema_version: 5\nname: governed\ncatalog: wren\nschema: main\n"
        "data_source: duckdb\n",
        encoding="utf-8",
    )
    (path / "relationships.yml").write_text("relationships: []\n", encoding="utf-8")
    (model / "metadata.yml").write_text(
        yaml.safe_dump(
            {
                "name": "guests",
                "table_reference": {
                    "catalog": "data",
                    "schema": "main",
                    "table": "guests",
                },
                "columns": [
                    {"name": "owner", "type": "VARCHAR"},
                    {"name": "name", "type": "VARCHAR"},
                    {
                        "name": "secret",
                        "type": "VARCHAR",
                        "column_level_access_control": {
                            "name": "secret_access",
                            "required_properties": [
                                {"name": "session_access_level", "required": True}
                            ],
                            "operator": "EQUALS",
                            "threshold": "1",
                        },
                    },
                ],
                "row_level_access_controls": [
                    {
                        "name": "owner_access",
                        "required_properties": [
                            {"name": "session_user_id", "required": True}
                        ],
                        "condition": "owner = @session_user_id",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    with duckdb.connect(str(path / "data.duckdb")) as connection:
        connection.execute("CREATE TABLE guests(owner VARCHAR,name VARCHAR,secret VARCHAR)")
        connection.execute(
            "INSERT INTO guests VALUES "
            "('alice','Alice Guest','A-SECRET'),"
            "('bob','Bob Guest','B-SECRET')"
        )


def register(client, workspace, project_path="a/v1", publication="v1"):
    return client.post(
        "/v1/admin/publications",
        json={
            "workspace_id": workspace,
            "publication_id": publication,
            "access_token": TOKENS.get(workspace, "registered-query-token"),
            "project_path": project_path,
            "backend": {"type": "duckdb"},
            "sources": [
                {
                    "model": "guests",
                    "dataset_id": f"dataset-{workspace}",
                    "source_name": "guests.xlsx",
                    "content_hash": "a" * 64,
                    "sheet": "Data",
                }
            ],
        },
        headers=ADMIN_HEADERS,
    )


def make_client(tmp_path):
    data = tmp_path / "data"
    write_project(data / "a" / "v1", ["A_ONLY", "A_SECOND"])
    write_project(data / "b" / "v1", ["B_ONLY"])
    settings = ServiceSettings(
        admin_token=SecretStr("test-admin-token"),
        workspaces_file=tmp_path / "config" / "workspaces.json",
        duckdb_root=data,
        wren_home=tmp_path / "config" / "wren-home",
    )
    client = TestClient(create_app(settings))
    for workspace in ("a", "b"):
        response = register(client, workspace, f"{workspace}/v1")
        assert response.status_code == 200, response.text
    return client


def test_official_toolkit_auth_models_isolation_and_row_limit(tmp_path):
    with make_client(tmp_path) as client:
        assert client.get("/health").json()["status"] == "ok"
        context = {"workspace_id": "a", "publication_id": "v1"}
        assert client.post("/v1/models", json=context).status_code == 401
        models = client.post(
            "/v1/models",
            json=context,
            headers={"Authorization": "Bearer test-service-token-a"},
        )
        assert models.status_code == 200, models.text
        assert models.json()["models"] == [
            {
                "name": "guests",
                "description": "Synthetic guests",
                "columns": ["name"],
            }
        ]
        response = client.post(
            "/v1/sql-queries",
            json={**context, "sql": "SELECT name FROM guests ORDER BY name", "limit": 1},
            headers={"Authorization": "Bearer test-service-token-a"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["rows"] == [{"name": "A_ONLY"}]
        assert response.json()["truncated"]
        response = client.post(
            "/v1/sql-queries",
            json={
                "workspace_id": "b",
                "publication_id": "v1",
                "sql": "SELECT name FROM guests",
            },
            headers={"Authorization": "Bearer test-service-token-b"},
        )
        assert response.json()["rows"] == [{"name": "B_ONLY"}]
        denied = client.post(
            "/v1/sql-queries",
            json={
                "workspace_id": "b",
                "publication_id": "v1",
                "sql": "SELECT name FROM guests",
            },
            headers={"Authorization": "Bearer test-service-token-a"},
        )
        assert denied.status_code == 401


def test_official_toolkit_plan_dry_run_query_and_read_only_guard(tmp_path):
    with make_client(tmp_path) as client:
        headers = {"Authorization": "Bearer test-service-token-a"}
        body = {
            "workspace_id": "a",
            "publication_id": "v1",
            "sql": "SELECT COUNT(*) AS guest_count FROM guests",
        }
        plan = client.post("/v1/sql-plans", json=body, headers=headers)
        assert plan.status_code == 200, plan.text
        assert '"data"."main".guests' in plan.json()["dialect_sql"]
        dry_run = client.post("/v1/sql-dry-runs", json=body, headers=headers)
        assert dry_run.status_code == 200, dry_run.text
        assert dry_run.json()["valid"]
        query = client.post("/v1/sql-queries", json=body, headers=headers)
        assert query.status_code == 200, query.text
        assert query.json()["rows"] == [{"guest_count": 2}]
        denied = client.post(
            "/v1/sql-queries",
            json={**body, "sql": "DELETE FROM guests"},
            headers=headers,
        )
        assert denied.status_code == 422


def test_official_toolkit_executes_declared_multi_table_relationship(tmp_path):
    data = tmp_path / "data"
    project = data / "sales" / "v1"
    write_relationship_project(project)
    settings = ServiceSettings(
        admin_token=SecretStr("test-admin-token"),
        workspaces_file=tmp_path / "config" / "workspaces.json",
        duckdb_root=data,
        wren_home=tmp_path / "config" / "wren-home",
    )
    with TestClient(create_app(settings)) as client:
        assert register(client, "sales", "sales/v1").status_code == 200
        response = client.post(
            "/v1/sql-queries",
            json={
                "workspace_id": "sales",
                "publication_id": "v1",
                "sql": (
                    "SELECT customers.name AS customer_name, SUM(orders.amount) AS total "
                    "FROM orders JOIN customers ON orders.customer_id=customers.id "
                    "GROUP BY customers.name ORDER BY customers.name"
                ),
            },
            headers={"Authorization": "Bearer registered-query-token"},
        )
        view_response = client.post(
            "/v1/sql-queries",
            json={
                "workspace_id": "sales",
                "publication_id": "v1",
                "sql": "SELECT customer_name, amount FROM customer_orders "
                "ORDER BY customer_name, amount",
            },
            headers={"Authorization": "Bearer registered-query-token"},
        )
        cube_response = client.post(
            "/v1/cube-queries",
            json={
                "workspace_id": "sales",
                "publication_id": "v1",
                "cube": "order_metrics",
                "measures": ["total_revenue", "order_count"],
                "dimensions": ["customer_id"],
                "order_by": [{"member": "customer_id", "direction": "asc"}],
            },
            headers={"Authorization": "Bearer registered-query-token"},
        )
    assert response.status_code == 200, response.text
    target = json.loads((project / "target" / "mdl.json").read_text(encoding="utf-8"))
    assert target["relationships"] == [
        {
            "name": "orders_customer",
            "models": ["orders", "customers"],
            "joinType": "MANY_TO_ONE",
            "condition": "orders.customer_id = customers.id",
        }
    ]
    assert [item["name"] for item in target["views"]] == ["customer_orders"]
    assert [item["name"] for item in target["cubes"]] == ["order_metrics"]
    with duckdb.connect(str(project / "data.duckdb"), read_only=True) as connection:
        expected = connection.execute(
            "SELECT customers.name AS customer_name, SUM(orders.amount) AS total "
            "FROM orders JOIN customers ON orders.customer_id=customers.id "
            "GROUP BY customers.name ORDER BY customers.name"
        ).fetchall()
    assert response.json()["rows"] == [
        {"customer_name": name, "total": total} for name, total in expected
    ]
    assert view_response.status_code == 200, view_response.text
    assert view_response.json()["rows"] == [
        {"customer_name": "Alpha", "amount": 7.5},
        {"customer_name": "Alpha", "amount": 12.5},
        {"customer_name": "Beta", "amount": 3.0},
    ]
    assert cube_response.status_code == 200, cube_response.text
    assert cube_response.json()["rows"] == [
        {"customer_id": 1, "total_revenue": 20.0, "order_count": 2},
        {"customer_id": 2, "total_revenue": 3.0, "order_count": 1},
    ]


def test_session_properties_enforce_wren_row_and_column_access(tmp_path):
    data = tmp_path / "data"
    project = data / "governed" / "v1"
    write_access_control_project(project)
    settings = ServiceSettings(
        admin_token=SecretStr("test-admin-token"),
        workspaces_file=tmp_path / "config" / "workspaces.json",
        duckdb_root=data,
        wren_home=tmp_path / "config" / "wren-home",
    )
    headers = {"Authorization": "Bearer registered-query-token"}
    base = {
        "workspace_id": "governed",
        "publication_id": "v1",
    }
    with TestClient(create_app(settings)) as client:
        assert register(client, "governed", "governed/v1").status_code == 200
        missing = client.post(
            "/v1/sql-queries",
            json={**base, "sql": "SELECT name FROM guests"},
            headers=headers,
        )
        alice = client.post(
            "/v1/sql-queries",
            json={
                **base,
                "sql": "SELECT name FROM guests",
                "session_properties": {
                    "session_user_id": "'alice'",
                    "session_access_level": "0",
                },
            },
            headers=headers,
        )
        hidden = client.post(
            "/v1/sql-queries",
            json={
                **base,
                "sql": "SELECT secret FROM guests",
                "session_properties": {
                    "session_user_id": "'alice'",
                    "session_access_level": "0",
                },
            },
            headers=headers,
        )
        allowed = client.post(
            "/v1/sql-queries",
            json={
                **base,
                "sql": "SELECT secret FROM guests",
                "session_properties": {
                    "session_user_id": "'alice'",
                    "session_access_level": "1",
                },
            },
            headers=headers,
        )

    assert missing.status_code == 422
    assert alice.status_code == 200, alice.text
    assert alice.json()["rows"] == [{"name": "Alice Guest"}]
    assert hidden.status_code == 422
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["rows"] == [{"secret": "A-SECRET"}]


def test_admin_registration_is_validated_immutable_and_never_persists_tokens(tmp_path):
    data = tmp_path / "data"
    write_project(data / "registered" / "p1", ["REGISTERED"])
    settings = ServiceSettings(
        admin_token=SecretStr("test-admin-token"),
        workspaces_file=tmp_path / "config" / "workspaces.json",
        duckdb_root=data,
        wren_home=tmp_path / "config" / "wren-home",
    )
    registry_file = settings.workspaces_file
    with TestClient(create_app(settings)) as client:
        assert register(client, "registered", "registered/p1", "p1").status_code == 200
        assert register(client, "registered", "registered/p1", "p1").status_code == 200
        write_project(data / "other" / "p1", ["OTHER"])
        conflict = register(client, "registered", "other/p1", "p1")
        assert conflict.status_code == 409
        unchanged = client.post(
            "/v1/sql-queries",
            json={
                "workspace_id": "registered",
                "publication_id": "p1",
                "sql": "SELECT name FROM guests",
            },
            headers={"Authorization": "Bearer registered-query-token"},
        )
        assert unchanged.status_code == 200, unchanged.text
        assert unchanged.json()["rows"] == [{"name": "REGISTERED"}]
        missing = register(client, "missing", "does-not-exist", "p1")
        assert missing.status_code == 422
    persisted = registry_file.read_text(encoding="utf-8")
    assert "registered-query-token" not in persisted
    assert "test-admin-token" not in persisted
    assert (data / "registered" / "p1" / "target" / "mdl.json").is_file()


def count_agent_model(call_log):
    def respond(messages, info):
        call_log.append(messages)
        returns = [
            part
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if not returns:
            return ModelResponse(
                parts=[ToolCallPart("wren_list_models", {}, "list-models")]
            )
        if returns[-1].tool_name == "wren_list_models":
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "wren_query",
                        {"sql": "SELECT COUNT(*) AS total FROM guests", "limit": 100},
                        "run-query",
                    )
                ]
            )
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {"status": "answered", "answer": "共有 2 条记录。"},
                    "final-answer",
                )
            ]
        )

    return FunctionModel(respond, model_name="test-wren-agent")


def cube_agent_model():
    def respond(messages, info):
        returns = [
            part
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if not returns:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "wren_cube_query",
                        {
                            "cube": "order_metrics",
                            "measures": ["total_revenue", "order_count"],
                            "dimensions": ["customer_id"],
                            "order_by": [
                                {"member": "customer_id", "direction": "asc"}
                            ],
                        },
                        "run-cube",
                    )
                ]
            )
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {"status": "answered", "answer": "已按确认口径统计。"},
                    "final-answer",
                )
            ]
        )

    return FunctionModel(respond, model_name="test-wren-agent")


def clarify_agent_model(call_log):
    def respond(messages, info):
        call_log.append(messages)
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {
                        "status": "needs_clarification",
                        "answer": "请说明要统计哪个范围。",
                    },
                    "final-answer",
                )
            ]
        )

    return FunctionModel(respond, model_name="test-wren-agent")


def failed_query_model():
    def respond(messages, info):
        if not any(
            part.part_kind == "retry-prompt"
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        ):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "wren_query",
                        {"sql": "SELECT missing_column FROM guests", "limit": 100},
                        "bad-query",
                    )
                ]
            )
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {"status": "unsupported", "answer": "现有数据无法回答。"},
                    "final-answer",
                )
            ]
        )

    return FunctionModel(respond, model_name="test-wren-agent")


def exploratory_query_model():
    def respond(messages, info):
        query_returns = [
            part
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == "wren_query"
        ]
        if not query_returns:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "wren_query",
                        {"sql": "SELECT name FROM guests WHERE name = 'MISSING'", "limit": 100},
                        "empty-query",
                    )
                ]
            )
        if len(query_returns) == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "wren_query",
                        {"sql": "SELECT DISTINCT name FROM guests", "limit": 100},
                        "explore-query",
                    )
                ]
            )
        output_tool = info.output_tools[0]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {"status": "answered", "answer": "没有找到对应记录。"},
                    "final-answer",
                )
            ]
        )

    return FunctionModel(respond, model_name="test-wren-agent")


class RecordingMemory:
    def __init__(self):
        self.stored = []

    def store(self, question, sql, *, tags):
        self.stored.append((question, sql, tags))


class ToolkitWithRecordingMemory:
    def __init__(self, toolkit):
        self.toolkit = toolkit
        self.memory = RecordingMemory()

    def toolset(self, **kwargs):
        return self.toolkit.toolset(**kwargs)

    def instructions(self, **kwargs):
        return self.toolkit.instructions(**kwargs)

    def __getattr__(self, name):
        return getattr(self.toolkit, name)


def question_app(settings, model):
    configured = settings.model_copy(update={"memory_enabled": False})
    return create_app(configured, model_factory=lambda _: model)


def test_question_runs_official_agent_tools_and_returns_trace(tmp_path):
    with make_client(tmp_path) as first:
        settings = first.app.state.settings
    calls = []
    model = count_agent_model(calls)
    request = {
        "workspace_id": "a",
        "publication_id": "v1",
        "request_id": "question-1",
        "question": "一共有多少条记录？",
    }
    with TestClient(question_app(settings, model)) as client:
        response = client.post(
            "/v1/questions",
            json=request,
            headers={"Authorization": "Bearer test-service-token-a"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["rows"] == [{"total": 2}]
        assert body["answer"] == "共有 2 条记录。"
        assert body["logical_sql"] == "SELECT COUNT(*) AS total FROM guests"
        assert '"data"."main".guests' in body["dialect_sql"]
        assert body["sources"][0]["dataset_id"] == "dataset-a"
        assert body["model_name"] == "test-wren-agent"
        assert [item["name"] for item in body["tool_trace"]] == [
            "wren_list_models",
            "wren_query",
        ]
        assert all(item["status"] == "succeeded" for item in body["tool_trace"])
        repeated = client.post(
            "/v1/questions",
            json=request,
            headers={"Authorization": "Bearer test-service-token-a"},
        )
        assert repeated.status_code == 200
        assert len(calls) == 6


def test_question_agent_uses_governed_cube_metric(tmp_path):
    data = tmp_path / "data"
    write_relationship_project(data / "sales" / "v1")
    settings = ServiceSettings(
        admin_token=SecretStr("test-admin-token"),
        workspaces_file=tmp_path / "config" / "workspaces.json",
        duckdb_root=data,
        wren_home=tmp_path / "config" / "wren-home",
        memory_enabled=False,
    )
    with TestClient(create_app(settings)) as registrar:
        assert register(registrar, "sales", "sales/v1").status_code == 200
    with TestClient(question_app(settings, cube_agent_model())) as client:
        response = client.post(
            "/v1/questions",
            json={
                "workspace_id": "sales",
                "publication_id": "v1",
                "request_id": "cube-question",
                "question": "按客户统计收入和订单数。",
            },
            headers={"Authorization": "Bearer registered-query-token"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer"] == "已按确认口径统计。"
    assert body["rows"] == [
        {"customer_id": 1, "total_revenue": 20.0, "order_count": 2},
        {"customer_id": 2, "total_revenue": 3.0, "order_count": 1},
    ]
    assert [item["name"] for item in body["tool_trace"]] == ["wren_cube_query"]


def test_question_clarification_uses_history_and_skips_wren_query(tmp_path):
    with make_client(tmp_path) as first:
        settings = first.app.state.settings
    calls = []
    with TestClient(question_app(settings, clarify_agent_model(calls))) as client:
        response = client.post(
            "/v1/questions",
            json={
                "workspace_id": "a",
                "publication_id": "v1",
                "request_id": "clarify-1",
                "question": "帮我统计一下。",
                "history": [
                    {"role": "user", "content": "看看嘉宾数据。"},
                    {"role": "assistant", "content": "你想看什么指标？"},
                ],
            },
            headers={"Authorization": "Bearer test-service-token-a"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "needs_clarification"
    assert response.json()["logical_sql"] is None
    assert response.json()["tool_trace"] == []
    assert len(calls[0]) >= 3


def test_failed_sql_is_traced_and_never_written_to_memory(tmp_path):
    with make_client(tmp_path) as first:
        settings = first.app.state.settings.model_copy(update={"memory_enabled": True})
    registry = open_workspace_registry(settings.workspaces_file, settings.duckdb_root)
    runtime = WrenProjectRuntime(registry, memory_enabled=False)
    entry = registry.get("a", "v1")
    toolkit = ToolkitWithRecordingMemory(runtime.toolkit(entry))
    runtime.agent_toolkit = lambda _entry: toolkit
    service = WrenQuestionService(runtime, failed_query_model(), settings)

    response = asyncio.run(
        service.answer(
            entry,
            QuestionRequest(
                workspace_id="a",
                publication_id="v1",
                request_id="failed-query",
                question="查询一个不存在的字段。",
            ),
        )
    )

    assert response.status == "unsupported"
    assert response.logical_sql is None
    assert [(trace.name, trace.status) for trace in response.tool_trace] == [
        ("wren_query", "failed")
    ]
    assert toolkit.memory.stored == []


def test_multi_query_exploration_is_not_written_as_a_confirmed_pair(tmp_path):
    with make_client(tmp_path) as first:
        settings = first.app.state.settings.model_copy(update={"memory_enabled": True})
    registry = open_workspace_registry(settings.workspaces_file, settings.duckdb_root)
    runtime = WrenProjectRuntime(registry, memory_enabled=False)
    entry = registry.get("a", "v1")
    toolkit = ToolkitWithRecordingMemory(runtime.toolkit(entry))
    runtime.agent_toolkit = lambda _entry: toolkit
    service = WrenQuestionService(runtime, exploratory_query_model(), settings)

    response = asyncio.run(
        service.answer(
            entry,
            QuestionRequest(
                workspace_id="a",
                publication_id="v1",
                request_id="exploratory-query",
                question="不存在的嘉宾有哪些？",
            ),
        )
    )

    assert response.status == "answered"
    assert [trace.summary for trace in response.tool_trace] == ["rows=0", "rows=2"]
    assert toolkit.memory.stored == []
