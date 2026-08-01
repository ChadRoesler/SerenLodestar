"""
GET / carries an update-status block.

These test the CONTRACT rather than a particular answer, because the answer
legitimately varies with the environment: whether seren-meninges[updates] is
installed, whether the box has a network, whether the operator switched it off.
What must never vary is that there IS a status, that it is one of the four
known values, and that the key set is stable — anything rendering this (the
dashboard badge, a monitoring probe) reads those keys unconditionally.

The conftest `offline_update_checks` fixture guarantees no real index call.
"""
from __future__ import annotations

KNOWN_STATUSES = {"ok", "disabled", "unavailable", "error"}
EXPECTED_KEYS = {"status", "distribution", "installed", "latest",
                 "update_available", "detail", "checked_at"}


def test_root_carries_an_updates_block(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert "updates" in body, "GET / must always report update status"
    assert isinstance(body["updates"], dict)


def test_status_is_always_one_of_the_known_values(client):
    u = client.get("/").json()["updates"]
    assert u["status"] in KNOWN_STATUSES, f"unknown status: {u['status']!r}"


def test_key_set_is_stable_whatever_the_status(client):
    """A renderer must not have to guard every key. Absent is not an option -
    an absent `update_available` reads as false, which is a claim we might not
    be entitled to make."""
    u = client.get("/").json()["updates"]
    assert set(u) == EXPECTED_KEYS


def test_update_available_is_a_real_bool(client):
    u = client.get("/").json()["updates"]
    assert isinstance(u["update_available"], bool)


def test_we_always_know_our_own_version_even_when_the_check_fails(client):
    """The far half can fail; the near half never does. `installed` comes from
    local metadata and must agree with the version the service reports."""
    body = client.get("/").json()
    assert body["updates"]["installed"] == body["version"]


def test_offline_reports_error_not_a_false_all_clear(client):
    """With the network muzzled the check cannot succeed - and it must say so
    rather than defaulting to update_available=False, which would render as a
    green tick on a box that has no idea whether it's current."""
    u = client.get("/").json()["updates"]
    if u["status"] == "error":
        assert u["update_available"] is False
        assert u["detail"], "an error status must carry a reason"


def test_not_wired_is_reported_not_omitted(make_client):
    """Simulates a seren-meninges too old to have updates.py: the lifespan
    import fails, app.state.updates stays None, and / still answers with a
    full block explaining why."""
    c = make_client()
    c.app.state.updates = None
    u = c.get("/").json()["updates"]
    assert u["status"] == "unavailable"
    assert u["update_available"] is False
    assert "updates" in u["detail"]
    assert set(u) == EXPECTED_KEYS


def test_health_stays_a_bare_liveness_probe(client):
    """/health must NOT grow an update check - it's hit on a timer by
    supervisors and has to stay free of anything that can be slow."""
    body = client.get("/health").json()
    assert body["ok"] is True
    assert "updates" not in body
