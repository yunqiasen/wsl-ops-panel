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
    if source == "gemini" and target == "openai_chat":
        return TransformResult(_gemini_to_chat_request(body), _ENDPOINTS[target])
    if source == "gemini" and target == "openai_responses":
        return TransformResult(
            _chat_to_responses_request(_gemini_to_chat_request(body)),
            _ENDPOINTS[target],
        )
    if target == "gemini" and source == "openai_chat":
        return _chat_to_gemini_result(body)
    if target == "gemini" and source == "openai_responses":
        return _chat_to_gemini_result(_responses_to_chat_request(body))
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
    if source == "gemini" and target == "openai_chat":
        return _gemini_to_chat_response(body)
    if source == "gemini" and target == "openai_responses":
        return _chat_to_responses_response(_gemini_to_chat_response(body))
    if target == "gemini" and source == "openai_chat":
        return _chat_to_gemini_response(body)
    if target == "gemini" and source == "openai_responses":
        return _chat_to_gemini_response(_responses_to_chat_response(body))
    raise UnsupportedProtocolTransform(f"unsupported protocol transform: {source} -> {target}")


def transform_sse(
    source_format: str, target_format: str, chunks: Iterable[bytes]
) -> list[bytes]:
    transformer = SseTransformer(source_format, target_format)
    result: list[bytes] = []
    for chunk in chunks:
        result.extend(transformer.feed(chunk))
    result.extend(transformer.finish())
    return result


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



def _gemini_to_chat_request(body: dict[str, Any]) -> dict[str, Any]:
    """Convert a Gemini GenerateContent request to OpenAI Chat shape."""
    messages: list[dict[str, Any]] = []
    system_instruction = body.get("systemInstruction")
    system = (
        _text_content(system_instruction.get("parts"))
        if isinstance(system_instruction, dict)
        else ""
    )
    if system:
        messages.append({"role": "system", "content": system})
    for item in body.get("contents") or []:
        if not isinstance(item, dict):
            continue
        role = "assistant" if item.get("role") in {"model", "assistant"} else "user"
        content_parts: list[dict[str, Any]] = []
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        message_signature: str | None = None
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        for part in item.get("parts") or []:
            if not isinstance(part, dict):
                continue
            if part.get("text") is not None:
                text = str(part.get("text") or "")
                if part.get("thought") is True:
                    reasoning_parts.append(text)
                    message_signature = _gemini_thought_signature(part) or message_signature
                else:
                    text_parts.append(text)
                    content_parts.append({"type": "text", "text": text})
                    message_signature = _gemini_thought_signature(part) or message_signature
            media = _gemini_inline_data_to_openai(part)
            if media is not None:
                content_parts.append(media)
            function_call = part.get("functionCall")
            if isinstance(function_call, dict):
                tool_call = {
                    "id": function_call.get("id"),
                    "type": "function",
                    "function": {
                        "name": function_call.get("name"),
                        "arguments": json.dumps(
                            function_call.get("args") or {}, ensure_ascii=False
                        ),
                    },
                }
                signature = _gemini_thought_signature(part)
                if signature:
                    tool_call["extra_content"] = _google_signature(signature)
                tool_calls.append(tool_call)
            function_response = part.get("functionResponse")
            if isinstance(function_response, dict):
                tool_result = {
                    "role": "tool",
                    "tool_call_id": function_response.get("id"),
                    "name": function_response.get("name"),
                    "content": _json_text(function_response.get("response") or {}),
                }
                tool_results.append(_without_none(tool_result))
        if tool_results:
            messages.extend(tool_results)
            continue
        has_media = any(part.get("type") != "text" for part in content_parts)
        content_value: str | list[dict[str, Any]] | None
        if has_media:
            content_value = content_parts
        else:
            content_value = "".join(text_parts)
        message: dict[str, Any] = {
            "role": role,
            "content": content_value,
        }
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if message_signature:
            message["extra_content"] = _google_signature(message_signature)
        if tool_calls:
            message["tool_calls"] = tool_calls
            if not message["content"]:
                message["content"] = None
        if message["content"] or reasoning_parts or tool_calls:
            messages.append(message)
    generation = body.get("generationConfig")
    generation = generation if isinstance(generation, dict) else {}
    result: dict[str, Any] = {
        "model": body.get("model"),
        "messages": messages,
    }
    for source_key, target_key in {
        "maxOutputTokens": "max_tokens",
        "temperature": "temperature",
        "topP": "top_p",
        "stopSequences": "stop",
    }.items():
        if source_key in generation:
            result[target_key] = copy.deepcopy(generation[source_key])
    tools = _gemini_tools_to_chat(body.get("tools"))
    if tools:
        result["tools"] = tools
    return _without_none(result)


