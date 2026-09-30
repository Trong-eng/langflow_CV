"""Shared warm-graph resolution for opt-in cached execution.

When ``LANGFLOW_WARM_REGISTRY_ENABLED=true`` flows are pre-built once and
kept warm in the process-local registry. The v1 ``simple_run_flow`` and v2 sync paths
resolve a **deepcopy of the pre-built template** and apply this run's identity to the
copy — skipping per-request ``Graph.from_payload`` work and, on metadata-validated v2
hits, avoiding a repeat read of the large ``Flow.data`` column. Stream/background and
public execution retain their established graph-build paths.

Core model: warm deepcopy + set-values. A run falls back to the normal cold rebuild
whenever the per-request work can't be layered onto a shared template — see the
per-caller gates (unsupported tweaks, request context/globals, bound globals, HITL) and
the built-in gates here (warming disabled, cache miss, store unavailable).
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from langflow.api.global_variable_fields import is_global_variable_eligible_field
from langflow.services.warm_registry.service import flow_version

if TYPE_CHECKING:
    from lfx.graph.graph.base import Graph

    from langflow.api.v1.schemas import SimplifiedAPIRequest
    from langflow.services.database.models.flow.model import Flow


def is_warm_registry_enabled(settings: Any) -> bool:
    """Return whether the independently opt-in warm graph registry is enabled."""
    return bool(getattr(settings, "warm_registry_enabled", False))


def flow_needs_auto_globals(data: dict | None) -> bool:
    """True if the flow has any field an auto-bound global variable COULD target.

    Conservative over-approximation (the caller's variables aren't known here): if the
    flow has at least one empty, global-eligible str field with a display_name, some
    user's variable ``default_fields`` might auto-bind it at run time — so that flow must
    check the caller's bindings before using the warm path. Only
    ``apply_global_variable_defaults`` performs that binding, and it changes graph_data
    per user, never the stored flow.data. If there is no such field, no auto-binding is
    possible for anyone and the warm template is complete.
    Explicit ``load_from_db`` fields ARE in flow.data and resolve warm (identity threaded).
    """
    for node in (data or {}).get("nodes", []):
        if not isinstance(node, dict):
            continue
        template = (node.get("data") or {}).get("node", {}).get("template")
        if not isinstance(template, dict):
            continue
        for field_name, field in template.items():
            if field_name == "_type":
                continue
            if is_global_variable_eligible_field(field) and isinstance(field.get("display_name"), str):
                return True
    return False


def _supports_chat_input_file_tweaks(data: dict | None, tweaks: dict) -> bool:
    """Accept only top-level ChatInput file values addressed by an unambiguous node ID.

    Keep arbitrary template edits, display-name targeting, grouped proxies, and
    global tweaks on the cold path. File values still go through ``process_tweaks``
    and normal parameter preparation; this predicate does not authorize file access
    or bypass the deployment's tweak policy.
    """
    if not isinstance(data, dict) or not isinstance(tweaks, dict):
        return False
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        return False
    for node_id, overrides in tweaks.items():
        if not isinstance(overrides, dict) or set(overrides) != {"files"}:
            return False
        files = overrides["files"]
        if not isinstance(files, str) and not (isinstance(files, list) and all(isinstance(p, str) for p in files)):
            return False
        matches = [node for node in nodes if isinstance(node, dict) and node.get("id") == node_id]
        if len(matches) != 1:
            return False
        node_data = matches[0].get("data")
        if not isinstance(node_data, dict) or node_data.get("type") != "ChatInput":
            return False
        component = node_data.get("node")
        if not isinstance(component, dict) or "flow" in component:
            return False
        template = component.get("template")
        field = template.get("files") if isinstance(template, dict) else None
        if (
            not isinstance(field, dict)
            or field.get("type") != "file"
            or field.get("list") is not True
            or "proxy" in field
        ):
            return False
    return True


def _apply_implicit_stream_tweak(graph: Graph, *, stream: bool) -> None:
    """Apply ``process_tweaks(..., stream=...)`` semantics to an already-built graph.

    Registry entries are built before the request's execution mode is known. The
    cold path injects a global ``stream`` tweak before ``Graph.from_payload``;
    mirror that on the request-local deepcopy by overriding only vertices that
    actually declare a ``stream`` parameter. ``update_raw_params`` makes the value
    survive subsequent parameter preparation, and removing the DB marker matches
    the cold tweak path's ``load_from_db = False`` behavior.
    """
    # Apply the implicit tweak to the original top-level frontend shape, exactly
    # where the cold path runs ``process_tweaks``. Grouped children that do not
    # expose a stream proxy must retain their persisted value. If a top-level
    # field changes, rebuild the still-lazy vertex structure so ``process_flow``
    # propagates exposed group proxies before constructors run.
    raw_graph_data = getattr(graph, "raw_graph_data", None)
    raw_nodes = raw_graph_data.get("nodes") if isinstance(raw_graph_data, dict) else None
    raw_edges = raw_graph_data.get("edges") if isinstance(raw_graph_data, dict) else None
    if isinstance(raw_nodes, list) and isinstance(raw_edges, list):
        for node in raw_nodes:
            if not isinstance(node, dict):
                continue
            node_data = node.get("data")
            component_data = node_data.get("node") if isinstance(node_data, dict) else None
            template = component_data.get("template") if isinstance(component_data, dict) else None
            stream_field = template.get("stream") if isinstance(template, dict) else None
            if not isinstance(stream_field, dict):
                continue
            stream_field["value"] = stream
            if "load_from_db" in stream_field:
                stream_field["load_from_db"] = False
        # ``Graph.copy_for_run`` invokes this before its first structural build.
        # Returning even when no top-level stream field exists deliberately
        # preserves hidden grouped-child values.
        return

    # Compatibility fallback for programmatic/test graphs without retained raw
    # frontend data. Warm registry templates always take the raw-shape path.
    for vertex in graph.vertices:
        raw_params = getattr(vertex, "raw_params", None)
        if not isinstance(raw_params, dict) or "stream" not in raw_params:
            continue
        # Constructors receive the Vertex and may inspect its original template,
        # while ``update_raw_params`` changes only prepared parameter maps. Mirror
        # cold ``process_tweaks`` in both representations before construction.
        full_data = getattr(vertex, "full_data", None)
        if isinstance(full_data, dict):
            vertex_data = full_data.get("data")
            node_data = vertex_data.get("node") if isinstance(vertex_data, dict) else None
            template = node_data.get("template") if isinstance(node_data, dict) else None
            stream_field = template.get("stream") if isinstance(template, dict) else None
            if isinstance(stream_field, dict):
                stream_field["value"] = stream
                if "load_from_db" in stream_field:
                    stream_field["load_from_db"] = False
        vertex.update_raw_params({"stream": stream}, overwrite=True)  # type: ignore[dict-item]
        load_from_db_fields = getattr(vertex, "load_from_db_fields", None)
        if isinstance(load_from_db_fields, list):
            while "stream" in load_from_db_fields:
                load_from_db_fields.remove("stream")


async def warm_deepcopy(
    flow_id: str,
    *,
    expected_version: str,
    user_id: Any,
    session_id: str | None,
    stream: bool = False,
    tweaks: dict | None = None,
) -> Graph | None:
    """Return a run-ready deepcopy of the warm template, or ``None`` to rebuild cold.

    Built-in fall-backs: warming disabled, cache miss (and not lazily warmable), or a
    transient store-availability failure. The returned graph carries the flow's structure
    (built at warm time) plus this run's ``user_id``/``session_id`` (applied to the copy),
    so callers use it exactly like a freshly-built graph. Per-request *policy* gates
    (context / auto-bind / HITL) are the caller's responsibility — they differ by
    run path — and must be checked BEFORE calling this. Only standard ChatInput file
    tweaks are supported and they are checked against the cached raw template too.
    """
    from lfx.run._defaults import apply_run_defaults
    from lfx.utils.file_path_security import LocalFileAccessError
    from lfx.utils.flow_validation import validate_flow_for_current_settings

    from langflow.services.deps import get_settings_service
    from langflow.services.warm_registry.reconcile import warm_one
    from langflow.services.warm_registry.service import (
        FlowStoreUnavailableError,
        WarmRegistryCapacityError,
        get_warm_registry,
    )

    if not is_warm_registry_enabled(get_settings_service().settings):
        return None

    flow_id_str = str(flow_id)
    registry = get_warm_registry()
    hit = registry.get(flow_id_str)
    if hit is None:
        if registry.rejects_version(flow_id_str, expected_version):
            return None
        try:
            hit = await warm_one(flow_id_str)
        except (FlowStoreUnavailableError, WarmRegistryCapacityError):
            # Let the caller's cold path do its own row read and surface errors normally.
            return None
        except LocalFileAccessError:
            # A user-neutral template cannot resolve a saved user-owned file.
            # Rebuild cold with caller identity and request tweaks instead. This
            # catch covers only template construction: file checks on the actual
            # request-local graph below (and the cold path) must still propagate.
            return None
    if hit is None:
        return None
    if hit[1] != expected_version:
        # Bind execution to the revision the caller fetched and authorized. A stale
        # hit or a reconcile swap after authorization must use the cold FlowRead path.
        return None
    migration_report = None
    if getattr(hit[0], "requires_extension_event_replay", False):
        # A rewritten template has lost the original legacy references; only
        # cold parsing can reproduce its migration events. Old templates with
        # unknown rewrite provenance must also take that conservative path.
        if getattr(hit[0], "extension_migration_had_rewrites", None) is not False:
            return None
        if not callable(getattr(hit[0], "copy_for_run", None)):
            return None
        from lfx.extension.migration import migrate_flow_payload

        raw_data = getattr(hit[0], "raw_graph_data", None)
        if not isinstance(raw_data, dict):
            return None
        # Error-only templates retain original types. Recompute against today's
        # migration table so resolved errors disappear and newly mapped types
        # cold-fallback, instead of serving stale diagnostics or executable types.
        migration_report = migrate_flow_payload(deepcopy(raw_data))
        if migration_report.any_rewritten:
            return None

    # Catalog and custom-component policy can change without touching the Flow
    # row. Revalidate the cached raw payload on every hit, matching the defense
    # in depth in Graph.from_payload instead of letting a newly blocked
    # component survive until the next flow revision.
    validate_flow_for_current_settings(hit[0])

    run_user_id = str(user_id) if user_id is not None else None
    copy_for_run = getattr(hit[0], "copy_for_run", None)
    if tweaks and (
        not callable(copy_for_run)
        or not _supports_chat_input_file_tweaks(getattr(hit[0], "raw_graph_data", None), tweaks)
    ):
        return None

    def apply_request_tweaks(run_graph: Graph) -> None:
        if tweaks:
            from lfx.processing.process import process_tweaks

            # The copy hook runs before graph structure and constructors are built.
            # Reuse cold-path policy, file_path and load_from_db handling, and detach
            # list values so neither request input nor registry state can be mutated.
            process_tweaks(run_graph.raw_graph_data, deepcopy(tweaks), stream=stream)
        else:
            _apply_implicit_stream_tweak(run_graph, stream=stream)
        if migration_report is not None:
            from lfx.extension.migration.events import report_migration

            report_migration(migration_report, flow_id=flow_id_str, user_id=run_user_id)

    if callable(copy_for_run):
        graph = copy_for_run(
            user_id=run_user_id,
            before_instantiate=apply_request_tweaks,
        )
    else:
        graph = deepcopy(hit[0])
        apply_request_tweaks(graph)
    # Thread this run's identity onto the copy (the template is user-agnostic). This is
    # what lets explicit load_from_db fields resolve for the calling user, exactly like a
    # cold from_payload(user_id=...).
    apply_run_defaults(
        graph,
        session_id=session_id,
        user_id=run_user_id,
        overwrite_user_id=user_id is not None,
    )
    return graph


async def try_warm_run_graph(
    flow: Flow,
    input_request: SimplifiedAPIRequest,
    *,
    user_id: Any,
    context: dict | None,
    stream: bool = False,
) -> Graph | None:
    """v1 ``simple_run_flow`` warm resolver: gate on the v1 signals, then ``warm_deepcopy``.

    Cold-falls-back for unsupported tweaks, request context, actual user-specific global
    bindings, or HITL flows. Standard ChatInput file uploads apply to an isolated copy.
    """
    from langflow.services.deps import get_settings_service

    if context is not None or not is_warm_registry_enabled(get_settings_service().settings):
        return None
    # Keep v1 router initialization out of this module's clean-import path.
    from langflow.api.v1.global_variable_defaults import apply_global_variable_defaults
    from langflow.api.v1.run_validation import flow_requires_hitl

    data = flow.data or {}
    tweaks = input_request.tweaks
    if tweaks is not None and not isinstance(tweaks, dict):
        tweaks = tweaks.model_dump()
    if tweaks and not _supports_chat_input_file_tweaks(data, tweaks):
        return None
    if flow_requires_hitl(data):
        return None
    if user_id is not None and flow_needs_auto_globals(data):
        from lfx.processing.process import process_tweaks

        # Empty fields alone do not require a rebuild when this caller has no
        # matching defaults. Preserve cold ordering: tweaks first, then bindings.
        # The established helper also preserves cold behavior on lookup failure.
        candidate = process_tweaks(deepcopy(data), deepcopy(tweaks or {}), stream=stream)
        if await apply_global_variable_defaults(candidate, user_id) != candidate:
            return None
    return await warm_deepcopy(
        str(flow.id),
        expected_version=flow_version(flow.updated_at),
        user_id=user_id,
        session_id=input_request.session_id,
        stream=stream,
        tweaks=tweaks,
    )
