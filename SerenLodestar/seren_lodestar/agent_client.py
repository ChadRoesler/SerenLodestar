"""
seren_lodestar.agent_client
=======================================================================

Typed HTTP client for the per-Jetson seren-agent API at /api/v1/...
Ported from SerenCluster/JetsonAgentClient.cs.

One instance per agent — see cluster.py for routing across many.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional
from urllib.parse import quote

import httpx

from .dtos import (
    AgentUpdateResponse,
    HealthResponse,
    LogsResponse,
    NodeResponse,
    PingResponse,
    PortHealth,
    RebootCancelResponse,
    RebootResponse,
    ReclaimResponse,
    ServiceLifecycleResponse,
    ServiceManifest,
    ServiceStatus,
    ServicesResponse,
    ThermalResponse,
    VersionResponse,
    JetsonNodeOptions,
)

log = logging.getLogger("seren_lodestar.agent_client")


class JetsonAgentClient:
    """Typed HTTP client for one Jetson's seren-agent API."""

    def __init__(self, options: JetsonNodeOptions, log_fn=None):
        if not options.name:
            raise ValueError("node name must not be empty")
        if not options.agent_url:
            raise ValueError("agent_url must not be empty")

        self._node_name = options.name
        self._log = log_fn or (lambda msg: log.info(f"[{self._node_name}] {msg}"))
        self._agent_token = options.agent_token
        self._agent_update_path = options.agent_update_path
        self._is_host = options.is_host
        self._nickname = options.nickname or ""

        base = options.agent_url.rstrip("/") + "/"
        headers = {}
        if options.agent_token:
            headers["Authorization"] = f"Bearer {options.agent_token}"
        self._client = httpx.AsyncClient(base_url=base, headers=headers, timeout=35.0)

    # ── properties ──────────────────────────────────────────────────────────

    @property
    def node_name(self) -> str:
        return self._node_name

    @property
    def base_url(self) -> str:
        return str(self._client.base_url)

    @property
    def agent_update_path(self) -> str:
        return self._agent_update_path

    @property
    def is_host(self) -> bool:
        return self._is_host

    @property
    def nickname(self) -> str:
        return self._nickname

    # ── system endpoints ────────────────────────────────────────────────────

    async def ping_async(self) -> Optional[PingResponse]:
        return await self._get_json("api/v1/system/ping", PingResponse)

    async def version_async(self) -> Optional[VersionResponse]:
        return await self._get_json("api/v1/system/version", VersionResponse)

    async def get_node_async(self) -> Optional[NodeResponse]:
        return await self._get_json("api/v1/system/node", NodeResponse)

    async def get_thermal_async(self) -> Optional[ThermalResponse]:
        return await self._get_json("api/v1/system/thermal", ThermalResponse)

    async def get_services_async(self) -> Optional[ServicesResponse]:
        return await self._get_json("api/v1/system/services", ServicesResponse)

    async def get_health_async(self) -> Optional[HealthResponse]:
        return await self._get_json("api/v1/system/health", HealthResponse)

    async def reclaim_async(
        self, exclude: Optional[list[str]] = None
    ) -> Optional[ReclaimResponse]:
        body = {"exclude": exclude or []}
        return await self._post_json("api/v1/system/reclaim", body, ReclaimResponse)

    async def reboot_async(
        self, delay_minutes: int = 1
    ) -> Optional[RebootResponse]:
        body = {"delay_minutes": delay_minutes}
        return await self._post_json("api/v1/system/reboot", body, RebootResponse)

    async def reboot_cancel_async(self) -> Optional[RebootCancelResponse]:
        return await self._post_json(
            "api/v1/system/reboot/cancel", None, RebootCancelResponse
        )

    async def push_agent_update_async(
        self, package_bytes: bytes, filename: str, dest_path: str
    ) -> Optional[AgentUpdateResponse]:
        """Push a seren-agent.tar.gz package to this node."""
        import httpx

        try:
            files = {
                "package": (filename, package_bytes, "application/octet-stream"),
                "dest_path": (None, dest_path),
            }
            resp = await self._client.post(
                "api/v1/system/agent-update",
                files=files,
                timeout=120.0,
            )
            if not resp.is_success:
                self._log(
                    f"POST agent-update -> HTTP {resp.status_code}"
                )
                return AgentUpdateResponse(
                    ok=False, error=f"HTTP {resp.status_code}"
                )
            return _from_dict(resp.json(), AgentUpdateResponse)
        except httpx.TimeoutException:
            self._log("POST agent-update -> timeout")
            return AgentUpdateResponse(ok=False, error="timeout")
        except Exception as ex:
            self._log(
                f"POST agent-update -> {type(ex).__name__}: {ex}"
            )
            return AgentUpdateResponse(ok=False, error=str(ex))

    # ── per-service lifecycle ───────────────────────────────────────────────

    async def get_service_manifest_async(
        self, service: str
    ) -> Optional[ServiceManifest]:
        path = f"api/v1/service/{quote(service, safe='')}/manifest"
        return await self._get_json(path, ServiceManifest)

    async def get_service_status_async(
        self, service: str
    ) -> Optional[ServiceStatus]:
        path = f"api/v1/service/{quote(service, safe='')}/status"
        return await self._get_json(path, ServiceStatus)

    async def get_service_health_async(
        self, service: str
    ) -> Optional[PortHealth]:
        path = f"api/v1/service/{quote(service, safe='')}/health"
        return await self._get_json(path, PortHealth)

    async def start_service_async(
        self, service: str
    ) -> Optional[ServiceLifecycleResponse]:
        path = f"api/v1/service/{quote(service, safe='')}/start"
        return await self._post_json(path, None, ServiceLifecycleResponse)

    async def stop_service_async(
        self, service: str
    ) -> Optional[ServiceLifecycleResponse]:
        path = f"api/v1/service/{quote(service, safe='')}/stop"
        return await self._post_json(path, None, ServiceLifecycleResponse)

    async def restart_service_async(
        self, service: str
    ) -> Optional[ServiceLifecycleResponse]:
        path = f"api/v1/service/{quote(service, safe='')}/restart"
        return await self._post_json(path, None, ServiceLifecycleResponse)

    async def get_service_logs_async(
        self, service: str, lines: int = 100
    ) -> Optional[LogsResponse]:
        path = f"api/v1/service/{quote(service, safe='')}/logs?lines={lines}"
        return await self._get_json(path, LogsResponse)

    # ── service-specific models (llama, comfy, whisper) ─────────────────────

    async def get_service_models_async(
        self, service: str
    ) -> Optional[Any]:
        """Returns raw JSON element since per-service shapes differ."""
        path = f"api/v1/service/{quote(service, safe='')}/models"
        return await self._get_json_raw(path)

    # ── internal HTTP helpers ───────────────────────────────────────────────

    async def _get_json(self, path: str, dto_class: type) -> Optional[Any]:
        try:
            resp = await self._client.get(path)
            if not resp.is_success:
                self._log(f"GET {path} -> HTTP {resp.status_code}")
                return None
            data = resp.json()
            return _from_dict(data, dto_class)
        except httpx.TimeoutException:
            self._log(f"GET {path} -> timeout")
            return None
        except Exception as ex:
            self._log(f"GET {path} -> {type(ex).__name__}: {ex}")
            return None

    async def _get_json_raw(self, path: str) -> Optional[Any]:
        """Return raw JSON data (dict/list) without DTO mapping."""
        try:
            resp = await self._client.get(path)
            if not resp.is_success:
                self._log(f"GET {path} -> HTTP {resp.status_code}")
                return None
            return resp.json()
        except httpx.TimeoutException:
            self._log(f"GET {path} -> timeout")
            return None
        except Exception as ex:
            self._log(f"GET {path} -> {type(ex).__name__}: {ex}")
            return None

    async def _post_json(
        self, path: str, body: Optional[Any], dto_class: type
    ) -> Optional[Any]:
        try:
            if body is None:
                resp = await self._client.post(path)
            else:
                resp = await self._client.post(path, json=body)
            if not resp.is_success:
                self._log(f"POST {path} -> HTTP {resp.status_code}")
                return None
            data = resp.json()
            return _from_dict(data, dto_class)
        except httpx.TimeoutException:
            self._log(f"POST {path} -> timeout")
            return None
        except Exception as ex:
            self._log(f"POST {path} -> {type(ex).__name__}: {ex}")
            return None