def _chat_to_gemini_result(body: dict[str, Any]) -> TransformResult:
    model = str(body.get("model") or "")
    if not model:
        raise UnsupportedProtocolTransform("Gemini requests require a model")
    stream = bool(body.get("stream"))
    method = "streamGenerateContent?alt=sse" if stream else "generateContent"
    return TransformResult(
        _chat_to_gemini_request(body),
        f"/v1beta/models/{model}:{method}",
    )


def _chat_to_gemini_request(body: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    contents: list[dict[str, Any]] = []
    system_parts: list[dict[str, str]] = []
    messages = [
        message for message in body.get("messages") or [] if isinstance(message, dict)
    ]
    tool_names = _chat_tool_name_map(messages)
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        if role == "system":
            text = _text_content(content)
            if text:
                system_parts.append({"text": text})
            continue
        gemini_role = "model" if role == "assistant" else "user"
        parts: list[dict[str, Any]] = []
        message_signature = _openai_thought_signature(message)
        reasoning = str(message.get("reasoning_content") or "")
        if reasoning:
            thought_part: dict[str, Any] = {"text": reasoning, "thought": True}
            if message_signature:
                thought_part["thoughtSignature"] = message_signature
            parts.append(thought_part)
        parts.extend(_openai_content_to_gemini_parts(content))
        if message_signature and not reasoning and parts:
            parts[0].setdefault("thoughtSignature", message_signature)
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            if not isinstance(function, dict):
                continue
            try:
                args = json.loads(function.get("arguments") or "{}")
            except (TypeError, json.JSONDecodeError):
                args = {}
            call_part: dict[str, Any] = {
                "functionCall": {
                    "id": call.get("id"),
                    "name": function.get("name"),
                    "args": args,
                }
            }
            signature = _openai_thought_signature(call)
            if signature:
                call_part["thoughtSignature"] = signature
            parts.append(call_part)
        if role == "tool":
            call_id = message.get("tool_call_id")
            parts = [
                {
                    "functionResponse": {
                        "id": call_id,
                        "name": message.get("name")
                        or tool_names.get(str(call_id or ""))
                        or "tool",
                        "response": _chat_tool_response(message.get("content")),
                    }
                }
            ]
        if parts:
            contents.append({"role": gemini_role, "parts": parts})
    if system_parts:
        result["systemInstruction"] = {"parts": system_parts}
    result["contents"] = contents
    generation: dict[str, Any] = {}
    for source_key, target_key in {
        "max_tokens": "maxOutputTokens",
        "temperature": "temperature",
        "top_p": "topP",
        "stop": "stopSequences",
    }.items():
        if source_key in body:
            generation[target_key] = copy.deepcopy(body[source_key])
    if generation:
        result["generationConfig"] = generation
    tools = _chat_tools_to_gemini(body.get("tools"))
    if tools:
        result["tools"] = tools
    return result


def _gemini_inline_data_to_openai(part: dict[str, Any]) -> dict[str, Any] | None:
    value = part.get("inlineData")
    if not isinstance(value, dict):
        value = part.get("inline_data")
    if not isinstance(value, dict):
        return None
    data = value.get("data")
    if not data:
        return None
    mime_type = value.get("mimeType") or value.get("mime_type") or "application/octet-stream"
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime_type};base64,{data}"},
    }


def _gemini_thought_signature(part: dict[str, Any]) -> str | None:
    for key in ("thoughtSignature", "thought_signature"):
        value = part.get(key)
        if value:
            return str(value)
    return None


