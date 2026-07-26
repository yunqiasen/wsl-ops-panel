from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class TransformResult:
    body: dict[str, Any]
    endpoint: str


class UnsupportedProtocolTransform(ValueError):
    pass


_FORMATS = {"anthropic", "openai_chat", "openai_responses", "gemini"}
_ENDPOINTS = {
    "anthropic": "/v1/messages",
    "openai_chat": "/v1/chat/completions",
    "openai_responses": "/v1/responses",
    "gemini": "/v1beta/models",
}


def transform_request(
    source_format: str, target_format: str, body: dict[str, Any]
) -> TransformResult:
    source = _format(source_format)
    target = _format(target_format)
    original = copy.deepcopy(body)
    if source == target:
        return TransformResult(original, _ENDPOINTS[target])
    if source == "anthropic" and target == "openai_chat":
        return TransformResult(_anthropic_to_chat_request(body), _ENDPOINTS[target])
    if source == "openai_chat" and target == "anthropic":
        return TransformResult(_chat_to_anthropic_request(body), _ENDPOINTS[target])
    if source == "anthropic" and target == "openai_responses":
        return TransformResult(_anthropic_to_responses_request(body), _ENDPOINTS[target])
    if source == "openai_responses" and target == "anthropic":
        return TransformResult(_responses_to_anthropic_request(body), _ENDPOINTS[target])
    if source == "openai_responses" and target == "openai_chat":
        return TransformResult(_responses_to_chat_request(body), _ENDPOINTS[target])
    if source == "openai_chat" and target == "openai_responses":
        return TransformResult(_chat_to_responses_request(body), _ENDPOINTS[target])
    raise UnsupportedProtocolTransform(f"unsupported protocol transform: {source} -> {target}")


def transform_response(
    source_format: str, target_format: str, body: dict[str, Any]
) -> dict[str, Any]:
    source = _format(source_format)
    target = _format(target_format)
    if source == target:
        return copy.deepcopy(body)
    if source == "openai_chat" and target == "anthropic":
        return _chat_to_anthropic_response(body)
    if source == "anthropic" and target == "openai_chat":
        return _anthropic_to_chat_response(body)
    if source == "openai_responses" and target == "openai_chat":
        return _responses_to_chat_response(body)
    if source == "openai_chat" and target == "openai_responses":
        return _chat_to_responses_response(body)
    if source == "anthropic" and target == "openai_responses":
        return _chat_to_responses_response(_anthropic_to_chat_response(body))
    if source == "openai_responses" and target == "anthropic":
        return _chat_to_anthropic_response(_responses_to_chat_response(body))
    raise UnsupportedProtocolTransform(f"unsupported protocol transform: {source} -> {target}")


def transform_sse(
    source_format: str, target_format: str, chunks: Iterable[bytes]
) -> list[bytes]:
    source = _format(source_format)
    target = _format(target_format)
    incoming = list(chunks)
    if source == target:
        return incoming
    if {source, target} <= {"openai_chat", "anthropic"}:
        return _transform_chat_anthropic_sse(source, target, incoming)
    if {source, target} <= {"openai_chat", "openai_responses"}:
        return _transform_chat_responses_sse(source, target, incoming)
    if {source, target} <= {"anthropic", "openai_responses"}:
        return _transform_chat_anthropic_sse(source, target, incoming)
    raise UnsupportedProtocolTransform(f"unsupported protocol transform: {source} -> {target}")


