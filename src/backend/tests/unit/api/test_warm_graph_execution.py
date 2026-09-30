"""Focused import and execution-mode contracts for warm graph copies."""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from langflow.api import warm_graph
from langflow.processing.process import process_tweaks
from langflow.services.warm_registry.service import flow_version


class _FakeVertex:
    def __init__(self, raw_params: dict, *, load_from_db_fields: list[str] | None = None) -> None:
        self.raw_params = raw_params.copy()
        self.params = raw_params.copy()
        template = {key: {"value": value} for key, value in raw_params.items()}
        for field_name in load_from_db_fields or []:
            template[field_name]["load_from_db"] = True
        self.full_data = {
            "data": {
                "node": {
                    "template": template,
                }
            }
        }
        self.load_from_db_fields = list(load_from_db_fields or [])
        self.updated_raw_params = False

    def update_raw_params(self, new_params: dict, *, overwrite: bool = False) -> None:
        assert overwrite is True
        self.raw_params.update(new_params)
        self.params = self.raw_params.copy()
        self.updated_raw_params = True


class _FakeGraph:
    def __init__(self, vertices: list[_FakeVertex]) -> None:
        self.vertices = vertices
        self.user_id = None
        self.session_id = None
        self.constructor_stream: bool | None = None
        self.constructor_template_stream: dict | None = None
        self.has_session_id_vertices: list[str] = []

    def get_vertex(self, _vertex_id: str):
        return None

    def copy_for_run(self, *, user_id: str | None, before_instantiate=None):
        copied = deepcopy(self)
        copied.user_id = user_id
        if before_instantiate is not None:
            before_instantiate(copied)
        copied.constructor_stream = copied.vertices[0].params.get("stream") if copied.vertices else None
        if copied.vertices:
            copied.constructor_template_stream = copied.vertices[0].full_data["data"]["node"]["template"].get("stream")
        return copied


def _grouped_stream_graph(*, expose_stream: bool):
    from lfx.graph import Graph

    stream_field = {
        "name": "stream",
        "type": "bool",
        "value": True,
        "list": False,
        "show": True,
        "advanced": False,
    }
    child = {
        "id": "child-1",
        "type": "genericNode",
        "data": {
            "id": "child-1",
            "type": "Generic",
            "node": {
                "template": {"_type": "Generic", "stream": stream_field},
                "base_classes": [],
                "display_name": "Child",
                "outputs": [],
            },
        },
    }
    group_template = {}
    if expose_stream:
        group_template["stream"] = {
            **stream_field,
            "proxy": {"field": "stream", "id": "child-1"},
        }
    group = {
        "id": "group-1",
        "type": "genericNode",
        "data": {
            "id": "group-1",
            "type": "Group",
            "node": {
                "template": group_template,
                "flow": {"data": {"nodes": [child], "edges": []}},
            },
        },
    }
    graph = Graph(flow_id="flow-id", instantiate_components=False)
    graph.add_nodes_and_edges([group], [])
    return graph


def test_warm_graph_has_a_clean_import_path() -> None:
    """Importing the low-level helper must not initialize the v1 router package."""
    repo_root = Path(__file__).resolve().parents[5]
    python_path = os.pathsep.join(
        [
            str(repo_root / "src" / "backend" / "base"),
            str(repo_root / "src" / "lfx" / "src"),
            os.environ.get("PYTHONPATH", ""),
        ]
    )
    env = {**os.environ, "PYTHONPATH": python_path}
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            ("import sys; import langflow.api.warm_graph; assert 'langflow.api.v1' not in sys.modules; print('clean')"),
        ],
        cwd=repo_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "clean"