def _google_signature(signature: str) -> dict[str, Any]:
    return {"google": {"thought_signature": signature}}


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _chat_tool_name_map(messages: list[dict[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            call_id = call.get("id")
            function = call.get("function") or {}
            if call_id and isinstance(function, dict) and function.get("name"):
                names[str(call_id)] = str(function["name"])
    return names


def _openai_thought_signature(value: dict[str, Any]) -> str | None:
    direct = value.get("thoughtSignature") or value.get("thought_signature")
    if direct:
        return str(direct)
    for container in (value.get("extra_content"), value.get("extraContent")):
        if not isinstance(container, dict):
            continue
        google = container.get("google")
        if isinstance(google, dict):
            signature = google.get("thought_signature") or google.get("thoughtSignature")
            if signature:
                return str(signature)
    function = value.get("function")
    if isinstance(function, dict):
        return _openai_thought_signature(function)
    return None


def _openai_content_to_gemini_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}] if content else []
    if isinstance(content, dict):
        content = [content]
    if isinstance(content, list):
        parts: list[dict[str, Any]] = []
        for item in content:
            if isinstance(item, str):
                if item:
                    parts.append({"text": item})
                continue
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind in {"text", "input_text", "output_text"}:
                text = item.get("text")
                if text:
                    parts.append({"text": str(text)})
                continue
            if kind in {"image_url", "input_image"} or "image_url" in item:
                image = item.get("image_url") or item.get("imageUrl")
                url = image.get("url") if isinstance(image, dict) else image
                inline = _data_url_to_gemini(url)
                if inline is not None:
                    parts.append(inline)
                continue
            if item.get("text") is not None:
                parts.append({"text": str(item["text"])})
        return parts
    if content is None:
        return []
    return [{"text": str(content)}]


def _data_url_to_gemini(url: Any) -> dict[str, Any] | None:
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    header, separator, data = url[5:].partition(",")
    if not separator or not data:
        return None
    mime_type, _, encoding = header.partition(";")
    if encoding.lower() != "base64" or not mime_type:
        return None
    return {"inlineData": {"mimeType": mime_type, "data": data}}


def _chat_tool_response(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return copy.deepcopy(content)
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        if parsed is not None:
            return {"result": parsed}
        return {"result": content}
    if content is None:
        return {}
    return {"result": copy.deepcopy(content)}


def _gemini_tools_to_chat(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for group in value or []:
        if not isinstance(group, dict):
            continue
        for declaration in group.get("functionDeclarations") or []:
            if not isinstance(declaration, dict):
                continue
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": declaration.get("name"),
                        "description": declaration.get("description"),
                        "parameters": declaration.get("parameters") or {},
                    },
                }
            )
    return result


def _chat_tools_to_gemini(value: Any) -> list[dict[str, Any]]:
    declarations: list[dict[str, Any]] = []
    for tool in value or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(function, dict):
            continue
        declarations.append(
            {
                "name": function.get("name"),
                "description": function.get("description"),
                "parameters": function.get("parameters") or {},
            }
        )
    return [{"functionDeclarations": declarations}] if declarations else []

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



def _gemini_to_chat_response(body: dict[str, Any]) -> dict[str, Any]:
    candidate = (body.get("candidates") or [{}])[0]
    content = candidate.get("content") if isinstance(candidate, dict) else {}
    parts = content.get("parts") if isinstance(content, dict) else []
    message = _gemini_response_parts_to_chat(parts)
    finish_reason = str(candidate.get("finishReason") or "").upper()
    finish = {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "SAFETY": "content_filter",
    }.get(finish_reason)
    if message.get("tool_calls"):
        finish = "tool_calls"
    usage = _gemini_usage_to_chat(body.get("usageMetadata"))
    if message.get("tool_calls") and not message.get("content"):
        message["content"] = None
    if not message.get("content") and not message.get("tool_calls"):
        message.setdefault("content", "")
    return {
        "id": body.get("responseId") or body.get("id"),
        "object": "chat.completion",
        "model": body.get("modelVersion") or body.get("model"),
        "choices": [
            {"index": candidate.get("index", 0), "message": message, "finish_reason": finish}
        ],
        "usage": usage,
    }


