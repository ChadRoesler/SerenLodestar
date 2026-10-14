"""
seren_lodestar.comfy_templates
════════════════════════════════════════════════════════════════════════

An image request -> a ComfyUI workflow, using the node's catalog (what each
model file IS - seren_observatory.comfy_catalog) to pick the pieces.

A request names what it wants in words a person (or Wren) would use - a
prompt, maybe a model, some LoRAs with weights, a size - and never a graph.
Each model family (arch) has one template: the loaders, text encoder, VAE,
sampler and settings that family needs. A LoRA is checked against the model
first: a different arch is refused (it would not load), a different base
within the arch is allowed and said ("may work").

Templates so far, each proven on a real node:
    krea2   Krea 2 (Turbo): Qwen3-VL text encoder, Wan 2.1 VAE, 8 steps, cfg 1
    sdxl    SDXL and its bases - Pony and Illustrious get their quality tags
    sd15    SD 1.5
Video (wan) and the rest are catalogued but not drawn with yet.

Three things carried over from the old SerenTools image path (its
ImagePromptHelper and checkpoint profiles), which was built to take ComfyUI's
barrier away - a person says what they want, the machine says it the way the
model needs:

    STYLES     how each family wants to be prompted - booru tags for Pony /
               Illustrious / NoobAI, sentences for the T5/LLM-encoder ones,
               short for SD 1.5. Whoever writes the prompt (Wren, or a local
               model translating a request) is told which, in every plan.
    negatives  the anatomy negative, and a "single subject" one when one
               person is asked for and nobody else is mentioned.
    profiles   what someone LIKES with a model (the node keeps them beside
               its catalog): LoRAs and weights worn when a request names
               none, a negative added to every picture, a VAE in place of
               the model's own.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any, Optional

MODEL_ROLES = ("checkpoint", "diffusion")
MAX_SIDE = 2048
MAX_COUNT = 4

# What each base wants said, before the prompt and in the negative. Pony was
# trained to read its score tags; Illustrious its quality words.
BASE_WORDS = {
    "Pony": ("score_9, score_8_up, score_7_up", "score_4, score_5, score_6, lowres, bad anatomy, bad hands, jpeg artifacts"),
    "Illustrious": ("masterpiece, best quality, very aesthetic", "lowres, worst quality, low quality, bad anatomy, bad hands, jpeg artifacts"),
    "NoobAI": ("masterpiece, best quality, very aesthetic", "lowres, worst quality, low quality, bad anatomy, bad hands"),
}
PLAIN_NEGATIVE = "lowres, worst quality, low quality, bad anatomy, bad hands, jpeg artifacts, watermark"
# SerenTools' BuildNegativePrompt, carried over.
ANATOMY_NEGATIVE = ("low quality, blurry, jpeg artifacts, overprocessed skin, bad anatomy, deformed anatomy, "
                    "malformed hands, bad hands, extra fingers, missing fingers, fused fingers, extra limbs, "
                    "missing limbs, duplicate body, duplicate person, twin body, conjoined, cloned face, "
                    "asymmetrical eyes, disfigured face, broken neck, twisted torso, bad proportions, mutation, "
                    "watermark, logo, text")
SINGLE_NEGATIVE = ("two people, multiple people, group shot, duo, pair, twins, second person, extra person, "
                   "background person, duplicate subject, mirrored person")
_PERSON = re.compile(r"\b(person|woman|man|girl|boy|portrait|face|lady|guy)\b", re.I)
_MANY = re.compile(r"\b(two|three|couple|group|multiple|friends|people|duo|pair|crowd|they|together)\b", re.I)

# SerenTools' GetModelTypeTranslationHint, by family - with Krea 2 among the
# sentence readers (its text encoder is a Qwen3-VL language model).
STYLES = {
    "booru": ("Booru tags (Pony / Illustrious / NoobAI): comma-separated danbooru-style tags, no sentences. "
              "Quality tags first (score_9, score_8_up, score_7_up for Pony; masterpiece, best quality for "
              "Illustrious - added for you), then subject (1girl, solo, long hair, blue eyes...), then scene and "
              "composition, then style."),
    "sdxl": ("SDXL: descriptive phrases mixed with comma-separated tags - subject first, then composition, "
             "lighting and style; 40-80 tokens."),
    "prose": ("Sentences: describe the scene, subject, light, mood, framing and style in 2-4 full sentences of "
              "plain language. No tag lists, no 'masterpiece' or score tags - this model reads prose."),
    "sd15": ("SD 1.5: short comma-separated phrases, 30-50 tokens - it cuts everything past 77 tokens. The most "
             "important things first."),
    "mixed": ("Tags or sentences both work: a short description, then tags for the details that matter."),
}
_STYLE_OF_ARCH = {"krea2": "prose", "flux": "prose", "zimage": "prose", "qwen-image": "prose", "wan": "prose",
                  "sdxl": "sdxl", "sd15": "sd15", "cosmos": "mixed"}


def prompt_style(model: dict[str, Any]) -> dict[str, str]:
    """How a model wants to be prompted: {style, guide}."""
    arch, base = model.get("arch") or "", model.get("base") or ""
    key = "booru" if arch == "sdxl" and base in BASE_WORDS else _STYLE_OF_ARCH.get(arch, "mixed")
    return {"style": key, "guide": STYLES[key]}


def single_subject(prompt: str) -> bool:
    """One person asked for, and nobody else mentioned."""
    return bool(_PERSON.search(prompt or "")) and not _MANY.search(prompt or "")


def negative_for(model: dict[str, Any], prompt: str, *extra: str) -> str:
    """The base's own words, the anatomy negative, the single-subject one
    when it applies, then the profile's and the request's - each phrase once."""
    base_neg = BASE_WORDS.get(model.get("base") or "", ("", ""))[1]
    parts = [base_neg, ANATOMY_NEGATIVE, SINGLE_NEGATIVE if single_subject(prompt) else "", *extra]
    seen, out = set(), []
    for chunk in parts:
        for phrase in (chunk or "").split(","):
            ph = phrase.strip()
            if ph and ph.lower() not in seen:
                seen.add(ph.lower())
                out.append(ph)
    return ", ".join(out)

SETTINGS = {
    "krea2": {"steps": 8, "cfg": 1.0, "sampler": "euler", "scheduler": "simple", "size": (1024, 1024)},
    "sdxl": {"steps": 28, "cfg": 6.0, "sampler": "dpmpp_2m", "scheduler": "karras", "size": (1024, 1024)},
    "sd15": {"steps": 25, "cfg": 7.0, "sampler": "dpmpp_2m", "scheduler": "karras", "size": (512, 768)},
}
TEMPLATES = tuple(SETTINGS)


class Refused(ValueError):
    """A request that cannot be drawn as asked; the message says why."""


@dataclass
class Plan:
    """What will be drawn, and how: the choices and the graph."""
    arch: str
    model: dict[str, Any]
    loras: list[dict[str, Any]]
    prompt: str
    negative: str
    width: int
    height: int
    count: int
    seed: int
    settings: dict[str, Any]
    workflow: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    vae: str = ""                       # a profile's VAE, in place of the model's own

    def describe(self) -> dict[str, Any]:
        return {"arch": self.arch, "model": self.model["key"], "base": self.model.get("base"),
                "loras": [{"name": l["name"], "weight": l["weight"], "fit": l["fit"]} for l in self.loras],
                "prompt": self.prompt, "negative": self.negative, "width": self.width, "height": self.height,
                "count": self.count, "seed": self.seed, "settings": self.settings, "notes": self.notes,
                "style": prompt_style(self.model), **({"vae": self.vae} if self.vae else {})}


def profile_of(catalog: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    """The model's profile, by catalog key or by file name."""
    profiles = catalog.get("profiles") or {}
    return profiles.get(model["key"]) or profiles.get(model.get("name", "")) or {}


