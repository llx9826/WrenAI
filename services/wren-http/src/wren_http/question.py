from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import sqlglot
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, FunctionToolset
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits
from pydantic_core import to_jsonable_python
from wren.context import load_rules

from .contracts import (
    QuestionRequest,
    QuestionResponse,
    QuestionSource,
    ToolCallTrace,
)
from .model import model_name
from .registry import WorkspacePublication
from .service import WrenProjectRuntime
from .settings import ServiceSettings


class ModelNotConfiguredError(RuntimeError):
    pass


class AgentAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["answered", "needs_clarification", "unsupported"]
    answer: str = Field(min_length=1)


@dataclass(frozen=True)
class AgentRunData:
    output: AgentAnswer
    messages: tuple[Any, ...]
    model_calls: int


class CubeToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logical_sql: str
    dialect_sql: str
    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    row_count: int
    truncated: bool


class WrenQuestionService:
    """Run one natural-language turn through the official Wren Agent tools."""

    def __init__(
        self,
        runtime: WrenProjectRuntime,
        model: Model | None,
        settings: ServiceSettings,
    ):
        self.runtime = runtime
        self.model = model
        self.settings = settings

    async def answer(
        self,
        entry: WorkspacePublication,
        body: QuestionRequest,
    ) -> QuestionResponse:
        if self.model is None:
            raise ModelNotConfiguredError("question model is not configured")

        toolkit = await asyncio.to_thread(
            self.runtime.session_toolkit,
            entry,
            body.session_properties,
        )
        toolset = toolkit.toolset(include_memory_write=False)
        toolsets = [toolset]
        cube_toolset = self._cube_toolset(entry, body.session_properties)
        if cube_toolset is not None:
            toolsets.append(cube_toolset)
        agent = self._build_agent(entry, toolkit, toolsets)
        result = await agent.run(
            body.question,
            message_history=_message_history(body),
            usage_limits=UsageLimits(request_limit=self.settings.model_request_limit),
        )
        run = AgentRunData(
            output=result.output,
            messages=tuple(result.all_messages()),
            model_calls=result.usage.requests,
        )
        response = self._build_response(entry, body, run)
        if (
            self.settings.memory_enabled
            and response.status == "answered"
            and response.logical_sql
            and _has_one_successful_query(response.tool_trace)
        ):
            memory_trace = await self._store_successful_query(
                toolkit,
                body.question,
                response.logical_sql,
            )
            response = response.model_copy(
                update={"tool_trace": response.tool_trace + (memory_trace,)}
            )
        return response

    def _build_agent(self, entry, toolkit, toolsets) -> Agent:
        rules, _ = load_rules(self.runtime.registry.project(entry))
        instructions = toolkit.instructions(toolset=toolsets[0])
        if len(toolsets) > 1:
            instructions += (
                "\n\nFor governed metrics declared as Wren cubes, call "
                "`wren_cube_query` instead of recreating the aggregation in SQL."
            )
        if rules:
            instructions += "\n\n# Project business rules\n\n" + rules
        instructions += (
            "\n\n# Response contract\n\n"
            "Answer in Chinese. Use `answered` only after `wren_query` succeeds, and "
            "base the answer only on its returned rows. Use `needs_clarification` when "
            "the request lacks a necessary subject, measure, filter, or time range. Use "
            "`unsupported` when the project data cannot answer the request. Do not invent "
            "schema, SQL results, or facts. State clearly when the query returned no rows "
            "or a truncated result."
        )
        return Agent(
            self.model,
            output_type=AgentAnswer,
            instructions=instructions,
            toolsets=toolsets,
            retries=2,
            tool_timeout=self.settings.model_timeout_seconds,
        )

    def _cube_toolset(self, entry, properties):
        cubes = self.runtime.registry.manifest(entry).get("cubes") or []
        if not cubes:
            return None
        toolset = FunctionToolset()

        @toolset.tool_plain(retries=2)
        def wren_cube_query(
            cube: str,
            measures: list[str],
            dimensions: list[str] | None = None,
            time_dimensions: list[dict[str, Any]] | None = None,
            filters: list[dict[str, Any]] | None = None,
            order_by: list[dict[str, Any]] | None = None,
            limit: int = 100,
            offset: int = 0,
        ) -> CubeToolResult:
            """Execute a governed Wren cube metric query."""

            result = self.runtime.cube_query(
                entry,
                cube=cube,
                measures=tuple(measures),
                dimensions=tuple(dimensions or ()),
                time_dimensions=tuple(time_dimensions or ()),
                filters=tuple(filters or ()),
                order_by=tuple(order_by or ()),
                limit=limit,
                offset=offset,
                properties=properties,
            )
            return CubeToolResult(
                logical_sql=result.logical_sql or "",
                dialect_sql=result.dialect_sql,
                columns=result.columns,
                rows=result.rows,
                row_count=len(result.rows),
                truncated=result.truncated,
            )

        return toolset

    def _build_response(
        self,
        entry: WorkspacePublication,
        body: QuestionRequest,
        run: AgentRunData,
    ) -> QuestionResponse:
        traces, query = _tool_trace(run.messages)
        if run.output.status == "answered" and query is None:
            raise ValueError("Wren Agent answered without a successful wren_query call")

        logical_sql = query.sql if query else None
        dialect_sql = (
            self.runtime.plan(
                entry,
                logical_sql,
                body.session_properties,
            ).dialect_sql
            if logical_sql
            else None
        )
        rows = query.rows[: body.limit] if query else ()
        truncated = bool(query and (query.truncated or len(query.rows) > body.limit))
        return QuestionResponse(
            workspace_id=body.workspace_id,
            publication_id=body.publication_id,
            request_id=body.request_id,
            trace_id=uuid.uuid4().hex,
            status=run.output.status,
            answer=run.output.answer,
            logical_sql=logical_sql,
            dialect_sql=dialect_sql,
            columns=query.columns if query else (),
            rows=rows,
            truncated=truncated,
            sources=_sources(entry, self.runtime.models(entry), logical_sql),
            tool_trace=traces,
            model_name=model_name(self.model),
            model_calls=run.model_calls,
            provider_request_ids=_provider_request_ids(run.messages),
        )

    async def _store_successful_query(
        self,
        toolkit,
        question: str,
        sql: str,
    ) -> ToolCallTrace:
        arguments = {"nl": question, "sql": sql, "tags": ["confirmed"]}
        try:
            await asyncio.to_thread(
                toolkit.memory.store,
                question,
                sql,
                tags=["confirmed"],
            )
        except Exception as exc:
            return ToolCallTrace(
                name="wren_store_query",
                arguments=arguments,
                status="failed",
                error=type(exc).__name__,
            )
        return ToolCallTrace(
            name="wren_store_query",
            arguments=arguments,
            status="succeeded",
            summary="stored",
        )