# ── helper: dict -> dataclass ───────────────────────────────────────────────

def _from_dict(data, dto_class):
    """dict -> dataclass, recursing into nested dataclasses, list[...] and dict[str, ...]."""
    import dataclasses, typing
    if data is None:
        return None
    if not dataclasses.is_dataclass(dto_class):
        return data
    hints = typing.get_type_hints(dto_class)
    field_names = {f.name for f in dataclasses.fields(dto_class)}
    kwargs = {}
    for k, v in (data.items() if isinstance(data, dict) else []):
        name = k.replace("-", "_").replace(".", "_")
        if name in field_names:
            kwargs[name] = _coerce(v, hints.get(name))
    return dto_class(**kwargs)

def _coerce(value, hint):
    import dataclasses, typing
    if hint is None or value is None:
        return value
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if origin is typing.Union:                       # Optional[X]
        non_none = [a for a in args if a is not type(None)]
        return _coerce(value, non_none[0]) if len(non_none) == 1 else value
    if origin in (list,) and args:
        return [_coerce(i, args[0]) for i in value]
    if origin in (dict,) and len(args) == 2:
        return {k: _coerce(val, args[1]) for k, val in value.items()}
    if dataclasses.is_dataclass(hint):
        return _from_dict(value, hint)
    return value

async def aclose(self) -> None:
    """Close the underlying HTTP client and its connection pool."""
    await self._client.aclose()