def _gemini_response_parts_to_chat(parts: Any) -> dict[str, Any]:
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    images: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    thought_signature: str | None = None
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if text is not None:
            text_value = str(text or "")
            signature = _gemini_thought_signature(part)
            if part.get("thought") is True:
                reasoning_parts.append(text_value)
                thought_signature = signature or thought_signature
            else:
                text_parts.append(text_value)
        media = _gemini_inline_data_to_openai(part)
        if media is not None:
            images.append(media)
        function_call = part.get("functionCall")
        if isinstance(function_call, dict):
            call: dict[str, Any] = {
                "id": function_call.get("id"),
                "type": "function",
                "function": {
                    "name": function_call.get("name"),
                    "arguments": json.dumps(
                        function_call.get("args") or {}, ensure_ascii=False
                    ),
                },
            }
            signature = _gemini_thought_signature(part)
            if signature:
                call["extra_content"] = _google_signature(signature)
            tool_calls.append(call)
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts),
    }
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if thought_signature:
        message["extra_content"] = _google_signature(thought_signature)
    if images:
        message["images"] = images
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _gemini_usage_to_chat(value: Any) -> dict[str, Any]:
    usage = value if isinstance(value, dict) else {}
    prompt_tokens = _as_int(usage.get("promptTokenCount"))
    if "candidatesTokenCount" in usage:
        completion_tokens = _as_int(usage.get("candidatesTokenCount"))
    else:
        completion_tokens = max(_as_int(usage.get("totalTokenCount")) - prompt_tokens, 0)
    total_tokens = _as_int(usage.get("totalTokenCount"))
    if not total_tokens:
        total_tokens = prompt_tokens + completion_tokens
    result: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
    cached_tokens = _as_int(usage.get("cachedContentTokenCount"))
    thoughts_tokens = _as_int(usage.get("thoughtsTokenCount"))
    if cached_tokens:
        result["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    if thoughts_tokens:
        result["completion_tokens_details"] = {"reasoning_tokens": thoughts_tokens}
    return result


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _chat_to_gemini_response(body: dict[str, Any]) -> dict[str, Any]:
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    parts: list[dict[str, Any]] = []
    text = _text_content(message.get("content"))
    if text:
        parts.append({"text": text})
    for image in message.get("images") or []:
        if isinstance(image, dict):
            inline = _openai_content_to_gemini_parts(image)
            parts.extend(inline)
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            args = json.loads(function.get("arguments") or "{}")
        except (TypeError, json.JSONDecodeError):
            args = {}
        call_part: dict[str, Any] = {
            "functionCall": {
                "id": call.get("id"),
                "name": function.get("name"),
                "args": args,
            }
        }
        signature = _openai_thought_signature(call)
        if signature:
            call_part["thoughtSignature"] = signature
        parts.append(call_part)
    finish = {
        "stop": "STOP",
        "length": "MAX_TOKENS",
        "tool_calls": "STOP",
        "content_filter": "SAFETY",
    }.get(choice.get("finish_reason"), "STOP")
    usage = body.get("usage") or {}
    return {
        "responseId": body.get("id"),
        "modelVersion": body.get("model"),
        "candidates": [
            {
                "content": {"role": "model", "parts": parts},
                "finishReason": finish,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": usage.get("prompt_tokens", 0),
            "candidatesTokenCount": usage.get("completion_tokens", 0),
            "totalTokenCount": usage.get(
                "total_tokens",
                usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0),
            ),
        },
    }

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



class SseTransformer:
    """Incrementally transform complete SSE events without buffering a stream."""

    def __init__(self, source_format: str, target_format: str) -> None:
        self.source = _format(source_format)
        self.target = _format(target_format)
        if self.source != self.target and not (
            self.source in _FORMATS and self.target in _FORMATS
        ):
            raise UnsupportedProtocolTransform(
                f"unsupported protocol transform: {self.source} -> {self.target}"
            )
        self._buffer = bytearray()
        self._started = False
        self._content_started = False
        self._finished = False
        self._finish_emitted = False

    def feed(self, chunk: bytes) -> list[bytes]:
        if self.source == self.target:
            return [chunk]
        if not chunk:
            return []
        self._buffer.extend(chunk)
        result: list[bytes] = []
        while True:
            block = self._take_block()
            if block is None:
                break
            event, data = _parse_sse_block(block)
            if data is None:
                continue
            result.extend(self._convert(event, data))
        return result

    def finish(self) -> list[bytes]:
        if self.source == self.target or self._finished:
            return []
        result: list[bytes] = []
        if self._buffer.strip():
            event, data = _parse_sse_block(bytes(self._buffer))
            self._buffer.clear()
            if data is not None:
                result.extend(self._convert(event, data))
        if not self._finish_emitted:
            result.extend(self._convert("", "[DONE]"))
        return result

    def _take_block(self) -> bytes | None:
        raw = bytes(self._buffer)
        candidates = [
            (raw.find(b"\n\n"), 2),
            (raw.find(b"\r\n\r\n"), 4),
        ]
        candidates = [(index, size) for index, size in candidates if index >= 0]
        if not candidates:
            return None
        index, size = min(candidates, key=lambda item: item[0])
        block = raw[:index]
        del self._buffer[: index + size]
        return block

    def _convert(self, event: str, data: str) -> list[bytes]:
        if data.strip() == "[DONE]":
            return self._finish()
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        if self.source == "gemini":
            kind, value = _gemini_sse_to_chat(payload)
        elif self.source == "anthropic":
            kind, value = _anthropic_sse_to_chat(event, payload)
        elif self.source == "openai_responses":
            kind, value = _responses_sse_to_chat(event, payload)
        else:
            kind, value = _openai_chat_sse_to_chat(payload)
        if kind == "done":
            return self._finish()
        if self.target == "openai_chat":
            return _chat_sse_to_openai(value)
        if self.target == "anthropic":
            return self._chat_sse_to_anthropic(kind, value)
        if self.target == "openai_responses":
            return _chat_sse_to_responses(kind, value)
        if self.target == "gemini":
            return _chat_sse_to_gemini(kind, value)
        return []

    def _finish(self) -> list[bytes]:
        if self._finish_emitted:
            return []
        self._finish_emitted = True
        self._finished = True
        result: list[bytes] = []
        if self.target == "anthropic":
            if not self._started:
                result.append(
                    _sse(
                        "message_start",
                        {
                            "type": "message_start",
                            "message": {
                                "id": "",
                                "type": "message",
                                "role": "assistant",
                                "model": "",
                                "content": [],
                                "usage": {"input_tokens": 0, "output_tokens": 0},
                            },
                        },
                    )
                )
                self._started = True
            if self._content_started:
                result.append(
                    _sse(
                        "content_block_stop",
                        {"type": "content_block_stop", "index": 0},
                    )
                )
            result.append(
                _sse(
                    "message_stop",
                    {"type": "message_stop"},
                )
            )
            return result
        if self.target == "openai_chat":
            return [b"data: [DONE]\n\n"]
        if self.target == "openai_responses":
            return [
                _sse(
                    "response.completed",
                    {"type": "response.completed", "response": {"status": "completed"}},
                )
            ]
        return []

    def _chat_sse_to_anthropic(self, kind: str, value: dict[str, Any]) -> list[bytes]:
        result: list[bytes] = []
        if not self._started:
            result.append(
                _sse(
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": value.get("id", ""),
                            "type": "message",
                            "role": "assistant",
                            "model": value.get("model", ""),
                            "content": [],
                            "usage": {"input_tokens": 0, "output_tokens": 0},
                        },
                    },
                )
            )
            self._started = True
        text = value.get("text")
        if text:
            if not self._content_started:
                result.append(
                    _sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                )
                self._content_started = True
            result.append(
                _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )
            )
        if kind == "finish":
            reason = value.get("finish_reason")
            result.append(
                _sse(
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {
                            "stop_reason": {
                                "stop": "end_turn",
                                "length": "max_tokens",
                                "tool_calls": "tool_use",
                            }.get(reason, reason),
                            "stop_sequence": None,
                        },
                        "usage": value.get("usage", {}),
                    },
                )
            )
        return result