@pytest.mark.parametrize(("stream", "stored_value"), [(True, False), (False, True)])
async def test_warm_deepcopy_matches_cold_implicit_stream_tweak_without_mutating_template(
    monkeypatch: pytest.MonkeyPatch,
    stream,
    stored_value,
) -> None:
    """Warm copies force the run mode just as ``process_tweaks`` does cold."""
    from langflow.services import deps
    from langflow.services.warm_registry import service as registry_service

    template = _FakeGraph(
        [
            _FakeVertex({"stream": stored_value}, load_from_db_fields=["stream"]),
            _FakeVertex({"temperature": 0.2}),
            _FakeVertex({"stream": stored_value}),
        ]
    )
    registry = SimpleNamespace(get=lambda _flow_id: (template, "v1"))
    monkeypatch.setattr(warm_graph, "is_warm_registry_enabled", lambda _settings: True)
    monkeypatch.setattr(deps, "get_settings_service", lambda: SimpleNamespace(settings=SimpleNamespace()))
    monkeypatch.setattr(registry_service, "get_warm_registry", lambda: registry)

    graph = await warm_graph.warm_deepcopy(
        "flow-id",
        expected_version="v1",
        user_id="user-id",
        session_id="session-id",
        stream=stream,
    )

    cold_payload = {
        "nodes": [
            {
                "id": "model",
                "data": {
                    "node": {
                        "display_name": "Model",
                        "template": {
                            "stream": {
                                "type": "bool",
                                "show": True,
                                "value": stored_value,
                                "load_from_db": True,
                            }
                        },
                    }
                },
            }
        ],
        "edges": [],
    }
    cold_result = process_tweaks(deepcopy(cold_payload), {}, stream=stream)
    cold_stream_field = cold_result["nodes"][0]["data"]["node"]["template"]["stream"]

    assert graph is not None
    assert graph is not template
    assert graph.vertices[0].params["stream"] is cold_stream_field["value"] is stream
    assert graph.constructor_stream is stream
    assert graph.constructor_template_stream["value"] is cold_stream_field["value"] is stream
    assert graph.constructor_template_stream["load_from_db"] is cold_stream_field["load_from_db"] is False
    assert "stream" not in graph.vertices[0].load_from_db_fields
    assert graph.vertices[0].updated_raw_params is True
    assert graph.vertices[1].params == {"temperature": 0.2}
    assert "load_from_db" not in graph.vertices[2].full_data["data"]["node"]["template"]["stream"]
    assert graph.user_id == "user-id"
    assert graph.session_id == "session-id"

    # Only the request-local deepcopy changes; the shared registry template stays pristine.
    assert template.vertices[0].params["stream"] is stored_value
    assert template.vertices[0].load_from_db_fields == ["stream"]
    assert template.vertices[0].updated_raw_params is False


@pytest.mark.parametrize(("expose_stream", "expected"), [(False, True), (True, False)])
def test_warm_stream_tweak_matches_cold_group_proxy_scope(monkeypatch, expose_stream, expected) -> None:
    """Hidden grouped fields stay persisted; exposed proxies receive the tweak."""
    from lfx.graph import Graph

    monkeypatch.setattr(Graph, "_instantiate_components_in_vertices", lambda _graph: None)
    template = _grouped_stream_graph(expose_stream=expose_stream)
    run_graph = template.copy_for_run(
        user_id="caller-id",
        before_instantiate=lambda graph: warm_graph._apply_implicit_stream_tweak(graph, stream=False),
    )

    assert run_graph.get_vertex("child-1").raw_params["stream"] is expected
    assert template.get_vertex("child-1").raw_params["stream"] is True


@pytest.fixture
def warm_chat_input(monkeypatch, tmp_path):
    """A real lazy registry template with explicit, non-auto-bound inputs."""
    from langflow.services.warm_registry import service as registry_service
    from lfx.components.input_output import ChatInput
    from lfx.graph import Graph
    from lfx.services.storage.local import LocalStorageService

    storage = LocalStorageService(None, SimpleNamespace(settings=SimpleNamespace(config_dir=str(tmp_path))))
    monkeypatch.setattr("lfx.graph.vertex.param_handler.get_storage_service", lambda: storage)
    flow_id = uuid4()
    node = ChatInput(_id="ChatInput-Q4OPx").to_frontend_node()
    fields = node["data"]["node"]["template"]
    for field in fields.values():
        if isinstance(field, dict) and field.get("type") == "str" and not field.get("value"):
            field["value"] = "persisted"
    fields["files"].update(file_path=[f"{flow_id}/original.png"], load_from_db=True)
    payload = {"nodes": [node], "edges": []}
    flow = SimpleNamespace(
        id=flow_id, data=payload, updated_at=datetime(2026, 9, 28, tzinfo=timezone.utc), storage_root=tmp_path
    )
    template = Graph.from_payload(deepcopy(payload), flow_id=str(flow.id), instantiate_components=False)
    registry = SimpleNamespace(get=lambda _flow_id: (template, flow_version(flow.updated_at)))
    monkeypatch.setattr(warm_graph, "is_warm_registry_enabled", lambda _settings: True)
    monkeypatch.setattr(registry_service, "get_warm_registry", lambda: registry)
    return flow, template


