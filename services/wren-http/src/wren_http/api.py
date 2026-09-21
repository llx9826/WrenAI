from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

from fastapi import FastAPI, Header, HTTPException
from pydantic_ai.exceptions import (
    ModelAPIError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)

from .contracts import (
    CubeRequest,
    DryRunResponse,
    ModelsResponse,
    ModelSummary,
    PlanResponse,
    PublicationRequest,
    PublicationResponse,
    QueryResponse,
    QuestionRequest,
    QuestionResponse,
    SqlRequest,
    WorkspaceRequest,
)
from .question import ModelNotConfiguredError, WrenQuestionService
from .registry import WorkspacePublication, WorkspaceRegistry
from .service import WrenProjectRuntime
from .settings import ServiceSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiDependencies:
    settings: ServiceSettings
    registry: WorkspaceRegistry
    runtime: WrenProjectRuntime
    question_service: WrenQuestionService


def build_api(dependencies: ApiDependencies) -> FastAPI:
    """Define HTTP routes using dependencies created by the composition root."""

    settings = dependencies.settings
    registry = dependencies.registry
    runtime = dependencies.runtime
    question_service = dependencies.question_service
    app = FastAPI(title="Wren Generic HTTP Service", version="0.1.0")
    app.state.settings = settings

    def authenticate(entry: WorkspacePublication, authorization: str) -> None:
        scheme, _, credential = authorization.partition(" ")
        try:
            accepted = registry.authenticate(entry, credential)
        except ValueError as exc:
            raise HTTPException(503, "workspace credential is not configured") from exc
        if scheme.lower() != "bearer" or not accepted:
            raise HTTPException(401, "invalid service credential")

    def authenticate_admin(authorization: str) -> None:
        configured = settings.admin_token
        if configured is None:
            raise HTTPException(503, "publication administration is not configured")
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            credential, configured.get_secret_value()
        ):
            raise HTTPException(401, "invalid administration credential")

    def resolve(
        body: SqlRequest | QuestionRequest | WorkspaceRequest,
    ) -> WorkspacePublication:
        try:
            return registry.get(body.workspace_id, body.publication_id)
        except LookupError as exc:
            raise HTTPException(404, "workspace publication was not found") from exc

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "model_configured": settings.model_api_key is not None,
            "memory_enabled": settings.memory_enabled,
        }

    @app.post("/v1/admin/publications", response_model=PublicationResponse)
    def register_publication(
        body: PublicationRequest,
        authorization: str = Header(default=""),
    ) -> PublicationResponse:
        authenticate_admin(authorization)
        try:
            entry = registry.register(
                workspace_id=body.workspace_id,
                publication_id=body.publication_id,
                access_token=body.access_token.get_secret_value(),
                project_path=body.project_path,
                backend=body.backend,
                sources=body.sources,
            )
        except FileExistsError as exc:
            raise HTTPException(409, "publication id is already registered") from exc
        except ValueError as exc:
            raise HTTPException(422, "publication registration is invalid") from exc
        return PublicationResponse(
            workspace_id=entry.workspace_id,
            publication_id=entry.publication_id,
            backend=entry.backend.type,
        )

    @app.post("/v1/models", response_model=ModelsResponse)
    def models(
        body: WorkspaceRequest,
        authorization: str = Header(default=""),
    ) -> ModelsResponse:
        entry = resolve(body)
        authenticate(entry, authorization)
        items = tuple(
            ModelSummary(
                name=model["name"],
                description=model.get("properties", {}).get("description"),
                columns=tuple(column["name"] for column in model.get("columns", ())),
            )
            for model in runtime.models(entry)
        )
        return ModelsResponse(
            workspace_id=body.workspace_id,
            publication_id=body.publication_id,
            models=items,
        )

    @app.post("/v1/sql-plans", response_model=PlanResponse)
    def plan(
        body: SqlRequest,
        authorization: str = Header(default=""),
    ) -> PlanResponse:
        entry = resolve(body)
        authenticate(entry, authorization)
        try:
            result = runtime.plan(entry, body.sql, body.session_properties)
        except Exception as exc:
            logger.error(
                "query planning failed for workspace=%s publication=%s error_type=%s",
                body.workspace_id,
                body.publication_id,
                type(exc).__name__,
            )
            raise HTTPException(422, "query planning failed") from exc
        return PlanResponse(
            workspace_id=body.workspace_id,
            publication_id=body.publication_id,
            backend=result.backend,
            dialect_sql=result.dialect_sql,
        )

    @app.post("/v1/sql-queries", response_model=QueryResponse)
    def query(
        body: SqlRequest,
        authorization: str = Header(default=""),
    ) -> QueryResponse:
        entry = resolve(body)
        authenticate(entry, authorization)
        try:
            result = runtime.query(
                entry,
                body.sql,
                body.limit,
                body.session_properties,
            )
        except Exception as exc:
            logger.error(
                "query execution failed for workspace=%s publication=%s error_type=%s",
                body.workspace_id,
                body.publication_id,
                type(exc).__name__,
            )
            raise HTTPException(422, "query execution failed") from exc
        return QueryResponse(
            workspace_id=body.workspace_id,
            publication_id=body.publication_id,
            backend=result.backend,
            dialect_sql=result.dialect_sql,
            columns=result.columns,
            rows=result.rows,
            truncated=result.truncated,
        )

    @app.post("/v1/sql-dry-runs", response_model=DryRunResponse)
    def dry_run(
        body: SqlRequest,
        authorization: str = Header(default=""),
    ) -> DryRunResponse:
        entry = resolve(body)
        authenticate(entry, authorization)
        try:
            result = runtime.dry_run(entry, body.sql, body.session_properties)
        except Exception as exc:
            logger.error(
                "query dry run failed for workspace=%s publication=%s error_type=%s",
                body.workspace_id,
                body.publication_id,
                type(exc).__name__,
            )
            raise HTTPException(422, "query dry run failed") from exc
        return DryRunResponse(
            workspace_id=body.workspace_id,
            publication_id=body.publication_id,
            backend=result.backend,
            dialect_sql=result.dialect_sql,
        )

    @app.post("/v1/cube-queries", response_model=QueryResponse)
    def cube_query(
        body: CubeRequest,
        authorization: str = Header(default=""),
    ) -> QueryResponse:
        entry = resolve(body)
        authenticate(entry, authorization)
        try:
            result = runtime.cube_query(
                entry,
                cube=body.cube,
                measures=body.measures,
                dimensions=body.dimensions,
                time_dimensions=body.time_dimensions,
                filters=body.filters,
                order_by=body.order_by,
                limit=body.limit,
                offset=body.offset,
                properties=body.session_properties,
            )
        except Exception as exc:
            logger.error(
                "cube query failed for workspace=%s publication=%s error_type=%s",
                body.workspace_id,
                body.publication_id,
                type(exc).__name__,
            )
            raise HTTPException(422, "cube query failed") from exc
        return QueryResponse(
            workspace_id=body.workspace_id,
            publication_id=body.publication_id,
            backend=result.backend,
            dialect_sql=result.dialect_sql,
            columns=result.columns,
            rows=result.rows,
            truncated=result.truncated,
        )

    @app.post("/v1/questions", response_model=QuestionResponse)
    async def ask(
        body: QuestionRequest,
        authorization: str = Header(default=""),
    ) -> QuestionResponse:
        entry = resolve(body)
        authenticate(entry, authorization)
        try:
            return await question_service.answer(entry, body)
        except ModelNotConfiguredError as exc:
            raise HTTPException(503, "question model is not configured") from exc
        except ModelAPIError as exc:
            raise HTTPException(503, "question model is temporarily unavailable") from exc
        except (UnexpectedModelBehavior, UsageLimitExceeded) as exc:
            logger.error(
                "question model output rejected for workspace=%s publication=%s "
                "error_type=%s",
                body.workspace_id,
                body.publication_id,
                type(exc).__name__,
            )
            raise HTTPException(502, "question model returned invalid output") from exc
        except Exception as exc:
            logger.error(
                "question processing failed for workspace=%s publication=%s error_type=%s",
                body.workspace_id,
                body.publication_id,
                type(exc).__name__,
            )
            raise HTTPException(422, "question processing failed") from exc

    return app