@dataclass(frozen=True)
class SuccessfulQuery:
    sql: str
    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    truncated: bool


def _message_history(body: QuestionRequest) -> list[ModelRequest | ModelResponse]:
    messages: list[ModelRequest | ModelResponse] = []
    for turn in body.history:
        if turn.role == "user":
            messages.append(ModelRequest(parts=[UserPromptPart(turn.content)]))
        else:
            messages.append(ModelResponse(parts=[TextPart(turn.content)]))
    return messages


def _tool_trace(
    messages: tuple[Any, ...],
) -> tuple[tuple[ToolCallTrace, ...], SuccessfulQuery | None]:
    calls: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    successful_query: SuccessfulQuery | None = None

    for message in messages:
        if isinstance(message, ModelResponse):
            for part in message.parts:
                if not isinstance(part, ToolCallPart) or not part.tool_name.startswith(
                    "wren_"
                ):
                    continue
                call = {
                    "name": part.tool_name,
                    "arguments": part.args_as_dict(),
                    "status": "pending",
                    "summary": None,
                    "error": None,
                }
                calls.append(call)
                by_id[part.tool_call_id] = call
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, ToolReturnPart) and part.tool_call_id in by_id:
                    call = by_id[part.tool_call_id]
                    payload = _json_value(part.content)
                    call["status"] = "succeeded"
                    call["summary"] = _tool_summary(call["name"], payload)
                    if call["name"] in {"wren_query", "wren_cube_query"}:
                        successful_query = _query_result(call["arguments"], payload)
                elif isinstance(part, RetryPromptPart) and part.tool_call_id in by_id:
                    call = by_id[part.tool_call_id]
                    call["status"] = "failed"
                    call["error"] = str(part.content)[:500]

    traces = tuple(ToolCallTrace.model_validate(call) for call in calls)
    return traces, successful_query


