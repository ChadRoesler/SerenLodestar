"""
The shelf (23 Oct 2026): Lodestar pulls .kbh files and hands them to nodes,
so no harness box has to reach the internet.

- a release lands on the shelf only where it matches its own .sha256
- a file dropped in by hand is on the shelf too, and says so
- each node's carabiner says how it stands against the shelf
- an upgrade sends the shelf's file, base64 with its hash, to the node
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import zipfile

import httpx
from fastapi.testclient import TestClient

from seren_lodestar.app import create_app
from seren_lodestar.config import CarabinersConfig, LodestarConfig
from seren_lodestar.shelf import Shelf, status


def kbh_bytes(version: str, build: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("__main__.py", "print('hi')\n")
        zf.writestr("kbh/__init__.py", f'__version__ = "{version}"\n')
        if build:
            zf.writestr("kbh/_build.py", f"BUILD = {build!r}\n")
    return buf.getvalue()


def github(files: dict[str, bytes], sha_lies: tuple[str, ...] = (), tag: str = "v5.1.4"):
    """A stand-in for the GitHub API and its release downloads."""
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.path.endswith(("/releases/latest", f"/releases/tags/{tag}")):
            assets = []
            for name in files:
                assets += [{"name": name, "browser_download_url": f"https://dl.example/{name}"},
                           {"name": name + ".sha256", "browser_download_url": f"https://dl.example/{name}.sha256"}]
            return httpx.Response(200, json={"tag_name": tag, "assets": assets})
        name = req.url.path.rsplit("/", 1)[-1]
        if name.endswith(".sha256"):
            kbh = name[:-7]
            sha = "0" * 64 if kbh in sha_lies else hashlib.sha256(files[kbh]).hexdigest()
            return httpx.Response(200, text=f"{sha}  {kbh}\n")
        if name in files:
            return httpx.Response(200, content=files[name])
        return httpx.Response(404)
    return httpx.MockTransport(handler), seen


def test_status_says_how_a_node_stands_against_the_shelf():
    shelf = {"version": "5.1.4+abc1234", "sha256": "s1"}
    assert status("5.1.4+abc1234", "s1", shelf) == "current"
    assert status("5.1.2", "s0", shelf) == "behind"
    assert status("5.2.0", "s2", shelf) == "ahead"
    assert status("5.1.4+def5678.dirty.12345678", "s3", shelf) == "different"
    assert status("5.1.2", "s0", None) == "none"


def test_a_release_lands_only_where_it_matches_its_own_sha256(tmp_path):
    files = {"claude.kbh": kbh_bytes("5.1.4", "abc1234"), "codex.kbh": kbh_bytes("5.1.4", "abc1234"),
             "Not A Name.kbh": b"x"}
    transport, seen = github(files, sha_lies=("codex.kbh",))
    shelf = Shelf(tmp_path / "shelf", "alice/SerenCarabiners", transport=transport)
    out = asyncio.run(shelf.fetch())
    assert out["release"] == "v5.1.4" and [f["carabiner"] for f in out["fetched"]] == ["claude"]
    assert "codex" in out["refused"] and not out["ok"]
    assert not (tmp_path / "shelf" / "codex.kbh").exists(), "a file that does not match never lands"
    e = shelf.entries()["claude"]
    assert e["version"] == "5.1.4+abc1234" and e["source"] == "release v5.1.4" and e["fetched_at"]
    assert seen[0] == "https://api.github.com/repos/alice/SerenCarabiners/releases/latest"

    (tmp_path / "shelf" / "claude.kbh").write_bytes(kbh_bytes("5.1.5", "def5678.dirty.12345678"))
    assert shelf.entries()["claude"]["source"] == "by hand", "a file swapped in by hand says so"


def test_a_release_that_cannot_be_read_says_why(tmp_path):
    shelf = Shelf(tmp_path / "shelf", "alice/SerenCarabiners",
                  transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"message": "Not Found"})))
    out = asyncio.run(shelf.fetch("v9.9.9"))
    assert not out["ok"] and "v9.9.9" in out["error"] and shelf.entries() == {}


class FakeAgent:
    def __init__(self, version="5.1.2", sha="old"):
        self.seen = []
        self.version, self.sha = version, sha

    async def carabiner_call_async(self, method, path, body=None, timeout=90.0):
        self.seen.append((method, path, body, timeout))
        if path == "/api/v1/carabiners":
            return 200, {"ok": True, "carabiners": [{"carabiner": "claude", "configured": True,
                                                    "version": self.version, "sha256": self.sha}]}
        if path.endswith("/upgrade"):
            return 200, {"ok": True, "was": "5.1.2", "now": "5.1.4+abc1234", "changed": True, "node": "DESKTOP-XYZ"}
        return 200, {"ok": True, "carabiner": "claude", "fields": []}


def _client(monkeypatch, tmp_path, agents):
    c = TestClient(create_app(LodestarConfig(carabiners=CarabinersConfig(shelf=str(tmp_path / "shelf")))))
    c.__enter__()
    monkeypatch.setattr(c.app.state.cluster, "_agents", dict(agents))
    monkeypatch.setattr(c.app.state.cluster, "get_snapshots", lambda: {})
    return c


def test_the_rack_marks_who_is_behind_and_an_upgrade_carries_the_shelf_file(monkeypatch, tmp_path):
    desk = FakeAgent()
    c = _client(monkeypatch, tmp_path, {"desktop": desk})
    r = c.post("/api/v1/carabiner/claude/upgrade", json={"node": "desktop"})
    assert not r.json()["ok"] and "fetch a release" in r.json()["error"], "nothing on the shelf yet: said, not a bare 404"

    data = kbh_bytes("5.1.4", "abc1234")
    (tmp_path / "shelf").mkdir()
    (tmp_path / "shelf" / "claude.kbh").write_bytes(data)
    rack = c.get("/api/v1/rack").json()
    (carabiner,) = rack["nodes"][0]["carabiners"]
    assert carabiner["update"] == "behind" and carabiner["shelf_version"] == "5.1.4+abc1234"
    assert rack["shelf"][0]["carabiner"] == "claude"
    assert c.get("/api/v1/rack/shelf").json()["shelf"][0]["source"] == "by hand"

    r = c.post("/api/v1/carabiner/claude/upgrade", json={"node": "desktop", "reason": "try it", "sneaky": 1})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["node"] == "desktop"
    method, path, body, timeout = desk.seen[-1]
    assert (method, path) == ("POST", "/api/v1/carabiner/claude/upgrade") and timeout >= 600
    assert base64.b64decode(body["kbh"]) == data and body["sha256"] == hashlib.sha256(data).hexdigest()
    assert body["holder"] == "lodestar-rack" and body["version"] == "5.1.4+abc1234" and "sneaky" not in body
    assert c.post("/api/v1/carabiner/claude/upgrade", json={"node": "toaster"}).status_code == 404
    c.__exit__(None, None, None)