# ── finding things in the catalog ────────────────────────────────────────

def files_of(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {k: {**v, "key": k} for k, v in (catalog.get("files") or {}).items() if not v.get("error")}


def find(files: dict[str, dict[str, Any]], name: str, roles: tuple[str, ...]) -> Optional[dict[str, Any]]:
    """By catalog key, by file name, or by a name it starts with."""
    for f in files.values():
        if f.get("role") in roles and name in (f["key"], f.get("name")):
            return f
    low = name.lower()
    hits = [f for f in files.values() if f.get("role") in roles and f.get("name", "").lower().startswith(low)]
    return hits[0] if len(hits) == 1 else None


def default_model(files: dict[str, dict[str, Any]], arch: str) -> Optional[dict[str, Any]]:
    """The arch's model when none is named: a checkpoint before a bare
    diffusion model (it needs fewer companions), a Turbo before the rest."""
    cands = [f for f in files.values() if f.get("arch") == arch and f.get("role") in MODEL_ROLES]
    cands.sort(key=lambda f: (f["role"] != "checkpoint", "turbo" not in f.get("name", "").lower(), f.get("name", "")))
    return cands[0] if cands else None


def _companion(files: dict[str, dict[str, Any]], role: str, test, prefer: str = "") -> Optional[dict[str, Any]]:
    cands = [f for f in files.values() if f.get("role") == role and test(f)]
    cands.sort(key=lambda f: (prefer not in f.get("name", "").lower(), f.get("name", "")))
    return cands[0] if cands else None


# ── a request -> a plan ──────────────────────────────────────────────────

def _int(v: Any, what: str, lo: int, hi: int, default: int) -> int:
    if v is None or v == "":
        return default
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise Refused(f"{what} is a whole number") from None
    if not lo <= n <= hi:
        raise Refused(f"{what} is between {lo} and {hi}")
    return n


def plan(catalog: dict[str, Any], req: dict[str, Any]) -> Plan:
    """The request {prompt, negative?, model?, arch?, loras?: [{name, weight}],
    width?, height?, count?, seed?, steps?, cfg?, use_triggers?} against the
    catalog -> what will be drawn. Raises Refused with the reason."""
    files = files_of(catalog)
    prompt = str(req.get("prompt") or "").strip()
    if not prompt:
        raise Refused("a picture needs a prompt")
    if req.get("model"):
        model = find(files, str(req["model"]), MODEL_ROLES)
        if model is None:
            raise Refused(f"no model called {req['model']!r} on the node")
    else:
        arch = str(req.get("arch") or "krea2")
        model = default_model(files, arch)
        if model is None:
            raise Refused(f"no {arch} model on the node")
    arch = model.get("arch") or ""
    if arch not in SETTINGS:
        raise Refused(f"{model['name']} is {arch or 'a model the catalog could not place'}; "
                      f"pictures are drawn with {', '.join(TEMPLATES)} so far")
    notes: list[str] = []
    profile = profile_of(catalog, model)
    wanted = req.get("loras")
    if wanted is None and profile.get("loras"):
        wanted = profile["loras"]
        notes.append("the model's profile chose the LoRAs")
    loras = []
    for want in wanted or []:
        name = want.get("name") if isinstance(want, dict) else want
        lora = find(files, str(name), ("lora",)) if name else None
        if lora is None:
            raise Refused(f"no LoRA called {name!r} on the node")
        if lora.get("arch") != arch:
            raise Refused(f"{lora['name']} is a {lora.get('arch') or 'unplaced'} LoRA; it will not load on "
                          f"{model['name']} ({arch})")
        fit = "good" if not (model.get("base") and lora.get("base")) or model["base"] == lora["base"] else "may work"
        if fit != "good":
            notes.append(f"{lora['name']} was made for {lora['base']}, the model is {model['base']} - it may work")
        try:
            weight = float(want.get("weight", 1.0)) if isinstance(want, dict) else 1.0
        except (TypeError, ValueError):
            raise Refused(f"the weight for {lora['name']} is a number") from None
        loras.append({"name": lora["name"], "key": lora["key"], "weight": weight, "fit": fit,
                      "triggers": lora.get("triggers") or lora.get("tags") or []})
    s = dict(SETTINGS[arch])
    w0, h0 = s.pop("size")
    width = _int(req.get("width"), "width", 256, MAX_SIDE, w0) // 8 * 8
    height = _int(req.get("height"), "height", 256, MAX_SIDE, h0) // 8 * 8
    count = _int(req.get("count"), "count", 1, MAX_COUNT, 1)
    seed = _int(req.get("seed"), "seed", 0, 2 ** 48, random.randrange(1, 2 ** 48))
    s["steps"] = _int(req.get("steps"), "steps", 1, 150, s["steps"])
    if req.get("cfg") is not None:
        try:
            s["cfg"] = float(req["cfg"])
        except (TypeError, ValueError):
            raise Refused("cfg is a number") from None

    # The words: a LoRA's first trigger, when the prompt does not say it yet,
    # then the base's quality tags - each only once.
    lead = []
    if req.get("use_triggers", True):
        for l in loras:
            t = next((x for x in l["triggers"] if isinstance(x, str) and x.strip()), "")
            t = t.strip().rstrip(",")
            if t and t.lower() not in prompt.lower():
                lead.append(t)
    base_pos = BASE_WORDS.get(model.get("base") or "", ("", ""))[0]
    if arch in ("sdxl", "sd15") and base_pos and base_pos.split(",")[0] not in prompt:
        lead.insert(0, base_pos)
    full = ", ".join([*lead, prompt]) if lead else prompt
    # Krea 2 runs at cfg 1 with a zeroed negative: there is nothing to say no with.
    negative = "" if arch == "krea2" else negative_for(model, prompt, profile.get("negative", ""),
                                                       str(req.get("negative") or ""))
    vae = str(req.get("vae") or profile.get("vae") or "")
    if vae and find(files, vae, ("vae",)) is None:
        raise Refused(f"no VAE called {vae!r} on the node")
    p = Plan(arch, model, loras, full, negative, width, height, count, seed, s, notes=notes, vae=vae)
    p.workflow = BUILDERS[arch](files, p, str(req.get("save_as") or "seren"))
    return p


# ── the templates ────────────────────────────────────────────────────────

def _prefix(save_as: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", save_as)[:40] or "seren"


def _krea2(files: dict[str, dict[str, Any]], p: Plan, save_as: str) -> dict[str, Any]:
    """Krea 2: the diffusion model alone, the Qwen3-VL text encoder (type
    krea2), the Wan 2.1 VAE; LoRAs on the model only; no negative (cfg 1)."""
    enc = _companion(files, "text_encoder", lambda f: f.get("arch") == "qwen3-vl")
    vae = (find(files, p.vae, ("vae",)) if p.vae else None) or \
        _companion(files, "vae", lambda f: "krea2" in (f.get("decodes") or []), prefer="wan_2.1")
    if enc is None:
        raise Refused("Krea 2 needs its Qwen3-VL text encoder in text_encoders/, and the node has none")
    if vae is None:
        raise Refused("Krea 2 needs the Wan 2.1 VAE in vae/, and the node has none")
    m = p.model
    g: dict[str, Any] = {
        "1": ({"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": m["name"]}} if m["role"] == "checkpoint"
              else {"class_type": "UNETLoader", "inputs": {"unet_name": m["name"], "weight_dtype": "default"}}),
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": enc["name"], "type": "krea2", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae["name"]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": p.prompt, "clip": ["2", 0]}},
        "6": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["5", 0]}},
        "7": {"class_type": "EmptyLatentImage", "inputs": {"width": p.width, "height": p.height, "batch_size": p.count}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": f"{_prefix(save_as)}/{p.seed}"}},
    }
    model = ["1", 0]
    for i, l in enumerate(p.loras):
        g[f"L{i}"] = {"class_type": "LoraLoaderModelOnly",
                      "inputs": {"model": model, "lora_name": l["name"], "strength_model": l["weight"]}}
        model = [f"L{i}", 0]
    g["8"] = _sampler(model, ["5", 0], ["6", 0], p)
    return g


