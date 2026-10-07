"""Bounded process-local cache for source-derived component artifacts."""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from threading import RLock
from typing import TYPE_CHECKING, Generic, TypeVar, cast

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import CodeType

COMPONENT_COMPILATION_ARTIFACT_GENERATION = 1
COMPONENT_COMPILATION_CACHE_MAX_ENTRIES = 128
COMPONENT_COMPILATION_CACHE_MAX_SOURCE_BYTES = 262_144

ArtifactT = TypeVar("ArtifactT")
_CacheKey = tuple[int, str, str]


@dataclass(frozen=True, slots=True)
class ComponentCompilationArtifact:
    """Source-only objects that are safe to reuse before runtime execution."""

    source: str
    generation: int
    module_template: bytes
    class_name: str
    compiled_class: CodeType
    trusted_vector_store_alias: str | None


@dataclass(frozen=True, slots=True)
class ComponentArtifactLookup(Generic[ArtifactT]):
    """Artifact plus whether the process cache retains it."""

    artifact: ArtifactT
    retained: bool


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    source: str
    artifact: object


_cache: OrderedDict[_CacheKey, _CacheEntry] = OrderedDict()
_cache_lock = RLock()
_stats = {
    "hits": 0,
    "misses": 0,
    "bypasses": 0,
    "evictions": 0,
    "builds": 0,
}


@lru_cache(maxsize=1)
def _cache_enabled() -> bool:
    """Snapshot the worker setting until the cache lifecycle is explicitly cleared."""
    from lfx.services.deps import get_settings_service

    settings_service = get_settings_service()
    settings = getattr(settings_service, "settings", None)
    return bool(getattr(settings, "component_compilation_cache_enabled", False))


def _source_digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8", errors="surrogatepass")).hexdigest()


def _bypass(builder: Callable[[], ArtifactT]) -> ComponentArtifactLookup[ArtifactT]:
    with _cache_lock:
        _stats["bypasses"] += 1
    return ComponentArtifactLookup(artifact=builder(), retained=False)


def get_or_build_component_artifact(
    source: str,
    builder: Callable[[], ArtifactT],
    *,
    variant: str = "",
) -> ArtifactT:
    return get_or_build_component_artifact_with_status(source, builder, variant=variant).artifact


def get_or_build_component_artifact_with_status(
    source: str,
    builder: Callable[[], ArtifactT],
    *,
    variant: str = "",
) -> ComponentArtifactLookup[ArtifactT]:
    """Return a cached artifact or build one without retaining failures.

    The lock intentionally covers source preparation on a miss to prevent a
    same-source compilation stampede. Callers must keep runtime imports, exec,
    class creation, and constructor work outside ``builder``.
    """
    if not _cache_enabled():
        return _bypass(builder)
    if len(source.encode("utf-8", errors="surrogatepass")) > COMPONENT_COMPILATION_CACHE_MAX_SOURCE_BYTES:
        return _bypass(builder)

    key = (COMPONENT_COMPILATION_ARTIFACT_GENERATION, _source_digest(source), variant)
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and cached.source == source:
            _cache.move_to_end(key)
            _stats["hits"] += 1
            return ComponentArtifactLookup(artifact=cast("ArtifactT", cached.artifact), retained=True)

        _stats["misses"] += 1
        artifact = builder()
        _stats["builds"] += 1
        _cache[key] = _CacheEntry(source=source, artifact=artifact)
        _cache.move_to_end(key)
        if len(_cache) > COMPONENT_COMPILATION_CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)
            _stats["evictions"] += 1
        return ComponentArtifactLookup(artifact=artifact, retained=True)


def clear_component_compilation_cache() -> None:
    """Clear all artifacts and counters; used by tests and lifecycle hooks."""
    with _cache_lock:
        _cache.clear()
        for name in _stats:
            _stats[name] = 0
        clear_enabled = getattr(_cache_enabled, "cache_clear", None)
        if clear_enabled is not None:
            clear_enabled()


def component_compilation_cache_stats() -> dict[str, int]:
    """Return a consistent snapshot without exposing cache entries."""
    with _cache_lock:
        return {"entries": len(_cache), **_stats}


def component_compilation_cache_accounting() -> dict[str, object]:
    """Read retained source/AST byte accounting; compiled code is not heap-sized."""
    with _cache_lock:
        return {
            "entries": len(_cache),
            "source_utf8_bytes": sum(
                len(entry.source.encode("utf-8", errors="surrogatepass")) for entry in _cache.values()
            ),
            "ast_pickle_bytes": sum(
                len(entry.artifact.module_template)
                for entry in _cache.values()
                if isinstance(entry.artifact, ComponentCompilationArtifact)
            ),
            "heap_bytes": None,
            "accounting_unit": "retained_source_utf8_and_ast_pickle_bytes",
            "limits": {
                "max_entries": COMPONENT_COMPILATION_CACHE_MAX_ENTRIES,
                "max_source_bytes_per_entry": COMPONENT_COMPILATION_CACHE_MAX_SOURCE_BYTES,
            },
        }
