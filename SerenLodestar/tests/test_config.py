"""
Config tests — validates that LodestarConfig loads defaults, parses YAML,
and applies env overrides correctly.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from seren_lodestar.config import (
    LodestarConfig, ClusterConfig, JetsonNodeConfig,
    SchedulerConfig, RuntimeConfig, load_config,
)


def test_default_config():
    """With no config file or env, defaults should be sane."""
    cfg = load_config()
    assert cfg.server.port == 6361
    assert cfg.server.host == "0.0.0.0"
    assert not cfg.server.bearer_token


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("SEREN_LODESTAR_PORT", "7426")
    monkeypatch.setenv("SEREN_LODESTAR_HOST", "10.0.0.1")
    cfg = load_config()
    assert cfg.server.port == 7426
    assert cfg.server.host == "10.0.0.1"


def test_yaml_loading(tmp_path):
    yaml_content = {
        "server": {"port": 7427},
        "cluster": {
            "nodes": [
                {"name": "test-node", "agent_url": "http://localhost:7374"},
            ],
        },
    }
    cfg_path = tmp_path / "seren-lodestar.yaml"
    with open(cfg_path, "w") as f:
        yaml.dump(yaml_content, f)

    cfg = load_config(str(cfg_path))
    assert cfg.server.port == 7427
    assert len(cfg.cluster.nodes) == 1
    assert cfg.cluster.nodes[0].name == "test-node"


def test_cluster_validation():
    """Empty cluster nodes should log a warning but not crash."""
    cfg = load_config()
    assert len(cfg.cluster.nodes) == 0
    # Validation is lenient — no crash


def test_node_config():
    node = JetsonNodeConfig(
        name="orin-nano",
        agent_url="http://10.0.0.2:7374",
        preferred_for=["whisper", "kokoro"],
    )
    assert node.name == "orin-nano"
    assert "whisper" in node.preferred_for


# ── the "Lodestar can't see my node" evening ───────────────────────────

def test_top_level_nodes_is_ignored_and_says_so(tmp_path, caplog):
    """YAML accepts a top-level `nodes:` happily; the loader reads
    cluster.nodes and ignores it. Result: an orchestrator with nothing to
    orchestrate, and no error anywhere obvious. The diagnostic has to name
    THIS mistake, not just report 'empty'."""
    import logging
    p = tmp_path / "c.yaml"
    p.write_text(
        'server:\n  port: 6361\n'
        'nodes:\n'
        '  - name: "nuc"\n'
        '    agent_url: "http://127.0.0.1:7777"\n'
    )
    with caplog.at_level(logging.WARNING):
        cfg = load_config(str(p))
    assert len(cfg.cluster.nodes) == 0
    blob = caplog.text
    assert "TOP-LEVEL" in blob, "must name the actual mistake"
    assert "cluster:" in blob, "must show the correct shape"


def test_correctly_nested_nodes_load(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        'cluster:\n  nodes:\n'
        '    - name: "nuc"\n'
        '      agent_url: "http://127.0.0.1:7777"\n'
        '      is_host: true\n'
    )
    cfg = load_config(str(p))
    assert [n.name for n in cfg.cluster.nodes] == ["nuc"]
    assert cfg.cluster.nodes[0].is_host is True


def test_bind_address_as_a_destination_is_flagged():
    """0.0.0.0 as a target WORKS on Linux, which is exactly why it survives
    into configs that then fail somewhere less forgiving."""
    from seren_lodestar.config import ClusterConfig
    c = ClusterConfig.from_dict(
        {"nodes": [{"name": "nuc", "agent_url": "http://0.0.0.0:7777"}]})
    err = c.validate()
    assert err and "0.0.0.0" in err and "127.0.0.1" in err
