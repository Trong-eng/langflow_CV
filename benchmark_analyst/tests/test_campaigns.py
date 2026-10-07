"""Controlled runs reject ineffective treatments before producing conclusions."""

import pytest


def test_controlled_profile_rejects_ineffective_telemetry_logging_or_gc():
    from benchmark_analyst.campaigns import verify_profile_worker

    snapshot = {
        "effective": {
            "product_telemetry_enabled": True,
            "native_tracing": True,
            "uvicorn_access_enabled": False,
            "gc_enabled": True,
        }
    }
    verify_profile_worker(snapshot, telemetry=True)
    for key, wrong in [
        ("product_telemetry_enabled", False),
        ("native_tracing", False),
        ("uvicorn_access_enabled", True),
        ("gc_enabled", False),
    ]:
        changed = {"effective": {**snapshot["effective"], key: wrong}}
        with pytest.raises(ValueError, match="effective|profile"):
            verify_profile_worker(changed, telemetry=True)


def test_diagnostic_control_uses_same_drain_cadence_but_no_extra_phase_hooks():
    from benchmark_analyst.campaigns import variant_settings

    assert variant_settings("D0") == {"diagnostics": False, "telemetry": True, "warmups": 5}
    assert variant_settings("D1") == {"diagnostics": True, "telemetry": True, "warmups": 5}
    assert variant_settings("D2") == {"diagnostics": True, "telemetry": False, "warmups": 5}
    assert variant_settings("D3") == {"diagnostics": True, "telemetry": True, "warmups": 20}
    with pytest.raises(ValueError):
        variant_settings("invented")


def test_observed_settings_exclude_ephemeral_paths_but_detect_threads_gc_versions():
    from benchmark_analyst.campaigns import observed_settings

    first = {
        "effective": {
            "database_path": "/slot1/db",
            "config_dir": "/slot1/storage",
            "gc_counts": [1, 2, 3],
            "gc_thresholds": [700, 10, 10],
            "thread_environment": {"OMP_NUM_THREADS": "2"},
            "versions": {"python": "3.13"},
        }
    }
    second = {"effective": {**first["effective"], "database_path": "/slot2/db", "gc_counts": [50, 5, 4]}}
    assert observed_settings(first) == observed_settings(second)
    second["effective"]["thread_environment"] = {"OMP_NUM_THREADS": "4"}
    assert observed_settings(first) != observed_settings(second)
