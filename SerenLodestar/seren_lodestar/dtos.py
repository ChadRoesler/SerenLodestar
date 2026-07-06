"""
Data-transfer objects matching the Seren agent API responses.
Ported from SerenCluster/Models/AgentDtos.cs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ── System endpoints ───────────────────────────────────────────────────────

@dataclass
class PingResponse:
    ok: bool
    ts: int  # unix timestamp


@dataclass
class VersionResponse:
    agent_version: str
    manifest_schema: int


@dataclass
class NodeManifest:
    hostname: Optional[str] = None
    ip_addresses: Optional[list[str]] = None
    platform: Optional[str] = None
    jetpack_release: Optional[str] = None
    cuda_arch: Optional[str] = None
    cuda_version: Optional[str] = None
    unified_memory_gb: Optional[int] = None
    cpu_cores: Optional[int] = None
    installed_at: Optional[str] = None
    schema_version: int = 0


@dataclass
class NodeRuntime:
    load_avg: Optional[list[float]] = None
    memory_mb_total: Optional[int] = None
    memory_mb_available: Optional[int] = None
    memory_pct_used: Optional[float] = None
    uptime_seconds: Optional[int] = None


@dataclass
class NodeResponse:
    manifest: Optional[NodeManifest] = None
    runtime: Optional[NodeRuntime] = None


@dataclass
class ThermalZone:
    zone: str
    type: str
    temp_c: float


@dataclass
class ThermalResponse:
    available: bool
    zones: Optional[list[ThermalZone]] = None
    max_temp_c: Optional[float] = None


# ── Services ────────────────────────────────────────────────────────────────

@dataclass
class ServiceManifest:
    service: str
    implementation: Optional[str] = None
    port: int = 0
    endpoint: Optional[str] = None
    start_script: Optional[str] = None
    stop_script: Optional[str] = None
    pid_path: Optional[str] = None
    log_path: Optional[str] = None
    venv_path: Optional[str] = None
    repo_path: Optional[str] = None
    persistence_dir: Optional[str] = None
    installed_at: Optional[str] = None
    schema_version: int = 0
    service_specific: Optional[dict[str, Any]] = None


@dataclass
class PortHealth:
    ok: bool
    status_code: Optional[int] = None
    latency_ms: Optional[int] = None
    probed_path: Optional[str] = None
    error: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class ServiceStatus:
    service: Optional[str] = None
    service_type: Optional[str] = None
    running: bool = False
    pid: Optional[int] = None
    memory_mb: Optional[int] = None
    cpu_percent: Optional[float] = None
    uptime_seconds: Optional[int] = None
    library_mode: Optional[bool] = None
    port_health: Optional[PortHealth] = None


@dataclass
class ServiceEntry:
    manifest: ServiceManifest
    status: Optional[ServiceStatus] = None


@dataclass
class ServicesResponse:
    count: int
    services: dict[str, ServiceEntry]


@dataclass
class HealthResponse:
    ok: bool
    total: int
    healthy: int
    degraded: list[str]
    not_running: list[str]


@dataclass
class ReclaimFailure:
    service: str
    error: Optional[str] = None


@dataclass
class ReclaimResponse:
    stopped: list[str]
    kept: list[str]
    failed: Optional[list[ReclaimFailure]] = None


# ── Service lifecycle ───────────────────────────────────────────────────────

@dataclass
class ServiceLifecycleResponse:
    ok: bool
    pid: Optional[int] = None
    already_running: Optional[bool] = None
    was_running: Optional[bool] = None
    exit_code: Optional[int] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    error: Optional[str] = None


@dataclass
class LogsResponse:
    ok: bool
    lines: Optional[list[str]] = None
    log_path: Optional[str] = None
    note: Optional[str] = None
    error: Optional[str] = None


# ── Reboot ──────────────────────────────────────────────────────────────────

@dataclass
class RebootResponse:
    scheduled: bool
    scheduled_at: Optional[str] = None
    delay_minutes: Optional[int] = None
    method: Optional[str] = None
    error: Optional[str] = None
    hint: Optional[str] = None


@dataclass
class RebootCancelResponse:
    cancelled: bool
    error: Optional[str] = None


# ── Agent update ────────────────────────────────────────────────────────────

@dataclass
class AgentUpdateResponse:
    ok: bool
    message: Optional[str] = None
    error: Optional[str] = None


@dataclass
class AgentUpdateNodeResult:
    ok: bool
    node: str
    message: Optional[str] = None
    error: Optional[str] = None


# ── Cluster-internal DTOs ──────────────────────────────────────────────────

@dataclass
class NodeSnapshot:
    online: bool
    installed_services: list[str]
    status: dict[str, Optional[ServiceStatus]]
    last_error: Optional[str] = None
    last_probed: Optional[str] = None  # ISO timestamp string


@dataclass
class RoutedService:
    node_name: str
    capability: str
    manifest: ServiceManifest
    base_url: Optional[str] = None
    library_mode: bool = False


@dataclass
class RefreshSummary:
    total_nodes: int
    online_nodes: int
    per_node: dict[str, NodeSnapshot]


# ── Reclaim request body ───────────────────────────────────────────────────

@dataclass
class ReclaimRequest:
    exclude: Optional[list[str]] = None
    nodes: Optional[list[str]] = None


@dataclass
class RebootRequest:
    delay_minutes: Optional[int] = None


# ── Cluster options (from YAML) ────────────────────────────────────────────

@dataclass
class JetsonNodeOptions:
    name: str
    agent_url: str
    agent_token: str = ""
    preferred_for: list[str] = field(default_factory=list)
    agent_update_path: str = ""
    is_host: bool = False
    nickname: str = ""


@dataclass
class ClusterOptions:
    nodes: list[JetsonNodeOptions] = field(default_factory=list)
    refresh_interval_seconds: int = 1800  # 30 minutes
    discovery_timeout_seconds: float = 2.0
    health_strict_mode: bool = False


# ── Runtime options (from YAML) ────────────────────────────────────────────

@dataclass
class RuntimeOptions:
    host: str = "0.0.0.0"
    port: int = 6361
    bearer_token: str = ""
    inject_bearer_token: bool = True
    agent_package_path: str = ""
    scheduler_persistence_dir: str = ""


@dataclass
class RuntimeHostOptions:
    runtime: RuntimeOptions = field(default_factory=RuntimeOptions)
    cluster: ClusterOptions = field(default_factory=ClusterOptions)
