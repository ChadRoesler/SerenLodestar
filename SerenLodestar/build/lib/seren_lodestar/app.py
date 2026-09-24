"""
seren_lodestar.app
════════════════════════════════════════════════════════════════════════

The FastAPI application for the SerenLodestar cluster head. Wires the
cluster topology manager, discovery service, scheduler, tooling, the
operator dashboard, and the MCP transport.

Serves:
    GET  /                                      — service info + update status
    GET  /health                                — liveness
    GET  /viewer                                — operator dashboard
    GET  /api/v1/system/ping                    — public ping
    GET  /api/v1/system/version                 — public version
    GET  /api/v1/system/status                  — node status
    GET  /api/v1/system/health                  — cluster health
    POST /api/v1/system/reclaim                 — stop services on nodes
    POST /api/v1/system/reboot/{node}           — reboot a node
    POST /api/v1/system/reboot/{node}/cancel    — cancel reboot
    POST /api/v1/system/agent-update            — push Observatory to all nodes
    POST /api/v1/node/{node}/agent-update       — push Observatory to one node
    POST /api/v1/cluster/refresh                — refresh all nodes
    POST /api/v1/cluster/refresh/{node}         — refresh one node
    GET  /api/v1/cluster/capabilities           — capability map
    GET/POST /api/v1/service/{name}/*           — service lifecycle
    GET/POST /api/v1/node/{node}/service/{svc}/* — per-node service
    GET/POST /api/v1/scheduler/tasks            — list/add tasks
    DELETE /api/v1/scheduler/tasks/{name}       — delete a task
    POST /api/v1/scheduler/tasks/{name}/pause   — pause a task
    POST /api/v1/scheduler/tasks/{name}/resume  — resume a task
    POST /api/v1/chat                           — chat inference
    GET  /api/v1/chat/health                    — chat backend health
    GET  /api/v1/chat/last_user_at              — last user activity
    POST /api/v1/chat/inspect                   — debug tool injection
    POST /api/v1/chat/stream                    — streamed chat
    /mcp                                        — MCP transport endpoint

Integrates seren_meninges (config/auth/viewer baseplate) and seren_sinew
(request logging) — following the same pattern as the rest of the Seren family.
Accent color: light golden yellow (#F5D76E, butter).
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager, AsyncExitStack
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from .cluster import JetsonClusterClient, JetsonDiscoveryService
from .config import LodestarConfig, load_config
from .routes import info as info_routes
from .routes import system as system_routes
from .routes import cluster_routes as cluster_routes
from .routes import services as services_routes
from .routes import scheduler as scheduler_routes
from .routes import chat as chat_routes
from .routes import agent_update as agent_update_routes

from seren_meninges import get_version
from seren_meninges.updates import updates_payload
from seren_meninges.auth import bearer_auth_middleware, DEFAULT_PUBLIC_PATHS
from seren_meninges.viewer import render_from_dir
from seren_sinew.request_log import RequestLoggingMiddleware

from . import __version__ as _fallback_version
APP_VERSION = get_version("seren-lodestar", fallback=_fallback_version)

# Accent color for the dashboard
ACCENT = "#F5D76E"  # light golden yellow, like butter

log = logging.getLogger("seren_lodestar")


def create_app(config: Optional[LodestarConfig] = None) -> FastAPI:
    cfg = config or load_config()
    bearer = cfg.server.resolve_bearer()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.config = cfg
        app.state.accent = ACCENT

        # ── Cluster client ────────────────────────────────────────────────
        # `default_token`: with runtime.inject_bearer_token, a node that has
        # no agent_token of its own is spoken to with Lodestar's own bearer.
        cluster = JetsonClusterClient(
            cfg.cluster,
            log_fn=lambda m: log.info(f"[cluster] {m}"),
            default_token=bearer if cfg.runtime.inject_bearer_token else "",
        )
        app.state.cluster = cluster

        # ── HTTP client pool ───────────────────────────────────────────────
        # llama gets its own timeout shape: connect fast, read slow. A Nano
        # producing 1024 tokens is minutes, not the 120s everything else gets.
        _http_clients: dict[str, httpx.AsyncClient] = {}
        _llama_timeout = httpx.Timeout(
            connect=cfg.chat.connect_timeout_seconds,
            read=cfg.chat.generation_timeout_seconds,
            write=60.0, pool=cfg.chat.connect_timeout_seconds,
        )

        def _http_client_factory(name: str) -> httpx.AsyncClient:
            if name not in _http_clients:
                timeout = _llama_timeout if name == "llama-upstream" else httpx.Timeout(120)
                _http_clients[name] = httpx.AsyncClient(timeout=timeout)
            return _http_clients[name]

        app.state.http_client_factory = _http_client_factory

        # ── Dialect ───────────────────────────────────────────────────────
        from .tooling import QwenHermesDialect, build_tool_client
        app.state.dialect = QwenHermesDialect()

        # ── Mount the MCP surface FIRST: the tool client is built on it ────
        try:
            from .mcp.server import mount_mcp_routes
            mcp_server = mount_mcp_routes(app)
        except ImportError as exc:
            mcp_server = None
            log.info("MCP surface not available; HTTP-only mode (%s)", exc)
        except Exception as exc:
            mcp_server = None
            log.warning("MCP mount failed: %r — continuing without MCP", exc)
        app.state.mcp_server = mcp_server

        # ── The tool client: in-process for our own tools, remote for the rest
        tool_client = build_tool_client(mcp_server, cfg.tooling.remote_mcp)
        app.state.tool_client = tool_client
        if mcp_server is None and not cfg.tooling.remote_mcp:
            log.warning("no tools: MCP is not mounted and tooling.remote_mcp is empty - "
                        "chat runs without a tool loop")

        # ── Scheduler ──────────────────────────────────────────────────────
        from .scheduling import SchedulerService

        scheduler_dir = cfg.scheduler.persistence_dir
        if not scheduler_dir:
            if cfg.config_path:
                scheduler_dir = os.path.join(os.path.dirname(cfg.config_path), "scheduler")
            else:
                scheduler_dir = os.path.join(os.path.expanduser("~"), "seren-lodestar", "scheduler")
        scheduler_dir = os.path.expanduser(scheduler_dir)
        os.makedirs(scheduler_dir, exist_ok=True)
        scheduler_state_path = os.path.join(scheduler_dir, "scheduled_tasks.json")
        log.info("scheduler state: %s", scheduler_state_path)

        scheduler = SchedulerService(
            tool_client=tool_client,
            state_file_path=scheduler_state_path,
        )
        app.state.scheduler = scheduler

        # ── Update checker ─────────────────────────────────────────────
        # "is there a newer seren-lodestar". Cosmetic: it polls on a TTL, never
        # in the request path, and every failure mode is a status string rather
        # than an exception.
        #
        # The try/except guards the IMPORT, because a Meninges older than the
        # one that introduced updates.py has no such module. Note this gate is
        # DELIBERATELY VISIBLE - app.state.updates stays None and the info route
        # reports status="unavailable" with a reason. A silent fallback here
        # would render as "you're up to date", which is the exact failure shape
        # that let mcp 2.0.0 quietly delete every /mcp endpoint in the family.
        try:
            from seren_meninges.updates import UpdateChecker
            app.state.updates = UpdateChecker(
                "seren-lodestar",
                enabled=cfg.updates.enabled,
                index_url=cfg.updates.index_url,
                ttl_seconds=cfg.updates.check_interval_hours * 3600.0,
                allow_prerelease=cfg.updates.allow_prerelease,
                fallback_version=APP_VERSION,
            )
        # Catch EVERYTHING, not just ImportError. This whole feature is cosmetic -
        # seren_meninges/version.py states the contract: a version read must never
        # crash startup. A too-narrow catch here already bit us: cfg.updates was
        # missing, the AttributeError sailed past `except ImportError`, and five
        # services failed to boot on a feature that only draws a badge.
        except Exception as exc:
            app.state.updates = None
            log.info("update checking unavailable (%s)", exc)

        # ── Discovery service ──────────────────────────────────────────────
        discovery = JetsonDiscoveryService(
            cluster, cfg.cluster,
            log_fn=lambda m: log.info(f"[discovery] {m}"),
        )
        app.state.discovery = discovery

        # Start services
        import asyncio
        asyncio.ensure_future(discovery.start())
        log.info("discovery service started")

        if scheduler:
            asyncio.ensure_future(scheduler.start())
            log.info("scheduler service started")

        async with AsyncExitStack() as _mcp_stack:
            session_manager = getattr(mcp_server, "session_manager", None)
            if session_manager is not None:
                await _mcp_stack.enter_async_context(session_manager.run())
                log.info("MCP session manager running")
            yield

        # Shutdown
        await cluster.aclose()
        if scheduler:
            await scheduler.stop()
            log.info("scheduler service stopped")
        await discovery.stop()
        log.info("discovery service stopped")
        
        log.info("seren_lodestar shut down")

    app = FastAPI(
        title="SerenLodestar",
        description="Cluster head / guiding star for the Seren stack — "
                    "manages Jetson nodes, routes inference, schedules tasks.",
        version=APP_VERSION,
        lifespan=lifespan,
    )

    # ── Auth + logging stack ───────────────────────────────────────────
    # ping + version are token-free liveness/identity probes (see the route table in
    # this module's docstring). Meninges' DEFAULT_PUBLIC_PATHS only covers /, /health
    # and /viewer, so extend it with the two public system routes -- derived from
    # system_routes.API_VERSION so the allowlist can't drift from the route paths if
    # the API version ever bumps.
    _sys_v = system_routes.API_VERSION
    app.add_middleware(bearer_auth_middleware(
        bearer,
        public_paths=DEFAULT_PUBLIC_PATHS | {
            f"/api/{_sys_v}/system/ping",
            f"/api/{_sys_v}/system/version",
        },
    ))
    app.add_middleware(
        RequestLoggingMiddleware,
        service_name="seren-lodestar",
        env_prefix="SEREN_LODESTAR",
    )

    # ── The operator dashboard viewer ──────────────────────────────────
    viewer_dir = Path(__file__).resolve().parent / "viewer" / "ui"

    @app.get("/")
    async def root(request: Request):
        return {
            "service": "SerenLodestar",
            "version": APP_VERSION,
            "updates": await updates_payload(
                getattr(request.app.state, "updates", None),
                distribution="seren-lodestar", installed=APP_VERSION),
        }

    @app.get("/viewer")
    async def viewer():
        html = render_from_dir(
            viewer_dir,
            title="SerenLodestar",
            brand="Seren<b>Lodestar</b> · Cluster Head",
            subtitle=f"v{APP_VERSION} · the guiding star",
            accent=ACCENT,
        )
        return HTMLResponse(html)

    # ── Route subpackage mounts ────────────────────────────────────────
    app.include_router(info_routes.router)
    app.include_router(system_routes.router)
    app.include_router(cluster_routes.router)
    app.include_router(services_routes.router)
    app.include_router(scheduler_routes.router)
    app.include_router(chat_routes.router)
    app.include_router(agent_update_routes.router)

    return app
