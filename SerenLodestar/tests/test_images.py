"""
Pictures through the cluster: a request in words -> a workflow from the
image node's catalog (seren_lodestar.comfy_templates), then the whole road -
Lodestar -> a REAL Observatory (in-process) -> its ComfyUI (scripted).
"""
from __future__ import annotations

import base64
import json
import struct
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from seren_lodestar import comfy_templates as ct
from seren_lodestar.app import create_app
from seren_lodestar.config import LodestarConfig

# The shape of a real node's catalog (a ComfyUI models folder, 14 Oct 2026).
CATALOG = {"schema": 1, "files": {
    "checkpoints/krea2TurboOfficialComfy_krea2TurboBf16.safetensors":
        {"role": "checkpoint", "name": "krea2TurboOfficialComfy_krea2TurboBf16.safetensors", "arch": "krea2", "kind": "image"},
    "checkpoints/snofsSexNudesAndOther_krea2RawV14_bf16.safetensors":
        {"role": "checkpoint", "name": "snofsSexNudesAndOther_krea2RawV14_bf16.safetensors", "arch": "krea2", "kind": "image"},
    "diffusion_models/krea2_turbo_fp8_scaled.safetensors":
        {"role": "diffusion", "name": "krea2_turbo_fp8_scaled.safetensors", "arch": "krea2", "kind": "image"},
    "checkpoints/autismmixSDXL_autismmixPony.safetensors":
        {"role": "checkpoint", "name": "autismmixSDXL_autismmixPony.safetensors", "arch": "sdxl", "base": "Pony", "kind": "image"},
    "checkpoints/juggernautXL_v9.safetensors": {"role": "checkpoint", "error": "not a safetensors file", "kind": "unknown"},
    "diffusion_models/wan2.2_i2v_high_noise_14B_fp16.safetensors":
        {"role": "diffusion", "name": "wan2.2_i2v_high_noise_14B_fp16.safetensors", "arch": "wan", "kind": "video"},
    "loras/Storybook_Folk_Art_Krea2.safetensors":
        {"role": "lora", "name": "Storybook_Folk_Art_Krea2.safetensors", "arch": "krea2", "kind": "image",
         "triggers": ["storybook folk art illustration"]},
    "loras/watercolor ink illustration_krea.safetensors":
        {"role": "lora", "name": "watercolor ink illustration_krea.safetensors", "arch": "krea2", "kind": "image"},
    "loras/Style_Asanagi_XL_ff.safetensors":
        {"role": "lora", "name": "Style_Asanagi_XL_ff.safetensors", "arch": "sdxl", "base": "Pony", "kind": "image"},
    "loras/Rapscallion_Illustrious.safetensors":
        {"role": "lora", "name": "Rapscallion_Illustrious.safetensors", "arch": "sdxl", "base": "Illustrious",
         "kind": "image", "triggers": ["Rapscallionstyle"]},
    "loras/Wan22_Lightning.safetensors": {"role": "lora", "name": "Wan22_Lightning.safetensors", "arch": "wan", "kind": "video"},
    "text_encoders/Huihui-Qwen3-VL-4B-Instruct-abliterated-fp8_scaled.safetensors":
        {"role": "text_encoder", "name": "Huihui-Qwen3-VL-4B-Instruct-abliterated-fp8_scaled.safetensors", "arch": "qwen3-vl"},
    "vae/Wan2.1_VAE_upscale2x_imageonly_real_v1.safetensors":
        {"role": "vae", "name": "Wan2.1_VAE_upscale2x_imageonly_real_v1.safetensors", "arch": "wan-vae",
         "decodes": ["wan", "krea2", "qwen-image"]},
    "vae/wan_2.1_vae.safetensors": {"role": "vae", "name": "wan_2.1_vae.safetensors", "arch": "wan-vae",
                                    "decodes": ["wan", "krea2", "qwen-image"]},
}}


def nodes_of(workflow, kind):
    return [n for n in workflow.values() if n["class_type"] == kind]


# ── a request -> a workflow ──────────────────────────────────────────────

