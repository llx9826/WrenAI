from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .registry import Backend, PublicationSource


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SqlRequest(Contract):
    workspace_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
    publication_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
    sql: str = Field(min_length=1, max_length=20_000)
    limit: int = Field(default=500, ge=1, le=1_000)
    session_properties: dict[str, str] = Field(default_factory=dict)


class PlanResponse(Contract):
    workspace_id: str
    publication_id: str
    backend: Literal["duckdb", "postgres"]
    dialect_sql: str


class QueryResponse(PlanResponse):
    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    truncated: bool


class ErrorBody(Contract):
    code: str
    message: str


class PublicationRequest(Contract):
    workspace_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
    publication_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
    access_token: SecretStr = Field(min_length=16)
    project_path: Path
    backend: Backend
    sources: tuple[PublicationSource, ...] = ()


class PublicationResponse(Contract):
    workspace_id: str
    publication_id: str
    backend: Literal["duckdb", "postgres"]


class WorkspaceRequest(Contract):
    workspace_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
    publication_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")


class CubeRequest(WorkspaceRequest):
    cube: str = Field(min_length=1, max_length=128)
    measures: tuple[str, ...] = Field(min_length=1)
    dimensions: tuple[str, ...] = ()
    time_dimensions: tuple[dict[str, Any], ...] = ()
    filters: tuple[dict[str, Any], ...] = ()
    order_by: tuple[dict[str, Any], ...] = ()
    limit: int = Field(default=200, ge=1, le=1_000)
    offset: int = Field(default=0, ge=0)
    session_properties: dict[str, str] = Field(default_factory=dict)


class ModelSummary(Contract):
    name: str
    description: str | None = None
    columns: tuple[str, ...] = ()


class ModelsResponse(WorkspaceRequest):
    models: tuple[ModelSummary, ...]


class DryRunResponse(PlanResponse):
    valid: bool = True


class ConversationTurn(Contract):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4_000)


class QuestionRequest(Contract):
    workspace_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
    publication_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
    request_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
    question: str = Field(min_length=1, max_length=4_000)
    history: tuple[ConversationTurn, ...] = Field(default=(), max_length=12)
    limit: int = Field(default=200, ge=1, le=500)
    session_properties: dict[str, str] = Field(default_factory=dict)


class QuestionSource(Contract):
    model: str
    dataset_id: str | None = None
    source_name: str | None = None
    content_hash: str | None = None
    sheet: str | None = None
    description: str | None = None


class ToolCallTrace(Contract):
    name: str
    arguments: dict[str, Any]
    status: Literal["pending", "succeeded", "failed"]
    summary: str | None = None
    error: str | None = None


class QuestionResponse(Contract):
    workspace_id: str
    publication_id: str
    request_id: str
    trace_id: str
    status: Literal["answered", "needs_clarification", "unsupported"]
    answer: str
    logical_sql: str | None = None
    dialect_sql: str | None = None
    columns: tuple[str, ...] = ()
    rows: tuple[dict[str, Any], ...] = ()
    truncated: bool = False
    sources: tuple[QuestionSource, ...] = ()
    tool_trace: tuple[ToolCallTrace, ...] = ()
    model_name: str
    model_calls: int = 0
    provider_request_ids: tuple[str, ...] = ()
