"""
Chat routes — /api/v1/chat/* inference proxy with the tool-call loop.

THE LOOP, once, for both routes. A request becomes a message list with the
tool definitions injected into the system prompt; the model answers; if the
answer contains a tool call, the tools run, their results go back in as the
dialect says they should, and the model answers again - up to MAX_TOOL_ROUNDS.
`/chat` returns the final text; `/chat/stream` streams it.

ONE INFERENCE PER ROUND, STREAMED. The old streaming route ran a full
non-streaming completion first to see whether the model wanted a tool, threw
the text away, and then ran the identical request again with stream=true -
every streamed answer cost two generations, on the one path the companion
uses most, on an 8GB Nano. Now each round IS the stream: chunks are forwarded
as they arrive, holding back only the few characters that could be the start
of a `<tool_call>` tag, and the moment the tag appears the client stops
receiving chunks and the round becomes a tool round instead.

WHAT A SLOW MODEL MEANS. A generation that takes longer than the read
timeout is a 504 and NOTHING ELSE. It used to mark the whole node offline
for thirty minutes - a Nano doing 1024 tokens at a few tokens a second
crosses two minutes easily, and one long answer took the box out of the
cluster. A connection that is REFUSED or RESET is different: that node is
marked suspect for a short backoff and re-probed on demand (see cluster.py).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..cluster import JetsonClusterClient

API_VERSION = "v1"

# ── Constants ──────────────────────────────────────────────────────────
LLAMA_CAPABILITY = "llama"
LLAMA_CHAT_PATH = "/v1/chat/completions"
DEFAULT_MAX_TOKENS = 1024
MAX_TOOL_ROUNDS = 5
CTX_BUDGET_TOKENS = int(os.environ.get("SEREN_CTX_BUDGET", "6000"))
CHARS_PER_TOKEN_ESTIMATE = 3
TOOL_RESPONSE_OPEN = "<tool_response>"
TOOL_RESPONSE_CLOSE = "</tool_response>"

router = APIRouter(tags=["chat"])

# ── Chat activity tracking ────────────────────────────────────────────
_last_user_at_unix: int = 0
_lock = threading.Lock()


def _record_chat_activity():
    global _last_user_at_unix
    with _lock:
        _last_user_at_unix = int(datetime.now(timezone.utc).timestamp())


def _read_last_user_at() -> int:
    with _lock:
        return _last_user_at_unix


# ── Helpers ──────────────────────────────────────────────────────────────

def _build_messages(req: dict) -> list[dict]:
    messages: list[dict] = []
    system_prompt = req.get("system_prompt")
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    history = req.get("history")
    if history and isinstance(history, list):
        for m in history:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            content = m.get("content")
            if not role or content is None:
                continue
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": req.get("prompt", "")})
    return messages


def _build_upstream_request(req: dict, messages: list[dict], stream: bool) -> dict:
    model_override = req.get("model_override")
    upstream = {
        "model": model_override if model_override else "seren",
        "messages": messages,
        "max_tokens": req.get("max_tokens", DEFAULT_MAX_TOKENS) or DEFAULT_MAX_TOKENS,
        "temperature": req.get("temperature"),
        "repeat_penalty": req.get("repeat_penalty"),
        "stream": stream,
        "stop": ["</tool_call>"],
    }
    return {k: v for k, v in upstream.items() if v is not None}


def _enforce_token_budget(messages: list[dict]) -> None:
    def estimate_tokens():
        total = 0
        for m in messages:
            content = m.get("content", "")
            if content:
                total += len(content) // CHARS_PER_TOKEN_ESTIMATE
        return total
    if estimate_tokens() <= CTX_BUDGET_TOKENS:
        return
    for i, m in enumerate(messages):
        if estimate_tokens() <= CTX_BUDGET_TOKENS:
            break
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if TOOL_RESPONSE_OPEN not in content:
            continue
        original_tokens = len(content) // CHARS_PER_TOKEN_ESTIMATE
        marker = (
            f"{TOOL_RESPONSE_OPEN}\n"
            f"[truncated to fit context, original was ~{original_tokens} tokens]\n"
            f"{TOOL_RESPONSE_CLOSE}"
        )
        messages[i] = {"role": "user", "content": marker}


def _inject_tools(messages: list[dict], tools_block: Optional[str]) -> None:
    if not tools_block:
        return
    if messages and messages[0]["role"] == "system":
        messages[0] = {"role": "system",
                       "content": tools_block + "\n\n" + messages[0]["content"]}
    else:
        messages.insert(0, {"role": "system", "content": tools_block})


async def _prepare(request: Request, body: dict):
    """Route to llama, build the messages, inject the tools. Shared by both routes."""
    cluster: JetsonClusterClient = request.app.state.cluster
    routed = await cluster.get_service_url_async(LLAMA_CAPABILITY)
    if routed is None or routed.base_url is None:
        return None, None, None, None
    tool_client = request.app.state.tool_client
    dialect = request.app.state.dialect
    messages = _build_messages(body)
    tools = await tool_client.list_tools_async()
    _inject_tools(messages, dialect.format_tools_for_system_prompt(tools))
    return routed, messages, tool_client, dialect


async def _run_tools(calls, tool_client, dialect, messages, on_tool=None) -> None:
    for call in calls:
        if on_tool is not None:
            await on_tool(call.name)
        result_json = await tool_client.call_tool_async(call.name, call.arguments)
        role, content = dialect.format_tool_result(call.name, result_json)
        messages.append({"role": role, "content": content})
    _enforce_token_budget(messages)


def _out_of_rounds(dialect, text: str) -> str:
    preamble = dialect.extract_preamble(text)
    if preamble:
        return preamble + "\n\n(I ran out of tool-call attempts finishing that.)"
    return "(I got stuck calling tools and couldn't finish that - try rephrasing?)"


def _suspect(cluster: JetsonClusterClient, node: str, reason: str) -> None:
    cluster.mark_node_suspect(node, reason)


# ── Endpoints ──────────────────────────────────────────────────────────

@router.post(f"/api/{API_VERSION}/chat")
async def chat_endpoint(request: Request):
    cluster: JetsonClusterClient = request.app.state.cluster
    try:
        body = await request.json()
    except Exception as ex:
        return JSONResponse({"error": f"malformed body: {ex}"}, status_code=400)
    if not body or not body.get("prompt"):
        return JSONResponse({"error": "prompt is required"}, status_code=400)

    _record_chat_activity()
    routed, messages, tool_client, dialect = await _prepare(request, body)
    if routed is None:
        return JSONResponse({
            "error": "no online node is serving llama",
            "hint": "start the llama service on a node, then retry",
        }, status_code=503)

    llm_client = request.app.state.http_client_factory("llama-upstream")
    url = routed.base_url.rstrip("/") + LLAMA_CHAT_PATH
    tool_rounds = 0
    final_text = ""
    final_model = None
    last_usage = None

    try:
        while True:
            upstream = _build_upstream_request(body, messages, stream=False)
            resp = await llm_client.post(url, json=upstream, headers={"Content-Type": "application/json"})
            if not resp.is_success:
                return JSONResponse({
                    "error": f"llama-server returned HTTP {resp.status_code}",
                    "node": routed.node_name,
                    "detail": resp.text[:500],
                }, status_code=502)
            completion = resp.json()
            choices = completion.get("choices", [])
            message = (choices[0] if choices else {}).get("message", {})
            text = message.get("content", "") or ""
            final_model = completion.get("model") or final_model
            last_usage = completion.get("usage")
            if not dialect.contains_tool_call(text):
                final_text = text
                break
            if tool_rounds >= MAX_TOOL_ROUNDS:
                final_text = _out_of_rounds(dialect, text)
                break
            calls = dialect.parse_tool_calls(text)
            if not calls:
                # The tag was there but nothing parseable was inside it -
                # the model gets its own text back rather than a silent loop.
                final_text = text
                break
            messages.append({"role": "assistant", "content": text})
            await _run_tools(calls, tool_client, dialect, messages)
            tool_rounds += 1

        return JSONResponse({
            "response": final_text,
            "model": final_model or "seren",
            "node": routed.node_name,
            "tool_rounds": tool_rounds,
            "usage": last_usage,
        })
    except httpx.TimeoutException:
        # A slow generation is not a dead node. Say what happened and leave
        # the cluster's picture of the node alone.
        return JSONResponse({
            "error": "llama-server timed out",
            "node": routed.node_name,
            "hint": "the model may be loading or generating a long response. Retry, "
                    "or raise chat.generation_timeout_seconds for this box.",
        }, status_code=504)
    except httpx.RequestError as ex:
        _suspect(cluster, routed.node_name, f"llama-server unreachable: {ex}")
        return JSONResponse({
            "error": "llama-server unreachable",
            "node": routed.node_name,
            "detail": str(ex),
        }, status_code=502)


@router.get(f"/api/{API_VERSION}/chat/health")
async def chat_health(request: Request):
    cluster: JetsonClusterClient = request.app.state.cluster
    routed = await cluster.get_service_url_async(LLAMA_CAPABILITY)
    if routed is None or routed.base_url is None:
        return {"ok": False, "inference_backend": "llama.cpp", "model": None,
                "reason": "no online node serving llama"}
    return {"ok": True, "inference_backend": "llama.cpp", "node": routed.node_name,
            "base_url": routed.base_url}


@router.get(f"/api/{API_VERSION}/chat/last_user_at")
async def last_user_at():
    ts = _read_last_user_at()
    return {"last_user_at_unix": ts if ts > 0 else None}


@router.post(f"/api/{API_VERSION}/chat/inspect")
async def chat_inspect(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "malformed body"}, status_code=400)
    tool_client = request.app.state.tool_client
    dialect = request.app.state.dialect
    tools = await tool_client.list_tools_async()
    tools_block = dialect.format_tools_for_system_prompt(tools)
    messages = _build_messages(body)
    _inject_tools(messages, tools_block)
    return {
        "dialect": dialect.name,
        "tools_count": len(tools),
        "tool_names": [t.name for t in tools],
        "has_tools_block": bool(tools_block),
        "tools_block_chars": len(tools_block) if tools_block else 0,
        "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
    }


# ── streaming ─────────────────────────────────────────────────────────

def _prefix_overlap(text: str, tag: str) -> int:
    """How many trailing characters of `text` could be the start of `tag`.

    Those characters are held back rather than sent to the client, so a
    `<tool_call>` that arrives split across two chunks never leaks its first
    half to the screen.
    """
    limit = min(len(text), len(tag) - 1)
    for k in range(limit, 0, -1):
        if tag.startswith(text[-k:]):
            return k
    return 0


async def _stream_one_round(llm_client, url: str, upstream: dict, open_tag: str):
    """Yield ("chunk", text) for client-visible text, then one ("end", state).

    `state` carries the full model text, whether a tool tag was seen, and
    the model name. Client-visible text stops at the tool tag.
    """
    full_parts: list[str] = []
    pending = ""
    tool_mode = False
    model_name: Optional[str] = None
    async with llm_client.stream("POST", url, json=upstream,
                                 headers={"Content-Type": "application/json"}) as resp:
        if not resp.is_success:
            err_body = await resp.aread()
            err_text = (err_body[:300].decode(errors="replace") if isinstance(err_body, bytes)
                        else str(err_body)[:300])
            yield "error", f"llama-server returned HTTP {resp.status_code}: {err_text}"
            return
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if model_name is None and chunk.get("model"):
                model_name = chunk["model"]
            choices = chunk.get("choices", [])
            if not choices:
                continue
            content = choices[0].get("delta", {}).get("content", "") or ""
            if not content:
                continue
            full_parts.append(content)
            if tool_mode:
                continue
            pending += content
            idx = pending.find(open_tag)
            if idx >= 0:
                if idx > 0:
                    yield "chunk", pending[:idx]
                pending = ""
                tool_mode = True
                continue
            keep = _prefix_overlap(pending, open_tag)
            emit = pending[:len(pending) - keep] if keep else pending
            if emit:
                yield "chunk", emit
            pending = pending[len(pending) - keep:] if keep else ""
    if not tool_mode and pending:
        yield "chunk", pending
    yield "end", {"text": "".join(full_parts), "tool_call": tool_mode, "model": model_name}


@router.post(f"/api/{API_VERSION}/chat/stream")
async def chat_stream(request: Request):
    try:
        body = await request.json()
    except Exception as ex:
        return JSONResponse({"error": f"malformed body: {ex}"}, status_code=400)
    if not body or not body.get("prompt"):
        return JSONResponse({"error": "prompt is required"}, status_code=400)

    cluster: JetsonClusterClient = request.app.state.cluster

    async def stream_generator() -> AsyncIterator[str]:
        _record_chat_activity()
        routed, messages, tool_client, dialect = await _prepare(request, body)
        if routed is None:
            yield json.dumps({"type": "error", "error": "no online node is serving llama"}) + "\n"
            return
        url = routed.base_url.rstrip("/") + LLAMA_CHAT_PATH
        llm_client = request.app.state.http_client_factory("llama-upstream")
        open_tag = getattr(dialect, "OPEN_TAG", "<tool_call>")
        rounds = 0
        model_name: Optional[str] = None
        emitted: list[str] = []

        try:
            while True:
                upstream = _build_upstream_request(body, messages, stream=True)
                state: dict = {}
                async for kind, payload in _stream_one_round(llm_client, url, upstream, open_tag):
                    if kind == "chunk":
                        emitted.append(payload)
                        yield json.dumps({"type": "chunk", "content": payload}) + "\n"
                    elif kind == "error":
                        yield json.dumps({"type": "error", "error": payload}) + "\n"
                        return
                    else:
                        state = payload
                model_name = state.get("model") or model_name
                text = state.get("text", "")
                if not state.get("tool_call"):
                    break
                if rounds >= MAX_TOOL_ROUNDS:
                    note = _out_of_rounds(dialect, "")
                    emitted.append(note)
                    yield json.dumps({"type": "chunk", "content": note}) + "\n"
                    break
                calls = dialect.parse_tool_calls(text)
                if not calls:
                    # A tag with nothing parseable inside: send the model's
                    # text as it was rather than loop or drop it.
                    tail = text[len("".join(emitted)):] if text.startswith("".join(emitted)) else text
                    if tail:
                        emitted.append(tail)
                        yield json.dumps({"type": "chunk", "content": tail}) + "\n"
                    break
                messages.append({"role": "assistant", "content": text})
                # Tool status lines go out BEFORE the tool runs so the client can
                # show "using scheduler_list…" while it happens.
                for call in calls:
                    yield json.dumps({"type": "tool_status", "tool": call.name}) + "\n"
                await _run_tools(calls, tool_client, dialect, messages)
                rounds += 1
                # Text emitted so far belongs to the previous round's preamble;
                # the next round starts a fresh answer.
                emitted = []
        except httpx.TimeoutException:
            yield json.dumps({"type": "error", "error": "llama-server timed out",
                              "hint": "long generation or a loading model; the node is not "
                                      "marked offline. Retry, or raise "
                                      "chat.generation_timeout_seconds."}) + "\n"
            return
        except httpx.RequestError as ex:
            _suspect(cluster, routed.node_name, f"llama-server unreachable: {ex}")
            yield json.dumps({"type": "error", "error": f"llama-server unreachable: {ex}"}) + "\n"
            return

        yield json.dumps({
            "type": "done",
            "response": "".join(emitted),
            "model": model_name or body.get("model_override", "seren"),
            "node": routed.node_name,
            "tool_rounds": rounds,
        }) + "\n"

    return StreamingResponse(stream_generator(),
                             media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