def test_krea2_is_the_portrait_recipe():
    p = ct.plan(CATALOG, {"prompt": "a wren on a lantern-lit chair", "seed": 7,
                          "loras": [{"name": "Storybook_Folk_Art_Krea2.safetensors", "weight": 0.7},
                                    {"name": "watercolor ink", "weight": 0.5}]})
    w = p.workflow
    assert p.model["name"] == "krea2TurboOfficialComfy_krea2TurboBf16.safetensors", "a Turbo checkpoint by default"
    assert nodes_of(w, "CLIPLoader")[0]["inputs"] == {
        "clip_name": "Huihui-Qwen3-VL-4B-Instruct-abliterated-fp8_scaled.safetensors", "type": "krea2", "device": "default"}
    assert nodes_of(w, "VAELoader")[0]["inputs"]["vae_name"] == "wan_2.1_vae.safetensors", "the plain Wan VAE, not the upscaler"
    loras = nodes_of(w, "LoraLoaderModelOnly")
    assert [(l["inputs"]["lora_name"], l["inputs"]["strength_model"]) for l in loras] == [
        ("Storybook_Folk_Art_Krea2.safetensors", 0.7), ("watercolor ink illustration_krea.safetensors", 0.5)]
    k = nodes_of(w, "KSampler")[0]["inputs"]
    assert (k["steps"], k["cfg"], k["sampler_name"], k["scheduler"], k["seed"]) == (8, 1.0, "euler", "simple", 7)
    assert k["model"] == ["L1", 0] and nodes_of(w, "ConditioningZeroOut")
    assert p.prompt == "storybook folk art illustration, a wren on a lantern-lit chair", "the LoRA's trigger leads"
    assert nodes_of(w, "SaveImage")[0]["inputs"]["filename_prefix"] == "seren/7"


def test_a_bare_krea2_diffusion_model_gets_a_unet_loader():
    w = ct.plan(CATALOG, {"prompt": "x", "model": "krea2_turbo_fp8"}).workflow
    assert w["1"] == {"class_type": "UNETLoader",
                      "inputs": {"unet_name": "krea2_turbo_fp8_scaled.safetensors", "weight_dtype": "default"}}


def test_pony_gets_its_score_tags_and_an_illustrious_lora_may_work():
    p = ct.plan(CATALOG, {"prompt": "a fox in snow", "model": "autismmix",
                          "loras": [{"name": "Style_Asanagi_XL_ff.safetensors"}, {"name": "Rapscallion_Illustrious.safetensors"}]})
    assert p.prompt.startswith("score_9, score_8_up, score_7_up, Rapscallionstyle, a fox in snow")
    assert p.negative.startswith("score_4")
    assert [l["fit"] for l in p.loras] == ["good", "may work"]
    assert p.notes == ["Rapscallion_Illustrious.safetensors was made for Illustrious, the model is Pony - it may work"]
    w = p.workflow
    assert nodes_of(w, "LoraLoader")[1]["inputs"]["clip"] == ["L0", 1], "an sdxl LoRA wears the text encoder too"
    assert w["9"]["inputs"]["vae"] == ["1", 2], "the checkpoint's own VAE"
    assert nodes_of(w, "KSampler")[0]["inputs"]["steps"] == 28


@pytest.mark.parametrize("req, why", [
    ({"prompt": "x", "loras": [{"name": "Style_Asanagi_XL_ff.safetensors"}]}, "will not load"),
    ({"prompt": "x", "loras": [{"name": "Wan22_Lightning.safetensors"}]}, "wan LoRA"),
    ({"prompt": "x", "model": "wan2.2_i2v_high_noise_14B_fp16.safetensors"}, "drawn with"),
    ({"prompt": "x", "model": "juggernautXL_v9.safetensors"}, "no model called"),
    ({"prompt": "x", "loras": [{"name": "nope"}]}, "no LoRA called"),
    ({"prompt": ""}, "needs a prompt"),
    ({"prompt": "x", "width": 9000}, "width is between"),
    ({"prompt": "x", "count": 12}, "count is between"),
])
def test_what_cannot_be_drawn_is_refused_with_why(req, why):
    with pytest.raises(ct.Refused, match=why):
        ct.plan(CATALOG, req)


def test_a_model_and_what_goes_with_it():
    c = ct.compatible(CATALOG, "autismmixSDXL_autismmixPony.safetensors")
    assert c["drawable"] and [(l["name"], l["fit"]) for l in c["loras"]] == [
        ("Style_Asanagi_XL_ff.safetensors", "good"), ("Rapscallion_Illustrious.safetensors", "may work")]
    assert not ct.compatible(CATALOG, "wan2.2_i2v_high_noise_14B_fp16.safetensors")["drawable"]


# ── the whole road ───────────────────────────────────────────────────────


def _fake_model(path, tensors, meta=None):
    header, off = {}, 0
    for name, shape in tensors.items():
        header[name] = {"dtype": "U8", "shape": shape, "data_offsets": [off, off + 1]}
        off += 1
    if meta:
        header["__metadata__"] = meta
    raw = json.dumps(header).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + b"\0" * off)


