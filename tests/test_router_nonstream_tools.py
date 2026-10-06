"""Responses output items form one Chat assistant turn, not separate choices."""

import pytest

from app.agent_router.transforms import transform_response


def text(value):
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": value}],
    }


def call(name):
    return {
        "type": "function_call",
        "id": f"item_{name}",
        "call_id": f"call_{name}",
        "name": name,
        "arguments": '{"x": 1}',
    }


@pytest.mark.parametrize(
    "output,content",
    [
        ([text("hello"), call("a"), call("b"), text("world")], "hello\nworld"),
        ([call("a"), text("hello"), call("b")], "hello"),
        ([call("a"), call("b")], None),
    ],
)
@pytest.mark.parametrize("target", ["openai_chat", "anthropic", "gemini"])
def test_nonstream_preserves_every_tool_and_text(output, content, target):
    actual = transform_response(
        "openai_responses",
        target,
        {
            "id": "resp-fixture",
            "status": "completed",
            "output": output,
            "model": "fixture",
            "usage": {"input_tokens": 4, "output_tokens": 5, "total_tokens": 9},
        },
    )
    if target == "openai_chat":
        choice = actual["choices"][0]
        assert choice["message"]["content"] == content
        assert choice["finish_reason"] == "tool_calls"
        calls = choice["message"]["tool_calls"]
        assert [v["id"] for v in calls] == ["call_a", "call_b"]
        assert [v["function"] for v in calls] == [
            {"name": name, "arguments": '{"x": 1}'} for name in ("a", "b")
        ]
        assert actual["usage"]["total_tokens"] == 9
    elif target == "anthropic":
        assert actual["stop_reason"] == "tool_use"
        assert [v["id"] for v in actual["content"] if v["type"] == "tool_use"] == [
            "call_a",
            "call_b",
        ]
    else:
        parts = actual["candidates"][0]["content"]["parts"]
        assert [v["functionCall"]["id"] for v in parts if "functionCall" in v] == [
            "call_a",
            "call_b",
        ]


@pytest.mark.parametrize(
    "reason,finish",
    [("max_output_tokens", "length"), ("content_filter", "content_filter")],
)
def test_incomplete_takes_precedence_over_tool_finish(reason, finish):
    actual = transform_response(
        "openai_responses",
        "openai_chat",
        {
            "status": "incomplete",
            "incomplete_details": {"reason": reason},
            "output": [call("a")],
        },
    )
    assert actual["choices"][0]["finish_reason"] == finish


@pytest.mark.parametrize(
    "output,extra,expected",
    [
        ([text("hello"), text("world")], {}, "hello\nworld"),
        ([], {"output_text": "fallback"}, "fallback"),
        ([], {}, ""),
    ],
)
def test_text_only_and_empty_response(output, extra, expected):
    actual = transform_response(
        "openai_responses",
        "openai_chat",
        {
            "status": "completed",
            "output": output,
            **extra,
        },
    )
    choice = actual["choices"][0]
    assert choice["message"] == {"role": "assistant", "content": expected}
    assert choice["finish_reason"] == "stop"


def test_real_router_request_returns_all_tools_from_responses_upstream(tmp_path):
    import httpx
    from fastapi.testclient import TestClient
    from app.agent_router.app import create_agent_router_app
    from app.services.agent_router_config import AgentRouterConfigStore

    store = AgentRouterConfigStore(tmp_path)
    store.set_provider(
        "codex",
        {
            "base_url": "https://fixture.example/v1",
            "api_format": "openai_responses",
            "auth_mode": "none",
        },
    )
    captured = []

    def upstream(request):
        captured.append(request)
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "resp-fixture",
                "status": "completed",
                "output": [text("hello"), call("a"), call("b")],
            },
        )

    app = create_agent_router_app(store=store, transport=httpx.MockTransport(upstream))
    with TestClient(app) as client:
        response = client.post(
            "/codex/v1/chat/completions",
            json={
                "model": "fixture",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
            },
        )
        assert response.status_code == 200, response.text
        assert captured[0].url.path == "/v1/responses"
        choice = response.json()["choices"][0]
        assert choice["message"]["content"] == "hello"
        assert [v["id"] for v in choice["message"]["tool_calls"]] == [
            "call_a",
            "call_b",
        ]
        assert choice["finish_reason"] == "tool_calls"
        assert client.get("/health").json()["counters"]["active"] == 0


@pytest.mark.parametrize("source", ["openai_responses", "openai_chat"])
def test_filtered_output_uses_anthropic_native_stop_reason(source):
    if source == "openai_responses":
        body = {
            "status": "incomplete",
            "incomplete_details": {"reason": "content_filter"},
            "output": [text("partial")],
        }
    else:
        body = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "partial"},
                    "finish_reason": "content_filter",
                }
            ]
        }
    actual = transform_response(source, "anthropic", body)
    assert actual["stop_reason"] == "refusal"
    assert actual["content"] == [{"type": "text", "text": "partial"}]
