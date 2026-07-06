"""
seren_lodestar.__main__
════════════════════════════════════════════════════════════════════════

Entry point for ``python -m seren_lodestar`` — starts the uvicorn server.

Usage::

    python -m seren_lodestar [--config CONFIG] [--port PORT] [--host HOST]

Config is loaded from ./seren-lodestar.yaml by default. Override with
--config or the SEREN_LODESTAR_CONFIG env var.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys


def main():
    parser = argparse.ArgumentParser(
        description="SerenLodestar — cluster head for the Seren stack"
    )
    parser.add_argument("--config", "-c", default=None,
                        help="Path to seren-lodestar.yaml (default: "
                             "./seren-lodestar.yaml or $SEREN_LODESTAR_CONFIG)")
    parser.add_argument("--port", type=int, default=0,
                        help="Override the configured port")
    parser.add_argument("--host", type=str, default=None,
                        help="Override the configured host")
    args = parser.parse_args()

    from .app import create_app
    from .config import load_config

    cfg = load_config(args.config)
    if args.port:
        cfg.server.port = args.port
    if args.host:
        cfg.server.host = args.host

    # Log the cluster topology
    print(f"[lodestar] config: {cfg.server.host}:{cfg.server.port}", file=sys.stderr)
    print(f"[lodestar] inbound auth: "
          + ("DISABLED (no token)" if not cfg.server.bearer_token else "enabled"),
          file=sys.stderr)
    print(f"[lodestar] cluster: {len(cfg.cluster.nodes)} node(s) configured",
          file=sys.stderr)
    for n in cfg.cluster.nodes:
        prefs = ",".join(n.preferred_for) if n.preferred_for else "-"
        print(f"           {n.name:<12} {n.agent_url:<32} preferred:[{prefs}]",
              file=sys.stderr)

    # Configure logging
    _setup_logging()

    app = create_app(cfg)

    import uvicorn
    print("[lodestar] ready", file=sys.stderr)
    uvicorn.run(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level=os.environ.get("SEREN_LODESTAR_LOG_LEVEL", "info").lower(),
    )


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )


if __name__ == "__main__":
    main()