def _parse_sse_block(block: bytes) -> tuple[str, str | None]:
    event = ""
    data_lines: list[str] = []
    for line in block.replace(b"\r\n", b"\n").split(b"\n"):
        if line.startswith(b"event:"):
            event = line[6:].lstrip().decode("utf-8", errors="replace")
        elif line.startswith(b"data:"):
            data_lines.append(line[5:].lstrip().decode("utf-8", errors="replace"))
    return event, "\n".join(data_lines) if data_lines else None


def _openai_chat_sse_to_chat(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    choices = payload.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    delta = choice.get("delta") or {}
    return (
        "finish" if choice.get("finish_reason") else "delta",
        {
            "id": payload.get("id", ""),
            "model": payload.get("model", ""),
            "text": delta.get("content") or "",
            "tool_calls": delta.get("tool_calls") or [],
            "finish_reason": choice.get("finish_reason"),
            "usage": payload.get("usage") or {},
        },
    )


def _anthropic_sse_to_chat(
    event: str, payload: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    event_type = str(payload.get("type") or event)
    if event_type == "message_stop":
        return "done", {}
    if event_type == "message_delta":
        delta = payload.get("delta") or {}
        return "finish", {"finish_reason": delta.get("stop_reason"), "usage": payload.get("usage") or {}}
    delta = payload.get("delta") or {}
    return "delta", {
        "id": (payload.get("message") or {}).get("id", ""),
        "model": (payload.get("message") or {}).get("model", ""),
        "text": delta.get("text") or "",
        "finish_reason": None,
        "usage": payload.get("usage") or {},
    }


def _responses_sse_to_chat(
    event: str, payload: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    event_type = str(payload.get("type") or event)
    if event_type in {"response.completed", "response.done"}:
        response = payload.get("response") or {}
        return "finish", {"finish_reason": "stop", "usage": response.get("usage") or {}}
    if event_type.endswith("output_text.delta") or event_type == "response.output_text.delta":
        return "delta", {"text": payload.get("delta") or ""}
    if payload.get("delta") is not None:
        return "delta", {"text": _text_content(payload.get("delta"))}
    return "delta", {"text": ""}


def _gemini_sse_to_chat(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    candidate = (payload.get("candidates") or [{}])[0]
    if not isinstance(candidate, dict):
        return "delta", {"text": ""}
    content = candidate.get("content") or {}
    parts = content.get("parts") if isinstance(content, dict) else []
    message = _gemini_response_parts_to_chat(parts)
    value: dict[str, Any] = {
        "id": payload.get("responseId", ""),
        "model": payload.get("modelVersion", ""),
        "text": message.get("content", ""),
        "reasoning_content": message.get("reasoning_content", ""),
        "images": message.get("images") or [],
        "tool_calls": message.get("tool_calls") or [],
        "extra_content": message.get("extra_content"),
        "usage": _gemini_usage_to_chat(payload.get("usageMetadata")),
    }
    reason = candidate.get("finishReason")
    kind = "finish" if reason else "delta"
    value["finish_reason"] = (
        "tool_calls"
        if value["tool_calls"]
        else "stop"
        if reason == "STOP"
        else "length"
        if reason == "MAX_TOKENS"
        else "content_filter"
        if reason in {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT"}
        else None
    )
    return kind, value


def _chat_sse_to_openai(value: dict[str, Any]) -> list[bytes]:
    delta: dict[str, Any] = {}
    if value.get("text"):
        delta["content"] = value["text"]
    if value.get("reasoning_content"):
        delta["reasoning_content"] = value["reasoning_content"]
    if value.get("images"):
        delta["images"] = value["images"]
    if value.get("tool_calls"):
        delta["tool_calls"] = value["tool_calls"]
    if value.get("extra_content"):
        delta["extra_content"] = value["extra_content"]
    choice: dict[str, Any] = {"index": 0, "delta": delta}
    if value.get("finish_reason"):
        choice["finish_reason"] = value["finish_reason"]
    result: dict[str, Any] = {
        "id": value.get("id", ""),
        "model": value.get("model", ""),
        "choices": [choice],
    }
    if value.get("usage"):
        result["usage"] = value["usage"]
    return [_json_sse(result)]


def _chat_sse_to_responses(kind: str, value: dict[str, Any]) -> list[bytes]:
    if kind == "finish":
        return [
            _sse(
                "response.completed",
                {"type": "response.completed", "response": {"status": "completed"}},
            )
        ]
    return [
        _sse(
            "response.output_text.delta",
            {"type": "response.output_text.delta", "delta": value.get("text", "")},
        )
    ]


def _chat_sse_to_gemini(kind: str, value: dict[str, Any]) -> list[bytes]:
    candidate: dict[str, Any] = {"content": {"role": "model", "parts": []}}
    if value.get("text"):
        candidate["content"]["parts"].append({"text": value["text"]})
    if kind == "finish":
        candidate["finishReason"] = value.get("finish_reason") or "STOP"
    return [_json_sse({"candidates": [candidate]})]

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
