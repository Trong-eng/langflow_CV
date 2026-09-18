from __future__ import annotations

import builtins
import gc
import importlib.util
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from textwrap import dedent
from types import SimpleNamespace

import pytest

MODULE_NAME = "lfx.custom.component_compilation_cache"


@dataclass(frozen=True)
class _Artifact:
    value: str


def _cache_module():
    module_spec = importlib.util.find_spec(MODULE_NAME)
    assert module_spec is not None, "component compilation cache module must exist"
    from lfx.custom import component_compilation_cache

    return component_compilation_cache


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    module_spec = importlib.util.find_spec(MODULE_NAME)
    if module_spec is None:
        yield
        return
    from lfx.custom import component_compilation_cache

    component_compilation_cache.clear_component_compilation_cache()
    yield
    component_compilation_cache.clear_component_compilation_cache()


def test_cache_module_exists() -> None:
    _cache_module()


def test_same_source_misses_once_then_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache_module()
    monkeypatch.setattr(cache, "_cache_enabled", lambda: True)
    builds = 0

    def build() -> _Artifact:
        nonlocal builds
        builds += 1
        return _Artifact("first")

    first = cache.get_or_build_component_artifact("same source", build)
    second = cache.get_or_build_component_artifact("same source", build)

    assert first is second
    assert builds == 1
    assert cache.component_compilation_cache_stats() == {
        "entries": 1,
        "hits": 1,
        "misses": 1,
        "bypasses": 0,
        "evictions": 0,
        "builds": 1,
    }


