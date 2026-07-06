"""
Auth tests — bearer token enforcement for SerenLodestar.

Same pattern as SerenMemory/tests/test_auth.py and SerenLoci/tests/test_auth.py.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from seren_lodestar.app import create_app
from seren_meninges import ServerConfig, TlsConfig
from seren_lodestar.config import LodestarConfig


@pytest.fixture
def auth_client():
    """TestClient with a bearer token configured."""
    cfg = LodestarConfig(
        server=ServerConfig(bearer_token="sekret"),
        tls=TlsConfig(),
    )
    app = create_app(cfg)
    with TestClient(app) as c:
        yield c


# ── Public routes (no auth required) ────────────────────────────────────

def test_health_is_public(auth_client):
    r = auth_client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_system_ping_is_public(auth_client):
    r = auth_client.get("/api/v1/system/ping")
    assert r.status_code == 200


def test_system_version_is_public(auth_client):
    r = auth_client.get("/api/v1/system/version")
    assert r.status_code == 200


# ── Protected routes ────────────────────────────────────────────────────

def test_root_is_public(auth_client):
    """Root endpoint is intentionally public (DEFAULT_PUBLIC_PATHS)."""
    r = auth_client.get("/")
    assert r.status_code == 200


def test_viewer_is_public(auth_client):
    r = auth_client.get("/viewer")
    assert r.status_code == 200


def test_cluster_capabilities_requires_auth(auth_client):
    r = auth_client.get("/api/v1/cluster/capabilities")
    assert r.status_code == 401


def test_chat_health_requires_auth(auth_client):
    r = auth_client.get("/api/v1/chat/health")
    assert r.status_code == 401


# ── Valid token access ──────────────────────────────────────────────────

def test_root_with_valid_token(auth_client):
    r = auth_client.get("/", headers={"Authorization": "Bearer sekret"})
    assert r.status_code == 200
    assert r.json()["service"] == "SerenLodestar"


def test_system_status_with_valid_token(auth_client):
    r = auth_client.get("/api/v1/system/status",
                        headers={"Authorization": "Bearer sekret"})
    assert r.status_code == 200


def test_cluster_capabilities_with_valid_token(auth_client):
    r = auth_client.get("/api/v1/cluster/capabilities",
                        headers={"Authorization": "Bearer sekret"})
    assert r.status_code == 200


def test_scheduler_tasks_with_valid_token(auth_client):
    r = auth_client.get("/api/v1/scheduler/tasks",
                        headers={"Authorization": "Bearer sekret"})
    assert r.status_code == 200