def test_a_picture_from_words_through_a_real_observatory(tmp_path, monkeypatch):
    obs_app = pytest.importorskip("seren_observatory.app")
    from seren_observatory import lifecycle
    from seren_observatory.services import comfy as obs_comfy
    from seren_lodestar.agent_client import JetsonAgentClient

    home = tmp_path / "home"
    (home / ".seren" / "services").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    models = tmp_path / "ComfyUI" / "models"
    _fake_model(models / "checkpoints" / "krea2TurboOfficialComfy_krea2TurboBf16.safetensors",
                {"blocks.0.attn.qknorm.knorm.scale": [128]})
    _fake_model(models / "loras" / "Storybook_Folk_Art_Krea2.safetensors",
                {"diffusion_model.blocks.0.attn.gate.lora_A.weight": [16, 3072]}, {"modelspec.architecture": "krea2"})
    _fake_model(models / "text_encoders" / "Huihui-Qwen3-VL-4B-Instruct-abliterated-fp8_scaled.safetensors",
                {"model.language_model.layers.0.mlp.down_proj.weight": [2560, 9728]})
    _fake_model(models / "vae" / "wan_2.1_vae.safetensors", {"conv1.weight": [96, 3, 3, 3, 3]})
    (home / ".seren" / "services" / "comfy.json").write_text(json.dumps({
        "schema_version": 1, "service": "comfy", "service_type": "pid_file", "port": 8188, "health_path": "/system_stats",
        "serviceSpecific": {"checkpoints_dir": str(models / "checkpoints")}}))
    (home / ".seren" / "secrets.json").write_text(json.dumps({"observatory_token": "obs-secret"}))

    box = {"running": False, "stops": 0}

    async def probe(manifest):
        return {"ok": box["running"]}

    async def start(manifest):
        box["running"] = True
        return {"ok": True, "pid": 7}

    async def stop(manifest):
        box["running"] = False
        box["stops"] += 1
        return {"ok": True}

    drawn = {}

    async def generate(port, workflow, timeout, free_after, client=None, poll=1.0):
        drawn.update(port=port, workflow=workflow, free_after=free_after, running=box["running"])
        return {"ok": True, "prompt_id": "p1", "seconds": 4.2,
                "images": [{"filename": "7_00001_.png", "mime": "image/png", "data": base64.b64encode(b"PNG").decode()}]}

    monkeypatch.setattr(lifecycle, "probe_port", probe)
    monkeypatch.setattr(lifecycle, "start", start)
    monkeypatch.setattr(lifecycle, "stop", stop)
    monkeypatch.setattr(lifecycle, "ENSURE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(obs_comfy, "generate", generate)
    monkeypatch.setattr(obs_comfy.SCANNER, "start", lambda *a: True)      # no background hashing in a test
    observatory = obs_app.create_app()

    agent = JetsonAgentClient.__new__(JetsonAgentClient)
    agent._node_name = "node-c"
    agent._log = lambda m: None
    agent._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=observatory), base_url="http://192.0.2.102:7777/",
                                      headers={"Authorization": "Bearer obs-secret"})
    cluster = SimpleNamespace(
        choose_node_for=lambda svc: SimpleNamespace(name="node-c") if svc == "comfy" else None,
        get_agent=lambda name: agent if name == "node-c" else None,
        mark_node_suspect=lambda *a: None)

    with TestClient(create_app(LodestarConfig())) as c:
        c.app.state.cluster = cluster
        cat = c.get("/api/v1/comfy/catalog").json()
        assert cat["node"] == "node-c" and cat["nodes"][0]["summary"]["lora"] == {"Krea 2": 1}
        assert cat["files"]["loras/Storybook_Folk_Art_Krea2.safetensors"]["nodes"] == ["node-c"]
        assert box["running"] is False, "the catalog is read without waking ComfyUI"
        model = next(f for f in cat["files"].values() if f.get("role") in ("checkpoint", "diffusion"))
        assert model["style"]["style"] == "prose" and "Sentences" in model["style"]["guide"], "how to prompt it rides with the model"
        assert "style" not in cat["files"]["loras/Storybook_Folk_Art_Krea2.safetensors"]

        plan = c.post("/api/v1/image/plan", json={"prompt": "a wren", "seed": 7,
                                                  "loras": [{"name": "Storybook_Folk_Art_Krea2.safetensors", "weight": 0.7}]}).json()
        assert plan["ok"] and plan["model"].endswith("krea2TurboOfficialComfy_krea2TurboBf16.safetensors")
        assert plan["loras"] == [{"name": "Storybook_Folk_Art_Krea2.safetensors", "weight": 0.7, "fit": "good"}]

        out = c.post("/api/v1/image", json={"prompt": "a wren", "seed": 7, "holder": "wren",
                                            "loras": [{"name": "Storybook_Folk_Art_Krea2.safetensors", "weight": 0.7}]}).json()
        assert out["ok"] and base64.b64decode(out["images"][0]["data"]) == b"PNG" and out["seed"] == 7
        assert drawn["running"] is True, "ComfyUI was up while it drew"
        assert drawn["free_after"] is True and drawn["workflow"] == plan["workflow"]
        assert box["stops"] == 1 and box["running"] is False, "and let go after"
        # ComfyUI brought up by hand (or left up): still put away after a draw
        # when nobody else holds it (15 Oct 2026: the Spark's ran all night)
        box["running"], box["stops"] = True, 0
        assert c.post("/api/v1/image", json={"prompt": "a wren", "seed": 7, "holder": "wren"}).json()["ok"]
        assert box["stops"] == 1 and box["running"] is False, "put away, whoever started it"

        # An edited plan is drawn as it is.
        edited = {**plan["workflow"], "8": {**plan["workflow"]["8"], "inputs": {**plan["workflow"]["8"]["inputs"], "steps": 12}}}
        assert c.post("/api/v1/image", json={"workflow": edited}).json()["ok"]
        assert drawn["workflow"]["8"]["inputs"]["steps"] == 12

        bad = c.post("/api/v1/image", json={"prompt": "x", "loras": [{"name": "nope"}]})
        assert bad.status_code == 400 and "no LoRA called" in bad.json()["error"]

        # A profile, kept on the node, worn by the next plan that names no LoRAs.
        r = c.put("/api/v1/comfy/profiles", json={"model": "krea2Turbo",
                                                   "loras": [{"name": "Storybook_Folk_Art_Krea2.safetensors", "weight": 0.4}]})
        assert r.status_code == 200, r.text
        assert (home / ".seren" / "comfy" / "profiles.json").is_file()
        again = c.post("/api/v1/image/plan", json={"prompt": "a wren"}).json()
        assert again["loras"] == [{"name": "Storybook_Folk_Art_Krea2.safetensors", "weight": 0.4, "fit": "good"}]
        assert c.put("/api/v1/comfy/profiles", json={"model": "nope"}).status_code == 404
        assert c.put("/api/v1/comfy/profiles", json={"model": "krea2Turbo", "loras": [{"name": "zzz"}]}).status_code == 400
        assert c.put("/api/v1/comfy/profiles", json={"model": "krea2Turbo"}).json()["profiles"] == {}, "emptied: gone"


