"""
Smoke test for SerenLodestar — checks that the app starts and serves
its core routes.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from seren_lodestar.app import create_app
from seren_lodestar.config import LodestarConfig, load_config


def test_app_starts_with_minimal_config():
    """Even with an empty config, the app should start."""
    cfg = load_config()
    app = create_app(cfg)
    assert app is not None
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200
        root = r.json()
        assert root["service"] == "SerenLodestar"


def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_viewer_endpoint(client):
    r = client.get("/viewer")
    assert r.status_code == 200
    assert "Seren" in r.text
    assert "Lodestar" in r.text


def test_system_ping(client):
    r = client.get("/api/v1/system/ping")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_system_version(client):
    r = client.get("/api/v1/system/version")
    assert r.status_code == 200
    assert "runtime_version" in r.json()


def test_cluster_capabilities_unconfigured(client):
    """Without configured nodes, capabilities should be empty."""
    r = client.get("/api/v1/cluster/capabilities")
    assert r.status_code == 200
    assert r.json()["capabilities"] == {}


def test_chat_health_unconfigured(client):
    """Without a llama node, chat health should report not ok."""
    r = client.get("/api/v1/chat/health")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