def _format(value: str) -> str:
    normalized = str(value).strip().lower().replace("-", "_")
    aliases = {
        "openai": "openai_chat",
        "chat": "openai_chat",
        "responses": "openai_responses",
        "anthropic_messages": "anthropic",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in _FORMATS:
        raise UnsupportedProtocolTransform(f"unsupported protocol format: {normalized}")
    return normalized


def _anthropic_to_chat_request(body: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "model": body.get("model"),
        "messages": _anthropic_messages_to_chat(body.get("messages") or []),
    }
    system = _text_content(body.get("system"))
    if system:
        result["messages"].insert(0, {"role": "system", "content": system})
    _copy_if_present(body, result, "temperature", "top_p", "stream")
    if "max_tokens" in body:
        result["max_tokens"] = body["max_tokens"]
    if "stop_sequences" in body:
        result["stop"] = body["stop_sequences"]
    if "tools" in body:
        result["tools"] = _anthropic_tools_to_chat(body["tools"])
    if "tool_choice" in body:
        result["tool_choice"] = _anthropic_tool_choice_to_chat(body["tool_choice"])
    return _without_none(result)


def _chat_to_anthropic_request(body: dict[str, Any]) -> dict[str, Any]:
    messages = body.get("messages") or []
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user") if isinstance(message, dict) else "user"
        if role == "system":
            text = _text_content(message.get("content")) if isinstance(message, dict) else ""
            if text:
                system_parts.append(text)
            continue
        if not isinstance(message, dict):
            continue
        converted.append(_chat_message_to_anthropic(message))
    result: dict[str, Any] = {
        "model": body.get("model"),
        "messages": converted,
        "max_tokens": body.get("max_tokens", 4096),
    }
    if system_parts:
        result["system"] = "\n\n".join(system_parts)
    _copy_if_present(body, result, "temperature", "top_p", "stream")
    if "stop" in body:
        result["stop_sequences"] = body["stop"]
    if "tools" in body:
        result["tools"] = _chat_tools_to_anthropic(body["tools"])
    if "tool_choice" in body:
        result["tool_choice"] = _chat_tool_choice_to_anthropic(body["tool_choice"])
    return _without_none(result)


def _anthropic_to_responses_request(body: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "model": body.get("model"),
        "input": _anthropic_messages_to_responses(body.get("messages") or []),
    }
    system = _text_content(body.get("system"))
    if system:
        result["instructions"] = system
    if "max_tokens" in body:
        result["max_output_tokens"] = body["max_tokens"]
    _copy_if_present(body, result, "temperature", "top_p", "stream")
    if "stop_sequences" in body:
        result["stop"] = body["stop_sequences"]
    if "tools" in body:
        result["tools"] = _anthropic_tools_to_responses(body["tools"])
    return _without_none(result)


def _responses_to_anthropic_request(body: dict[str, Any]) -> dict[str, Any]:
    result = _chat_to_anthropic_request(_responses_to_chat_request(body))
    if "instructions" in body:
        result["system"] = _text_content(body["instructions"])
    if "max_output_tokens" in body:
        result["max_tokens"] = body["max_output_tokens"]
    return _without_none(result)


def _responses_to_chat_request(body: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    input_value = body.get("input", "")
    if isinstance(input_value, str):
        messages.append({"role": "user", "content": input_value})
    elif isinstance(input_value, list):
        for item in input_value:
            if isinstance(item, dict) and item.get("type") in {"function_call_output", "function_call"}:
                messages.extend(_response_tool_item_to_chat(item))
            elif isinstance(item, dict) and "role" in item:
                messages.append(_responses_message_to_chat(item))
            else:
                text = _text_content(item)
                if text:
                    messages.append({"role": "user", "content": text})
    result: dict[str, Any] = {"model": body.get("model"), "messages": messages}
    if "instructions" in body:
        result["messages"].insert(0, {"role": "system", "content": _text_content(body["instructions"])})
    if "max_output_tokens" in body:
        result["max_tokens"] = body["max_output_tokens"]
    _copy_if_present(body, result, "temperature", "top_p", "stream")
    if "tools" in body:
        result["tools"] = _responses_tools_to_chat(body["tools"])
    if "stop" in body:
        result["stop"] = body["stop"]
    return _without_none(result)


def _chat_to_responses_request(body: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "model": body.get("model"),
        "input": _chat_messages_to_responses(body.get("messages") or []),
    }
    system_messages = [
        message
        for message in body.get("messages") or []
        if isinstance(message, dict) and message.get("role") == "system"
    ]
    if system_messages:
        result["instructions"] = "\n\n".join(
            _text_content(message.get("content")) for message in system_messages
        )
        result["input"] = [
            item
            for item in result["input"]
            if item.get("role") != "system"
        ]
    if "max_tokens" in body:
        result["max_output_tokens"] = body["max_tokens"]
    _copy_if_present(body, result, "temperature", "top_p", "stream")
    if "tools" in body:
        result["tools"] = _chat_tools_to_responses(body["tools"])
    if "stop" in body:
        result["stop"] = body["stop"]
    return _without_none(result)


def _anthropic_messages_to_chat(messages: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content")
        blocks = content if isinstance(content, list) else [content]
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in blocks:
            if isinstance(block, str):
                text_parts.append(block)
            elif isinstance(block, dict):
                kind = block.get("type")
                if kind in {"text", "input_text", "output_text"}:
                    text_parts.append(str(block.get("text") or ""))
                elif kind == "tool_use":
                    tool_calls.append(
                        {
                            "id": block.get("id"),
                            "type": "function",
                            "function": {
                                "name": block.get("name"),
                                "arguments": json.dumps(
                                    block.get("input") or {}, ensure_ascii=False
                                ),
                            },
                        }
                    )
                elif kind == "tool_result":
                    result.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id"),
                            "content": _text_content(block.get("content")),
                        }
                    )
        item: dict[str, Any] = {"role": role, "content": "\n".join(p for p in text_parts if p)}
        if tool_calls:
            item["tool_calls"] = tool_calls
            if not item["content"]:
                item["content"] = None
        result.append(item)
    return result


def _chat_message_to_anthropic(message: dict[str, Any]) -> dict[str, Any]:
    role = str(message.get("role") or "user")
    if role == "tool":
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id"),
                    "content": message.get("content") or "",
                }
            ],
        }
    content = message.get("content")
    blocks: list[dict[str, Any]] = []
    text = _text_content(content)
    if text:
        blocks.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except (TypeError, json.JSONDecodeError):
            arguments = {"raw": function.get("arguments") or ""}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id"),
                "name": function.get("name"),
                "input": arguments,
            }
        )
    return {"role": "assistant" if role == "assistant" else "user", "content": blocks or [{"type": "text", "text": ""}]}