def _sd(files: dict[str, dict[str, Any]], p: Plan, save_as: str) -> dict[str, Any]:
    """SDXL and SD 1.5: one checkpoint holds the model, its text encoders
    and its VAE; LoRAs on model and text encoder both."""
    if p.model["role"] != "checkpoint":
        raise Refused(f"{p.model['name']} is a bare diffusion model; {p.arch} is drawn from a checkpoint")
    g: dict[str, Any] = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": p.model["name"]}},
        "7": {"class_type": "EmptyLatentImage", "inputs": {"width": p.width, "height": p.height, "batch_size": p.count}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["1", 2]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": f"{_prefix(save_as)}/{p.seed}"}},
    }
    if p.vae:
        g["3"] = {"class_type": "VAELoader", "inputs": {"vae_name": find(files, p.vae, ("vae",))["name"]}}
        g["9"]["inputs"]["vae"] = ["3", 0]
    model, clip = ["1", 0], ["1", 1]
    for i, l in enumerate(p.loras):
        g[f"L{i}"] = {"class_type": "LoraLoader", "inputs": {"model": model, "clip": clip, "lora_name": l["name"],
                                                             "strength_model": l["weight"], "strength_clip": l["weight"]}}
        model, clip = [f"L{i}", 0], [f"L{i}", 1]
    g["5"] = {"class_type": "CLIPTextEncode", "inputs": {"text": p.prompt, "clip": clip}}
    g["6"] = {"class_type": "CLIPTextEncode", "inputs": {"text": p.negative, "clip": clip}}
    g["8"] = _sampler(model, ["5", 0], ["6", 0], p)
    return g


