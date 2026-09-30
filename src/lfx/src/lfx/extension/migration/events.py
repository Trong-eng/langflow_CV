"""Shared migration diagnostics for cold builds and request-local warm copies."""

from __future__ import annotations

from typing import TYPE_CHECKING

from lfx.log.logger import logger

if TYPE_CHECKING:
    from lfx.extension.migration.rewrite import MigrationReport


def report_migration(
    migration_report: MigrationReport,
    *,
    flow_id: str | None,
    user_id: str | None,
    emit_extension_events: bool = True,
) -> None:
    """Log migration diagnostics and optionally emit them in the caller's keyspace."""
    # Surface every typed error from the report through the standard
    # logger so unmapped or ambiguous component references are not
    # silently dropped.  We log rather than raise because the
    # rewriter is intentionally tolerant -- a partially-broken flow
    # still loads, and the frontend renders missing nodes as red
    # placeholders.  The structured ``code``/``hint`` come from
    # ``ExtensionError`` so log scrapers can parse the payload.
    for migration_error in migration_report.errors:
        # Use %s-style positional formatting consistent with the rest of
        # the extension subsystem so the rendered message is readable
        # without relying on structlog's keyword-binding behavior.
        logger.warning(
            "extension migration: code=%s flow_id=%s location=%s hint=%s message=%s",
            migration_error.code,
            flow_id,
            migration_error.location,
            migration_error.hint,
            migration_error.message,
        )
    # Emit extension events so the frontend can surface migration results.
    if emit_extension_events:
        try:
            from lfx.services.deps import get_extension_events_service

            _svc = get_extension_events_service()
            if _svc is not None:
                # Per-user keyspace so flow_id / migration error details only
                # reach the user that loaded the flow; fall back to "global"
                # for unauthenticated paths (CLI, tests, single-user dev).
                _keyspace = f"user:{user_id}" if user_id else "global"
                if migration_report.any_rewritten:
                    _svc.emit(
                        "flow_migrated",
                        {
                            "flow_id": str(flow_id) if flow_id else None,
                            "rewritten_count": migration_report.rewritten_count,
                        },
                        keyspace=_keyspace,
                    )
                for migration_error in migration_report.errors:
                    _svc.emit(
                        "extension_error",
                        {
                            "flow_id": str(flow_id) if flow_id else None,
                            "code": migration_error.code,
                            "message": migration_error.message,
                            "hint": migration_error.hint,
                            "location": migration_error.location,
                        },
                        keyspace=_keyspace,
                    )
        except Exception:  # noqa: BLE001 -- best-effort emit; never break flow load on an event-bus failure
            logger.warning(
                "extension.event_emit_failed: failed to emit migration events in from_payload.",
                exc_info=True,
            )