@pytest.mark.parametrize("files", ["image.png", ["image.png", "second.png"], []])
async def test_warm_chat_input_upload_matches_cold_before_instantiation(warm_chat_input, monkeypatch, files):
    """An upload must replace file_path and its DB marker before constructors see it."""
    from langflow.api.v1.schemas import SimplifiedAPIRequest
    from lfx.graph import Graph

    flow, template = warm_chat_input
    original_payload = deepcopy(flow.data)
    original_template = deepcopy(template.raw_graph_data)
    scoped_files = f"{flow.id}/{files}" if isinstance(files, str) else [f"{flow.id}/{name}" for name in files]
    tweaks = {"ChatInput-Q4OPx": {"files": scoped_files}}
    request = SimplifiedAPIRequest(tweaks=deepcopy(tweaks), session_id="upload-session")
    constructor_files = []
    instantiate = Graph._instantiate_components_in_vertices

    def capture_constructor_inputs(graph):
        vertex = graph.get_vertex("ChatInput-Q4OPx")
        constructor_files.append((deepcopy(vertex.raw_params["files"]), list(vertex.load_from_db_fields)))
        instantiate(graph)

    monkeypatch.setattr(Graph, "_instantiate_components_in_vertices", capture_constructor_inputs)
    graph = await warm_graph.try_warm_run_graph(flow, request, user_id="upload-user", context=None)

    assert graph is not None
    cold_payload = process_tweaks(deepcopy(flow.data), deepcopy(tweaks))
    cold = Graph.from_payload(cold_payload, flow_id=str(flow.id), user_id="upload-user")
    vertex = graph.get_vertex("ChatInput-Q4OPx")
    cold_vertex = cold.get_vertex("ChatInput-Q4OPx")
    expected_files = [
        str(flow.storage_root / str(flow.id) / name) for name in ([files] if isinstance(files, str) else files)
    ]
    assert vertex.raw_params["files"] == cold_vertex.raw_params["files"] == expected_files
    assert constructor_files == [(expected_files, []), (expected_files, [])]
    assert "files" not in vertex.load_from_db_fields
    assert graph.raw_graph_data["nodes"][0]["data"]["node"]["template"]["files"]["load_from_db"] is False
    assert graph.user_id == "upload-user"
    assert graph.session_id == "upload-session"
    assert flow.data == original_payload
    assert template.raw_graph_data == original_template
    assert request.tweaks.model_dump() == tweaks
    if isinstance(files, list):
        graph.raw_graph_data["nodes"][0]["data"]["node"]["template"]["files"]["file_path"].append("run-only.png")
        assert request.tweaks.model_dump() == tweaks


@pytest.mark.parametrize("allowed_upload", [True, False])
async def test_warm_cache_miss_with_user_owned_saved_file_falls_back_without_weakening_scope(
    monkeypatch, tmp_path, allowed_upload
):
    from langflow.api.v1.schemas import SimplifiedAPIRequest
    from langflow.services.warm_registry import reconcile, service
    from lfx.components.input_output import ChatInput
    from lfx.graph import Graph
    from lfx.services.storage.local import LocalStorageService
    from lfx.utils.file_path_security import StorageNamespaceError

    storage = LocalStorageService(None, SimpleNamespace(settings=SimpleNamespace(config_dir=str(tmp_path))))
    monkeypatch.setattr("lfx.graph.vertex.param_handler.get_storage_service", lambda: storage)
    user_id = str(uuid4())
    node = ChatInput(_id="ChatInput-upload").to_frontend_node()
    fields = node["data"]["node"]["template"]
    for field in fields.values():
        if isinstance(field, dict) and field.get("type") == "str" and not field.get("value"):
            field["value"] = "persisted"
    fields["files"]["file_path"] = [f"{user_id}/old.png"]
    flow = SimpleNamespace(
        id=uuid4(),
        name="upload",
        data={"nodes": [node], "edges": []},
        updated_at=datetime(2026, 9, 29, tzinfo=timezone.utc),
    )
    original = deepcopy(flow.data)
    namespace = user_id if allowed_upload else str(uuid4())
    request = SimplifiedAPIRequest(tweaks={"ChatInput-upload": {"files": [f"{namespace}/new.png"]}})
    registry = service.WarmGraphRegistry()
    monkeypatch.setattr(service, "get_warm_registry", lambda: registry)
    monkeypatch.setattr(warm_graph, "is_warm_registry_enabled", lambda _settings: True)

    async def warm_saved_flow(flow_id):
        # Only replace the DB fetch; use the real template builder and failed-revision handling.
        await registry.add(flow_id, flow.name, flow.data, flow_version(flow.updated_at))
        return registry.get(flow_id)

    monkeypatch.setattr(reconcile, "warm_one", warm_saved_flow)
    for _ in range(2):
        assert await warm_graph.try_warm_run_graph(flow, request, user_id=user_id, context=None) is None
        payload = process_tweaks(deepcopy(flow.data), request.tweaks)
        if allowed_upload:
            graph = Graph.from_payload(payload, flow_id=str(flow.id), user_id=user_id)
            assert graph.get_vertex("ChatInput-upload").raw_params["files"] == [str(tmp_path / user_id / "new.png")]
        else:
            with pytest.raises(StorageNamespaceError):
                Graph.from_payload(payload, flow_id=str(flow.id), user_id=user_id)
    assert flow.data == original


