from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


class EntryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DuckDBBackend(EntryModel):
    type: Literal["duckdb"]


class PostgresBackend(EntryModel):
    type: Literal["postgres"]
    env_prefix: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")


Backend = Annotated[DuckDBBackend | PostgresBackend, Field(discriminator="type")]


class PublicationSource(EntryModel):
    model: str = Field(min_length=1, max_length=128)
    dataset_id: str = Field(min_length=1, max_length=128)
    source_name: str = Field(min_length=1, max_length=255)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    sheet: str = Field(min_length=1, max_length=255)


class WorkspacePublication(EntryModel):
    workspace_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
    publication_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
    access_token_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{1,127}$")
    access_token_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    project_path: Path
    profile_name: str
    backend: Backend
    sources: tuple[PublicationSource, ...] = ()

    @model_validator(mode="after")
    def one_access_token_source(self):
        if (self.access_token_env is None) == (self.access_token_sha256 is None):
            raise ValueError("exactly one workspace access token source is required")
        return self


class RegistryDocument(EntryModel):
    workspaces: tuple[WorkspacePublication, ...]

    @model_validator(mode="after")
    def unique_publications(self):
        keys = [(entry.workspace_id, entry.publication_id) for entry in self.workspaces]
        if len(keys) != len(set(keys)):
            raise ValueError("workspace_id and publication_id pairs must be unique")
        return self


def open_workspace_registry(
    config_path: Path, projects_root: Path
) -> WorkspaceRegistry:
    resolved_config = config_path.resolve()
    resolved_projects = projects_root.resolve(strict=True)
    if not resolved_config.exists():
        resolved_config.parent.mkdir(parents=True, exist_ok=True)
        resolved_config.write_text('{"workspaces": []}\n', encoding="utf-8")
    if not resolved_config.is_file():
        raise ValueError("workspace registry must be a file")
    document = TypeAdapter(RegistryDocument).validate_json(
        resolved_config.read_text("utf-8")
    )
    return WorkspaceRegistry(resolved_config, resolved_projects, document)


class WorkspaceRegistry:
    def __init__(
        self,
        config_path: Path,
        projects_root: Path,
        document: RegistryDocument,
    ):
        self.config_path = config_path
        self.projects_root = projects_root
        self._lock = threading.RLock()
        self._entries = {
            (entry.workspace_id, entry.publication_id): entry
            for entry in document.workspaces
        }

    def get(self, workspace_id: str, publication_id: str) -> WorkspacePublication:
        try:
            return self._entries[(workspace_id, publication_id)]
        except KeyError as exc:
            raise LookupError("workspace publication was not found") from exc

    @staticmethod
    def authenticate(entry: WorkspacePublication, token: str) -> bool:
        if entry.access_token_env:
            expected = os.environ.get(entry.access_token_env)
            return bool(
                expected
                and len(expected) >= 16
                and secrets.compare_digest(token, expected)
            )
        digest = hashlib.sha256(token.encode()).hexdigest()
        return bool(
            entry.access_token_sha256
            and secrets.compare_digest(digest, entry.access_token_sha256)
        )

    def register(
        self,
        *,
        workspace_id: str,
        publication_id: str,
        access_token: str,
        project_path: Path,
        backend: Backend,
        sources: tuple[PublicationSource, ...] = (),
    ) -> WorkspacePublication:
        if len(access_token) < 16:
            raise ValueError("workspace access token must contain at least 16 characters")
        project = self._resolve_project(project_path)
        profile_name = self._profile_name(workspace_id, publication_id)
        relative_project = project.relative_to(self.projects_root)
        entry = WorkspacePublication(
            workspace_id=workspace_id,
            publication_id=publication_id,
            access_token_sha256=hashlib.sha256(access_token.encode()).hexdigest(),
            project_path=relative_project,
            profile_name=profile_name,
            backend=backend,
            sources=sources,
        )
        with self._lock:
            old = self._entries.get((workspace_id, publication_id))
            if old is not None:
                if old != entry:
                    raise FileExistsError(
                        "publication id already exists with different content"
                    )
                self._build_project(project)
                self._write_profile(profile_name, project, backend)
                return old
            self._build_project(project)
            self._write_profile(profile_name, project, backend)
            self._entries[(workspace_id, publication_id)] = entry
            try:
                self._persist()
            except Exception:
                self._entries.pop((workspace_id, publication_id), None)
                raise
        return entry

    def project(self, entry: WorkspacePublication) -> Path:
        return self._resolve_project(entry.project_path)

    def manifest(self, entry: WorkspacePublication) -> dict[str, Any]:
        target = self.project(entry) / "target" / "mdl.json"
        if not target.is_file():
            raise ValueError("Wren project target is missing")
        document = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("Wren project target must be a JSON object")
        return document

    def _persist(self) -> None:
        document = RegistryDocument(workspaces=tuple(self._entries.values()))
        target = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        target.write_text(
            document.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        os.replace(target, self.config_path)

    def _resolve_project(self, candidate: Path) -> Path:
        path = candidate if candidate.is_absolute() else self.projects_root / candidate
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ValueError("Wren project directory does not exist") from exc
        if not resolved.is_relative_to(self.projects_root) or not resolved.is_dir():
            raise ValueError("Wren project must be a directory under the projects root")
        if not (resolved / "wren_project.yml").is_file():
            raise ValueError("Wren project is missing wren_project.yml")
        return resolved

    @staticmethod
    def _profile_name(workspace_id: str, publication_id: str) -> str:
        digest = hashlib.sha256(
            f"{workspace_id}\0{publication_id}".encode("utf-8")
        ).hexdigest()[:24]
        return f"wren_http_{digest}"

    @staticmethod
    def _build_project(project: Path) -> None:
        from wren.context import build_json, save_target, validate_project

        errors = [
            str(item)
            for item in validate_project(project)
            if getattr(item, "level", None) == "error"
        ]
        if errors:
            raise ValueError("Wren project validation failed: " + "; ".join(errors))
        save_target(build_json(project), project)

    @staticmethod
    def _write_profile(name: str, project: Path, backend: Backend) -> None:
        from wren.profile import add_profile

        if isinstance(backend, DuckDBBackend):
            profile = {
                "datasource": "duckdb",
                "url": str(project),
                "format": "duckdb",
            }
        else:
            prefix = backend.env_prefix
            profile = {
                "datasource": "postgres",
                "host": f"${{{prefix}HOST}}",
                "port": f"${{{prefix}PORT}}",
                "database": f"${{{prefix}DATABASE}}",
                "user": f"${{{prefix}USER}}",
                "password": f"${{{prefix}PASSWORD}}",
            }
        add_profile(name, profile)
