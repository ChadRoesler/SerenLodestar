"""
The ensure chain across the REAL pieces: Lodestar's route and node client
talking to a real Observatory app (in-process), which starts a scripted
service. Runs only where the Observatory is importable (a dev checkout); the
two halves are spelled separately on purpose and this is what holds them to
each other.

    Lodestar /service/llama/ensure -> JetsonAgentClient.ensure_service_async
        -> Observatory /api/v1/service/llama/ensure -> lifecycle.ensure_ready
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

obs_app_mod = pytest.importorskip("seren_observatory.app")
from seren_observatory import lifecycle  # noqa: E402

from seren_lodestar.agent_client import JetsonAgentClient  # noqa: E402
from seren_lodestar.app import create_app  # noqa: E402
from seren_lodestar.config import LodestarConfig  # noqa: E402


def test_lodestar_to_a_real_observatory_and_back(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".seren" / "services").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (home / ".seren" / "services" / "llama.json").write_text(json.dumps({
        "schema_version": 1, "service": "llama", "service_type": "pid_file", "port": 8090, "health_path": "/health"}))
    (home / ".seren" / "node.json").write_text(json.dumps({"hostname": "node-a"}))
    (home / ".seren" / "secrets.json").write_text(json.dumps({"observatory_token": "obs-secret"}))

    box = {"running": False, "probes": 0, "stops": 0}

    async def probe(manifest):
        if not box["running"]:
            return {"ok": False}
        box["probes"] += 1
        return {"ok": box["probes"] > 1}

    async def start(manifest):
        box["running"], box["probes"] = True, 0
        return {"ok": True, "pid": 7}

    async def stop(manifest):
        box["stops"] += 1
        box["running"] = False
        return {"ok": True}

    monkeypatch.setattr(lifecycle, "probe_port", probe)
    monkeypatch.setattr(lifecycle, "start", start)
    monkeypatch.setattr(lifecycle, "stop", stop)
    monkeypatch.setattr(lifecycle, "ENSURE_POLL_SECONDS", 0.01)
    observatory = obs_app_mod.create_app()

    agent = JetsonAgentClient.__new__(JetsonAgentClient)
    agent._node_name = "node-a"
    agent._log = lambda m: None
    agent._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=observatory), base_url="http://192.0.2.101:7777/",
                                      headers={"Authorization": "Bearer obs-secret"})
    cluster = SimpleNamespace(
        choose_node_for=lambda svc: SimpleNamespace(name="node-a") if svc == "llama" else None,
        get_agent=lambda name: agent if name == "node-a" else None,
        mark_node_suspect=lambda *a: None)

    with TestClient(create_app(LodestarConfig())) as c:
        c.app.state.cluster = cluster
        d = c.post("/api/v1/service/llama/ensure", json={"holder": "seren-hippocampus", "wait_seconds": 5}).json()
        assert d["ok"] and d["ready"] and d["started"], d
        assert d["node"] == "node-a" and d["base_url"] == "http://192.0.2.101:8090" and d["health_path"] == "/health"
        assert d["holders"] == ["seren-hippocampus"] and box["running"] is True
        rel = c.post("/api/v1/service/llama/release", json={"holder": "seren-hippocampus"}).json()
        assert rel["ok"] and rel["stopped"] is True and box["stops"] == 1 and box["running"] is False
