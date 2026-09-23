"""
The cluster's picture of a node, and the client's reading of what a node says.

- a connection failure is a short suspicion, not thirty minutes of offline;
- a lapsed suspicion is re-probed on demand, not on the discovery clock;
- a manifest with no `service` key does not take the whole node down;
- Observatory's `observatory_version` reaches the DTO;
- `inject_bearer_token` really does present Lodestar's own bearer;
- the per-node service path trusts the node's own list, not a stale set;
- the scheduler keeps its state beside the config, never in /tmp.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from seren_lodestar import agent_client
from seren_lodestar.app import create_app
from seren_lodestar.cluster import JetsonClusterClient
from seren_lodestar.config import LodestarConfig, load_config
from seren_lodestar.dtos import (
    ClusterOptions, JetsonNodeOptions, NodeSnapshot, ServiceEntry, ServiceManifest,
    ServicesResponse, VersionResponse,
)


def _cluster(default_token=""):
    opts = ClusterOptions(nodes=[JetsonNodeOptions(name="nano", agent_url="http://127.0.0.1:7777")])
    return JetsonClusterClient(opts, log_fn=lambda m: None, default_token=default_token)


def _online(cluster, *services):
    cluster._snapshots["nano"] = NodeSnapshot(online=True, installed_services=list(services), status={})


# ── suspicion ────────────────────────────────────────────────────────────

def test_a_suspect_node_is_skipped_by_routing_but_stays_online(monkeypatch):
    c = _cluster()
    _online(c, "llama")
    assert c.choose_node_for("llama") is not None
    c.mark_node_suspect("nano", "llama-server unreachable")
    assert c.choose_node_for("llama") is None
    assert c.get_snapshots()["nano"].online is True, "suspect, not offline"
    assert c.suspect_reason("nano") == "llama-server unreachable"


async def test_a_lapsed_suspicion_is_reprobed_on_demand(monkeypatch):
    c = _cluster()
    _online(c, "llama")
    c.mark_node_suspect("nano", "blip", backoff_s=0.0)      # lapses immediately
    probes = []

    async def services():
        probes.append(1)
        return ServicesResponse(count=1, services={
            "llama": ServiceEntry(manifest=ServiceManifest(service="llama", port=8090))})

    async def manifest(name):
        return ServiceManifest(service=name, port=8090)
    agent = c.get_agent("nano")
    monkeypatch.setattr(agent, "get_services_async", services)
    monkeypatch.setattr(agent, "get_service_manifest_async", manifest)
    routed = await c.get_service_url_async("llama")
    assert probes == [1], "one probe, on demand, not the 30-minute loop"
    assert routed is not None and routed.base_url == "http://127.0.0.1:8090"
    assert not c.is_suspect("nano")


def test_mark_node_offline_is_now_a_suspicion():
    c = _cluster()
    _online(c, "llama")
    c.mark_node_offline("nano", "old caller")
    assert c.is_suspect("nano") and c.get_snapshots()["nano"].online is True


# ── lenient DTOs ─────────────────────────────────────────────────────────

def test_a_manifest_without_a_service_key_does_not_blow_up_the_dto():
    resp = agent_client._from_dict({"count": 1, "services": {
        "searxng": {"manifest": {"port": 8888}, "status": {"running": True}}}}, ServicesResponse)
    assert resp.services["searxng"].manifest.service == ""      # filled with the zero value
    assert resp.services["searxng"].status.running is True


async def test_get_services_fills_the_service_name_from_the_key(monkeypatch):
    agent = _cluster().get_agent("nano")

    async def fake_get(path, dto):
        return agent_client._from_dict({"count": 1, "services": {
            "searxng": {"manifest": {"port": 8888}}}}, dto)
    monkeypatch.setattr(agent, "_get_json", fake_get)
    resp = await agent.get_services_async()
    assert resp.services["searxng"].manifest.service == "searxng"


def test_observatory_version_reaches_agent_version():
    v = agent_client._from_dict({"observatory_version": "2.0.0", "manifest_schema": 2}, VersionResponse)
    assert v.agent_version == "2.0.0" and v.manifest_schema == 2


# ── the injected bearer ──────────────────────────────────────────────────

def test_inject_bearer_presents_lodestars_token_to_a_node_without_one():
    agent = _cluster(default_token="head-secret").get_agent("nano")
    assert agent._client.headers["authorization"] == "Bearer head-secret"


def test_a_nodes_own_token_wins_over_the_injected_one():
    opts = ClusterOptions(nodes=[JetsonNodeOptions(name="n", agent_url="http://x:7777", agent_token="mine")])
    agent = JetsonClusterClient(opts, log_fn=lambda m: None, default_token="head-secret").get_agent("n")
    assert agent._client.headers["authorization"] == "Bearer mine"


# ── per-node service path ────────────────────────────────────────────────

def test_the_per_node_path_trusts_the_nodes_own_service_list():
    from seren_lodestar.routes.services import _resolve_per_node_agent
    c = _cluster()
    _online(c, "llama", "searxng")
    agent, err = _resolve_per_node_agent(c, "nano", "searxng")
    assert agent is not None and err is None, "searxng is real on this node; the old KNOWN_SERVICES set said otherwise"
    agent, err = _resolve_per_node_agent(c, "nano", "chroma")
    assert agent is None and err.status_code == 404
    agent, err = _resolve_per_node_agent(c, "ghost", "llama")
    assert agent is None and err.status_code == 404


# ── scheduler state location ─────────────────────────────────────────────

def test_scheduler_state_lives_beside_the_config_file(tmp_path, monkeypatch):
    cfg_file = tmp_path / "lodestar.yaml"
    cfg_file.write_text("server:\n  port: 6361\n", encoding="utf-8")
    monkeypatch.delenv("SEREN_LODESTAR_CONFIG", raising=False)
    cfg = load_config(str(cfg_file))
    assert cfg.config_path == str(cfg_file.resolve())
    with TestClient(create_app(cfg)) as c:
        path = Path(c.app.state.scheduler._state_path)
    assert path.parent == tmp_path / "scheduler"
    assert "tmp" not in str(path).replace(str(tmp_path), "")


def test_scheduler_state_without_a_config_file_is_under_home_not_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    with TestClient(create_app(LodestarConfig())) as c:
        path = Path(c.app.state.scheduler._state_path)
    assert path.parent == tmp_path / "seren-lodestar" / "scheduler"
