"""
Image routes - pictures, through the cluster.

    GET  /api/v1/comfy/catalog              what the image node's models ARE (its Observatory's catalog)
    POST /api/v1/comfy/catalog/scan         have it look again: {hash, civitai}
    GET  /api/v1/comfy/compatible?model=    what can go with a model - the LoRAs, and how well; its style and profile
    PUT  /api/v1/comfy/profiles             what someone likes with a model: {model, loras?, negative?, vae?}
    POST /api/v1/image/plan                 what a request would draw, and the workflow, without drawing
    POST /api/v1/image                      draw it: {prompt, model?, loras?, width?, height?, ...}

A caller asks for a picture in words; Lodestar plans it against every node
with ComfyUI (preferred_for: comfy first) and keeps those that have the model
AND whose ComfyUI can draw its family (the node's `families`: the Xavier 32GB
keeps every model but draws SDXL and SD 1.5 only), turns the words
into a workflow from that node's catalog (seren_lodestar.comfy_templates),
wakes ComfyUI the way any service is woken (ensure, a lease held by the
caller), has the node's Observatory run it, and lets go. The images come
back in the answer, base64; keeping them is the caller's business.

A plan can be looked at and changed before it is drawn: POST /image with
`workflow` (a plan's, edited) draws exactly that.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from .. import comfy_templates
from .services import service_ensure, service_release

API_VERSION = "v1"
SERVICE = "comfy"
DRAW_TIMEOUT = 900.0
CATALOG_TTL = 120.0

router = APIRouter(tags=["images"])


def _agents(request: Request) -> list:
    """Every online node with ComfyUI, the preferred ones first."""
    cluster = request.app.state.cluster
    if hasattr(cluster, "nodes_for"):
        nodes = cluster.nodes_for(SERVICE)
    else:                                    # a cluster from before nodes_for: its one choice
        one = cluster.choose_node_for(SERVICE)
        nodes = [one] if one is not None else []
    return [a for a in (cluster.get_agent(n.name) for n in nodes) if a is not None]


def _no_node() -> JSONResponse:
    return JSONResponse({"error": "service_unavailable", "detail": "no online node has ComfyUI installed"},
                        status_code=503)


async def _catalog(request: Request, agent) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    """A node's catalog, kept for a couple of minutes - a request form asks
    for it often, and it changes when someone adds a model."""
    cache = getattr(request.app.state, "comfy_catalog", None) or {}
    hit = cache.get(agent.node_name)
    if hit and time.monotonic() - hit[0] < CATALOG_TTL:
        return hit[1], None
    status, body = await agent.comfy_async("GET", "catalog", timeout=120)
    if status != 200 or not isinstance(body, dict):
        return None, JSONResponse({"error": "no catalog from the image node", "node": agent.node_name,
                                   "detail": body.get("detail") or body.get("error") if isinstance(body, dict) else body},
                                  status_code=502)
    cache[agent.node_name] = (time.monotonic(), body)
    request.app.state.comfy_catalog = cache
    return body, None


async def _catalogs(request: Request) -> tuple[list[tuple[Any, dict[str, Any]]], JSONResponse | None]:
    """(agent, catalog) for every image node that answered, in order. None
    answering is the error; one not answering is only left out."""
    agents = _agents(request)
    if not agents:
        return [], _no_node()
    got = await asyncio.gather(*(_catalog(request, a) for a in agents))
    out = [(a, cat) for a, (cat, err) in zip(agents, got) if cat is not None]
    if not out:
        return [], got[0][1]
    return out, None


def _forget(request: Request, agent) -> None:
    (getattr(request.app.state, "comfy_catalog", None) or {}).pop(agent.node_name, None)


def can_draw(cat: dict[str, Any], arch: str) -> bool:
    """The node has a template for the family and its ComfyUI runs it."""
    fams = cat.get("families") or []
    return arch in comfy_templates.SETTINGS and (not fams or arch in fams)


def _candidates(catalogs, body: dict[str, Any]):
    """[(agent, plan)] - every node that has what the request names AND can
    draw its family, in order - and why the others cannot."""
    want = str(body.get("node") or "")
    out, why = [], []
    for agent, cat in catalogs:
        if want and agent.node_name != want:
            continue
        try:
            p = comfy_templates.plan(cat, body)
        except comfy_templates.Refused as ex:
            why.append(f"{agent.node_name}: {ex}")
            continue
        if not can_draw(cat, p.arch):
            why.append(f"{agent.node_name}: its ComfyUI does not draw {p.arch} (it draws {', '.join(cat.get('families') or [])})")
            continue
        out.append((agent, p))
    return out, why


@router.get(f"/api/{API_VERSION}/comfy/catalog")
async def comfy_catalog(request: Request):
    """Every image node's catalog as one: each file with the nodes that have
    it and the nodes that can draw it; and each node's own summary."""
    catalogs, err = await _catalogs(request)
    if err:
        return err
    files: dict[str, Any] = {}
    for agent, cat in catalogs:
        for key, f in (cat.get("files") or {}).items():
            entry = files.setdefault(key, {**f, "nodes": [], "drawable_on": []})
            entry["nodes"].append(agent.node_name)
            if f.get("role") in comfy_templates.MODEL_ROLES:
                # how the model wants to be prompted rides with it, so a
                # request form can guide the words before there is a plan
                entry.setdefault("style", comfy_templates.prompt_style(f))
                if can_draw(cat, f.get("arch") or ""):
                    entry["drawable_on"].append(agent.node_name)
    return {"files": files, "node": catalogs[0][0].node_name,
            "nodes": [{"node": a.node_name, "families": c.get("families") or [], "summary": c.get("summary"),
                       "scan": c.get("scan"), "profiles": c.get("profiles") or {}} for a, c in catalogs]}