def test_different_sources_with_same_class_name_do_not_collide(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache_module()
    monkeypatch.setattr(cache, "_cache_enabled", lambda: True)
    source_a = "class SameComponent(Component):\n    marker = 'a'"
    source_b = "class SameComponent(Component):\n    marker = 'b'"

    artifact_a = cache.get_or_build_component_artifact(source_a, lambda: _Artifact("a"))
    artifact_b = cache.get_or_build_component_artifact(source_b, lambda: _Artifact("b"))

    assert artifact_a.value == "a"
    assert artifact_b.value == "b"
    assert cache.component_compilation_cache_stats()["misses"] == 2


def test_source_update_does_not_reuse_old_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache_module()
    monkeypatch.setattr(cache, "_cache_enabled", lambda: True)

    original = cache.get_or_build_component_artifact("source-v1", lambda: _Artifact("v1"))
    updated = cache.get_or_build_component_artifact("source-v2", lambda: _Artifact("v2"))

    assert updated is not original
    assert updated.value == "v2"


def test_exact_source_is_verified_even_if_digest_collides(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache_module()
    monkeypatch.setattr(cache, "_cache_enabled", lambda: True)
    monkeypatch.setattr(cache, "_source_digest", lambda _source: "0" * 64)

    first = cache.get_or_build_component_artifact("source-a", lambda: _Artifact("a"))
    second = cache.get_or_build_component_artifact("source-b", lambda: _Artifact("b"))

    assert first.value == "a"
    assert second.value == "b"
    assert cache.component_compilation_cache_stats()["misses"] == 2


def test_cache_evicts_least_recently_used_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache_module()
    monkeypatch.setattr(cache, "_cache_enabled", lambda: True)

    for index in range(cache.COMPONENT_COMPILATION_CACHE_MAX_ENTRIES):
        cache.get_or_build_component_artifact(f"source-{index}", lambda index=index: _Artifact(str(index)))
    cache.get_or_build_component_artifact("source-0", lambda: pytest.fail("source-0 should still be cached"))
    cache.get_or_build_component_artifact("overflow", lambda: _Artifact("overflow"))
    rebuilt = cache.get_or_build_component_artifact("source-1", lambda: _Artifact("rebuilt"))

    assert rebuilt.value == "rebuilt"
    stats = cache.component_compilation_cache_stats()
    assert stats["entries"] == cache.COMPONENT_COMPILATION_CACHE_MAX_ENTRIES
    assert stats["evictions"] == 2


def test_clear_removes_entries_and_resets_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache_module()
    monkeypatch.setattr(cache, "_cache_enabled", lambda: True)
    cache.get_or_build_component_artifact("source", lambda: _Artifact("cached"))

    cache.clear_component_compilation_cache()

    assert cache.component_compilation_cache_stats() == {
        "entries": 0,
        "hits": 0,
        "misses": 0,
        "bypasses": 0,
        "evictions": 0,
        "builds": 0,
    }


def test_clear_reloads_process_feature_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache_module()
    from lfx.services import deps

    state = {"enabled": False}
    monkeypatch.setattr(
        deps,
        "get_settings_service",
        lambda: SimpleNamespace(
            settings=SimpleNamespace(component_compilation_cache_enabled=state["enabled"]),
        ),
    )
    cache.clear_component_compilation_cache()
    assert cache._cache_enabled() is False

    state["enabled"] = True
    assert cache._cache_enabled() is False
    cache.clear_component_compilation_cache()
    assert cache._cache_enabled() is True


def test_oversize_source_bypasses_cache_but_still_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache_module()
    monkeypatch.setattr(cache, "_cache_enabled", lambda: True)
    source = "x" * (cache.COMPONENT_COMPILATION_CACHE_MAX_SOURCE_BYTES + 1)
    builds = 0

    def build() -> _Artifact:
        nonlocal builds
        builds += 1
        return _Artifact(str(builds))

    assert cache.get_or_build_component_artifact(source, build).value == "1"
    assert cache.get_or_build_component_artifact(source, build).value == "2"
    assert builds == 2
    assert cache.component_compilation_cache_stats()["bypasses"] == 2


def test_disabled_cache_preserves_uncached_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache_module()
    monkeypatch.setattr(cache, "_cache_enabled", lambda: False)
    builds = 0

    def build() -> _Artifact:
        nonlocal builds
        builds += 1
        return _Artifact(str(builds))

    assert cache.get_or_build_component_artifact("source", build).value == "1"
    assert cache.get_or_build_component_artifact("source", build).value == "2"
    assert cache.component_compilation_cache_stats()["entries"] == 0
    assert cache.component_compilation_cache_stats()["bypasses"] == 2


def test_disabled_lookup_reports_artifact_is_not_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache_module()
    monkeypatch.setattr(cache, "_cache_enabled", lambda: False)

    result = cache.get_or_build_component_artifact_with_status("source", lambda: _Artifact("uncached"))

    assert result.artifact.value == "uncached"
    assert result.retained is False


def test_enabled_lookup_reports_artifact_is_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache_module()
    monkeypatch.setattr(cache, "_cache_enabled", lambda: True)

    miss = cache.get_or_build_component_artifact_with_status("source", lambda: _Artifact("cached"))
    hit = cache.get_or_build_component_artifact_with_status("source", lambda: pytest.fail("cache hit must not rebuild"))

    assert miss.retained is True
    assert hit.retained is True
    assert hit.artifact is miss.artifact


def test_builder_errors_are_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache_module()
    monkeypatch.setattr(cache, "_cache_enabled", lambda: True)
    attempts = 0

    def reject() -> _Artifact:
        nonlocal attempts
        attempts += 1
        message = "invalid source"
        raise ValueError(message)

    for _ in range(2):
        with pytest.raises(ValueError, match="invalid source"):
            cache.get_or_build_component_artifact("invalid", reject)

    assert attempts == 2
    assert cache.component_compilation_cache_stats()["entries"] == 0
    assert cache.component_compilation_cache_stats()["builds"] == 0


def test_generation_change_invalidates_old_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache_module()
    monkeypatch.setattr(cache, "_cache_enabled", lambda: True)
    monkeypatch.setattr(cache, "COMPONENT_COMPILATION_ARTIFACT_GENERATION", 1)
    first = cache.get_or_build_component_artifact("source", lambda: _Artifact("generation-1"))

    monkeypatch.setattr(cache, "COMPONENT_COMPILATION_ARTIFACT_GENERATION", 2)
    second = cache.get_or_build_component_artifact("source", lambda: _Artifact("generation-2"))

    assert first.value == "generation-1"
    assert second.value == "generation-2"
    assert cache.component_compilation_cache_stats()["misses"] == 2


def test_concurrent_same_source_builds_once(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache_module()
    monkeypatch.setattr(cache, "_cache_enabled", lambda: True)
    build_count = 0
    count_lock = threading.Lock()

    def build() -> _Artifact:
        nonlocal build_count
        with count_lock:
            build_count += 1
        time.sleep(0.01)
        return _Artifact("shared")

    with ThreadPoolExecutor(max_workers=8) as pool:
        artifacts = list(pool.map(lambda _index: cache.get_or_build_component_artifact("source", build), range(8)))

    assert build_count == 1
    assert len({id(artifact) for artifact in artifacts}) == 1
    assert cache.component_compilation_cache_stats()["hits"] == 7


RUNTIME_ISOLATION_SOURCE = dedent("""
    from lfx.custom import Component
    from lfx.io import MessageTextInput, Output

    module_values = []

    class CacheIsolationComponent(Component):
        inputs = [MessageTextInput(name="value", display_name="Value")]
        outputs = [Output(name="output", display_name="Output", method="run")]
        class_values = []

        def __init__(self, **kwargs):
            self.parameters_seen = kwargs.get("_parameters", {}).copy()
            self.user_seen = kwargs.get("_user_id")
            self.vertex_seen = kwargs.get("_vertex")

        def run(self):
            return self.parameters_seen["value"]
    """)


def _enable_compilation_cache(monkeypatch: pytest.MonkeyPatch):
    cache = _cache_module()
    monkeypatch.setattr(cache, "_cache_enabled", lambda: True)
    cache.clear_component_compilation_cache()
    return cache


def test_eval_reuses_source_artifact_but_creates_fresh_class_and_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _enable_compilation_cache(monkeypatch)
    from lfx.custom.eval import eval_custom_component_code

    first_class = eval_custom_component_code(RUNTIME_ISOLATION_SOURCE)
    second_class = eval_custom_component_code(RUNTIME_ISOLATION_SOURCE)

    assert first_class is not second_class
    assert first_class.run.__globals__ is not second_class.run.__globals__
    assert cache.component_compilation_cache_stats()["misses"] == 1
    assert cache.component_compilation_cache_stats()["hits"] == 1


def test_mutable_class_attributes_and_source_globals_remain_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_compilation_cache(monkeypatch)
    from lfx.custom.eval import eval_custom_component_code

    first_class = eval_custom_component_code(RUNTIME_ISOLATION_SOURCE)
    second_class = eval_custom_component_code(RUNTIME_ISOLATION_SOURCE)
    first_class.class_values.append("first")
    first_class.run.__globals__["module_values"].append("first")
    first_class.inputs.append("first-only")
    first_class.outputs.append("first-only")

    assert second_class.class_values == []
    assert second_class.run.__globals__["module_values"] == []
    assert "first-only" not in second_class.inputs
    assert "first-only" not in second_class.outputs


def test_runtime_decorators_and_helper_definitions_execute_on_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_compilation_cache(monkeypatch)
    from lfx.custom.eval import eval_custom_component_code

    marker_name = "_lfx_component_cache_runtime_events"
    monkeypatch.setattr(builtins, marker_name, [], raising=False)
    source = dedent(f"""
        import builtins
        from lfx.custom import Component

        def decorate(component_class):
            builtins.{marker_name}.append(component_class.__name__)
            return component_class

        @decorate
        class DecoratedCacheComponent(Component):
            pass
        """)

    eval_custom_component_code(source)
    first_call_events = len(getattr(builtins, marker_name))
    eval_custom_component_code(source)
    second_call_events = len(getattr(builtins, marker_name)) - first_call_events

    assert first_call_events > 0
    assert second_call_events == first_call_events


def test_constructor_runs_each_time_with_current_parameters_and_user(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_compilation_cache(monkeypatch)
    from lfx.interface.initialize import loading

    first_vertex = SimpleNamespace(
        vertex_type="CacheIsolationComponent",
        base_type="component",
        params={"code": RUNTIME_ISOLATION_SOURCE, "value": "first"},
        id="first",
    )
    second_vertex = SimpleNamespace(
        vertex_type="CacheIsolationComponent",
        base_type="component",
        params={"code": RUNTIME_ISOLATION_SOURCE, "value": "second"},
        id="second",
    )

    first, _ = loading.instantiate_class(first_vertex, user_id="user-one")
    second, _ = loading.instantiate_class(second_vertex, user_id="user-two")

    assert first.parameters_seen == {"value": "first"}
    assert second.parameters_seen == {"value": "second"}
    assert first.user_seen == "user-one"
    assert second.user_seen == "user-two"
    assert first.run() == "first"
    assert second.run() == "second"


def test_concurrent_eval_and_instances_do_not_leak_values(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _enable_compilation_cache(monkeypatch)
    from lfx.custom.eval import eval_custom_component_code

    def run(index: int) -> tuple[int, str]:
        component_class = eval_custom_component_code(RUNTIME_ISOLATION_SOURCE)
        instance = component_class(_parameters={"value": f"value-{index}"}, _user_id=f"user-{index}")
        return index, f"{instance.user_seen}:{instance.run()}"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(pool.map(run, range(24)))

    assert results == {index: f"user-{index}:value-{index}" for index in range(24)}
    assert cache.component_compilation_cache_stats()["builds"] == 1


def test_cache_does_not_retain_completed_vertex(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_compilation_cache(monkeypatch)
    from lfx.custom.eval import eval_custom_component_code

    class Vertex:
        pass

    vertex = Vertex()
    vertex_reference = weakref.ref(vertex)
    component_class = eval_custom_component_code(RUNTIME_ISOLATION_SOURCE)
    instance = component_class(_parameters={"value": "done"}, _vertex=vertex)
    del instance
    del vertex
    gc.collect()

    assert vertex_reference() is None


def test_invalid_source_is_rejected_again_instead_of_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _enable_compilation_cache(monkeypatch)
    from lfx.custom.eval import eval_custom_component_code

    for _ in range(2):
        with pytest.raises(ValueError, match="Invalid Python code"):
            eval_custom_component_code("class InvalidComponent(Component) this is invalid")

    stats = cache.component_compilation_cache_stats()
    assert stats["entries"] == 0
    assert stats["builds"] == 0
    assert stats["misses"] == 2


def test_active_annotation_is_rejected_again_instead_of_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _enable_compilation_cache(monkeypatch)
    from lfx.custom.eval import eval_custom_component_code

    source = dedent("""
        from lfx.custom import Component

        class UnsafeAnnotationComponent(Component):
            def run(self) -> print("active"):
                return None
        """)
    for _ in range(2):
        with pytest.raises(ValueError, match="active expression"):
            eval_custom_component_code(source)

    assert cache.component_compilation_cache_stats()["entries"] == 0


def test_trusted_source_resolution_still_precedes_cache_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_compilation_cache(monkeypatch)
    from lfx.custom.eval import eval_custom_component_code
    from lfx.interface.initialize import loading
    from lfx.utils import flow_validation

    stored_source = RUNTIME_ISOLATION_SOURCE.replace("CacheIsolationComponent", "StoredComponent")
    trusted_source = RUNTIME_ISOLATION_SOURCE.replace("CacheIsolationComponent", "TrustedComponent").replace(
        "module_values = []", "module_values = ['trusted']"
    )
    eval_custom_component_code(stored_source)
    monkeypatch.setattr(flow_validation, "resolve_trusted_code_for_build", lambda *_args, **_kwargs: trusted_source)
    vertex = SimpleNamespace(
        vertex_type="TrustedComponent",
        base_type="component",
        params={"code": stored_source, "value": "resolved"},
        id="trusted",
    )

    instance, _ = loading.instantiate_class(vertex, user_id="current-user")

    assert type(instance).__name__ == "TrustedComponent"
    assert instance.run.__globals__["module_values"] == ["trusted"]


def test_policy_tightening_after_warm_cache_still_blocks_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_compilation_cache(monkeypatch)
    from lfx.custom.eval import eval_custom_component_code
    from lfx.interface.initialize import loading
    from lfx.utils import flow_validation

    eval_custom_component_code(RUNTIME_ISOLATION_SOURCE)

    class PolicyDeniedError(ValueError):
        pass

    def deny(*_args, **_kwargs):
        message = "policy tightened"
        raise PolicyDeniedError(message)

    monkeypatch.setattr(flow_validation, "resolve_trusted_code_for_build", deny)
    vertex = SimpleNamespace(
        vertex_type="CacheIsolationComponent",
        base_type="component",
        params={"code": RUNTIME_ISOLATION_SOURCE, "value": "blocked"},
        id="blocked",
    )

    with pytest.raises(PolicyDeniedError, match="policy tightened"):
        loading.instantiate_class(vertex, user_id="current-user")


def test_imports_helpers_and_inherited_components_keep_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_compilation_cache(monkeypatch)
    from lfx.custom.eval import eval_custom_component_code

    source = dedent("""
        import math
        from lfx.custom import Component

        def helper(value):
            return math.floor(value) + 1

        class LocalBase:
            def inherited(self):
                return "base"

        class CompatibleCacheComponent(LocalBase, Component):
            def run(self):
                return f"{self.inherited()}:{helper(2.5)}"
        """)

    first = eval_custom_component_code(source)()
    second = eval_custom_component_code(source)()

    assert first.run() == "base:3"
    assert second.run() == "base:3"


def test_prepared_artifact_cannot_be_used_with_different_source(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_compilation_cache(monkeypatch)
    from lfx.custom.validate import create_class, prepare_component_compilation_artifact

    source_a = RUNTIME_ISOLATION_SOURCE.replace("module_values = []", "module_values = ['a']")
    source_b = RUNTIME_ISOLATION_SOURCE.replace("module_values = []", "module_values = ['b']")
    artifact = prepare_component_compilation_artifact(source_a)

    with pytest.raises(ValueError, match="artifact source does not match"):
        create_class(source_b, artifact.class_name, artifact=artifact)
