"""
The exposure check is WIRED, not just available.

seren_meninges.exposure refuses an open bind with no token; these prove that
seren-lodestar's own main() calls it before the app is built, and that the
documented override really does let the same config through. A helper nobody
invokes is the defect this family keeps finding.
"""
from __future__ import annotations

import importlib
import sys

import pytest

OPEN = 'server:\n  host: 0.0.0.0\n  port: 6361\n'
ALLOWED = 'server:\n  host: 0.0.0.0\n  port: 6361\n  allow_open_lan: true\n'
SCRUB = ("SEREN_LODESTAR_ALLOW_OPEN_LAN", "SEREN_LODESTAR_HOST", "SEREN_LODESTAR_PORT", "SEREN_LODESTAR_CONFIG",
         "SEREN_LODESTAR_BEARER_TOKEN", "SEREN_LODESTAR_BEARER_TOKEN_ENV", "SEREN_LODESTAR_BEARER_TOKEN_KEYRING",
         "SEREN_LODESTAR_TOKEN")


class _AppBuilt(Exception):
    """Raised by the stub create_app: main() got past the gate."""


def _run(tmp_path, monkeypatch, yaml_text):
    for name in SCRUB:
        monkeypatch.delenv(name, raising=False)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml_text, encoding="utf-8")
    holder = importlib.import_module("seren_lodestar.app")
    monkeypatch.setattr(holder, "create_app",
                        lambda *a, **k: (_ for _ in ()).throw(_AppBuilt()))
    monkeypatch.setattr(sys, "argv", ["seren_lodestar", "--config", str(cfg)])
    main_mod = importlib.import_module("seren_lodestar.__main__")
    main_mod.main()


def test_an_open_bind_with_no_token_refuses_before_the_app_is_built(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, monkeypatch, OPEN)
    assert exc.value.code == 78, "EX_CONFIG - a supervisor can tell config from crash"
    out = capsys.readouterr()
    assert "REFUSING TO START" in out.out + out.err
    assert "allow_open_lan" in out.out + out.err, "the way out is printed"


def test_the_written_override_lets_the_same_config_through(tmp_path, monkeypatch, capsys):
    with pytest.raises(_AppBuilt):
        _run(tmp_path, monkeypatch, ALLOWED)
    out = capsys.readouterr()
    assert "OPEN ON THE NETWORK" in out.out + out.err, "and it says so"


def test_the_env_override_lets_the_same_config_through(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(OPEN, encoding="utf-8")
    for name in SCRUB:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SEREN_LODESTAR_ALLOW_OPEN_LAN", "1")
    holder = importlib.import_module("seren_lodestar.app")
    monkeypatch.setattr(holder, "create_app", lambda *a, **k: (_ for _ in ()).throw(_AppBuilt()))
    monkeypatch.setattr(sys, "argv", ["seren_lodestar", "--config", str(cfg)])
    with pytest.raises(_AppBuilt):
        importlib.import_module("seren_lodestar.__main__").main()