def _chat_to_anthropic_response(body: dict[str, Any]) -> dict[str, Any]:
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content: list[dict[str, Any]] = []
    text = _text_content(message.get("content"))
    if text:
        content.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except (TypeError, json.JSONDecodeError):
            arguments = {}
        content.append(
            {
                "type": "tool_use",
                "id": call.get("id"),
                "name": function.get("name"),
                "input": arguments,
            }
        )
    finish = choice.get("finish_reason")
    stop_reason = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}.get(finish, finish)
    usage = body.get("usage") or {}
    result: dict[str, Any] = {
        "id": body.get("id"),
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": body.get("model"),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }
    return _without_none(result, keep_none={"stop_sequence"})


def _anthropic_to_chat_response(body: dict[str, Any]) -> dict[str, Any]:
    content = body.get("content") or []
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    },
                }
            )
    message: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage = body.get("usage") or {}
    return {
        "id": body.get("id"),
        "object": "chat.completion",
        "model": body.get("model"),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": {"end_turn": "stop", "max_tokens": "length", "tool_use": "tool_calls"}.get(body.get("stop_reason"), body.get("stop_reason")),
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    }


def _responses_to_chat_response(body: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            text = _text_content(item.get("content"))
            message: dict[str, Any] = {"role": item.get("role", "assistant"), "content": text}
            messages.append(message)
        elif item.get("type") == "function_call":
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": item.get("call_id") or item.get("id"),
                            "type": "function",
                            "function": {
                                "name": item.get("name"),
                                "arguments": item.get("arguments", "{}"),
                            },
                        }
                    ],
                }
            )
    if not messages and body.get("output_text") is not None:
        messages = [{"role": "assistant", "content": body.get("output_text") or ""}]
    usage = body.get("usage") or {}
    return {
        "id": body.get("id"),
        "object": "chat.completion",
        "model": body.get("model"),
        "choices": [
            {
                "index": 0,
                "message": messages[0] if messages else {"role": "assistant", "content": ""},
                "finish_reason": _responses_status_to_finish(body.get("status")),
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


def _chat_to_responses_response(body: dict[str, Any]) -> dict[str, Any]:
    choices = body.get("choices") or [{}]
    choice = choices[0]
    message = choice.get("message") or {}
    content = message.get("content")
    output_content = [{"type": "output_text", "text": _text_content(content)}]
    output: list[dict[str, Any]] = [
        {"type": "message", "role": "assistant", "content": output_content}
    ]
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        output.append(
            {
                "type": "function_call",
                "id": call.get("id"),
                "call_id": call.get("id"),
                "name": function.get("name"),
                "arguments": function.get("arguments", "{}"),
            }
        )
    usage = body.get("usage") or {}
    return {
        "id": body.get("id"),
        "object": "response",
        "model": body.get("model"),
        "status": "completed",
        "output": output,
        "output_text": _text_content(content),
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get(
                "total_tokens",
                usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0),
            ),
        },
    }


def _anthropic_messages_to_responses(messages: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in _anthropic_messages_to_chat(messages):
        result.append(_chat_message_to_response(message))
    return result


def _chat_messages_to_responses(messages: list[Any]) -> list[dict[str, Any]]:
    return [
        _chat_message_to_response(message)
        for message in messages
        if isinstance(message, dict)
    ]


def _chat_message_to_response(message: dict[str, Any]) -> dict[str, Any]:
    role = message.get("role", "user")
    content = message.get("content")
    if content is None:
        content_items: list[dict[str, Any]] = []
    elif isinstance(content, list):
        content_items = [
            {"type": "input_text", "text": _text_content(item)} for item in content
        ]
    else:
        content_items = [{"type": "input_text", "text": str(content)}]
    result: dict[str, Any] = {"role": role, "content": content_items}
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        result.setdefault("tool_calls", []).append(
            {
                "type": "function_call",
                "call_id": call.get("id"),
                "name": function.get("name"),
                "arguments": function.get("arguments", "{}"),
            }
        )
    return result


def _responses_message_to_chat(item: dict[str, Any]) -> dict[str, Any]:
    return {"role": item.get("role", "user"), "content": _text_content(item.get("content"))}


def _response_tool_item_to_chat(item: dict[str, Any]) -> list[dict[str, Any]]:
    if item.get("type") == "function_call":
        return [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": item.get("call_id") or item.get("id"),
                        "type": "function",
                        "function": {
                            "name": item.get("name"),
                            "arguments": item.get("arguments", "{}"),
                        },
                    }
                ],
            }
        ]
    return [
        {
            "role": "tool",
            "tool_call_id": item.get("call_id"),
            "content": _text_content(item.get("output")),
        }
    ]