async def test_warm_chat_input_uploads_are_isolated_between_requests(warm_chat_input):
    """Uploading a second file cannot leak into another run or the lazy template."""
    from langflow.api.v1.schemas import SimplifiedAPIRequest

    flow, template = warm_chat_input
    runs = []
    for index in range(2):
        request = SimplifiedAPIRequest(tweaks={"ChatInput-Q4OPx": {"files": [f"{flow.id}/{index}.png"]}})
        runs.append(await warm_graph.try_warm_run_graph(flow, request, user_id=f"user-{index}", context=None))

    assert all(run is not None for run in runs)
    runs[1].get_vertex("ChatInput-Q4OPx").raw_params["files"].append("only-second.png")
    assert runs[0].get_vertex("ChatInput-Q4OPx").raw_params["files"] == [
        str(flow.storage_root / str(flow.id) / "0.png")
    ]
    assert runs[0].user_id == "user-0"
    assert runs[1].user_id == "user-1"
    assert template.get_vertex("ChatInput-Q4OPx").raw_params["files"] == [
        str(flow.storage_root / str(flow.id) / "original.png")
    ]
    assert template.raw_graph_data["nodes"][0]["data"]["node"]["template"]["files"]["load_from_db"] is True


async def test_warm_upload_preserves_file_namespace_enforcement(warm_chat_input):
    from langflow.api.v1.schemas import SimplifiedAPIRequest
    from lfx.graph import Graph
    from lfx.utils.file_path_security import StorageNamespaceError

    flow, _template = warm_chat_input
    request = SimplifiedAPIRequest(tweaks={"ChatInput-Q4OPx": {"files": "other-user/private.png"}})
    with pytest.raises(StorageNamespaceError):
        Graph.from_payload(process_tweaks(deepcopy(flow.data), request.tweaks), flow_id=str(flow.id), user_id="caller")
    with pytest.raises(StorageNamespaceError):
        await warm_graph.try_warm_run_graph(flow, request, user_id="caller", context=None)


@pytest.mark.parametrize(
    "tweaks",
    [
        {"ChatInput-Q4OPx": {"input_value": "changed"}},
        {"ChatInput-Q4OPx": {"files": "image.png", "input_value": "changed"}},
        {"ChatInput-Q4OPx": {"files": {"file_path": "image.png"}}},
        {"ChatInput-Q4OPx": {"files": ["image.png", None]}},
        {"ChatInput-Q4OPx": {"files": None}},
        {"Chat Input": {"files": "image.png"}},
        {"unknown": {"files": "image.png"}},
        {"files": "image.png"},
    ],
)
async def test_warm_upload_keeps_unsupported_tweaks_on_cold_path(warm_chat_input, tweaks):
    from langflow.api.v1.schemas import SimplifiedAPIRequest

    flow, _template = warm_chat_input
    request = SimplifiedAPIRequest(tweaks=tweaks)
    assert await warm_graph.try_warm_run_graph(flow, request, user_id="u", context=None) is None


@pytest.mark.parametrize("shape", ["other_component", "group", "proxy", "duplicate_id", "non_file"])
async def test_warm_upload_requires_an_unambiguous_standard_file_input(warm_chat_input, shape):
    from langflow.api.v1.schemas import SimplifiedAPIRequest

    flow, _template = warm_chat_input
    node = flow.data["nodes"][0]
    if shape == "other_component":
        node["data"]["type"] = "OtherInput"
    elif shape == "group":
        node["data"]["node"]["flow"] = {"data": {"nodes": [], "edges": []}}
    elif shape == "proxy":
        node["data"]["node"]["template"]["files"]["proxy"] = {"id": "child", "field": "files"}
    elif shape == "duplicate_id":
        flow.data["nodes"].append(deepcopy(node))
    else:
        node["data"]["node"]["template"]["files"]["type"] = "str"
    request = SimplifiedAPIRequest(tweaks={"ChatInput-Q4OPx": {"files": "image.png"}})
    assert await warm_graph.try_warm_run_graph(flow, request, user_id="u", context=None) is None