@router.post(f"/api/{API_VERSION}/comfy/catalog/scan")
async def comfy_catalog_scan(request: Request, body: dict = Body(default={})):
    agents = _agents(request)
    if not agents:
        return _no_node()
    got = await asyncio.gather(*(a.comfy_async("POST", "catalog/scan", body, timeout=60) for a in agents))
    for a in agents:
        _forget(request, a)
    return {"ok": all(s == 200 for s, _ in got),
            "nodes": {a.node_name: out for a, (_, out) in zip(agents, got)}}


@router.put(f"/api/{API_VERSION}/comfy/profiles")
async def comfy_profiles(request: Request, body: dict = Body(...)):
    """{model, loras?: [{name, weight}], negative?, vae?} - what someone likes
    with a model, kept on every image node that has it, so whichever draws
    wears the same. Only the model named: its profile goes."""
    catalogs, err = await _catalogs(request)
    if err:
        return err
    targets = []
    for agent, cat in catalogs:
        files = comfy_templates.files_of(cat)
        model = comfy_templates.find(files, str(body.get("model") or ""), comfy_templates.MODEL_ROLES)
        if model is None:
            continue
        for want in body.get("loras") or []:
            name = want.get("name") if isinstance(want, dict) else want
            lora = comfy_templates.find(files, str(name or ""), ("lora",))
            if lora is None:
                return JSONResponse({"error": f"no LoRA called {name!r} on {agent.node_name}"}, status_code=400)
            if lora.get("arch") != model.get("arch"):
                return JSONResponse({"error": f"{lora['name']} will not load on {model['name']}"}, status_code=400)
        targets.append((agent, model["key"]))
    if not targets:
        return JSONResponse({"error": f"no model called {body.get('model')!r} on any image node"}, status_code=404)
    got = await asyncio.gather(*(a.comfy_async("PUT", "profiles", {**body, "model": key}, timeout=30)
                                 for a, key in targets))
    for a, _ in targets:
        _forget(request, a)
    bad = [(a.node_name, out) for (a, _), (s, out) in zip(targets, got) if s != 200]
    if bad:
        return JSONResponse({"error": "not every node kept it", "nodes": dict(bad)}, status_code=502)
    return {"ok": True, "nodes": [a.node_name for a, _ in targets], "profiles": got[0][1].get("profiles")}


