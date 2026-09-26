"""Provider boundary tests: every failure mode must surface as a specific, user-explainable error."""

import json

import httpx
import pytest

from app.core.exceptions import ErrorCode, ModelError
from app.models.providers import create_provider
from app.models.providers.base import ChatMessage, ChatRequest, ModelConfig, ThinkStreamFilter, strip_reasoning


def provider_with(handler, **config):
    cfg = ModelConfig(id="m", endpoint="http://model.test/v1", model="served-model", **config)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return create_provider(cfg, client)


def chat_response(content, **extra):
    return httpx.Response(200, json={"model": "served-model", "choices": [
        {"index": 0, "message": {"role": "assistant", "content": content, **extra}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2}})


REQ = ChatRequest(messages=[ChatMessage(role="user", content="hi")])


async def test_success_parses_content_and_usage():
    p = provider_with(lambda r: chat_response("Hello!"))
    r = await p.chat(REQ)
    assert r.content == "Hello!" and r.usage.completion_tokens == 2


async def test_http_200_with_empty_content_is_failure():
    p = provider_with(lambda r: chat_response(""))
    with pytest.raises(ModelError) as exc:
        await p.chat(REQ)
    assert exc.value.code == ErrorCode.MODEL_EMPTY_RESPONSE


async def test_reasoning_only_output_is_empty_with_hint():
    p = provider_with(lambda r: chat_response(None, reasoning_content="long thoughts"))
    with pytest.raises(ModelError) as exc:
        await p.chat(REQ)
    assert exc.value.code == ErrorCode.MODEL_EMPTY_RESPONSE
    assert "reasoning" in (exc.value.hint or "")


async def test_think_tags_are_stripped():
    p = provider_with(lambda r: chat_response("<think>secret chain</think>Final answer"))
    r = await p.chat(REQ)
    assert r.content == "Final answer" and r.had_reasoning


async def test_invalid_json():
    p = provider_with(lambda r: httpx.Response(200, text="<html>proxy error</html>"))
    with pytest.raises(ModelError) as exc:
        await p.chat(REQ)
    assert exc.value.code == ErrorCode.MODEL_INVALID_RESPONSE


async def test_missing_choices():
    p = provider_with(lambda r: httpx.Response(200, json={"object": "chat.completion"}))
    with pytest.raises(ModelError) as exc:
        await p.chat(REQ)
    assert exc.value.code == ErrorCode.MODEL_INVALID_RESPONSE


@pytest.mark.parametrize("status,body,code", [
    (401, {"error": "bad key"}, ErrorCode.MODEL_AUTH_ERROR),
    (403, {"error": "forbidden"}, ErrorCode.MODEL_AUTH_ERROR),
    (404, {"error": {"message": "The model `x` does not exist."}}, ErrorCode.MODEL_WRONG_NAME),
    (404, {"detail": "Not Found"}, ErrorCode.MODEL_HTTP_ERROR),
    (500, {"error": "boom"}, ErrorCode.MODEL_HTTP_ERROR),
    (400, {"error": {"message": "request (5000 tokens) exceeds the available context size (4096 tokens)"}},
     ErrorCode.MODEL_CONTEXT_LENGTH_EXCEEDED),
])
async def test_http_errors(status, body, code):
    p = provider_with(lambda r: httpx.Response(status, json=body))
    with pytest.raises(ModelError) as exc:
        await p.chat(REQ)
    assert exc.value.code == code


async def test_context_error_explains_real_server_window():
    body = {"error": {"message": "exceeds the available context size (4096 tokens)", "n_ctx": 4096}}
    p = provider_with(lambda r: httpx.Response(400, json=body), context_length=16384)
    with pytest.raises(ModelError) as exc:
        await p.chat(REQ)
    assert "4096" in exc.value.hint


async def test_connection_refused_and_timeout():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    def slow(request):
        raise httpx.ReadTimeout("timeout", request=request)

    with pytest.raises(ModelError) as exc:
        await provider_with(refuse).chat(REQ)
    assert exc.value.code == ErrorCode.MODEL_CONNECTION_ERROR and exc.value.retryable
    with pytest.raises(ModelError) as exc:
        await provider_with(slow).chat(REQ)
    assert exc.value.code == ErrorCode.MODEL_TIMEOUT


def sse(*chunks, done=True):
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + ("data: [DONE]\n\n" if done else "")
    return lambda r: httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


async def test_streaming_success_and_think_filter():
    p = provider_with(sse(
        {"choices": [{"delta": {"content": "<thi"}}]},
        {"choices": [{"delta": {"content": "nk>hidden</think>Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
    ))
    text = "".join([c.content async for c in p.stream_chat(REQ)])
    assert text == "Hello"


async def test_streaming_empty_and_truncated():
    empty = provider_with(sse({"choices": [{"delta": {"content": ""}, "finish_reason": "stop"}]}))
    with pytest.raises(ModelError) as exc:
        _ = [c async for c in empty.stream_chat(REQ)]
    assert exc.value.code == ErrorCode.MODEL_EMPTY_RESPONSE
    truncated = provider_with(sse({"choices": [{"delta": {"content": "partial"}}]}, done=False))
    with pytest.raises(ModelError) as exc:
        _ = [c async for c in truncated.stream_chat(REQ)]
    assert exc.value.code == ErrorCode.MODEL_STREAM_ERROR
    malformed = provider_with(lambda r: httpx.Response(200, text="data: {not json\n\n"))
    with pytest.raises(ModelError) as exc:
        _ = [c async for c in malformed.stream_chat(REQ)]
    assert exc.value.code == ErrorCode.MODEL_STREAM_ERROR


async def test_embeddings_validation():
    ok = provider_with(lambda r: httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]}),
                       embedding_dimensions=2)
    assert (await ok.embed(["a"])).dimensions == 2
    wrong_dims = provider_with(lambda r: httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}]}),
                               embedding_dimensions=2)
    with pytest.raises(ModelError):
        await wrong_dims.embed(["a"])
    empty = provider_with(lambda r: httpx.Response(200, json={"data": [{"index": 0, "embedding": []}]}))
    with pytest.raises(ModelError) as exc:
        await empty.embed(["a"])
    assert exc.value.code == ErrorCode.MODEL_EMPTY_RESPONSE


async def test_ollama_native_provider():
    def handler(request: httpx.Request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "served-model:latest"}]})
        if request.url.path == "/api/chat":
            body = json.loads(request.content)
            assert body["options"]["num_ctx"] == 8192  # context window is honoured
            return httpx.Response(200, json={"model": "served-model", "message": {"content": "hi"},
                                             "done": True, "prompt_eval_count": 4, "eval_count": 1})
        if request.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[1.0, 0.0]]})
        return httpx.Response(404)

    cfg = ModelConfig(id="o", provider="ollama", endpoint="http://ollama.test", model="served-model", context_length=8192)
    p = create_provider(cfg, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert p.name_matches(await p.list_models())
    assert (await p.chat(REQ)).content == "hi"
    assert (await p.embed(["x"])).dimensions == 2


def test_strip_reasoning_variants():
    assert strip_reasoning("<think>a</think>b") == ("b", True)
    assert strip_reasoning("reasoning...</think>answer") == ("answer", True)
    assert strip_reasoning("plain") == ("plain", False)
    f = ThinkStreamFilter()
    out = f.feed("x<th") + f.feed("ink>y</th") + f.feed("ink>z") + f.flush()
    assert out == "xz"