@pytest.mark.parametrize("gate", ["context", "hitl"])
async def test_warm_upload_preserves_context_and_hitl_fallback(warm_chat_input, gate):
    from langflow.api.v1.schemas import SimplifiedAPIRequest

    flow, _template = warm_chat_input
    if gate == "hitl":
        flow.data["nodes"][0]["data"]["node"]["template"]["tools_metadata"] = {
            "value": [{"approval_actions": ["execute"]}]
        }
    request = SimplifiedAPIRequest(tweaks={"ChatInput-Q4OPx": {"files": "image.png"}})
    context = {} if gate == "context" else None
    assert await warm_graph.try_warm_run_graph(flow, request, user_id="u", context=context) is None


@pytest.mark.parametrize("policy", ["off", "declared"])
async def test_warm_upload_enforces_the_current_tweak_policy(warm_chat_input, monkeypatch, policy):
    from langflow.api.v1.schemas import SimplifiedAPIRequest
    from lfx.exceptions.tweaks import TweakRefusedError

    flow, template = warm_chat_input
    for payload in (flow.data, template.raw_graph_data):
        payload["nodes"][0]["data"]["node"]["template"]["input_value"]["api_editable"] = True
    original_template = deepcopy(template.raw_graph_data)
    monkeypatch.setattr("lfx.processing.process._resolve_tweak_policy", lambda: policy)
    request = SimplifiedAPIRequest(tweaks={"ChatInput-Q4OPx": {"files": "upload/image.png"}})
    with pytest.raises(TweakRefusedError) as cold_error:
        process_tweaks(deepcopy(flow.data), request.tweaks)
    with pytest.raises(TweakRefusedError) as warm_error:
        await warm_graph.try_warm_run_graph(flow, request, user_id="u", context=None)
    assert warm_error.value.refused == cold_error.value.refused == ["files"]
    assert str(warm_error.value) == str(cold_error.value)
    assert template.raw_graph_data == original_template


@pytest.fixture
def warm_chat_with_global_candidates(warm_chat_input, monkeypatch):
    """Use the real default-binding helper, replacing only its DB boundary."""
    from langflow.api.v1 import global_variable_defaults

    flow, template = warm_chat_input
    for payload in (flow.data, template.raw_graph_data):
        payload["nodes"][0]["data"]["node"]["template"]["input_value"]["value"] = ""

    bindings_by_user = {}
    queried_users = []

    async def get_bindings(*, user_id, session):  # noqa: ARG001
        queried_users.append(user_id)
        result = bindings_by_user.get(user_id, [])
        if isinstance(result, Exception):
            raise result
        return result

    @asynccontextmanager
    async def session_scope():
        yield object()

    monkeypatch.setattr(global_variable_defaults, "session_scope", session_scope)
    monkeypatch.setattr(
        global_variable_defaults,
        "get_variable_service",
        lambda: SimpleNamespace(get_default_field_bindings=get_bindings),
    )
    return flow, template, bindings_by_user, queried_users


@pytest.mark.parametrize("bindings", [[], [("unrelated", ["Unrelated Field"])]])
async def test_warm_upload_allows_empty_fields_without_matching_globals(warm_chat_with_global_candidates, bindings):
    from langflow.api.v1.schemas import SimplifiedAPIRequest

    flow, template, bindings_by_user, queried_users = warm_chat_with_global_candidates
    bindings_by_user["caller"] = bindings
    request = SimplifiedAPIRequest(tweaks={"ChatInput-Q4OPx": {"files": f"{flow.id}/image.png"}})
    graph = await warm_graph.try_warm_run_graph(flow, request, user_id="caller", context=None)

    assert graph is not None
    assert graph.get_vertex("ChatInput-Q4OPx").raw_params["input_value"] == ""
    assert queried_users == ["caller"]
    assert template.raw_graph_data["nodes"][0]["data"]["node"]["template"]["input_value"]["value"] == ""


async def test_warm_upload_global_bindings_are_checked_for_each_user(warm_chat_with_global_candidates):
    from langflow.api.v1.global_variable_defaults import apply_global_variable_defaults
    from langflow.api.v1.schemas import SimplifiedAPIRequest

    flow, template, bindings_by_user, queried_users = warm_chat_with_global_candidates
    bindings_by_user["bound-user"] = [("default_input", ["Input Text"])]
    request = SimplifiedAPIRequest(tweaks={"ChatInput-Q4OPx": {"files": f"{flow.id}/image.png"}})
    assert await warm_graph.try_warm_run_graph(flow, request, user_id="bound-user", context=None) is None
    graph = await warm_graph.try_warm_run_graph(flow, request, user_id="unbound-user", context=None)

    assert graph is not None
    assert graph.user_id == "unbound-user"
    assert graph.get_vertex("ChatInput-Q4OPx").raw_params["input_value"] == ""
    assert queried_users == ["bound-user", "unbound-user"]
    cold_payload = await apply_global_variable_defaults(flow.data, "bound-user")
    cold_field = cold_payload["nodes"][0]["data"]["node"]["template"]["input_value"]
    assert cold_field["value"] == "default_input"
    assert cold_field["load_from_db"] is True
    assert template.raw_graph_data["nodes"][0]["data"]["node"]["template"]["input_value"]["value"] == ""


