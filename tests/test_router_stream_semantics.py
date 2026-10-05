"""Exercise actual incremental stream interface, not helper projections."""

import json

import pytest

from app.agent_router.transforms import SseTransformer, transform_sse


def event(kind, value):
    return (f"event: {kind}\n" + "data: " + json.dumps(value) + "\n\n").encode()


def chat(delta, reason=None):
    return event(
        "message",
        {
            "id": "msg-fixture",
            "model": "fixture",
            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
        },
    )


def source_stream(source):
    if source == "openai_chat":
        return [
            chat(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "fixture_tool", "arguments": '{"x":'},
                        }
                    ]
                }
            ),
            chat({"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}),
            chat({}, "tool_calls"),
            b"data: [DONE]\n\n",
        ]
    if source == "anthropic":
        return [
            event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg-fixture",
                        "model": "fixture",
                        "usage": {"input_tokens": 4},
                    },
                },
            ),
            event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "fixture_tool",
                        "input": {},
                    },
                },
            ),
            event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"x":'},
                },
            ),
            event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": "1}"},
                },
            ),
            event("content_block_stop", {"type": "content_block_stop", "index": 0}),
            event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 3},
                },
            ),
            event("message_stop", {"type": "message_stop"}),
        ]
    if source == "openai_responses":
        return [
            event(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_1",
                        "name": "fixture_tool",
                        "arguments": "",
                    },
                },
            ),
            event(
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "item_id": "fc_1",
                    "delta": '{"x":',
                },
            ),
            event(
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "item_id": "fc_1",
                    "delta": "1}",
                },
            ),
            event(
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "id": "msg-fixture",
                        "status": "completed",
                        "usage": {"input_tokens": 4, "output_tokens": 3},
                    },
                },
            ),
        ]
    return [
        event(
            "message",
            {
                "responseId": "msg-fixture",
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {
                                        "id": "call_1",
                                        "name": "fixture_tool",
                                        "args": {"x": 1},
                                    }
                                }
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
            },
        )
    ]


def decoded(data):
    return [
        json.loads(line[6:])
        for line in data.decode().splitlines()
        if line.startswith("data: ") and line[6:] != "[DONE]"
    ]


@pytest.mark.parametrize(
    "source", ["openai_chat", "anthropic", "openai_responses", "gemini"]
)
@pytest.mark.parametrize(
    "target", ["openai_chat", "anthropic", "openai_responses", "gemini"]
)
def test_tools_survive_split_streams(source, target):
    raw = b"".join(source_stream(source))
    # Split in the middle of UTF-8 / JSON / event delimiters, not just at events.
    output = b"".join(
        transform_sse(source, target, [raw[i : i + 7] for i in range(0, len(raw), 7)])
    )
    assert b"fixture_tool" in output
    assert "call_1" in output.decode()
    values = decoded(output)
    if target == "anthropic":
        assert any(v.get("content_block", {}).get("type") == "tool_use" for v in values)
        assert json.loads(
            "".join(v.get("delta", {}).get("partial_json", "") for v in values)
        ) == {"x": 1}
        assert sum(v.get("type") == "message_stop" for v in values) == 1
    elif target == "openai_responses":
        assert any(v.get("item", {}).get("type") == "function_call" for v in values)
        assert sum(v.get("type") == "response.completed" for v in values) == 1
    elif target == "gemini":
        parts = [
            p
            for v in values
            for c in v.get("candidates", [])
            for p in c.get("content", {}).get("parts", [])
        ]
        assert any(p.get("functionCall", {}).get("args") == {"x": 1} for p in parts)


@pytest.mark.parametrize("target", ["anthropic", "openai_responses", "gemini"])
def test_missing_terminal_event_reports_incomplete(target):
    result = b"".join(
        transform_sse("openai_chat", target, [chat({"content": "partial"})])
    )
    assert b"response.completed" not in result
    assert b"message_stop" not in result
    assert any(
        "error" in item or item.get("type") == "response.incomplete"
        for item in decoded(result)
    )


@pytest.mark.parametrize("kind", ["response.failed", "response.incomplete", "error"])
@pytest.mark.parametrize("target", ["openai_chat", "anthropic", "gemini"])
def test_upstream_error_is_not_success(kind, target):
    raw = event(
        kind,
        {
            "type": kind,
            "response": {"status": kind.split(".")[-1]},
            "error": {"type": "fixture_error", "message": "fixture failure"},
        },
    )
    result = b"".join(transform_sse("openai_responses", target, [raw]))
    assert b"message_stop" not in result
    assert b"[DONE]" not in result
    assert b"error" in result or b"incomplete" in result


