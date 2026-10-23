"""
seren_lodestar.shelf - the .kbh files Lodestar hands to nodes (23 Oct 2026).

Lodestar pulls; the nodes never reach the internet. The shelf is a folder of
<carabiner>.kbh files: `fetch` fills it from a GitHub release (each asset
checked against the release's own <name>.kbh.sha256 before it lands), and a
file dropped in by hand (a dist/ build to try on one box first) is on the
shelf just the same, marked "by hand". An upgrade sends the shelf's file to
the node's Observatory (seren_sinew.carabiners has the rest of the road).

Each file is read, never run: its version comes out of the zip
(seren_sinew.carabiners.kbh_identity), and `status` compares a node's
carabiner with the shelf's - current, behind, ahead, or different (the same
release, another build).

The sha256 beside a release asset comes from the same release, so it proves
the file arrived whole, not who made it.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote

import httpx

from seren_sinew.carabiners import MAX_KBH, NAME, kbh_identity

META = "shelf.json"


def release_of(version: str) -> tuple[int, ...]:
    """5.1.3+1f116ed.dirty.62bba2db -> (5, 1, 3)."""
    return tuple(int(p) for p in re.findall(r"\d+", (version or "").split("+", 1)[0])[:3])


def status(node_version: str, node_sha: str, shelf: Optional[dict[str, Any]]) -> str:
    """How a node's .kbh stands against the shelf's: none (nothing on the
    shelf for it), current, behind, ahead, different."""
    if not shelf:
        return "none"
    if node_sha and node_sha == shelf.get("sha256"):
        return "current"
    mine, theirs = release_of(node_version), release_of(shelf.get("version", ""))
    if not mine or not theirs:
        return "different"
    if mine < theirs:
        return "behind"
    if mine > theirs:
        return "ahead"
    return "different"


class Shelf:
    def __init__(self, root: Path, repo: str, api: str = "https://api.github.com",
                 transport: Optional[httpx.AsyncBaseTransport] = None,
                 now: Callable[[], _dt.datetime] = lambda: _dt.datetime.now(_dt.timezone.utc)) -> None:
        self.root = Path(root)
        self.repo = repo
        self.api = api.rstrip("/")
        self._transport = transport
        self._now = now

    # ── what is on it ────────────────────────────────────────────────────
    def _meta(self) -> dict[str, Any]:
        try:
            d = json.loads((self.root / META).read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_meta(self, meta: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / (META + ".tmp")
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        os.replace(tmp, self.root / META)

    def entries(self) -> dict[str, dict[str, Any]]:
        """{carabiner: {file, version, sha256, source, fetched_at}}."""
        if not self.root.is_dir():
            return {}
        meta = self._meta()
        out = {}
        for p in sorted(self.root.glob("*.kbh")):
            if not NAME.fullmatch(p.stem):
                continue
            try:
                version, sha = kbh_identity(str(p))
            except OSError:
                continue
            m = meta.get(p.stem) if isinstance(meta.get(p.stem), dict) else {}
            fetched = m.get("sha256") == sha
            out[p.stem] = {"carabiner": p.stem, "file": str(p), "version": version, "sha256": sha,
                           "size": p.stat().st_size, "source": m.get("source", "") if fetched else "by hand",
                           "fetched_at": m.get("fetched_at", "") if fetched else ""}
        return out

    def get(self, carabiner: str) -> tuple[bytes, dict[str, Any]]:
        entry = self.entries().get(carabiner)
        if entry is None:
            raise LookupError(f"no {carabiner}.kbh on the shelf ({self.root}): fetch a release, or put one there")
        return Path(entry["file"]).read_bytes(), entry

    # ── filling it ───────────────────────────────────────────────────────
    async def fetch(self, tag: str = "") -> dict[str, Any]:
        """Every <name>.kbh of a release (the latest, or `tag`) onto the
        shelf, each only if it matches its .sha256 asset."""
        if not self.repo:
            return {"ok": False, "error": "no release repo set (carabiners.release_repo)"}
        where = f"tags/{quote(tag, safe='')}" if tag else "latest"
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "seren-lodestar"}
        token = os.environ.get("GITHUB_TOKEN", "")
        fetched: list[dict[str, Any]] = []
        refused: dict[str, str] = {}
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0, headers=headers,
                                     transport=self._transport) as client:
            try:
                r = await client.get(f"{self.api}/repos/{self.repo}/releases/{where}",
                                     headers={"Authorization": f"Bearer {token}"} if token else None)
                r.raise_for_status()
                rel = r.json()
            except (httpx.HTTPError, ValueError) as e:
                return {"ok": False, "error": f"cannot read the release of {self.repo} ({where}): "
                                              f"{type(e).__name__}: {e}"}
            name = str(rel.get("tag_name") or tag or "?")
            assets = {str(a.get("name")): str(a.get("browser_download_url"))
                      for a in rel.get("assets") or [] if isinstance(a, dict)}
            meta = self._meta()
            self.root.mkdir(parents=True, exist_ok=True)
            for asset in sorted(assets):
                stem = asset[:-4] if asset.endswith(".kbh") else ""
                if not stem or not NAME.fullmatch(stem):
                    continue
                if asset + ".sha256" not in assets:
                    refused[stem] = "the release has no .sha256 beside it"
                    continue
                try:
                    want = (await client.get(assets[asset + ".sha256"])).raise_for_status().text.split()[0].lower()
                    resp = (await client.get(assets[asset])).raise_for_status()
                except (httpx.HTTPError, IndexError) as e:
                    refused[stem] = f"{type(e).__name__}: {e}"
                    continue
                data = resp.content
                got = hashlib.sha256(data).hexdigest()
                if len(data) > MAX_KBH:
                    refused[stem] = f"bigger than {MAX_KBH} bytes"
                    continue
                if got != want:
                    refused[stem] = f"sha256 {got}, and the release says {want}: not put on the shelf"
                    continue
                tmp = self.root / f".{asset}.part"
                tmp.write_bytes(data)
                os.replace(tmp, self.root / asset)
                meta[stem] = {"source": f"release {name}", "sha256": got,
                              "fetched_at": self._now().isoformat(timespec="seconds")}
                fetched.append({"carabiner": stem, "sha256": got})
            self._save_meta(meta)
        return {"ok": not refused, "release": name, "fetched": fetched, "refused": refused,
                "shelf": list(self.entries().values()),
                "error": "; ".join(f"{k}: {v}" for k, v in refused.items())}
