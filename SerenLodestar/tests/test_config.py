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
