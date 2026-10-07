"""Shared smoke/treatment contracts without importing the serving runtime."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def observed_settings(snapshot: dict) -> dict:
    effective = snapshot.get("effective", {})
    if not isinstance(effective, dict):
        return {}
    return {
        key: effective.get(key)
        for key in (
            "database_backend",
            "database_engine",
            "sqlite_pragmas",
            "storage_type",
            "native_tracing",
            "telemetry_writer_enabled",
            "maintenance_settings",
            "uvicorn_access_level",
            "uvicorn_access_handlers",
            "uvicorn_access_propagate",
            "gc_enabled",
            "gc_thresholds",
            "thread_environment",
            "exporters_configured",
            "versions",
        )
    }


def diagnostics_declaration(enabled: bool, detail: str = "coarse") -> dict:
    return {
        "version": 1,
        "enabled": enabled,
        "detail": detail,
        "max_events": 8192,
        "lag_interval_ms": 20,
        "required_capabilities": ["setup", "components", "gc", "event_loop_lag"],
    }


def profile_identity(
    fixture_hash: str | None,
    *,
    diagnostics: bool,
    telemetry: bool,
    warmups: int,
    effective_common_settings: dict | None = None,
) -> tuple[str, dict]:
    profile = {
        "name": "controlled-scrfd-v1" if fixture_hash else "legacy-unisolated",
        "fixture_sha256": fixture_hash,
        "workers": 1,
        "concurrency": 1,
        "native_tracing": True,
        "product_telemetry": telemetry,
        "access_logging": False,
        "gc_enabled": True,
        "warmup_count": warmups,
        "diagnostics_enabled": diagnostics,
        "diagnostic_detail": "coarse",
        "http_client_connection_reuse": True,
        "idle_control_fresh_connection": True,
        "registry_preload_limit": 0,
    }
    if effective_common_settings is not None:
        profile["effective_common_settings"] = effective_common_settings
    digest = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
    return digest, profile


def bind_effective_profile(manifest: dict, observed: dict) -> None:
    """Finalize smoke identity, or verify the effective profile carried from smoke."""
    profile = manifest["profile"]
    previous = profile.get("effective_common_settings")
    if previous is not None and previous != observed:
        raise ValueError("effective profile changed since smoke")
    manifest["profile_id"], manifest["profile"] = profile_identity(
        manifest["fixture_sha256"],
        diagnostics=profile["diagnostics_enabled"],
        telemetry=profile["product_telemetry"],
        warmups=profile["warmup_count"],
        effective_common_settings=observed,
    )


def _declares_profile(manifest: dict) -> bool:
    return (
        any(key in manifest for key in ("profile", "profile_id", "fixture_sha256"))
        or manifest.get("kind") == "diagnostic"
        or any(
            isinstance(manifest.get(key), dict) and manifest[key].get("enabled") is True
            for key in ("resource_measurement", "diagnostics")
        )
    )


def validate_effective_profile(manifest: dict, evidence: list[dict], errors: list[str]) -> None:
    if not _declares_profile(manifest):
        return  # Historical factorial datasets did not declare this contract.
    profile = manifest.get("profile")
    if not isinstance(profile, dict):
        errors.append("effective profile descriptor is missing or must be an object")
        return
    bool_keys = (
        "native_tracing",
        "product_telemetry",
        "access_logging",
        "gc_enabled",
        "diagnostics_enabled",
        "http_client_connection_reuse",
        "idle_control_fresh_connection",
    )
    integer_keys = ("workers", "concurrency", "warmup_count", "registry_preload_limit")
    if (
        any(type(profile.get(key)) is not bool for key in bool_keys)
        or any(type(profile.get(key)) is not int for key in integer_keys)
        or profile.get("warmup_count", 0) < 1
    ):
        errors.append("effective profile has missing or invalid treatment settings")
        return
    _, expected = profile_identity(
        manifest.get("fixture_sha256"),
        diagnostics=profile["diagnostics_enabled"],
        telemetry=profile["product_telemetry"],
        warmups=profile["warmup_count"],
    )
    if any(profile.get(key) != value for key, value in expected.items()):
        errors.append("effective profile descriptor differs from controlled settings/fixture")
    if "warmups" in manifest and profile["warmup_count"] != manifest["warmups"]:
        errors.append("effective profile warmup count differs from manifest")
    declaration = manifest.get("diagnostics", {})
    if isinstance(declaration, dict) and profile["diagnostics_enabled"] is not declaration.get("enabled", False):
        errors.append("effective profile diagnostics differs from manifest")
    if "effective_common_settings" not in profile:
        errors.append("effective profile is missing frozen effective common settings")
        return
    common = profile["effective_common_settings"]
    if not isinstance(common, dict) or set(common) != set(observed_settings({})):
        errors.append("effective profile settings must be a complete object")
        return
    expected_hash = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
    if manifest.get("profile_id") != expected_hash:
        errors.append("effective profile descriptor/hash mismatch")
    for record in evidence:
        if not isinstance(record, dict):
            errors.append("effective profile evidence must be an object")
            continue
        for checkpoint in ("before_warmup", "after_warmup", "after_measurement", "after_idle"):
            snapshot = record.get(checkpoint)
            if (
                not isinstance(snapshot, dict)
                or not isinstance(snapshot.get("effective"), dict)
                or observed_settings(snapshot) != common
            ):
                errors.append(
                    f"effective profile differs at block={record.get('block')} arm={record.get('arm')} {checkpoint}"
                )
                continue
            intended = {
                "product_telemetry_enabled": profile["product_telemetry"],
                "native_tracing": True,
                "uvicorn_access_enabled": False,
                "gc_enabled": True,
            }
            for key, value in intended.items():
                if snapshot.get("effective", {}).get(key) is not value:
                    errors.append(f"effective {key} differs from declared profile at {checkpoint}")


def check_smoke(directory: Path, manifest: dict) -> None:
    directory = Path(directory)
    result = json.loads((directory / "analysis.json").read_text())
    previous = json.loads((directory / "manifest.json").read_text())
    valid = (
        result.get("overall_valid") is True
        if _declares_profile(previous)
        else result.get("overall_valid", result.get("valid", False))
    )
    if not valid:
        raise ValueError("smoke must have valid latency and all required evidence")
    if _declares_profile(previous) != _declares_profile(manifest):
        raise ValueError("smoke profile/isolation contract differs from target")
    required = ["manifest.json", "requests.jsonl", "worker_evidence.jsonl"]
    if previous.get("resource_measurement", {}).get("enabled"):
        required.append("memory.jsonl")
    if previous.get("diagnostics", {}).get("enabled"):
        required.extend(["diagnostic_events.jsonl", "diagnostic_metadata.jsonl"])
    recorded = result.get("raw_sha256", {})
    if (directory / "state.json").exists() or "state.json" in recorded:
        required.append("state.json")
    for name in required:
        path = directory / name
        if not path.is_file() or recorded.get(name) != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError(f"smoke raw {name} changed or lacks analyzed integrity evidence")
    fields = ("workload", "warmups", "profile_id", "fixture_sha256", "resource_measurement", "diagnostics")
    for key in fields:
        if previous.get(key) != manifest.get(key):
            raise ValueError(f"{key} changed since smoke; run matching smoke again")
    if previous.get("kind", "factorial") != manifest.get("kind", "factorial"):
        raise ValueError("experiment kind changed since smoke")
    if previous.get("variant") != manifest.get("variant"):
        raise ValueError("diagnostic variant changed since smoke")
    if previous.get("source", {}).get("sha256") != manifest.get("source", {}).get("sha256"):
        raise ValueError("source changed since smoke; freeze source and rerun smoke")
