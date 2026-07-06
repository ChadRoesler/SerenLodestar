"""
seren_lodestar — cluster head / guiding star for the Seren stack.

The Lodestar owns the cluster topology, manages service lifecycle across
Jetson nodes, routes inference requests to llama nodes with MCP tool-call
loops, and schedules recurring tasks. It is the "guiding star" that
coordinates the entire Seren fleet.

Integrates seren_meninges (config/auth/viewer baseplate) and seren_sinew
(request logging) — following the same pattern as seren_loci, seren_memory,
seren_corpus_callosum, seren_probe, and seren_workbench.
"""
from __future__ import annotations

# Version flows from the git tag via setuptools-scm (written to _version.py at
# build time, read here). Fallback only fires in a bare source checkout that was
# never built. Mirrors the family so every seren_* exposes __version__ alike.
try:
    from ._version import version as __version__
except Exception:  # noqa: BLE001 - source checkout without a build
    __version__ = "0.0.0+unknown"

from .config import LodestarConfig, load_config  # noqa: F401,E402

__all__ = [
    "__version__",
    "LodestarConfig",
    "load_config",
]
