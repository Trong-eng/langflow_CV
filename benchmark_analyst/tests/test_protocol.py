import math

import pytest
from benchmark_analyst import protocol


def test_main_has_1000_per_arm_with_balanced_positions():
    schedule = protocol.make_schedule(1000, 4)
    for arm in ("00", "01", "10", "11"):
        assert sum(s["count"] for s in schedule if s["arm"] == arm) == 1000
        assert {i % 4 for i, s in enumerate(schedule) if s["arm"] == arm} == {0, 1, 2, 3}


@pytest.mark.parametrize("requests,blocks", [(1001, 4), (0, 4), (1000, 0), (-1, 1)])
def test_invalid_design_is_rejected(requests, blocks):
    with pytest.raises(ValueError):
        protocol.make_schedule(requests, blocks)


def test_orchestration_excludes_union_of_custom_methods_only():
    observed = {
        "server_total_ms": 100,
        "complete": True,
        "status_code": 200,
        "component_intervals_ms": [
            {"node_id": "pre", "start_ms": 10, "end_ms": 40},
            {"node_id": "infer", "start_ms": 30, "end_ms": 60},
            {"node_id": "chat", "start_ms": 70, "end_ms": 80},
        ],
    }
    result = protocol.measure_overhead(observed, ["pre", "infer"])
    assert result == {"server_total_ms": 100, "scrfd_processing_ms": 50, "langflow_overhead_ms": 50}


@pytest.mark.parametrize("problem", ["missing_node", "nan", "reversed", "outside", "incomplete", "truncated"])
def test_bad_measurements_do_not_silently_become_overhead(problem):
    observed = {
        "server_total_ms": 100,
        "complete": True,
        "status_code": 200,
        "component_intervals_ms": [{"node_id": "infer", "start_ms": 10, "end_ms": 60}],
    }
    if problem == "missing_node":
        observed["component_intervals_ms"] = []
    elif problem == "nan":
        observed["server_total_ms"] = math.nan
    elif problem == "reversed":
        observed["component_intervals_ms"][0]["end_ms"] = 0
    elif problem == "outside":
        observed["component_intervals_ms"][0]["end_ms"] = 101
    elif problem == "truncated":
        observed["intervals_truncated"] = True
    else:
        observed["complete"] = False
    with pytest.raises(ValueError):
        protocol.measure_overhead(observed, ["infer"])


def test_production_payload_uses_chat_output_and_uploaded_file():
    config = {"input_node_id": "ChatInput-1"}
    assert protocol.run_payload(config, "flow/image.jpg", "marker", "session") == {
        "input_type": "chat",
        "output_type": "chat",
        "input_value": "marker",
        "session_id": "session",
        "tweaks": {"ChatInput-1": {"files": "flow/image.jpg"}},
    }


def test_result_requires_correct_output_node_and_relative_image_for_same_flow():
    output = {
        "outputs": [
            {
                "outputs": [
                    {
                        "component_id": "ChatOutput-1",
                        "results": {
                            "message": {
                                "data": {
                                    "text": "Detected 3 face(s).\n![result](/api/v1/files/images/flow/a.png)",
                                    "error": False,
                                }
                            }
                        },
                    }
                ]
            }
        ]
    }
    assert protocol.result_image_path(output, "ChatOutput-1", "flow", 3) == "/api/v1/files/images/flow/a.png"
    with pytest.raises(ValueError):
        protocol.result_image_path(output, "ChatOutput-1", "other", 3)
    with pytest.raises(ValueError):
        protocol.result_image_path(output, "ChatOutput-1", "flow", 2)


def test_model_provenance_must_be_the_file_actually_used_by_saved_inference_node(tmp_path):
    model = tmp_path / "real.onnx"
    config = {"model_path": str(model), "scrfd_node_ids": ["pre", "infer", "draw", "save"]}
    node = {
        "id": "infer",
        "data": {
            "type": "SCRFDInference",
            "node": {"template": {"model_path": {"value": str(model), "load_from_db": False}}},
        },
    }
    flow = {"data": {"nodes": [node]}}
    protocol.validate_model_binding(flow, config)
    config["model_path"] = str(tmp_path / "unrelated.onnx")
    with pytest.raises(ValueError, match="model"):
        protocol.validate_model_binding(flow, config)
    config["model_path"] = str(model)
    node["data"]["node"]["template"]["model_path"]["load_from_db"] = True
    with pytest.raises(ValueError, match="model"):
        protocol.validate_model_binding(flow, config)