@router.get(f"/api/{API_VERSION}/comfy/compatible")
async def comfy_compatible(request: Request, model: str):
    """What can go with a model - from the first node that can draw it, else
    the first that has it."""
    catalogs, err = await _catalogs(request)
    if err:
        return err
    found = []
    for agent, cat in catalogs:
        try:
            c = comfy_templates.compatible(cat, model)
        except comfy_templates.Refused:
            continue
        found.append((agent, cat, c))
    if not found:
        return JSONResponse({"error": f"no model called {model!r} on any image node"}, status_code=404)
    drawable_on = [a.node_name for a, cat, c in found if can_draw(cat, c["arch"] or "")]
    agent, cat, c = next(((a, ct, x) for a, ct, x in found if a.node_name in drawable_on), found[0])
    return {**c, "drawable": bool(drawable_on), "drawable_on": drawable_on, "node": agent.node_name}


@router.post(f"/api/{API_VERSION}/image/plan")
async def image_plan(request: Request, body: dict = Body(...)):
    catalogs, err = await _catalogs(request)
    if err:
        return err
    cands, why = _candidates(catalogs, body)
    if not cands:
        return JSONResponse({"error": why[0].split(": ", 1)[-1] if why else "nothing can draw that",
                             "nodes": why}, status_code=400)
    agent, p = cands[0]
    return {"ok": True, "node": agent.node_name, "also": [a.node_name for a, _ in cands[1:]],
            **p.describe(), "workflow": p.workflow}


@router.post(f"/api/{API_VERSION}/image")
async def image(request: Request, body: dict = Body(...)):
    """Draw a request on the first image node that can draw it and wakes
    (or a plan's `workflow`, as edited, on the plan's `node`). Holds ComfyUI
    on that node for the drawing - as `holder` (default "image") - and lets
    go after; the run policy decides when it is put away."""
    holder = str(body.get("holder") or "image")
    if isinstance(body.get("workflow"), dict) and body["workflow"]:
        agents = _agents(request)
        want = str(body.get("node") or "")
        agents = [a for a in agents if a.node_name == want] if want else agents[:1]
        if not agents:
            return JSONResponse({"error": f"no image node called {want!r} is online"} if want else
                                {"error": "service_unavailable", "detail": "no online node has ComfyUI installed"},
                                status_code=503)
        tries = [(agents[0], body["workflow"], {})]
    else:
        catalogs, err = await _catalogs(request)
        if err:
            return err
        cands, why = _candidates(catalogs, body)
        if not cands:
            return JSONResponse({"error": why[0].split(": ", 1)[-1] if why else "nothing can draw that",
                                 "nodes": why}, status_code=400)
        tries = [(a, p.workflow, p.describe()) for a, p in cands]
    asleep = []
    for agent, workflow, described in tries:
        up = await service_ensure(request, SERVICE, {"holder": holder, "reason": "drawing a picture",
                                                     "node": agent.node_name, "wait_seconds": 240})
        # put_away: ComfyUI is put away after the drawing when nobody else
        # holds it, whoever started it - a model on a small GPU is memory the
        # next job cannot have (and the Spark's stayed up all night once).
        let_go = {"holder": holder, "node": agent.node_name, "put_away": True}
        if not up.get("ok") or not up.get("ready"):
            asleep.append(f"{agent.node_name}: {up.get('error') or 'did not come up'}")
            if up.get("started"):
                await service_release(request, SERVICE, let_go)   # what this ensure started and could not use
            continue                         # the next node that can draw it
        try:
            status, out = await agent.comfy_async("POST", "generate", {
                "workflow": workflow, "timeout_seconds": DRAW_TIMEOUT,
                "free_after": bool(body.get("free_after", True))}, timeout=DRAW_TIMEOUT + 60)
        finally:
            await service_release(request, SERVICE, let_go)
        if status != 200 or not isinstance(out, dict) or not out.get("ok"):
            detail = out if isinstance(out, dict) else {"error": str(out)}
            return JSONResponse({"error": detail.get("error") or "the drawing failed", "detail": detail.get("detail"),
                                 "node_errors": detail.get("node_errors"), "node": agent.node_name, **described},
                                status_code=502)
        return {"ok": True, "node": agent.node_name, **described, "images": out.get("images", []),
                "seconds": out.get("seconds"), **({"skipped": asleep} if asleep else {})}
    return JSONResponse({"error": "ComfyUI did not come up on any node that can draw it", "nodes": asleep},
                        status_code=503)