def test_multiple_tools_keep_ids_and_argument_fragments():
    chunks = [
        chat(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "first", "arguments": '{"a":'},
                    },
                    {
                        "index": 1,
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "second", "arguments": '{"b":'},
                    },
                ]
            }
        ),
        chat(
            {
                "tool_calls": [
                    {"index": 1, "function": {"arguments": "2}"}},
                    {"index": 0, "function": {"arguments": "1}"}},
                ]
            }
        ),
        chat({}, "tool_calls"),
        b"data: [DONE]\n\n",
    ]
    values = decoded(b"".join(transform_sse("openai_chat", "anthropic", chunks)))
    starts = {
        v["index"]: v["content_block"]
        for v in values
        if v.get("type") == "content_block_start"
    }
    assert {v["id"] for v in starts.values()} == {"call_a", "call_b"}
    for idx, block in starts.items():
        args = "".join(
            v.get("delta", {}).get("partial_json", "")
            for v in values
            if v.get("index") == idx
        )
        assert json.loads(args) == ({"a": 1} if block["id"] == "call_a" else {"b": 2})


def test_same_protocol_is_byte_exact():
    raw = b'data: {"unknown": "preserved"}\r\n\r\ndata: [DONE]\n\n'
    stream = SseTransformer("openai_responses", "openai_responses")
    assert (
        b"".join([*stream.feed(raw[:11]), *stream.feed(raw[11:]), *stream.finish()])
        == raw
    )


@pytest.mark.parametrize("complete", [True, False])
def test_router_http_stream_accounts_for_protocol_completion(tmp_path, complete):
    import httpx
    from fastapi.testclient import TestClient
    from app.agent_router.app import create_agent_router_app
    from app.services.agent_router_config import AgentRouterConfigStore

    store = AgentRouterConfigStore(tmp_path)
    store.set_provider(
        "claude",
        {
            "base_url": "https://fixture.example/v1",
            "api_format": "openai_chat",
            "auth_mode": "none",
        },
    )

    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            for chunk in (
                source_stream("openai_chat")
                if complete
                else [chat({"content": "partial"})]
            ):
                yield chunk

        async def aclose(self):
            self.closed = True

    stream = Stream()
    app = create_agent_router_app(
        store=store,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )
        ),
    )
    response = TestClient(app).post(
        "/claude/v1/messages",
        json={
            "model": "fixture",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
    )
    assert response.status_code == 200
    if complete:
        assert "fixture_tool" in response.text and "message_stop" in response.text
    else:
        assert "error" in response.text and "message_stop" not in response.text
    assert stream.closed
    assert app.state.router_counters.snapshot() == {
        "active": 0,
        "total": 1,
        "success": int(complete),
        "failure": int(not complete),
    }


@pytest.mark.parametrize("target", ["openai_chat", "openai_responses", "gemini"])
def test_anthropic_stop_reason_without_message_stop_is_incomplete(target):
    raw = b"".join(source_stream("anthropic")[:-1])
    output = b"".join(transform_sse("anthropic", target, [raw]))
    values = decoded(output)
    assert any("error" in v or v.get("type") == "response.incomplete" for v in values)
    assert b"[DONE]" not in output and b"response.completed" not in output


@pytest.mark.parametrize("target", ["anthropic", "openai_responses", "gemini"])
def test_utf8_fragments_usage_and_length_are_preserved(target):
    raw = b"".join(
        [
            chat({"content": "中文片段"}),
            chat({}, "length"),
            event(
                "message",
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 7,
                        "total_tokens": 18,
                    },
                },
            ),
            b"data: [DONE]\n\n",
        ]
    )
    values = decoded(
        b"".join(
            transform_sse(
                "openai_chat", target, [raw[i : i + 1] for i in range(len(raw))]
            )
        )
    )
    assert "中文片段" in json.dumps(values, ensure_ascii=False)
    if target == "anthropic":
        terminal = next(v for v in values if v.get("type") == "message_delta")
        assert terminal["delta"]["stop_reason"] == "max_tokens"
        assert terminal["usage"]["input_tokens"] == 11
        assert terminal["usage"]["output_tokens"] == 7
    elif target == "openai_responses":
        terminal = next(v for v in values if v.get("type") == "response.incomplete")
        assert terminal["response"]["usage"]["input_tokens"] == 11
        assert (
            terminal["response"]["incomplete_details"]["reason"] == "max_output_tokens"
        )
    else:
        assert values[-1]["candidates"][0]["finishReason"] == "MAX_TOKENS"
        assert values[-1]["usageMetadata"]["promptTokenCount"] == 11


@pytest.mark.parametrize("target", ["anthropic", "openai_responses", "gemini"])
def test_truncated_tool_arguments_never_emit_success(target):
    raw = b"".join(
        [
            chat(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_broken",
                            "function": {"name": "test", "arguments": '{"x":'},
                        }
                    ]
                }
            ),
            chat({}, "tool_calls"),
            b"data: [DONE]\n\n",
        ]
    )
    values = decoded(b"".join(transform_sse("openai_chat", target, [raw])))
    assert any("error" in v or v.get("type") == "response.incomplete" for v in values)
    assert not any(
        v.get("type") in {"response.completed", "message_stop"} for v in values
    )
