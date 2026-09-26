"""Model registry, health service and router (selection, fallback, honesty about availability)."""

import httpx
import pytest

from app.core.exceptions import ErrorCode, ModelError
from app.models.health import InMemoryHealthStore, ModelHealthService
from app.models.providers import create_provider
from app.models.providers.base import ChatMessage, ChatRequest, HealthStatus, ModelRole
from app.models.registry import ModelRegistry
from app.models.router import ModelRequirements, ModelRouter


def build(models: dict, behaviour: dict):
    """behaviour: endpoint host -> 'ok' | 'down' | 'empty' | 'wrong_name'."""
    registry = ModelRegistry.from_dict({"models": models})

    def handler(request: httpx.Request) -> httpx.Response:
        mode = behaviour.get(request.url.host, "ok")
        if mode == "down":
            raise httpx.ConnectError("refused", request=request)
        if request.url.path.endswith("/models"):
            served = ["other"] if mode == "wrong_name" else [m["model"] for m in models.values()]
            return httpx.Response(200, json={"data": [{"id": s} for s in served]})
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.5, 0.5]}]})
        content = "" if mode == "empty" else f"answer from {request.url.host}"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    providers = {m.id: create_provider(m, http) for m in registry.all()}
    health = ModelHealthService(registry, providers, InMemoryHealthStore())
    return registry, health, ModelRouter(registry, providers, health)


MODELS = {
    "fast": {"endpoint": "http://fast/v1", "model": "f", "roles": ["fast"], "priority": 40},
    "coder": {"endpoint": "http://coder/v1", "model": "c", "roles": ["coding"], "priority": 60},
    "thinker": {"endpoint": "http://thinker/v1", "model": "t", "roles": ["reasoning"], "priority": 50,
                "context_length": 32768},
    "embed": {"endpoint": "http://embed/v1", "model": "e", "roles": ["embedding"]},
}
REQ = ChatRequest(messages=[ChatMessage(role="user", content="hello")])


async def test_health_reports_only_real_status():
    _, health, _ = build(MODELS, {"coder": "down", "thinker": "wrong_name", "fast": "empty"})
    reports = {r.model_id: r for r in await health.check_all()}
    assert reports["coder"].status == HealthStatus.OFFLINE
    assert reports["coder"].error_code == ErrorCode.MODEL_CONNECTION_ERROR
    assert reports["thinker"].error_code == ErrorCode.MODEL_WRONG_NAME
    assert reports["fast"].error_code == ErrorCode.MODEL_EMPTY_RESPONSE  # HTTP 200 + empty => not online
    assert reports["embed"].status == HealthStatus.ONLINE


async def test_role_based_selection():
    _, _, router = build(MODELS, {})
    assert (await router.select(ModelRequirements(role=ModelRole.CODING))).model_id == "coder"
    assert (await router.select(ModelRequirements(role=ModelRole.FAST, complexity="trivial"))).model_id == "fast"
    assert (await router.select(ModelRequirements(role=ModelRole.REASONING, complexity="complex"))).model_id == "thinker"
    assert (await router.select(ModelRequirements(role=ModelRole.EMBEDDING))).model_id == "embed"


async def test_context_requirement_filters_models():
    _, _, router = build(MODELS, {})
    decision = await router.select(ModelRequirements(role=ModelRole.CODING, min_context=20000))
    assert decision.model_id == "thinker"
    assert decision.notices  # tells the user the dedicated coding model was not used


async def test_fallback_on_failure_is_reported():
    _, health, router = build(MODELS, {"coder": "empty"})
    # coder passes no health check (empty) -> excluded before the call
    result = await router.chat(REQ, ModelRequirements(role=ModelRole.CODING))
    assert result.decision.model_id != "coder"
    assert result.response.content.startswith("answer from")


async def test_runtime_failure_triggers_fallback_and_passive_health():
    registry, health, router = build(MODELS, {})
    await health.check_all()
    # the coder goes down after being healthy
    router.providers["coder"].http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("down", request=r))))
    result = await router.chat(REQ, ModelRequirements(role=ModelRole.CODING))
    assert result.decision.fallback_from == ["coder"]
    assert any("failed" in n for n in result.decision.notices)
    assert (await health.get("coder")).status == HealthStatus.OFFLINE


async def test_no_model_available():
    _, _, router = build({"coder": MODELS["coder"]}, {"coder": "down"})
    with pytest.raises(ModelError) as exc:
        await router.chat(REQ, ModelRequirements(role=ModelRole.CODING))
    assert exc.value.code in (ErrorCode.MODEL_UNAVAILABLE, ErrorCode.MODEL_CONNECTION_ERROR)


async def test_single_model_serves_all_roles():
    _, _, router = build({"only": {"endpoint": "http://only/v1", "model": "o", "roles": ["coding"]}}, {})
    for role in (ModelRole.FAST, ModelRole.REASONING, ModelRole.CODING):
        assert (await router.select(ModelRequirements(role=role))).model_id == "only"


async def test_independence_never_beats_capability():
    _, _, router = build(MODELS, {})
    d = await router.select(ModelRequirements(role=ModelRole.CODING, avoid_models={"coder"}))
    assert d.model_id == "coder"  # the only coding model; the fast model must not be chosen instead
    assert any("same model" in n for n in d.notices)


def test_registry_env_interpolation(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_URL", "http://x/v1")
    monkeypatch.setenv("TEST_CTX", "4096")
    registry = ModelRegistry.from_dict({"models": {
        "a": {"endpoint": "${TEST_MODEL_URL}", "model": "${TEST_MODEL_NAME:-qwen2.5:7b}", "context_length": "${TEST_CTX}"},
        "b": {"endpoint": "${UNSET_URL_FOR_TEST}", "model": "${UNSET_NAME_FOR_TEST}"},
    }})
    a = registry.get("a")
    assert a.endpoint == "http://x/v1" and a.model == "qwen2.5:7b" and a.context_length == 4096
    assert registry.get("b") is None and registry.warnings  # skipped with a warning, not crashed


def test_registry_overrides():
    registry = ModelRegistry.from_dict({"models": {"a": {"endpoint": "http://x", "model": "m", "roles": ["fast"]}}})
    registry.apply_overrides({"a": {"enabled": False, "priority": None}})
    assert registry.get("a").enabled is False and registry.enabled() == []


async def test_light_health_checks_skip_generation():
    calls = {"chat": 0}
    registry = ModelRegistry.from_dict({"models": {"m": {"endpoint": "http://m/v1", "model": "x", "roles": ["fast"]}}})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "x"}]})
        calls["chat"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})

    providers = {m.id: create_provider(m, httpx.AsyncClient(transport=httpx.MockTransport(handler))) for m in registry.all()}
    health = ModelHealthService(registry, providers, InMemoryHealthStore())
    first = await health.check("m", light=True)  # no previous functional result -> full probe
    assert first.status == HealthStatus.ONLINE and calls["chat"] == 1
    second = await health.check("m", light=True)  # recent functional result -> reachability only
    assert second.status == HealthStatus.ONLINE and calls["chat"] == 1
    health.functional_interval_s = 0
    await health.check("m", light=True)  # functional result too old -> real probe again
    assert calls["chat"] == 2