def _json_value(value: Any) -> Any:
    converted = to_jsonable_python(value)
    if isinstance(converted, str):
        try:
            return json.loads(converted)
        except json.JSONDecodeError:
            return converted
    return converted


def _query_result(arguments: dict[str, Any], payload: Any) -> SuccessfulQuery:
    if not isinstance(payload, dict):
        raise ValueError("wren_query returned a non-object result")
    sql = (
        payload.get("logical_sql")
        if isinstance(payload, dict) and "logical_sql" in payload
        else arguments.get("sql")
    )
    columns = payload.get("columns")
    rows = payload.get("rows")
    if not isinstance(sql, str) or not isinstance(columns, list) or not isinstance(rows, list):
        raise ValueError("wren_query returned an invalid result")
    return SuccessfulQuery(
        sql=sql,
        columns=tuple(str(column) for column in columns),
        rows=tuple(dict(row) for row in rows),
        truncated=bool(payload.get("truncated")),
    )


def _tool_summary(name: str, payload: Any) -> str:
    if name in {"wren_query", "wren_cube_query"} and isinstance(payload, dict):
        return f"rows={payload.get('row_count', len(payload.get('rows', [])))}"
    if name == "wren_fetch_context" and isinstance(payload, dict):
        return f"strategy={payload.get('strategy', 'unknown')}"
    if name in {"wren_list_models", "wren_recall_queries"} and isinstance(payload, list):
        return f"items={len(payload)}"
    if name == "wren_store_query":
        return "stored"
    if name == "wren_dry_plan":
        return "planned"
    return "completed"


def _provider_request_ids(messages: tuple[Any, ...]) -> tuple[str, ...]:
    return tuple(
        message.provider_response_id
        for message in messages
        if isinstance(message, ModelResponse) and message.provider_response_id
    )


def _has_one_successful_query(traces: tuple[ToolCallTrace, ...]) -> bool:
    return (
        sum(
            trace.name in {"wren_query", "wren_cube_query"}
            and trace.status == "succeeded"
            for trace in traces
        )
        == 1
    )


def _sources(
    entry: WorkspacePublication,
    models: tuple[dict[str, Any], ...],
    sql: str | None,
) -> tuple[QuestionSource, ...]:
    if not sql:
        return ()
    names = {
        table.name.casefold()
        for table in sqlglot.parse_one(sql, read="duckdb").find_all(sqlglot.exp.Table)
    }
    descriptions = {model["name"].casefold(): model for model in models}
    registered = {source.model.casefold(): source for source in entry.sources}
    result = []
    for name in sorted(names):
        model = descriptions.get(name)
        if model is None:
            continue
        source = registered.get(name)
        result.append(
            QuestionSource(
                model=model["name"],
                dataset_id=source.dataset_id if source else None,
                source_name=source.source_name if source else None,
                content_hash=source.content_hash if source else None,
                sheet=source.sheet if source else None,
                description=model.get("properties", {}).get("description"),
            )
        )
    return tuple(result)