def _sampler(model, positive, negative, p: Plan) -> dict[str, Any]:
    s = p.settings
    return {"class_type": "KSampler", "inputs": {
        "model": model, "seed": p.seed, "steps": s["steps"], "cfg": s["cfg"], "sampler_name": s["sampler"],
        "scheduler": s["scheduler"], "positive": positive, "negative": negative, "latent_image": ["7", 0], "denoise": 1.0}}


BUILDERS = {"krea2": _krea2, "sdxl": _sd, "sd15": _sd}


def compatible(catalog: dict[str, Any], model_name: str) -> dict[str, Any]:
    """What can go with a model: its LoRAs (same arch; another base marked
    "may work") - the list a request form offers."""
    files = files_of(catalog)
    model = find(files, model_name, MODEL_ROLES)
    if model is None:
        raise Refused(f"no model called {model_name!r} on the node")
    out = []
    for f in files.values():
        if f.get("role") != "lora" or f.get("arch") != model.get("arch"):
            continue
        same = not (model.get("base") and f.get("base")) or f["base"] == model["base"]
        out.append({"name": f["name"], "base": f.get("base"), "fit": "good" if same else "may work",
                    "triggers": f.get("triggers") or f.get("tags") or [], "title": (f.get("civitai") or {}).get("name")})
    out.sort(key=lambda f: (f["fit"] != "good", f["name"].lower()))
    return {"model": model["key"], "arch": model.get("arch"), "base": model.get("base"),
            "drawable": model.get("arch") in SETTINGS, "style": prompt_style(model),
            "profile": profile_of(catalog, model), "loras": out}
