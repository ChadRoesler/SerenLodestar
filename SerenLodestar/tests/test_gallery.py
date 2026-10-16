"""
The gallery through the cluster (seren_lodestar.routes.gallery, 15 Oct 2026):
every node's Workbench gallery at one address, the file streamed through.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from seren_lodestar.app import create_app
from seren_lodestar.config import LodestarConfig
from seren_lodestar.dtos import NodeSnapshot

PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 40


class FakeAgent:
    """One node's Observatory, relaying its Workbench's gallery."""

    def __init__(self, node: str, days: dict[str, list[str]]):
        self.node_name, self.days, self.asked = node, days, []

    async def relay_json_async(self, path: str, timeout: float = 35.0):
        self.asked.append(path)
        parts = path.split("/")
        if path.endswith("/gallery"):
            return 200, {"gallery": "/g", "days": [{"day": d, "pictures": len(n)} for d, n in self.days.items()],
                         "count": sum(len(n) for n in self.days.values())}
        day = parts[-1]
        if day not in self.days:
            return 404, {"detail": f"no pictures on {day}"}
        return 200, {"day": day, "count": len(self.days[day]),
                     "pictures": [{"file": n, "bytes": len(PNG), "mime": "image/png", "note": {"why": "x"}} for n in self.days[day]]}

    async def open_relay_async(self, path: str, timeout: float = 300.0):
        self.asked.append(path)
        day, name = path.split("/")[-2:]
        if name in self.days.get(day, []):
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png", "x-content-type-options": "nosniff",
                                                             "content-disposition": f'inline; filename="{name}"'})
        return httpx.Response(404, json={"detail": f"no {name} on {day}"})


def _cluster():
    node_a = FakeAgent("node-a", {"2026-10-15": ["a.png", "b.png"], "2026-10-14": ["old.png"]})
    node_b = FakeAgent("node-b", {})
    snaps = {"node-a": NodeSnapshot(online=True, installed_services=["seren-memory-wren", "seren-workbench-wren"], status={}),
             "node-b": NodeSnapshot(online=True, installed_services=["seren-observatory-wren"], status={}),
             "node-c": NodeSnapshot(online=False, installed_services=["SerenWorkbench-wren"], status={})}
    agents = {"node-a": node_a, "node-b": node_b, "node-c": FakeAgent("node-c", {})}
    return SimpleNamespace(get_snapshots=lambda: snaps, get_agent=lambda n: agents.get(n)), node_a


def test_every_nodes_gallery_at_one_address():
    cluster, node_a = _cluster()
    with TestClient(create_app(LodestarConfig())) as c:
        c.app.state.cluster = cluster
        r = c.get("/api/v1/gallery").json()
        assert r["count"] == 3 and [n["node"] for n in r["nodes"]] == ["node-a"], "a node without a Workbench, and an offline one, are not asked"
        assert r["nodes"][0]["service"] == "seren-workbench-wren" and node_a.asked == ["api/v1/service/seren-workbench-wren/gallery"]
        day = c.get("/api/v1/gallery/node-a/2026-10-15").json()
        assert day["node"] == "node-a" and [p["file"] for p in day["pictures"]] == ["a.png", "b.png"]
        assert c.get("/api/v1/gallery/node-a/2026-10-16").status_code == 404
        assert c.get("/api/v1/gallery/node-a/scratch").status_code == 400
        assert c.get("/api/v1/gallery/node-b/2026-10-15").status_code == 404, "no Workbench there"
        assert c.get("/api/v1/gallery/mars/2026-10-15").status_code == 404


def test_the_picture_is_streamed_through_with_its_headers():
    cluster, node_a = _cluster()
    with TestClient(create_app(LodestarConfig())) as c:
        c.app.state.cluster = cluster
        r = c.get("/api/v1/gallery/node-a/2026-10-15/a.png")
        assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
        assert r.headers["x-content-type-options"] == "nosniff" and 'filename="a.png"' in r.headers["content-disposition"]
        assert c.get("/api/v1/gallery/node-a/2026-10-15/zzz.png").status_code == 404
        assert c.get("/api/v1/gallery/node-a/2026-10-15/..%2Fa.png").status_code in (400, 404)
