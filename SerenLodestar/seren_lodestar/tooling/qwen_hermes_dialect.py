"""
seren_lodestar.tooling.qwen_hermes_dialect
=======================================================================

Qwen/Hermes tool-calling dialect. Mirrors the format Seren's model was
trained on (SerenCoreLibrary ToolCallParser / JinjaTemplateExporter).
Ported from SerenLodestar/Tooling/QwenHermesDialect.cs.
"""
from __future__ import annotations

import json
from typing import Optional

from .i_tool_dialect import IToolDialect, McpToolDefinition, ParsedToolCall


class QwenHermesDialect(IToolDialect):
    """
    Qwen/Hermes dialect using <tool_call>{json}</tool_call> tags.
    """

    OPEN_TAG = "<tool_call>"
    CLOSE_TAG = "</tool_call>"

    @property
    def name(self) -> str:
        return "qwen-hermes"

    def contains_tool_call(self, model_output: str) -> bool:
        """Lenient: an open tag alone is enough to suspect a tool call."""
        return self.OPEN_TAG in model_output

    def parse_tool_calls(self, model_output: str) -> list[ParsedToolCall]:
        """Extract tool calls from model output. Tolerant of missing close tags."""
        results: list[ParsedToolCall] = []
        pos = 0

        while pos < len(model_output):
            open_idx = model_output.find(self.OPEN_TAG, pos)
            if open_idx < 0:
                break

            json_start = open_idx + len(self.OPEN_TAG)
            # skip whitespace
            while json_start < len(model_output) and model_output[json_start].isspace():
                json_start += 1

            if json_start >= len(model_output) or model_output[json_start] != "{":
                pos = open_idx + len(self.OPEN_TAG)
                continue

            json_end = self._find_json_end(model_output, json_start)
            if json_end < 0:
                break

            json_text = model_output[json_start: json_end + 1]
            try:
                doc = json.loads(json_text)
                if not isinstance(doc, dict):
                    pos = json_end + 1
                    continue

                name = doc.get("name", "")
                if not isinstance(name, str) or not name:
                    pos = json_end + 1
                    continue

                args = doc.get("arguments", {})
                results.append(ParsedToolCall(name=name, arguments=args))
            except json.JSONDecodeError:
                pass

            # advance past this call
            after_json = json_end + 1
            # skip optional close tag
            ws_skip = after_json
            while ws_skip < len(model_output) and model_output[ws_skip].isspace():
                ws_skip += 1
            if (ws_skip < len(model_output)
                    and model_output[ws_skip:].startswith(self.CLOSE_TAG)):
                pos = ws_skip + len(self.CLOSE_TAG)
            else:
                pos = after_json

        return results

    @staticmethod
    def _find_json_end(s: str, open_brace_pos: int) -> int:
        """Walk forward counting braces to find matching '}'."""
        depth = 0
        in_str = False
        esc = False
        for i in range(open_brace_pos, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                    continue
                if c == "\\":
                    esc = True
                    continue
                if c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i
        return -1

    def extract_preamble(self, model_output: str) -> str:
        idx = model_output.find(self.OPEN_TAG)
        if idx < 0:
            return model_output
        return model_output[:idx].rstrip()

    def format_tool_result(self, tool_name: str, result_json: str):
        """Tool results come back as a USER turn wrapped in <tool_response> tags."""
        content = f"<tool_response>\n{result_json}\n</tool_response>"
        return ("user", content)

    def format_tools_for_system_prompt(
        self, tools: list[McpToolDefinition]
    ) -> Optional[str]:
        if not tools:
            return None

        lines: list[str] = []
        lines.append("# Tools")
        lines.append("")
        lines.append(
            "You have access to the following tools. Call one by emitting a"
        )
        lines.append("JSON object inside <tool_call></tool_call> tags:")
        lines.append("")
        lines.append("<tool_call>")
        lines.append('{"name": "tool_name", "arguments": {"arg": "value"}}')
        lines.append("</tool_call>")
        lines.append("")
        lines.append("Rules for tool use:")
        lines.append(
            "- To USE a tool, you MUST emit the <tool_call> block above. There"
        )
        lines.append("  is no other way to invoke a tool.")
        lines.append(
            "- DO NOT narrate or pretend to call tools. Phrases like"
        )
        lines.append(
            '  "*checking status...*" or "I\'ll fetch that for you" without an'
        )
        lines.append(
            "  actual <tool_call> block are LIES - the tool did NOT run."
        )
        lines.append(
            "- If you don't know something (current time, cluster state, what's"
        )
        lines.append(
            "  in memory, what model you are), CALL the relevant tool. Do not"
        )
        lines.append(
            "  guess or invent plausible-sounding values."
        )
        lines.append(
            "- After a tool returns, use its real result. Do not embellish or"
        )
        lines.append(
            '  replace tool output with what you think it "should" say.'
        )
        lines.append("")
        lines.append("Available tools:")
        for t in tools:
            spec = {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.input_schema,
            }
            lines.append(json.dumps(spec, indent=2))

        return "\n".join(lines)
