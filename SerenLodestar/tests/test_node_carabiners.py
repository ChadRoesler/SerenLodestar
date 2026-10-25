"""
The carabiners on each box ride on the poll Lodestar already makes (24 Oct
2026: "Observatory should be aware of what carabiners are installed on the
box"). The Observatory's /system/node now says them, Rack or no Rack; the
lenient dict -> dataclass mapping drops any key the DTO does not name, so
NodeResponse has to name it or they vanish on the way to the glance.

- get_node_async keeps the rows as the Observatory sent them
- an Observatory from before says nothing: None, not an empty list
- /system/status hands them to the viewer under agent_node
- the node card says them (pinned by reading the page, as the other viewer tests do)
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from seren_lodestar.agent_client import JetsonAgentClient
from seren_lodestar.app import create_app
from seren_lodestar.cluster import NodeSnapshot
from seren_lodestar.config import LodestarConfig
from seren_lodestar.dtos import JetsonNodeOptions, NodeManifest, NodeResponse

UI = Path(__file__).resolve().parents[1] / "seren_lodestar" / "viewer" / "ui"

CLIP = {"carabiner": "claude", "node": "desktop", "kbh": "C:/Users/alice/seren/wren/kbh/claude.kbh",
        "configured": True, "version": "5.1.3+1f116ed", "sha256": "a" * 64, "present": True, "rack": False,
        "found_by": ["ledger"], "install": {"instance": "wren", "installed_at": "2026-10-23T18:00:00Z",
                                            "source": "release", "source_ref": "v5.1.3", "project": "", "record": ""}}


def _client(payload) -> JetsonAgentClient:
    c = JetsonAgentClient(JetsonNodeOptions(name="desktop", agent_url="http://10.0.0.9:7777"))
    c._client = httpx.AsyncClient(base_url="http://10.0.0.9:7777/",
                                  transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    return c


@pytest.mark.asyncio
async def test_the_node_read_keeps_the_carabiners():
    n = await _client({"manifest": {"hostname": "desktop"}, "runtime": {}, "carabiners": [CLIP]}).get_node_async()
    assert n.manifest.hostname == "desktop"
    assert n.carabiners == [CLIP], "the rows pass through whole"


@pytest.mark.asyncio
async def test_an_observatory_from_before_says_nothing():
    n = await _client({"manifest": {"hostname": "desktop"}, "runtime": {}}).get_node_async()
    assert n.carabiners is None


class Agent:
    nickname = ""
    is_host = False

    async def get_node_async(self):
        return NodeResponse(manifest=NodeManifest(hostname="desktop"), carabiners=[CLIP])

    async def get_thermal_async(self):
        return None

    async def get_services_async(self):
        return None


class Cluster:
    def __init__(self):
        self.agents = {"desktop": Agent()}

    def get_snapshots(self):
        return {"desktop": NodeSnapshot(online=True, installed_services=[], status={})}

    def mark_node_suspect(self, name, reason):
        pass

    def suspect_reason(self, name):
        return None


def test_the_status_hands_them_to_the_viewer(tmp_path):
    cfg = LodestarConfig(config_path=str(tmp_path / "seren-lodestar.yaml"))
    with TestClient(create_app(cfg)) as c:
        c.app.state.cluster = Cluster()
        node = c.get("/api/v1/system/status").json()["nodes"][0]
    assert node["agent_node"]["carabiners"][0]["carabiner"] == "claude"
    assert node["agent_node"]["carabiners"][0]["version"] == "5.1.3+1f116ed"


def test_the_node_card_says_them():
    js = (UI / "scripts.js").read_text(encoding="utf-8")
    card = js[js.index("function renderNode(node)"):js.index("// REFRESH CYCLE")]
    assert "node.agent_node?.carabiners" in card
    assert "<span>clips</span>" in card and "${clipRow}" in card
    assert "clips.length ?" in card, "no row for a box with none, or an Observatory that does not say"
