"""
The Qwen/Hermes tool dialect on its own: what the parser makes of model
output, shape by shape. test_tool_loop drives the whole seam (dialect ->
tool client -> FastMCP -> dialect -> model) with one well-formed call; this
file is the parser's own exam, with the malformed output a small model
really produces: a missing close tag, braces inside a string argument, junk
after the open tag, a call with no name, broken JSON next to a good call.
"""
from __future__ import annotations

import json

from seren_lodestar.tooling.i_tool_dialect import IToolDialect, McpToolDefinition, ParsedToolCall
from seren_lodestar.tooling.qwen_hermes_dialect import QwenHermesDialect


D = QwenHermesDialect()


def _call(name: str, args: dict, close: bool = True) -> str:
    body = json.dumps({"name": name, "arguments": args})
    return f"<tool_call>\n{body}\n</tool_call>" if close else f"<tool_call>\n{body}"


def test_it_is_a_dialect_and_knows_its_name():
    assert isinstance(D, IToolDialect)
    assert D.name == "qwen-hermes"


def test_contains_tool_call_is_the_open_tag():
    assert D.contains_tool_call("sure, " + _call("ping", {}))
    assert D.contains_tool_call("<tool_call>")            # lenient: an open tag alone counts
    assert not D.contains_tool_call("I would call a tool here but I won't.")
    assert not D.contains_tool_call("</tool_call> on its own is not a call")


def test_one_call_with_arguments():
    calls = D.parse_tool_calls("Let me check.\n" + _call("get_weather", {"city": "Duluth", "days": 3}))
    assert calls == [ParsedToolCall(name="get_weather", arguments={"city": "Duluth", "days": 3})]


def test_several_calls_in_order():
    out = _call("a", {"n": 1}) + "\nthen\n" + _call("b", {"n": 2}) + _call("c", {})
    assert [(c.name, c.arguments) for c in D.parse_tool_calls(out)] == [("a", {"n": 1}), ("b", {"n": 2}), ("c", {})]


def test_missing_close_tag_still_parses():
    out = "thinking... " + _call("ping", {"x": 1}, close=False) + "\nand I'm done"
    assert D.parse_tool_calls(out) == [ParsedToolCall("ping", {"x": 1})]


def test_braces_and_quotes_inside_string_arguments_do_not_end_the_call():
    args = {"code": 'if x: print("}")  # } here too', "nested": {"deep": {"er": [1, 2, {"z": "}"}]}}}
    calls = D.parse_tool_calls(_call("run", args))
    assert len(calls) == 1
    assert calls[0].arguments == args


def test_escaped_quotes_inside_strings():
    args = {"text": 'he said \\"hi\\" and left'}
    raw = '<tool_call>{"name": "say", "arguments": {"text": "he said \\"hi\\" and left"}}</tool_call>'
    calls = D.parse_tool_calls(raw)
    assert calls == [ParsedToolCall("say", {"text": 'he said "hi" and left'})]
    assert args  # the dict above documents what the model wrote; the parser unescapes it


def test_open_tag_without_json_is_skipped_and_the_next_call_still_parses():
    out = "<tool_call> not json at all </tool_call>" + _call("real", {"ok": True})
    assert D.parse_tool_calls(out) == [ParsedToolCall("real", {"ok": True})]


def test_json_that_is_not_an_object_or_has_no_name_is_skipped():
    out = ("<tool_call>[1, 2, 3]</tool_call>"           # not a dict: the find lands on '[' and skips
           '<tool_call>{"arguments": {"a": 1}}</tool_call>'     # no name
           '<tool_call>{"name": "", "arguments": {}}</tool_call>'   # empty name
           '<tool_call>{"name": 7, "arguments": {}}</tool_call>'    # name not a string
           + _call("keeper", {"k": 1}))
    assert D.parse_tool_calls(out) == [ParsedToolCall("keeper", {"k": 1})]


def test_broken_json_is_skipped_and_parsing_continues():
    out = '<tool_call>{"name": "bad", "arguments": {oops}}</tool_call>' + _call("good", {})
    assert D.parse_tool_calls(out) == [ParsedToolCall("good", {})]


def test_unterminated_json_ends_the_scan_with_what_was_found():
    out = _call("first", {}) + '<tool_call>{"name": "never", "arguments": {"a": '
    assert D.parse_tool_calls(out) == [ParsedToolCall("first", {})]


def test_arguments_default_to_an_empty_object():
    assert D.parse_tool_calls('<tool_call>{"name": "ping"}</tool_call>') == [ParsedToolCall("ping", {})]


def test_no_call_means_no_calls():
    assert D.parse_tool_calls("just words") == []
    assert D.parse_tool_calls("") == []


def test_preamble_is_the_text_before_the_first_call():
    assert D.extract_preamble("Let me look that up.  \n\n" + _call("look", {})) == "Let me look that up."
    assert D.extract_preamble("no call here") == "no call here"
    assert D.extract_preamble(_call("look", {})) == ""


def test_tool_result_is_a_user_turn_in_tool_response_tags():
    role, content = D.format_tool_result("look", '{"found": true}')
    assert role == "user"
    assert content == '<tool_response>\n{"found": true}\n</tool_response>'


def test_system_prompt_block_lists_every_tool_and_the_call_shape():
    tools = [
        McpToolDefinition(name="get_weather", description="the weather", input_schema={"type": "object", "properties": {"city": {"type": "string"}}}),
        McpToolDefinition(name="ping"),
    ]
    block = D.format_tools_for_system_prompt(tools)
    assert block is not None
    assert "<tool_call>" in block and "</tool_call>" in block        # the shape the model must emit
    for t in tools:
        assert f'"name": "{t.name}"' in block
    assert '"description": "the weather"' in block
    assert '"description": ""' in block                              # a tool with no description still lists
    assert '"city"' in block                                         # the schema rides along as parameters
    assert "DO NOT narrate" in block                                 # the no-pretend rule is in the prompt


def test_system_prompt_block_is_none_with_no_tools():
    assert D.format_tools_for_system_prompt([]) is None
