"""
seren_lodestar.config
════════════════════════════════════════════════════════════════════════

Service-specific config for the Lodestar cluster head. Uses seren_meninges
shared blocks (ServerConfig, TlsConfig) plus its own cluster-specific
sections: cluster (node topology) and scheduling.

Follows the same pattern as seren_loci.config, seren_memory.config,
seren_corpus_callosum.config, seren_probe.config, and
seren_workbench.config — the family's lenient-load discipline.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from seren_meninges import ServerConfig, TlsConfig

log = logging.getLogger(__name__)

# Port 6361 — family convention: Lodestar
DEFAULT_PORT = 6361


@dataclass
class JetsonNodeConfig:
    """One node in the cluster."""
    name: str = ""
    agent_url: str = ""
    agent_token: str = ""
    preferred_for: list[str] = field(default_factory=list)
    agent_update_path: str = ""
    is_host: bool = False
    nickname: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "JetsonNodeConfig":
        return cls(
            name=str(d.get("name", "") or ""),
            agent_url=str(d.get("agent_url", "") or ""),
            agent_token=str(d.get("agent_token", "") or ""),
            preferred_for=[str(s) for s in (d.get("preferred_for", []) or []) if s],
            agent_update_path=str(d.get("agent_update_path", "") or ""),
            is_host=bool(d.get("is_host", False)),
            nickname=str(d.get("nickname", "") or ""),
        )


@dataclass
class ClusterConfig:
    """Cluster topology — the list of Jetson nodes."""
    nodes: list[JetsonNodeConfig] = field(default_factory=list)
    refresh_interval_seconds: int = 1800  # 30 minutes
    discovery_timeout_seconds: float = 2.0
    health_strict_mode: bool = False

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "ClusterConfig":
        d = d or {}
        nodes_raw = d.get("nodes", []) or []
        nodes = [JetsonNodeConfig.from_dict(n) for n in nodes_raw]

        refresh = d.get("refresh_interval", "00:30:00")
        timeout = d.get("discovery_timeout", "00:00:02")

        return cls(
            nodes=nodes,
            refresh_interval_seconds=_parse_duration(refresh, default_seconds=1800),
            discovery_timeout_seconds=_parse_duration(timeout, default_seconds=2.0),
            health_strict_mode=bool(d.get("health_strict_mode", False)),
        )

    def validate(self) -> Optional[str]:
        """Returns an error string if validation fails, else None."""
        if not self.nodes:
            return "cluster.nodes is empty"
        for i, n in enumerate(self.nodes):
            if not n.name:
                return f"cluster.nodes[{i}].name is empty"
            if not n.agent_url:
                return f"cluster.nodes[{i}].agent_url is empty (node='{n.name}')"
        return None


@dataclass
class SchedulerConfig:
    """Scheduler knobs."""
    persistence_dir: str = ""

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "SchedulerConfig":
        d = d or {}
        return cls(
            persistence_dir=str(d.get("persistence_dir", "") or ""),
        )


@dataclass
class RuntimeConfig:
    """Runtime-specific overrides — agent package path for node updates."""
    inject_bearer_token: bool = True
    agent_package_path: str = ""

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "RuntimeConfig":
        d = d or {}
        return cls(
            inject_bearer_token=bool(d.get("inject_bearer_token", True)),
            agent_package_path=_expand_tilde(str(d.get("agent_package_path", "") or "")),
        )


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


@dataclass
class LodestarConfig:
    """The top-level config, composed from shared blocks + service blocks."""
    server: ServerConfig = field(default_factory=lambda: ServerConfig(port=DEFAULT_PORT))
    tls: TlsConfig = field(default_factory=TlsConfig)
    cluster: ClusterConfig = field(default_factory=ClusterConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)


def _expand_tilde(path: str) -> str:
    """Expand leading ~/ to the user's home directory."""
    if not path:
        return path
    home = Path.home()
    if path == "~":
        return str(home)
    if path.startswith("~/"):
        return str(home / path[2:])
    return path


def _parse_duration(val: str, default_seconds: float) -> float:
    """Parse HH:MM:SS or a bare number (seconds) to a float seconds value."""
    if not val:
        return default_seconds
    try:
        return float(val)
    except ValueError:
        pass
    parts = val.split(":")
    if len(parts) == 3:
        try:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        except ValueError:
            pass
    log.warning("unparseable duration %r — using default %ss", val, default_seconds)
    return default_seconds


def _apply_env_overrides(cfg: LodestarConfig) -> LodestarConfig:
    """SEREN_LODESTAR_* env wins last."""
    env = os.environ
    if v := env.get("SEREN_LODESTAR_HOST"):
        cfg.server.host = v
    if v := env.get("SEREN_LODESTAR_PORT"):
        cfg.server.port = int(v)
    if v := env.get("SEREN_LODESTAR_BEARER_TOKEN"):
        cfg.server.bearer_token = v
    if v := env.get("SEREN_LODESTAR_BEARER_TOKEN_ENV"):
        cfg.server.bearer_token_env = v
    if v := env.get("SEREN_LODESTAR_BEARER_TOKEN_KEYRING"):
        cfg.server.bearer_token_keyring = v
    if v := env.get("SEREN_LODESTAR_TRUST_SYSTEM_STORE"):
        cfg.tls.trust_system_store = v.lower() in ("1", "true", "yes", "on")
    return cfg


def load_config(path: Optional[str] = None) -> LodestarConfig:
    """Defaults -> yaml -> env (later wins). A missing file is fine — defaults
    + env is a valid zero-config run.

    Reads seren-lodestar.yaml.
    """
    data: dict[str, Any] = {}
    candidate = path or os.environ.get("SEREN_LODESTAR_CONFIG") or "seren-lodestar.yaml"
    cfg_path = Path(os.path.expanduser(candidate))

    if cfg_path.is_file():
        try:
            with open(cfg_path) as f:
                data = yaml.safe_load(f) or {}
        except Exception:  # noqa: BLE001
            data = {}

    server = ServerConfig.from_dict(data.get("server"), default_port=DEFAULT_PORT)
    tls = TlsConfig.from_dict(data.get("tls"))
    cluster = ClusterConfig.from_dict(data.get("cluster"))
    scheduler = SchedulerConfig.from_dict(data.get("scheduler"))
    runtime = RuntimeConfig.from_dict(data.get("runtime"))

    cfg = LodestarConfig(
        server=server,
        tls=tls,
        cluster=cluster,
        scheduler=scheduler,
        runtime=runtime,
    )

    # Validate cluster config
    err = cluster.validate()
    if err:
        log.warning("cluster validation: %s", err)

    return _apply_env_overrides(cfg)
