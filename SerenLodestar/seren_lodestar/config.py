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
    """Cluster topology — the nodes this head orchestrates.

    Node type is irrelevant here: anything running a SerenObservatory
    qualifies, Jetson or otherwise.
    """
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
            # 0.0.0.0 is a BIND address. As a destination Linux quietly routes
            # it to localhost, so this works right up until it's read by
            # something that doesn't - and it reads like a typo besides.
            if "//0.0.0.0" in n.agent_url:
                return (f"cluster.nodes[{i}].agent_url uses 0.0.0.0 "
                        f"(node='{n.name}') - use 127.0.0.1 for a local "
                        "Observatory, or the node's real address")
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


# NOTE: this module used to also define JetsonNodeOptions, ClusterOptions,
# RuntimeOptions and RuntimeHostOptions — a second, parallel config family
# left over from the C# port. Nothing imported them (the only imports from
# here are LodestarConfig and load_config), and JetsonNodeOptions was
# defined a SECOND time in dtos.py, which is the live one that cluster.py
# and agent_client.py actually use. Two classes with one name in one package
# is an import-the-wrong-type trap waiting on someone in a hurry.
#
# RuntimeHostOptions was the loudest tell: RuntimeHost is what this service
# was called before it was Lodestar. A fossil wearing a fossil's name.
#
# The live option types live in dtos.py. Add fields there.


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

    # Validate cluster config.
    #
    # LOUD, because the failure is otherwise invisible. A cluster head with
    # no cluster starts perfectly, serves every route, and simply reports
    # nothing — so "Lodestar can't see my node" looks like a network problem
    # for as long as you're willing to believe it is. The single most common
    # cause is `nodes:` written at the top level instead of under `cluster:`,
    # which YAML is happy to accept and this loader silently ignores, so the
    # message names that specifically rather than saying "empty".
    err = cluster.validate()
    if err:
        log.warning("cluster config problem: %s", err)
        if not cluster.nodes:
            top_level_nodes = isinstance(data.get("nodes"), list) and data["nodes"]
            log.warning(
                "Lodestar has NO NODES and will orchestrate nothing.%s",
                (
                    "\n  Found a TOP-LEVEL `nodes:` block with "
                    f"{len(data['nodes'])} entr"
                    f"{'y' if len(data['nodes']) == 1 else 'ies'} — it must be "
                    "nested under `cluster:`:\n"
                    "      cluster:\n"
                    "        nodes:\n"
                    "          - name: \"nuc\"\n"
                    "            agent_url: \"http://127.0.0.1:7777\"\n"
                    "            is_host: true"
                    if top_level_nodes else
                    "\n  Add a `cluster.nodes` list — see "
                    "seren-lodestar.yaml.sample."
                ),
            )

    return _apply_env_overrides(cfg)