def test_no_image_node_is_a_503(monkeypatch):
    with TestClient(create_app(LodestarConfig())) as c:
        c.app.state.cluster = SimpleNamespace(choose_node_for=lambda svc: None, get_agent=lambda n: None)
        assert c.post("/api/v1/image", json={"prompt": "x"}).status_code == 503
        assert c.get("/api/v1/comfy/catalog").status_code == 503


# ── carried over from SerenTools: styles, negatives, profiles ────────────

def test_each_model_says_how_it_wants_to_be_prompted():
    style = lambda **kw: ct.plan(CATALOG, {"prompt": "x", **kw}).describe()["style"]["style"]
    assert style() == "prose", "Krea 2 reads sentences"
    assert style(model="autismmix") == "booru", "Pony reads booru tags"
    c = ct.compatible(CATALOG, "autismmixSDXL_autismmixPony.safetensors")
    assert c["style"]["style"] == "booru" and "score_9" in c["style"]["guide"]


def test_one_person_asked_for_keeps_everyone_else_out():
    neg = ct.plan(CATALOG, {"prompt": "portrait of a woman reading", "model": "autismmix"}).negative
    assert neg.startswith("score_4") and "extra fingers" in neg and "two people" in neg
    two = ct.plan(CATALOG, {"prompt": "a couple dancing, a man and a woman", "model": "autismmix"}).negative
    assert "two people" not in two and "extra fingers" in two
    assert ct.plan(CATALOG, {"prompt": "a portrait of a woman"}).negative == "", "Krea 2 says no with nothing"
    mine = ct.plan(CATALOG, {"prompt": "a fox", "model": "autismmix", "negative": "snow, bad anatomy"}).negative
    assert mine.endswith(", snow") and mine.count("bad anatomy") == 1, "a request's negative is added, once"