def _anthropic_tools_to_chat(tools: Any) -> list[dict[str, Any]]:
    result = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        result.append(
            {
                "type": "function",
                "function": {
                    "name": tool.get("name"),
                    "description": tool.get("description"),
                    "parameters": tool.get("input_schema") or {},
                },
            }
        )
    return result


def _chat_tools_to_anthropic(tools: Any) -> list[dict[str, Any]]:
    result = []
    for tool in tools or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict):
            result.append(
                {
                    "name": function.get("name"),
                    "description": function.get("description"),
                    "input_schema": function.get("parameters") or {},
                }
            )
    return result


def _anthropic_tools_to_responses(tools: Any) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": tool.get("name"),
            "description": tool.get("description"),
            "parameters": tool.get("input_schema") or {},
        }
        for tool in tools or []
        if isinstance(tool, dict)
    ]


def _responses_tools_to_chat(tools: Any) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.get("name"),
                "description": tool.get("description"),
                "parameters": tool.get("parameters") or {},
            },
        }
        for tool in tools or []
        if isinstance(tool, dict)
    ]


def _chat_tools_to_responses(tools: Any) -> list[dict[str, Any]]:
    result = []
    for tool in tools or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict):
            result.append(
                {
                    "type": "function",
                    "name": function.get("name"),
                    "description": function.get("description"),
                    "parameters": function.get("parameters") or {},
                }
            )
    return result


