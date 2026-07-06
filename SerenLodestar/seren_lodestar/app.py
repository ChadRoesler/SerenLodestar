"""
seren_lodestar.app
════════════════════════════════════════════════════════════════════════

The FastAPI application for the SerenLodestar cluster head. Wires the
cluster topology manager, discovery service, scheduler, tooling, the
operator dashboard, and the MCP transport.

Serves:
    GET  /                                      — service info
    GET  /health                                — liveness
    GET  /viewer                                — operator dashboard
    GET  /api/v1/system/ping                    — public ping
    GET  /api/v1/system/version                 — public version
    GET  /api/v1/system/status                  — node status
    GET  /api/v1/system/health                  — cluster health
    POST /api/v1/system/reclaim                 — stop services on nodes
    POST /api/v1/system/reboot/{node}           — reboot a node
    POST /api/v1/system/reboot/{node}/cancel    — cancel reboot
    POST /api/v1/system/agent-update            — push agent to all nodes
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
from fastapi import FastAPI
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
from seren_meninges.auth import bearer_auth_middleware
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
        cluster = JetsonClusterClient(
            cfg.cluster,
            log_fn=lambda m: log.info(f"[cluster] {m}"),
        )
        app.state.cluster = cluster

        # ── HTTP client pool ───────────────────────────────────────────────
        _http_clients: dict[str, httpx.AsyncClient] = {}

        def _http_client_factory(name: str) -> httpx.AsyncClient:
            if name not in _http_clients:
                _http_clients[name] = httpx.AsyncClient(timeout=120)
            return _http_clients[name]

        app.state.http_client_factory = _http_client_factory

        # ── Tooling / chat ─────────────────────────────────────────────────
        from .tooling import QwenHermesDialect, McpToolClient
        dialect = QwenHermesDialect()
        app.state.dialect = dialect

        # ── Scheduler ──────────────────────────────────────────────────────
        from .scheduling import SchedulerService

        scheduler_dir = cfg.scheduler.persistence_dir
        if not scheduler_dir:
            config_path = os.environ.get("SEREN_LODESTAR_CONFIG", "")
            if config_path:
                cfg_dir = os.path.dirname(os.path.abspath(config_path))
                scheduler_dir = os.path.join(cfg_dir, "scheduler")
            else:
                scheduler_dir = "/tmp/seren-scheduler"

        os.makedirs(scheduler_dir, exist_ok=True)
        scheduler_state_path = (scheduler_dir.rstrip("/") + "/scheduled_tasks.json")

        def _scheduler_http_client(name: str):
            return httpx.AsyncClient(timeout=120)

        scheduler = SchedulerService(
            http_client_factory=_scheduler_http_client,
            state_file_path=scheduler_state_path,
        )
        app.state.scheduler = scheduler

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

        # ── Mount the MCP surface ──────────────────────────────────────────
        try:
            from .mcp.server import mount_mcp_routes
            mcp_server = mount_mcp_routes(app)
        except ImportError as exc:
            mcp_server = None
            log.info("MCP surface not available; HTTP-only mode (%s)", exc)
        except Exception as exc:
            mcp_server = None
            log.warning("MCP mount failed: %r — continuing without MCP", exc)

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
    app.add_middleware(bearer_auth_middleware(bearer))
    app.add_middleware(
        RequestLoggingMiddleware,
        service_name="seren-lodestar",
        env_prefix="SEREN_LODESTAR",
    )

    # ── The operator dashboard viewer ──────────────────────────────────
    viewer_dir = Path(__file__).resolve().parent / "viewer" / "ui"

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
