"""Frozen local fixtures isolate all benchmark writes from source data."""

import json
import sqlite3
from pathlib import Path

import pytest


def make_source(tmp_path):
    db = tmp_path / "source.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE flow (id TEXT PRIMARY KEY, data TEXT)")
        conn.execute("INSERT INTO flow VALUES (?, ?)", ("flow", json.dumps({"nodes": []})))
    storage = tmp_path / "live-storage"
    storage.mkdir()
    (storage / "secret_key").write_text("fixture-key")
    return db, storage


def test_sqlite_snapshot_and_each_slot_are_independent(tmp_path):
    from benchmark_analyst.fixtures import LocalFixture

    db, storage = make_source(tmp_path)
    fixture = LocalFixture.create(db, storage, tmp_path / "baseline", "flow")
    first = fixture.clone(tmp_path / "slot1")
    second = fixture.clone(tmp_path / "slot2")
    with sqlite3.connect(first["database_path"]) as conn:
        conn.execute("DELETE FROM flow")
    for path in [db, Path(second["database_path"]), fixture.database_path]:
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT count(*) FROM flow").fetchone()[0] == 1
    assert first["fixture_sha256"] == second["fixture_sha256"]
    assert Path(first["config_dir"]) != storage
    assert (Path(first["config_dir"]) / "secret_key").read_text() == "fixture-key"