async def test_warm_upload_preserves_global_lookup_failure_semantics(warm_chat_with_global_candidates):
    from langflow.api.v1.schemas import SimplifiedAPIRequest

    flow, _template, bindings_by_user, queried_users = warm_chat_with_global_candidates
    bindings_by_user["caller"] = RuntimeError("variable store unavailable")
    request = SimplifiedAPIRequest(tweaks={"ChatInput-Q4OPx": {"files": f"{flow.id}/image.png"}})
    graph = await warm_graph.try_warm_run_graph(flow, request, user_id="caller", context=None)

    assert graph is not None
    assert graph.get_vertex("ChatInput-Q4OPx").raw_params["input_value"] == ""
    assert queried_users == ["caller"]


async def test_disabled_warm_registry_does_not_query_globals(warm_chat_with_global_candidates, monkeypatch):
    from langflow.api.v1.schemas import SimplifiedAPIRequest

    flow, _template, _bindings_by_user, queried_users = warm_chat_with_global_candidates
    monkeypatch.setattr(warm_graph, "is_warm_registry_enabled", lambda _settings: False)
    assert await warm_graph.try_warm_run_graph(flow, SimplifiedAPIRequest(), user_id="caller", context=None) is None
    assert queried_users == []


@pytest.fixture
def warm_custom_input_with_migration_error(warm_chat_input, monkeypatch):
    from langflow.services.warm_registry import service as registry_service
    from lfx.graph import Graph

    flow, _template = warm_chat_input
    for index in range(4):
        custom = deepcopy(flow.data["nodes"][0])
        custom["id"] = custom["data"]["id"] = f"WarmReplayProbe-{index}"
        custom["data"]["type"] = f"WarmReplayProbe{index}"
        flow.data["nodes"].append(custom)
    template = Graph.from_payload(
        deepcopy(flow.data), flow_id=str(flow.id), instantiate_components=False, emit_extension_events=False
    )
    registry = SimpleNamespace(get=lambda _id: (template, flow_version(flow.updated_at)))
    monkeypatch.setattr(registry_service, "get_warm_registry", lambda: registry)
    return flow, template


async def test_warm_custom_input_replays_cold_migration_errors_for_each_user(
    warm_custom_input_with_migration_error, monkeypatch
):
    from langflow.api.v1.schemas import SimplifiedAPIRequest
    from lfx.graph import Graph

    flow, template = warm_custom_input_with_migration_error
    original_raw = deepcopy(template.raw_graph_data)
    events = []

    def emit(event_type, payload, *, keyspace):
        events.append((event_type, deepcopy(payload), keyspace))

    monkeypatch.setattr("lfx.services.deps.get_extension_events_service", lambda: SimpleNamespace(emit=emit))
    request = SimplifiedAPIRequest(tweaks={"ChatInput-Q4OPx": {"files": f"{flow.id}/image.png"}})
    for user_id in ("first-user", "second-user"):
        events.clear()
        Graph.from_payload(process_tweaks(deepcopy(flow.data), request.tweaks), flow_id=str(flow.id), user_id=user_id)
        cold_events = deepcopy(events)
        assert len(cold_events) == 4
        assert all(event[0] == "extension_error" for event in cold_events)
        assert all(event[2] == f"user:{user_id}" for event in cold_events)
        events.clear()
        graph = await warm_graph.try_warm_run_graph(flow, request, user_id=user_id, context=None)
        assert graph is not None
        assert events == cold_events
        assert graph.user_id == user_id
        assert graph.extension_migration_had_rewrites is False
    assert template.raw_graph_data == original_raw


async def test_warm_migration_errors_are_recomputed_when_resolution_changes(
    warm_custom_input_with_migration_error, monkeypatch
):
    from langflow.api.v1.schemas import SimplifiedAPIRequest
    from lfx.extension.migration.rewrite import MigrationReport

    flow, _template = warm_custom_input_with_migration_error
    events = []
    monkeypatch.setattr("lfx.extension.migration.migrate_flow_payload", lambda _payload: MigrationReport())
    monkeypatch.setattr(
        "lfx.services.deps.get_extension_events_service",
        lambda: SimpleNamespace(emit=lambda *args, **kwargs: events.append((args, kwargs))),
    )
    graph = await warm_graph.try_warm_run_graph(flow, SimplifiedAPIRequest(), user_id="caller", context=None)
    assert graph is not None
    assert events == []


