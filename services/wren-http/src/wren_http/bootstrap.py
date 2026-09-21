import os
from collections.abc import Callable

from fastapi import FastAPI
from pydantic_ai.models import Model

from .api import ApiDependencies, build_api
from .model import build_bailian_model
from .question import WrenQuestionService
from .registry import open_workspace_registry
from .service import WrenProjectRuntime
from .settings import ServiceSettings


def create_app(
    settings: ServiceSettings,
    *,
    model_factory: Callable[[ServiceSettings], Model | None] = build_bailian_model,
) -> FastAPI:
    """Construct the service from explicit runtime dependencies."""

    settings.duckdb_root.mkdir(parents=True, exist_ok=True)
    wren_home = settings.wren_home or settings.workspaces_file.parent / "wren-home"
    wren_home.mkdir(parents=True, exist_ok=True)
    os.environ["WREN_HOME"] = str(wren_home.resolve())
    registry = open_workspace_registry(settings.workspaces_file, settings.duckdb_root)
    runtime = WrenProjectRuntime(
        registry,
        memory_enabled=settings.memory_enabled,
    )
    model = model_factory(settings)
    question_service = WrenQuestionService(runtime, model, settings)
    dependencies = ApiDependencies(
        settings=settings,
        registry=registry,
        runtime=runtime,
        question_service=question_service,
    )
    return build_api(dependencies)
