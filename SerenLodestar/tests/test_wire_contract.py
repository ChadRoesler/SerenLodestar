"""
The wire contract from the head's side: Lodestar's node client, driven into
a REAL Observatory app (in-process), parses every answer it polls for, and
each raw answer holds to the shapes both sides share
(seren_sinew.cluster.dtos) with nothing unknown and nothing missing.

The two halves used to be spelled separately and drifted (the version
rename; see seren_sinew.cluster). test_ensure_contract pins the ensure
chain the same way; this pins the rest of the polling path: version, node,
thermal, services, health, a service's manifest / status / health / logs,
start, and reclaim.

Runs only where the Observatory is importable (a dev checkout).
"""
from __future__ import annotations

import json

import httpx
import pytest

obs_app_mod = pytest.importorskip("seren_observatory.app")

from seren_sinew.cluster import (  # noqa: E402
    HealthResponse,
    LogsResponse,
    NodeResponse,
    PingResponse,
    PortHealth,
    ReclaimResponse,
    ServiceLifecycleResponse,
    ServiceManifest,
    ServiceStatus,
    ServicesResponse,
    ThermalResponse,
    VersionResponse,
    drift,
)

from seren_lodestar.agent_client import JetsonAgentClient  # noqa: E402
from seren_lodestar.dtos import JetsonNodeOptions  # noqa: E402


@pytest.fixture()
def observatory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".seren" / "services").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (home / ".seren" / "services" / "llama.json").write_text(json.dumps({
        "schema_version": 2, "service": "llama", "service_type": "pid_file", "port": 8090, "health_path": "/health"}))
    (home / ".seren" / "services" / "coral.json").write_text(json.dumps({
        "schema_version": 2, "service": "coral", "service_type": "library", "port": 0}))
    (home / ".seren" / "node.json").write_text(json.dumps({"hostname": "node-a", "hardware": "a box"}))
    (home / ".seren" / "secrets.json").write_text(json.dumps({"observatory_token": "obs-secret"}))
    return obs_app_mod.create_app()


@pytest.fixture()
def node(observatory):
    """Lodestar's client for the node, its HTTP going straight into the
    Observatory app."""
    client = JetsonAgentClient(JetsonNodeOptions(name="node-a", agent_url="http://node-a:7777", agent_token="obs-secret"))
    client._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=observatory), base_url="http://node-a:7777/",
                                       headers={"Authorization": "Bearer obs-secret"})
    return client


async def _raw(node, method, path, **kw):
    r = await node._client.request(method, path, **kw)
    assert r.status_code < 300, f"{method} {path}: {r.status_code} {r.text[:200]}"
    return r.json()


def _holds(data, shape):
    problems = drift(data, shape)
    assert not problems, "the wire moved: " + "; ".join(str(p) for p in problems) + f"\nanswer: {data}"


async def test_everything_the_head_polls_parses_and_holds_to_the_shared_shapes(node):
    checks = [
        ("GET", "api/v1/system/ping", PingResponse, node.ping_async()),
        ("GET", "api/v1/system/version", VersionResponse, node.version_async()),
        ("GET", "api/v1/system/node", NodeResponse, node.get_node_async()),
        ("GET", "api/v1/system/thermal", ThermalResponse, node.get_thermal_async()),
        ("GET", "api/v1/system/services", ServicesResponse, node.get_services_async()),
        ("GET", "api/v1/system/health", HealthResponse, node.get_health_async()),
        ("GET", "api/v1/service/llama/manifest", ServiceManifest, node.get_service_manifest_async("llama")),
        ("GET", "api/v1/service/llama/status", ServiceStatus, node.get_service_status_async("llama")),
        ("GET", "api/v1/service/llama/health", PortHealth, node.get_service_health_async("llama")),
        ("GET", "api/v1/service/llama/logs?lines=3", LogsResponse, node.get_service_logs_async("llama", 3)),
        ("POST", "api/v1/service/llama/start", ServiceLifecycleResponse, node.start_service_async("llama")),
        ("POST", "api/v1/system/reclaim", ReclaimResponse, node.reclaim_async()),
    ]
    for method, path, shape, call in checks:
        parsed = await call
        assert parsed is not None, f"{path}: the client returned None - the wire and {shape.__name__} disagree"
        assert isinstance(parsed, shape)
        _holds(await _raw(node, method, path, **({"json": {}} if method == "POST" else {})), shape)


async def test_what_the_head_reads_off_the_answers_is_there(node):
    version = await node.version_async()
    assert version.agent_version == version.observatory_version and version.observatory_version
    node_info = await node.get_node_async()
    assert node_info.manifest.hostname == "node-a" and node_info.manifest.extra["hardware"] == "a box"
    services = await node.get_services_async()
    assert set(services.services) == {"llama", "coral"}
    assert services.services["llama"].manifest.service == "llama"
    assert services.services["llama"].status.running is False
    assert services.services["coral"].status.library_mode is True
    health = await node.get_health_async()
    assert health.not_running == ["llama"] and health.idle == []
    start = await node.start_service_async("llama")
    assert start.ok is False and "start script" in (start.error or ""), "a start that ran and failed is an answer"