def test_a_profile_wears_its_loras_when_a_request_names_none():
    cat = {**CATALOG, "profiles": {"checkpoints/autismmixSDXL_autismmixPony.safetensors": {
        "loras": [{"name": "Style_Asanagi_XL_ff.safetensors", "weight": 0.6}], "negative": "sepia",
        "vae": "wan_2.1_vae.safetensors"}}}
    p = ct.plan(cat, {"prompt": "a fox", "model": "autismmix"})
    assert [(l["name"], l["weight"]) for l in p.loras] == [("Style_Asanagi_XL_ff.safetensors", 0.6)]
    assert "the model's profile chose the LoRAs" in p.notes and p.negative.endswith("sepia")
    assert p.workflow["3"] == {"class_type": "VAELoader", "inputs": {"vae_name": "wan_2.1_vae.safetensors"}}
    assert p.workflow["9"]["inputs"]["vae"] == ["3", 0], "the profile's VAE in place of the checkpoint's"
    assert ct.plan(cat, {"prompt": "a fox", "model": "autismmix", "loras": []}).loras == [], "an empty list means none"
    assert ct.compatible(cat, "autismmix")["profile"]["negative"] == "sepia"
    with pytest.raises(ct.Refused, match="no VAE called"):
        ct.plan(cat, {"prompt": "x", "model": "autismmix", "vae": "nope.safetensors"})


# ── two image nodes: each draws what its ComfyUI can ────────────────────

def test_the_node_is_picked_by_what_it_can_draw(monkeypatch):
    from seren_lodestar.routes import images as routes

    drawn, woke = [], []

    def agent(name, families, up=True):
        cat = {**CATALOG, "families": families}

        async def comfy_async(method, path, body=None, timeout=35.0):
            if path == "catalog":
                return 200, cat
            if path == "generate":
                drawn.append(name)
                return 200, {"ok": True, "images": [], "seconds": 1.0}
            return 404, {}
        return SimpleNamespace(node_name=name, comfy_async=comfy_async, up=up)

    spark, xavier = agent("node-d", []), agent("node-c", ["sdxl", "sd15"])
    agents = {"node-d": spark, "node-c": xavier}
    order = ["node-d", "node-c"]

    async def ensure(request, service, body):
        woke.append(body["node"])
        return {"ok": agents[body["node"]].up, "ready": agents[body["node"]].up, "error": "asleep"}

    async def release(request, service, body):
        return {"ok": True}

    monkeypatch.setattr(routes, "service_ensure", ensure)
    monkeypatch.setattr(routes, "service_release", release)
    cluster = SimpleNamespace(nodes_for=lambda svc: [SimpleNamespace(name=n) for n in order],
                              get_agent=lambda n: agents.get(n))
    with TestClient(create_app(LodestarConfig())) as c:
        c.app.state.cluster = cluster
        cat = c.get("/api/v1/comfy/catalog").json()
        k = cat["files"]["checkpoints/krea2TurboOfficialComfy_krea2TurboBf16.safetensors"]
        assert k["nodes"] == ["node-d", "node-c"] and k["drawable_on"] == ["node-d"]
        assert cat["files"]["checkpoints/autismmixSDXL_autismmixPony.safetensors"]["drawable_on"] == order

        assert c.post("/api/v1/image", json={"prompt": "a wren"}).json()["node"] == "node-d", "Krea 2: the Spark"
        order.reverse()                                       # the Xavier is preferred now
        assert c.post("/api/v1/image", json={"prompt": "a wren"}).json()["node"] == "node-d", \
            "still the Spark: the Xavier keeps Krea 2's files but cannot draw it"
        assert c.post("/api/v1/image", json={"prompt": "a fox", "model": "autismmix"}).json()["node"] == "node-c"
        xavier.up = False
        out = c.post("/api/v1/image", json={"prompt": "a fox", "model": "autismmix"}).json()
        assert out["node"] == "node-d" and out["skipped"] == ["node-c: asleep"], "one asleep: the next draws"
        plan = c.post("/api/v1/image/plan", json={"prompt": "a fox", "model": "autismmix"}).json()
        assert plan["node"] == "node-c" and plan["also"] == ["node-d"]
        only = c.post("/api/v1/image/plan", json={"prompt": "a wren", "node": "node-c"})
        assert only.status_code == 400 and "does not draw krea2" in only.json()["nodes"][0]
        comp = c.get("/api/v1/comfy/compatible", params={"model": "krea2TurboOfficialComfy"}).json()
        assert comp["drawable_on"] == ["node-d"] and comp["node"] == "node-d"
