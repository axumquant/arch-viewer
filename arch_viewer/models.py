"""Architecture data models — the backbone that every view renders from."""

from __future__ import annotations

import time
from enum import Enum
from pydantic import BaseModel, Field


class ComponentType(str, Enum):
    BACKEND = "backend"
    FRONTEND = "frontend"
    EXTENSION = "extension"
    DATABASE = "database"
    CACHE = "cache"
    QUEUE = "queue"
    API_GATEWAY = "api_gateway"
    AUTH = "auth"
    STORAGE = "storage"
    CI_CD = "ci_cd"
    CONFIG = "config"
    DOCKER = "docker"
    MCP_SERVER = "mcp_server"
    WORKER = "worker"
    OTHER = "other"


class DataFlowDirection(str, Enum):
    UNIDIRECTIONAL = "unidirectional"
    BIDIRECTIONAL = "bidirectional"


class APIRoute(BaseModel):
    """An HTTP/WS endpoint."""
    method: str = "GET"
    path: str = "/"
    handler: str = ""
    file: str = ""
    description: str = ""


class FileInfo(BaseModel):
    """A single file in the project."""
    path: str
    language: str = "text"
    category: str = "other"
    size: int = 0
    modified: float = 0.0
    imports: list[str] = Field(default_factory=list)
    exports: list[str] = Field(default_factory=list)
    summary: str = ""  # AI-generated one-liner


class Component(BaseModel):
    """A logical component (backend, frontend, etc.)."""
    name: str
    type: ComponentType
    path: str
    tech_stack: list[str] = Field(default_factory=list)
    description: str = ""  # AI-generated description
    entry_points: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    api_routes: list[APIRoute] = Field(default_factory=list)
    env_vars: list[str] = Field(default_factory=list)


class DataFlow(BaseModel):
    """A connection between two components."""
    source: str  # component name
    target: str  # component name
    protocol: str = ""  # http, ws, grpc, message_queue, etc.
    description: str = ""
    direction: DataFlowDirection = DataFlowDirection.UNIDIRECTIONAL


class DependencyInfo(BaseModel):
    """External dependency."""
    name: str
    version: str = ""
    category: str = ""  # runtime, dev, peer


class Architecture(BaseModel):
    """
    The complete architecture model — the single source of truth.
    AI agent updates this, dashboard renders it, MCP tools query it.
    """
    project_name: str = ""
    description: str = ""  # AI-generated project description
    components: list[Component] = Field(default_factory=list)
    data_flows: list[DataFlow] = Field(default_factory=list)
    dependencies: list[DependencyInfo] = Field(default_factory=list)
    file_tree: dict = Field(default_factory=dict)  # nested dict tree
    stats: dict = Field(default_factory=dict)
    ai_summary: str = ""  # AI-generated architecture narrative
    last_analyzed: float = Field(default_factory=time.time)
    analysis_version: int = 0

    def get_component(self, name: str) -> Component | None:
        for c in self.components:
            if c.name == name:
                return c
        return None

    def get_flows_for(self, component_name: str) -> list[DataFlow]:
        return [
            f for f in self.data_flows
            if f.source == component_name or f.target == component_name
        ]
