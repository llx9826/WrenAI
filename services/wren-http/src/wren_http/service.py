from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlglot

from .registry import WorkspacePublication, WorkspaceRegistry


@dataclass(frozen=True)
class QueryData:
    backend: str
    dialect_sql: str
    logical_sql: str | None = None
    columns: tuple[str, ...] = ()
    rows: tuple[dict[str, Any], ...] = ()
    truncated: bool = False


class SessionToolkit:
    """Bind trusted request properties to every Wren planning and query call."""

    def __init__(self, toolkit, properties: dict[str, str]):
        self.toolkit = toolkit
        self.properties = properties

    def query(self, sql, limit=None):
        return self.toolkit.query(sql, limit=limit, properties=self.properties)

    def dry_plan(self, sql):
        return self.toolkit.dry_plan(sql, properties=self.properties)

    def dry_run(self, sql):
        return self.toolkit.dry_run(sql, properties=self.properties)

    @property
    def memory(self):
        return self.toolkit.memory

    def toolset(self, **kwargs):
        from wren_pydantic import WrenToolkit

        return WrenToolkit.toolset(self, **kwargs)

    def instructions(self, **kwargs):
        from wren_pydantic import WrenToolkit

        return WrenToolkit.instructions(self, **kwargs)

    def __getattr__(self, name):
        return getattr(self.toolkit, name)


class WrenProjectRuntime:
    """Thin, workspace-aware adapter over the official WrenToolkit API."""

    def __init__(self, registry: WorkspaceRegistry, *, memory_enabled: bool = True):
        self.registry = registry
        self.memory_enabled = memory_enabled
        self._toolkits: dict[tuple[str, str], Any] = {}
        self._agent_toolkits: dict[tuple[str, str], Any] = {}
        self._memory_ready: set[tuple[str, str]] = set()
        self._agent_locks: dict[tuple[str, str], threading.RLock] = {}
        self._lock = threading.RLock()

    def models(self, entry: WorkspacePublication) -> tuple[dict[str, Any], ...]:
        return tuple(self.registry.manifest(entry).get("models", ()))

    def plan(
        self,
        entry: WorkspacePublication,
        sql: str,
        properties: dict[str, str] | None = None,
    ) -> QueryData:
        self._validate_read_query(sql)
        planned = self.toolkit(entry).dry_plan(sql, properties=properties)
        return QueryData(backend=entry.backend.type, dialect_sql=planned)

    def dry_run(
        self,
        entry: WorkspacePublication,
        sql: str,
        properties: dict[str, str] | None = None,
    ) -> QueryData:
        planned = self.plan(entry, sql, properties)
        self.toolkit(entry).dry_run(sql, properties=properties)
        return planned

    def query(
        self,
        entry: WorkspacePublication,
        sql: str,
        limit: int,
        properties: dict[str, str] | None = None,
    ) -> QueryData:
        planned = self.plan(entry, sql, properties)
        table = self.toolkit(entry).query(
            sql,
            limit=limit + 1,
            properties=properties,
        )
        records = table.to_pylist()
        return QueryData(
            backend=entry.backend.type,
            dialect_sql=planned.dialect_sql,
            columns=tuple(table.column_names),
            rows=tuple(records[:limit]),
            truncated=len(records) > limit,
        )

    def cube_query(
        self,
        entry: WorkspacePublication,
        *,
        cube: str,
        measures: tuple[str, ...],
        dimensions: tuple[str, ...],
        time_dimensions: tuple[dict[str, Any], ...],
        filters: tuple[dict[str, Any], ...],
        order_by: tuple[dict[str, Any], ...],
        limit: int,
        offset: int,
        properties: dict[str, str] | None = None,
    ) -> QueryData:
        from wren_core import cube_query_to_sql

        query = {
            "cube": cube,
            "measures": list(measures),
            "dimensions": list(dimensions),
            "timeDimensions": list(time_dimensions),
            "filters": list(filters),
            "orderBy": list(order_by),
            "limit": limit + 1,
            "offset": offset,
        }
        logical_sql = cube_query_to_sql(
            json.dumps(query),
            json.dumps(self.registry.manifest(entry)),
        )
        planned = self.plan(entry, logical_sql, properties)
        table = self.toolkit(entry).query(logical_sql, properties=properties)
        records = table.to_pylist()
        return QueryData(
            backend=entry.backend.type,
            dialect_sql=planned.dialect_sql,
            logical_sql=logical_sql,
            columns=tuple(table.column_names),
            rows=tuple(records[:limit]),
            truncated=len(records) > limit,
        )

    def toolkit(self, entry: WorkspacePublication):
        """Open the official Toolkit used by direct SQL endpoints."""

        key = (entry.workspace_id, entry.publication_id)
        with self._lock:
            toolkit = self._toolkits.get(key)
            if toolkit is None:
                from wren_pydantic import WrenToolkit

                toolkit = WrenToolkit.from_project(
                    self.registry.project(entry), profile=entry.profile_name
                )
                self._toolkits[key] = toolkit
            return toolkit

    def agent_toolkit(self, entry: WorkspacePublication):
        """Open an isolated Toolkit with Wren Memory enabled for one publication."""

        if not self.memory_enabled:
            return self.toolkit(entry)
        key = (entry.workspace_id, entry.publication_id)
        project = self.registry.project(entry)
        with self._lock:
            agent_lock = self._agent_locks.setdefault(key, threading.RLock())
        with agent_lock:
            toolkit = self._agent_toolkits.get(key)
            if toolkit is None:
                memory_path = project / ".wren" / "memory"
                memory_path.mkdir(parents=True, exist_ok=True)
                self._prepare_memory(
                    project,
                    memory_path,
                    self.registry.manifest(entry),
                )
                from wren_pydantic import WrenToolkit

                toolkit = WrenToolkit.from_project(project, profile=entry.profile_name)
                self._agent_toolkits[key] = toolkit
            return toolkit

    def session_toolkit(
        self,
        entry: WorkspacePublication,
        properties: dict[str, str],
    ) -> SessionToolkit:
        return SessionToolkit(self.agent_toolkit(entry), properties)

    def _prepare_memory(
        self,
        project: Path,
        memory_path: Path,
        manifest: dict[str, Any],
    ) -> None:
        key = (str(project), str(memory_path))
        if key in self._memory_ready:
            return
        from wren.memory.markdown import load_query_pairs
        from wren.memory.store import MemoryStore

        store = MemoryStore(path=memory_path)
        if not store.schema_is_current(manifest):
            store.index_schema(manifest, seed_queries=True)
        store.sync_markdown_queries(load_query_pairs(project))
        self._memory_ready.add(key)

    @staticmethod
    def _validate_read_query(sql: str) -> None:
        statements = sqlglot.parse(sql, read="duckdb")
        if len(statements) != 1 or not isinstance(statements[0], sqlglot.exp.Query):
            raise ValueError("only one read-only query is allowed")
