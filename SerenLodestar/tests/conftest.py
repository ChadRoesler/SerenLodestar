"""
Shared test fixtures for SerenLodestar.

Follows the same pattern as SerenMemory/tests/conftest.py and
SerenLoci/tests/conftest.py:

    - ``make_client`` factory fixture — creates a TestClient backed by a
      fresh ``LodestarConfig`` with a random port (not used by TestClient).
      Tears down cleanly after the test.

    - ``client`` fixture — convenience fixture that calls ``make_client``
      with a default config.

What's NOT here:
    - No cluster client — the Lodestar needs configured nodes to start.
      Tests that need the full stack must mock or provide a minimal config.
    - No scheduler state file — tests that need the scheduler must mock.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from seren_lodestar.app import create_app
from seren_lodestar.config import LodestarConfig, load_config


@pytest.fixture(autouse=True)
def offline_update_checks(monkeypatch):
    """No test may talk to pypi.org.

    ``GET /`` carries the update status and update checking is ON by default,
    so without this EVERY test that touches the root route (test_auth's public
    and authed root cases, for two) would make a real network call - slow,
    flaky offline, and rude to someone else's server.

    Patching the CLASS method rather than an env var is deliberate: some tests
    build a LodestarConfig directly instead of going through load_config, so an
    env override wouldn't reach them. The checker still runs and still returns
    a well-formed status - just status="error" instead of a real answer, which
    is exactly what a box with no internet would see.
    """
    try:
        from seren_meninges.updates import UpdateChecker
    except ImportError:
        return  # older meninges, nothing to muzzle

    async def _no_network(self, distribution):
        raise ConnectionError("network disabled in tests")

    # Must be patched BEFORE any UpdateChecker is constructed - __init__ binds
    # self._fetch = fetcher or self._fetch_from_index. autouse + function scope
    # puts it in place before the app lifespan runs.
    monkeypatch.setattr(UpdateChecker, "_fetch_from_index", _no_network)


@pytest.fixture
def make_client():
    """Factory fixture. Call it with an LodestarConfig to get a fully wired
    TestClient that tears down cleanly after the test."""
    _clients: list[TestClient] = []

    def _factory(cfg: LodestarConfig | None = None,
                 raise_server_exceptions: bool = False) -> TestClient:
        cfg = cfg or load_config()
        app = create_app(cfg)
        tc = TestClient(app, raise_server_exceptions=raise_server_exceptions)
        tc.__enter__()
        _clients.append(tc)
        return tc

    yield _factory

    for tc in _clients:
        try:
            tc.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def client(make_client):
    """Convenience fixture: a default TestClient."""
    return make_client()
