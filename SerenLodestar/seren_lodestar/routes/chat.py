"""
Chat routes — /api/v1/chat/* inference proxy with MCP tool-call loop.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse 

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
    return {
        "model": model_override if model_override else "seren",
        "messages": messages,
        "max_tokens": req.get("max_tokens", DEFAULT_MAX_TOKENS) or DEFAULT_MAX_TOKENS,
        "temperature": req.get("temperature"),
        "repeat_penalty": req.get("repeat_penalty"),
        "stream": stream,
        "stop": ["</tool_call>"],
    }


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

    # 1. Where does llama live?
    routed = await cluster.get_service_url_async(LLAMA_CAPABILITY)
    if routed is None or routed.base_url is None:
        return JSONResponse({
            "error": "no online node is serving llama",
            "hint": "start the llama service on a node, then retry",
        }, status_code=503)

    # 2. Build message list
    messages = _build_messages(body)

    # 3. Tool injection
    from ..tooling import McpToolClient
    mcp = McpToolClient(request.app.state.http_client_factory)
    tools = await mcp.list_tools_async()
    dialect = request.app.state.dialect
    tools_block = dialect.format_tools_for_system_prompt(tools)
    if tools_block:
        if messages and messages[0]["role"] == "system":
            messages[0] = {
                "role": "system",
                "content": tools_block + "\n\n" + messages[0]["content"],
            }
        else:
            messages.insert(0, {"role": "system", "content": tools_block})

    # 4. The tool loop
    llm_client = request.app.state.http_client_factory("llama-upstream")
    url = routed.base_url.rstrip("/") + LLAMA_CHAT_PATH
    tool_rounds = 0
    final_text = ""
    final_model = None
    last_usage = None

    try:
        while True:
            upstream = _build_upstream_request(body, messages, stream=False)
            upstream = {k: v for k, v in upstream.items() if v is not None}
            resp = await llm_client.post(url, json=upstream, headers={"Content-Type": "application/json"})
            if not resp.is_success:
                body_text = resp.text[:500]
                return JSONResponse({
                    "error": f"llama-server returned HTTP {resp.status_code}",
                    "node": routed.node_name,
                    "detail": body_text,
                }, status_code=502)
            completion = resp.json()
            choices = completion.get("choices", [])
            first_choice = choices[0] if choices else {}
            message = first_choice.get("message", {})
            text = message.get("content", "")
            final_model = completion.get("model") or final_model
            last_usage = completion.get("usage")
            if not dialect.contains_tool_call(text):
                final_text = text
                break
            if tool_rounds >= MAX_TOOL_ROUNDS:
                preamble = dialect.extract_preamble(text)
                final_text = (preamble + "\n\n(I ran out of tool-call attempts finishing that.)"
                             ) if preamble else (
                    "(I got stuck calling tools and couldn't finish that - try rephrasing?)"
                )
                break
            calls = dialect.parse_tool_calls(text)
            messages.append({"role": "assistant", "content": text})
            for call in calls:
                result_json = await mcp.call_tool_async(call.name, call.arguments)
                role, content = dialect.format_tool_result(call.name, result_json)
                messages.append({"role": role, "content": content})
            _enforce_token_budget(messages)
            tool_rounds += 1

        return JSONResponse({
            "response": final_text,
            "model": final_model or "seren",
            "node": routed.node_name,
            "tool_rounds": tool_rounds,
            "usage": last_usage,
        })
    except httpx.TimeoutException:
        cluster.mark_node_offline(routed.node_name, "llama-server chat call timed out")
        return JSONResponse({
            "error": "llama-server timed out",
            "node": routed.node_name,
            "hint": "the model may be loading or generating a long response. Retry.",
        }, status_code=504)
    except httpx.RequestError as ex:
        cluster.mark_node_offline(routed.node_name, f"llama-server unreachable: {ex}")
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

    cluster: JetsonClusterClient = request.app.state.cluster
    from ..tooling import McpToolClient
    mcp_diag = McpToolClient(request.app.state.http_client_factory)
    tools = await mcp_diag.list_tools_async()
    dialect = request.app.state.dialect
    tools_block = dialect.format_tools_for_system_prompt(tools)
    messages = _build_messages(body)
    if tools_block:
        if messages and messages[0]["role"] == "system":
            messages[0] = {
                "role": "system",
                "content": tools_block + "\n\n" + messages[0]["content"],
            }
        else:
            messages.insert(0, {"role": "system", "content": tools_block})
    return {
        "dialect": dialect.name,
        "tools_count": len(tools),
        "tool_names": [t.name for t in tools],
        "has_tools_block": bool(tools_block),
        "tools_block_chars": len(tools_block) if tools_block else 0,
        "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
    }


@router.post(f"/api/{API_VERSION}/chat/stream")
async def chat_stream(request: Request):
    import httpx
    import asyncio

    try:
        body = await request.json()
    except Exception as ex:
        return JSONResponse({"error": f"malformed body: {ex}"}, status_code=400)
    if not body or not body.get("prompt"):
        return JSONResponse({"error": "prompt is required"}, status_code=400)

    cluster: JetsonClusterClient = request.app.state.cluster

    async def stream_generator():
        from ..tooling import McpToolClient
        _record_chat_activity()
        routed = await cluster.get_service_url_async(LLAMA_CAPABILITY)
        if routed is None or routed.base_url is None:
            yield json.dumps({"type": "error", "error": "no online node is serving llama"}) + "\n"
            return
        messages = _build_messages(body)
        mcp = McpToolClient(request.app.state.http_client_factory)
        tools = await mcp.list_tools_async()
        dialect = request.app.state.dialect
        tools_block = dialect.format_tools_for_system_prompt(tools)
        if tools_block:
            if messages and messages[0]["role"] == "system":
                messages[0] = {
                    "role": "system",
                    "content": tools_block + "\n\n" + messages[0]["content"],
                }
            else:
                messages.insert(0, {"role": "system", "content": tools_block})
        url = routed.base_url.rstrip("/") + LLAMA_CHAT_PATH
        stream_tool_rounds = 0
        stream_final_model = None
        llm_client = request.app.state.http_client_factory("llama-upstream")
        try:
            while stream_tool_rounds < MAX_TOOL_ROUNDS:
                probe = _build_upstream_request(body, messages, stream=False)
                probe = {k: v for k, v in probe.items() if v is not None}
                probe_resp = await llm_client.post(url, json=probe,
                                                   headers={"Content-Type": "application/json"})
                if not probe_resp.is_success:
                    err_body = probe_resp.text[:300]
                    yield json.dumps({
                        "type": "error",
                        "error": f"llama-server returned HTTP {probe_resp.status_code}: {err_body}",
                    }) + "\n"
                    return
                probe_completion = probe_resp.json()
                probe_choices = probe_completion.get("choices", [])
                probe_first = probe_choices[0] if probe_choices else {}
                probe_msg = probe_first.get("message", {})
                probe_text = probe_msg.get("content", "")
                if probe_completion.get("model"):
                    stream_final_model = probe_completion["model"]
                if not dialect.contains_tool_call(probe_text):
                    break
                calls = dialect.parse_tool_calls(probe_text)
                messages.append({"role": "assistant", "content": probe_text})
                for call in calls:
                    yield json.dumps({"type": "tool_status", "tool": call.name}) + "\n"
                    result_json = await mcp.call_tool_async(call.name, call.arguments)
                    role, content = dialect.format_tool_result(call.name, result_json)
                    messages.append({"role": role, "content": content})
                _enforce_token_budget(messages)
                stream_tool_rounds += 1
        except httpx.RequestError as ex:
            cluster.mark_node_offline(routed.node_name, f"llama-server unreachable: {ex}")
            yield json.dumps({"type": "error", "error": f"llama-server unreachable: {ex}"}) + "\n"
            return

        upstream = _build_upstream_request(body, messages, stream=True)
        upstream = {k: v for k, v in upstream.items() if v is not None}
        upstream["stream"] = True
        try:
            async with llm_client.stream("POST", url, json=upstream,
                                         headers={"Content-Type": "application/json"}) as resp:
                if not resp.is_success:
                    err_body = await resp.aread()
                    err_text = (err_body[:300].decode() if isinstance(err_body, bytes)
                                else str(err_body)[:300])
                    yield json.dumps({
                        "type": "error",
                        "error": f"llama-server returned HTTP {resp.status_code}: {err_text}",
                    }) + "\n"
                    return
                full_text_parts: list[str] = []
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if stream_final_model is None and "model" in chunk:
                        stream_final_model = chunk["model"]
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        full_text_parts.append(content)
                        yield json.dumps({"type": "chunk", "content": content}) + "\n"
                yield json.dumps({
                    "type": "done",
                    "response": "".join(full_text_parts),
                    "model": stream_final_model or body.get("model_override", "seren"),
                    "tool_rounds": stream_tool_rounds,
                }) + "\n"
        except httpx.TimeoutException:
            cluster.mark_node_offline(routed.node_name, "llama-server stream timed out")
            yield json.dumps({"type": "error", "error": "llama-server timed out"}) + "\n"
        except httpx.RequestError as ex:
            cluster.mark_node_offline(routed.node_name, f"llama-server unreachable: {ex}")
            yield json.dumps({"type": "error", "error": f"llama-server unreachable: {ex}"}) + "\n"

    return StreamingResponse(stream_generator(), 
                             media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", 
                                      "X-Accel-Buffering": "no"}
                             )