def test_fixture_uses_backup_including_uncheckpointed_wal(tmp_path):
    from benchmark_analyst.fixtures import LocalFixture

    db, storage = make_source(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("INSERT INTO flow VALUES (?, ?)", ("new", json.dumps({"nodes": []})))
        conn.commit()
        fixture = LocalFixture.create(db, storage, tmp_path / "baseline", "new")
        with sqlite3.connect(fixture.database_path) as snap:
            assert snap.execute("SELECT id FROM flow WHERE id='new'").fetchone() == ("new",)


def test_fixture_rejects_destination_inside_live_storage_and_symlink(tmp_path):
    from benchmark_analyst.fixtures import LocalFixture

    db, storage = make_source(tmp_path)
    with pytest.raises(ValueError, match="source|storage"):
        LocalFixture.create(db, storage, storage / "baseline", "flow")
    (tmp_path / "alias").symlink_to(storage, target_is_directory=True)
    with pytest.raises(ValueError, match="source|storage"):
        LocalFixture.create(db, storage, tmp_path / "alias" / "baseline", "flow")


def test_fixture_checks_effective_worker_paths_before_requests(tmp_path):
    from benchmark_analyst.fixtures import LocalFixture, verify_fixture_worker

    db, storage = make_source(tmp_path)
    slot = LocalFixture.create(db, storage, tmp_path / "baseline", "flow").clone(tmp_path / "slot")
    verify_fixture_worker(
        {
            "effective": {
                "database_engine": {"status": "measured", "dialect": "sqlite", "driver": "aiosqlite"},
                "database_backend": "sqlite",
                "database_path": slot["database_path"],
                "config_dir": slot["config_dir"],
                "storage_type": "local",
            }
        },
        slot,
    )
    with pytest.raises(ValueError, match="fixture|path"):
        verify_fixture_worker(
            {
                "effective": {
                    "database_engine": {"status": "measured", "dialect": "sqlite", "driver": "aiosqlite"},
                    "database_backend": "sqlite",
                    "database_path": str(db),
                    "config_dir": str(storage),
                    "storage_type": "local",
                }
            },
            slot,
        )


@pytest.mark.parametrize(
    ("engine", "backend"),
    [
        (None, "sqlite"),
        ({"status": "unavailable", "reason": "RuntimeError"}, "sqlite"),
        ({"status": "measured", "dialect": "postgresql", "driver": "asyncpg"}, "postgresql"),
        ({"status": "measured", "dialect": "sqlite", "driver": "aiosqlite"}, "postgresql"),
    ],
)
def test_fixture_rejects_configured_paths_without_actual_sqlite_engine(tmp_path, engine, backend):
    from benchmark_analyst.fixtures import verify_fixture_worker

    slot = {"database_path": str(tmp_path / "clone.db"), "config_dir": str(tmp_path)}
    snapshot = {"effective": {**slot, "storage_type": "local", "database_backend": backend}}
    if engine is not None:
        snapshot["effective"]["database_engine"] = engine
    with pytest.raises(ValueError, match="SQLite|engine|fixture"):
        verify_fixture_worker(snapshot, slot)


def test_fixture_refuses_missing_flow_and_changed_baseline(tmp_path):
    from benchmark_analyst.fixtures import LocalFixture

    db, storage = make_source(tmp_path)
    with pytest.raises(ValueError, match="flow"):
        LocalFixture.create(db, storage, tmp_path / "missing", "absent")
    fixture = LocalFixture.create(db, storage, tmp_path / "baseline", "flow")
    (fixture.storage_path / "secret_key").write_text("changed")
    with pytest.raises(ValueError, match="changed|integrity"):
        fixture.clone(tmp_path / "slot")


def test_release_only_removes_this_fixture_owned_slot(tmp_path):
    from benchmark_analyst.fixtures import LocalFixture

    db, storage = make_source(tmp_path)
    fixture = LocalFixture.create(db, storage, tmp_path / "baseline", "flow")
    slot = fixture.clone(tmp_path / "slot")
    fixture.release(slot)
    assert not (tmp_path / "slot").exists()
    assert db.exists() and storage.exists() and fixture.database_path.exists()
    with pytest.raises(ValueError, match="owned"):
        fixture.release({"slot_directory": str(storage)})


def test_flow_read_and_backup_use_one_read_snapshot(tmp_path, monkeypatch):
    import benchmark_analyst.fixtures as module

    db, storage = make_source(tmp_path)
    with sqlite3.connect(db) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        original = module._readonly

        class Cursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def fetchone(self):
                row = self.cursor.fetchone()
                writer.execute("UPDATE flow SET data=?", (json.dumps({"nodes": [], "revision": 2}),))
                writer.commit()
                return row

        class Connection:
            def __init__(self, path):
                self.connection = original(path)

            def __enter__(self):
                self.connection.__enter__()
                return self

            def __exit__(self, *args):
                return self.connection.__exit__(*args)

            def execute(self, sql, *args):
                cursor = self.connection.execute(sql, *args)
                return Cursor(cursor) if sql.startswith("SELECT data") else cursor

            def backup(self, target):
                return self.connection.backup(target)

        monkeypatch.setattr(module, "_readonly", Connection)
        fixture = module.LocalFixture.create(db, storage, tmp_path / "baseline", "flow")
        with sqlite3.connect(fixture.database_path) as copied:
            assert json.loads(copied.execute("SELECT data FROM flow").fetchone()[0]) == {"nodes": []}
        assert json.loads(writer.execute("SELECT data FROM flow").fetchone()[0])["revision"] == 2


def test_only_explicit_request_file_override_is_excluded_from_static_fixture(tmp_path):
    from benchmark_analyst.fixtures import LocalFixture

    db, storage = make_source(tmp_path)
    data = {
        "nodes": [{"id": "chat", "data": {"node": {"template": {"files": {"type": "file", "value": ["missing.jpg"]}}}}}]
    }
    with sqlite3.connect(db) as writer:
        writer.execute("UPDATE flow SET data=?", (json.dumps(data),))
    with pytest.raises(ValueError, match="reference is missing"):
        LocalFixture.create(db, storage, tmp_path / "bad", "flow")
    fixture = LocalFixture.create(
        db, storage, tmp_path / "baseline", "flow", request_file_overrides={("chat", "files")}
    )
    assert fixture.metadata["request_file_overrides"] == [["chat", "files"]]
    assert fixture.metadata["files"].keys() == {"database", "storage/secret_key"}