async def test_warm_migration_errors_fall_back_if_resolution_now_rewrites(
    warm_custom_input_with_migration_error, monkeypatch
):
    from langflow.api.v1.schemas import SimplifiedAPIRequest

    flow, template = warm_custom_input_with_migration_error
    monkeypatch.setattr(
        "lfx.extension.migration.migrate_flow_payload",
        lambda _payload: SimpleNamespace(any_rewritten=True),
    )
    graph = await warm_graph.try_warm_run_graph(flow, SimplifiedAPIRequest(), user_id="caller", context=None)
    assert graph is None
    assert template.raw_graph_data == flow.data


async def test_warm_deepcopy_rejects_a_graph_from_another_flow_revision(monkeypatch) -> None:
    """Execution must cold-fallback when the authorized FlowRead revision differs."""
    from langflow.services import deps
    from langflow.services.warm_registry import service as registry_service

    template = _FakeGraph([_FakeVertex({"stream": False})])
    registry = SimpleNamespace(get=lambda _flow_id: (template, "cached-version"))
    monkeypatch.setattr(warm_graph, "is_warm_registry_enabled", lambda _settings: True)
    monkeypatch.setattr(deps, "get_settings_service", lambda: SimpleNamespace(settings=SimpleNamespace()))
    monkeypatch.setattr(registry_service, "get_warm_registry", lambda: registry)

    graph = await warm_graph.warm_deepcopy(
        "flow-id",
        expected_version="authorized-version",
        user_id="user-id",
        session_id=None,
    )

    assert graph is None


@pytest.mark.parametrize("had_rewrites", [None, True])
async def test_warm_deepcopy_cold_falls_back_for_rewritten_or_unknown_migration_provenance(
    monkeypatch, had_rewrites
) -> None:
    """Rewritten templates and old snapshots still require parsing under the caller."""
    from langflow.services import deps
    from langflow.services.warm_registry import service as registry_service

    template = _FakeGraph([_FakeVertex({"stream": False})])
    template.requires_extension_event_replay = True
    template.extension_migration_had_rewrites = had_rewrites
    template.copy_for_run = Mock(side_effect=AssertionError("extension-event graph was copied"))
    registry = SimpleNamespace(get=lambda _flow_id: (template, "v1"))
    monkeypatch.setattr(warm_graph, "is_warm_registry_enabled", lambda _settings: True)
    monkeypatch.setattr(deps, "get_settings_service", lambda: SimpleNamespace(settings=SimpleNamespace()))
    monkeypatch.setattr(registry_service, "get_warm_registry", lambda: registry)

    graph = await warm_graph.warm_deepcopy(
        "flow-id",
        expected_version="v1",
        user_id="user-id",
        session_id=None,
    )

    assert graph is None
    template.copy_for_run.assert_not_called()


async def test_warm_deepcopy_revalidates_current_component_policy(monkeypatch) -> None:
    """A policy change after preload must reject the cached graph before copying it."""
    from langflow.services import deps
    from langflow.services.warm_registry import service as registry_service
    from lfx.utils import flow_validation

    template = _FakeGraph([_FakeVertex({"stream": False})])
    template.copy_for_run = Mock(side_effect=AssertionError("blocked graph was copied"))
    registry = SimpleNamespace(get=lambda _flow_id: (template, "v1"))
    monkeypatch.setattr(warm_graph, "is_warm_registry_enabled", lambda _settings: True)
    monkeypatch.setattr(deps, "get_settings_service", lambda: SimpleNamespace(settings=SimpleNamespace()))
    monkeypatch.setattr(registry_service, "get_warm_registry", lambda: registry)

    class PolicyChangedError(ValueError):
        pass

    def _reject(_target) -> None:
        message = "component is now blocked"
        raise PolicyChangedError(message)

    monkeypatch.setattr(flow_validation, "validate_flow_for_current_settings", _reject)

    with pytest.raises(PolicyChangedError, match="now blocked"):
        await warm_graph.warm_deepcopy(
            "flow-id",
            expected_version="v1",
            user_id="user-id",
            session_id=None,
        )

    template.copy_for_run.assert_not_called()