def _anthropic_tool_choice_to_chat(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    kind = value.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "tool":
        return {"type": "function", "function": {"name": value.get("name")}}
    return value


def _chat_tool_choice_to_anthropic(value: Any) -> Any:
    if value == "auto":
        return {"type": "auto"}
    if value == "required":
        return {"type": "any"}
    if isinstance(value, dict):
        name = (value.get("function") or {}).get("name")
        if name:
            return {"type": "tool", "name": name}
    return value


def _text_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if text is not None:
                    parts.append(str(text))
        return "".join(parts)
    if isinstance(value, dict):
        if value.get("text") is not None:
            return str(value["text"])
        if value.get("output_text") is not None:
            return str(value["output_text"])
    return str(value)


def _copy_if_present(source: dict[str, Any], target: dict[str, Any], *keys: str) -> None:
    for key in keys:
        if key in source:
            target[key] = copy.deepcopy(source[key])


def _without_none(
    value: dict[str, Any], *, keep_none: set[str] | None = None
) -> dict[str, Any]:
    keep = keep_none or set()
    return {key: item for key, item in value.items() if item is not None or key in keep}


def _responses_status_to_finish(status: Any) -> str:
    return {"completed": "stop", "incomplete": "length"}.get(str(status), "stop")


def _transform_chat_anthropic_sse(
    source: str, target: str, chunks: list[bytes]
) -> list[bytes]:
    events: list[bytes] = []
    for raw_event in _parse_sse_events(chunks):
        if raw_event == "[DONE]":
            if target == "anthropic":
                events.append(_sse("message_stop", {"type": "message_stop"}))
            else:
                events.append(b"data: [DONE]\n\n")
            continue
        try:
            payload = json.loads(raw_event)
        except json.JSONDecodeError:
            continue
        if source == "openai_chat" and target == "anthropic":
            delta = ((payload.get("choices") or [{}])[0].get("delta") or {})
            text = delta.get("content")
            if text:
                events.append(
                    _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": text},
                        },
                    )
                )
            if delta.get("tool_calls"):
                for call in delta["tool_calls"]:
                    function = call.get("function") or {}
                    if function.get("arguments"):
                        events.append(
                            _sse(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": 0,
                                    "delta": {
                                        "type": "input_json_delta",
                                        "partial_json": function["arguments"],
                                    },
                                },
                            )
                        )
        elif source == "anthropic" and target == "openai_chat":
            event_type = payload.get("type")
            if event_type == "content_block_delta":
                delta = payload.get("delta") or {}
                if delta.get("text"):
                    events.append(
                        _json_sse(
                            {
                                "choices": [
                                    {"index": 0, "delta": {"content": delta["text"]}}
                                ]
                            }
                        )
                    )
        else:
            # Responses and Anthropic both use named events; text deltas have
            # a compatible shape after normalizing the event payload.
            delta = payload.get("delta") or {}
            text = delta.get("text") or payload.get("text")
            if text:
                if target == "anthropic":
                    events.append(
                        _sse(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": 0,
                                "delta": {"type": "text_delta", "text": text},
                            },
                        )
                    )
                else:
                    events.append(_json_sse({"choices": [{"index": 0, "delta": {"content": text}}]}))
    return events


def _transform_chat_responses_sse(
    source: str, target: str, chunks: list[bytes]
) -> list[bytes]:
    # Both formats carry incremental text in a delta; normalize the wire event
    # rather than buffering the whole response.
    events: list[bytes] = []
    for raw_event in _parse_sse_events(chunks):
        if raw_event == "[DONE]":
            events.append(b"data: [DONE]\n\n")
            continue
        try:
            payload = json.loads(raw_event)
        except json.JSONDecodeError:
            continue
        text = _text_content(payload.get("delta"))
        if not text:
            text = _text_content(payload.get("text"))
        if target == "openai_chat":
            events.append(_json_sse({"choices": [{"index": 0, "delta": {"content": text}}]}))
        else:
            events.append(_json_sse({"type": "response.output_text.delta", "delta": text}))
    return events


def _parse_sse_events(chunks: Iterable[bytes]) -> list[str]:
    buffer = b"".join(chunks)
    result: list[str] = []
    for block in buffer.replace(b"\r\n", b"\n").split(b"\n\n"):
        data_lines = [line[6:] for line in block.splitlines() if line.startswith(b"data:")]
        if data_lines:
            result.append(b"\n".join(data_lines).decode("utf-8", errors="replace"))
    return result


def _sse(event: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()


def _json_sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()