async def test_v1_streaming_run_requests_a_streaming_warm_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The V1 streaming flag reaches the warm-copy stream-mode override."""
    from langflow.api.v1 import endpoints
    from langflow.api.v1.schemas import SimplifiedAPIRequest

    captured: dict = {}
    graph = SimpleNamespace(vertices=[], run_id=None)

    def set_run_id(run_id) -> None:
        graph.run_id = str(run_id)

    graph.set_run_id = set_run_id

    async def fake_warm_deepcopy(flow_id, *, expected_version, user_id, session_id, stream=False, tweaks=None):
        assert not tweaks
        captured.update(
            flow_id=flow_id,
            expected_version=expected_version,
            user_id=user_id,
            session_id=session_id,
            stream=stream,
        )
        return graph

    job_service = SimpleNamespace(
        create_job=AsyncMock(),
        execute_with_status=AsyncMock(return_value=([], "effective-session")),
    )
    monkeypatch.setattr(warm_graph, "warm_deepcopy", fake_warm_deepcopy)
    monkeypatch.setattr(warm_graph, "is_warm_registry_enabled", lambda _settings: True)
    monkeypatch.setattr(endpoints, "get_job_service", lambda: job_service)
    monkeypatch.setattr(
        endpoints,
        "get_task_service",
        lambda: SimpleNamespace(fire_and_forget_task=AsyncMock()),
    )
    monkeypatch.setattr(endpoints, "get_memory_base_service", lambda: SimpleNamespace(on_flow_output=AsyncMock()))
    monkeypatch.setattr(endpoints, "process_tweaks", Mock(side_effect=AssertionError("cold path used")))

    flow_id = uuid4()
    user_id = uuid4()
    updated_at = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
    result = await endpoints.simple_run_flow(
        flow=SimpleNamespace(
            id=flow_id,
            user_id=user_id,
            name="warm",
            data={"nodes": [], "edges": []},
            updated_at=updated_at,
        ),
        input_request=SimplifiedAPIRequest(session_id="v1-session"),
        stream=True,
        api_key_user=SimpleNamespace(id=user_id, is_superuser=False),
    )

    assert result.session_id == "effective-session"
    assert captured == {
        "flow_id": str(flow_id),
        "expected_version": flow_version(updated_at),
        "user_id": user_id,
        "session_id": "v1-session",
        "stream": True,
    }


async def test_v2_sync_run_requests_a_non_streaming_warm_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The V2 sync warm path explicitly overrides persisted streaming defaults."""
    from langflow.api.v2 import workflow_execution
    from lfx.workflow.converters import ParsedWorkflowRun

    captured: dict = {}
    graph = SimpleNamespace(vertices=[], run_id=None, get_terminal_nodes=list)

    def set_run_id(run_id) -> None:
        graph.run_id = str(run_id)

    graph.set_run_id = set_run_id

    async def fake_warm_deepcopy(flow_id, *, expected_version, user_id, session_id, stream=False):
        captured.update(
            flow_id=flow_id,
            expected_version=expected_version,
            user_id=user_id,
            session_id=session_id,
            stream=stream,
        )
        return graph

    job_service = SimpleNamespace(
        create_job=AsyncMock(),
        execute_with_status=AsyncMock(return_value=([], "effective-session")),
    )
    expected = object()
    monkeypatch.setattr(workflow_execution, "warm_deepcopy", fake_warm_deepcopy)
    monkeypatch.setattr(workflow_execution, "get_job_service", lambda: job_service)
    monkeypatch.setattr(
        workflow_execution,
        "get_task_service",
        lambda: SimpleNamespace(fire_and_forget_task=AsyncMock()),
    )
    monkeypatch.setattr(
        workflow_execution,
        "get_memory_base_service",
        lambda: SimpleNamespace(on_flow_output=AsyncMock()),
    )
    monkeypatch.setattr(workflow_execution, "run_response_to_workflow_response", Mock(return_value=expected))
    monkeypatch.setattr(workflow_execution, "process_tweaks", Mock(side_effect=AssertionError("cold path used")))

    flow_id = uuid4()
    user_id = uuid4()
    updated_at = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
    result = await workflow_execution.execute_sync_workflow(
        parsed=ParsedWorkflowRun(flow_id=str(flow_id), session_id="v2-session", mode="sync"),
        flow=SimpleNamespace(
            id=flow_id,
            user_id=user_id,
            name="warm",
            data={"nodes": [], "edges": []},
            updated_at=updated_at,
        ),
        job_id=uuid4(),
        current_user=SimpleNamespace(id=user_id),
        background_tasks=SimpleNamespace(),
        http_request=None,
    )

    assert result is expected
    assert captured == {
        "flow_id": str(flow_id),
        "expected_version": flow_version(updated_at),
        "user_id": str(user_id),
        "session_id": "v2-session",
        "stream": False,
    }
