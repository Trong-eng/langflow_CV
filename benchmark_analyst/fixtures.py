"""Frozen SQLite/local-storage fixtures; source data is opened read-only."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path

from benchmark_analyst.protocol import file_digest, utc_now, write_json


def _readonly(path: Path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)


def _content_hash(database: Path, storage: Path) -> tuple[str, dict]:
    hashes = {"database": file_digest(database)}
    for path in sorted(storage.rglob("*")):
        if path.is_symlink():
            raise ValueError("fixture storage cannot contain symlinks")
        if path.is_file():
            hashes["storage/" + path.relative_to(storage).as_posix()] = file_digest(path)
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    return digest, hashes


def _referenced_files(data: dict, request_file_overrides: set[tuple[str, str]] | None = None) -> list[str]:
    """Static uploaded-file values only; the fixed model path stays read-only."""
    paths = []
    for node in data.get("nodes", []):
        template = node.get("data", {}).get("node", {}).get("template", {})
        for name, field in template.items():
            if (node.get("id"), name) in (request_file_overrides or set()):
                continue
            if not isinstance(field, dict) or not (name == "files" or field.get("type") == "file"):
                continue
            value = field.get("value")
            values = value if isinstance(value, list) else [value]
            for value in values:
                if isinstance(value, str) and value:
                    path = Path(value)
                    if path.is_absolute() or ".." in path.parts:
                        raise ValueError("static storage reference must be relative to local storage")
                    paths.append(value)
    return sorted(set(paths))


@dataclass
class LocalFixture:
    directory: Path
    metadata: dict
    _owned_clones: set[Path] = dataclass_field(default_factory=set, repr=False)

    @property
    def database_path(self) -> Path:
        return self.directory / "database.db"

    @property
    def storage_path(self) -> Path:
        return self.directory / "storage"

    @property
    def sha256(self) -> str:
        return self.metadata["sha256"]

    @classmethod
    def create(
        cls,
        source_db: Path,
        source_storage: Path,
        destination: Path,
        flow_id: str,
        *,
        request_file_overrides: set[tuple[str, str]] | None = None,
    ):
        source_db, source_storage, destination = (Path(p).resolve() for p in (source_db, source_storage, destination))
        if (
            destination == source_db
            or destination.is_relative_to(source_storage)
            or source_storage.is_relative_to(destination)
        ):
            raise ValueError("fixture destination overlaps source database/storage")
        if destination.exists():
            raise ValueError("fixture destination exists; never overwrite a baseline")
        if not source_db.is_file() or not source_storage.is_dir():
            raise ValueError("local source database/storage is missing")
        with _readonly(source_db) as source:
            # Keep flow references and SQLite backup on the same WAL snapshot.
            source.execute("BEGIN")
            row = source.execute(
                "SELECT data FROM flow WHERE id IN (?, ?)", (flow_id, flow_id.replace("-", ""))
            ).fetchone()
            if row is None:
                raise ValueError("frozen flow is missing from source database")
            data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            references = _referenced_files(data, request_file_overrides)
            # The key is needed for existing encrypted variables; never serialize its value.
            relative_files = references + (["secret_key"] if (source_storage / "secret_key").is_file() else [])
            originals = {}
            for relative in relative_files:
                path = (source_storage / relative).resolve()
                if not path.is_relative_to(source_storage) or not path.is_file():
                    raise ValueError("flow storage reference is missing or escapes source storage")
                originals[relative] = file_digest(path)
            destination.mkdir(parents=True)
            storage = destination / "storage"
            storage.mkdir()
            with sqlite3.connect(destination / "database.db") as target:
                source.backup(target)
            for relative in relative_files:
                target = storage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_storage / relative, target)
                target.chmod(0o600)
                if (
                    file_digest(source_storage / relative) != originals[relative]
                    or file_digest(target) != originals[relative]
                ):
                    raise ValueError("source storage changed during consistent fixture capture")
            pragmas = {
                key: source.execute(f"PRAGMA {key}").fetchone()[0]
                for key in ("journal_mode", "synchronous", "wal_autocheckpoint", "page_size", "busy_timeout")
            }
        digest, hashes = _content_hash(destination / "database.db", storage)
        metadata = {
            "schema_version": 1,
            "created_at": utc_now(),
            "flow_id": flow_id,
            "sha256": digest,
            "files": hashes,
            "source_database_path": str(source_db),
            "source_config_dir": str(source_storage),
            "snapshot_method": "sqlite_backup_api",
            "source_connection_pragmas": pragmas,
            "storage_scope": "static frozen-flow file references and encryption key; generated outputs excluded",
            "request_file_overrides": sorted([list(pair) for pair in (request_file_overrides or set())]),
        }
        write_json(destination / "fixture.json", metadata)
        return cls(destination, metadata)

    @classmethod
    def open(cls, directory: Path):
        directory = Path(directory).resolve()
        fixture = cls(directory, json.loads((directory / "fixture.json").read_text()))
        fixture.verify()
        return fixture

    def verify(self):
        digest, _ = _content_hash(self.database_path, self.storage_path)
        if digest != self.sha256:
            raise ValueError("baseline fixture integrity changed")

    def clone(self, destination: Path) -> dict:
        self.verify()
        destination = Path(destination).resolve()
        source_db = Path(self.metadata["source_database_path"]).resolve()
        source_storage = Path(self.metadata["source_config_dir"]).resolve()
        if (
            destination.exists()
            or destination.is_relative_to(self.directory)
            or destination.is_relative_to(source_storage)
        ):
            raise ValueError("slot fixture destination exists or overlaps source/baseline storage")
        destination.mkdir(parents=True)
        db = destination / "database.db"
        storage = destination / "storage"
        shutil.copy2(self.database_path, db)
        shutil.copytree(self.storage_path, storage)
        if db.resolve() == source_db or _content_hash(db, storage)[0] != self.sha256:
            raise ValueError("slot fixture path/content integrity failed")
        self._owned_clones.add(destination)
        return {
            "database_path": str(db),
            "config_dir": str(storage),
            "fixture_sha256": self.sha256,
            "source_database_path": str(source_db),
            "source_config_dir": str(source_storage),
            "slot_directory": str(destination),
        }

    def release(self, slot: dict) -> None:
        """Remove only a registered, stopped slot's working copy; keep raw evidence."""
        directory = Path(slot.get("slot_directory", "")).resolve()
        if directory not in self._owned_clones or directory == self.directory:
            raise ValueError("refusing to remove a directory not owned by this fixture")
        shutil.rmtree(directory)
        self._owned_clones.remove(directory)


def verify_fixture_worker(snapshot: dict, slot: dict) -> None:
    effective = snapshot.get("effective", {})
    engine = effective.get("database_engine")
    if (
        not isinstance(engine, dict)
        or engine.get("status") != "measured"
        or engine.get("dialect") != "sqlite"
        or effective.get("database_backend") != "sqlite"
    ):
        raise ValueError("fixture profile requires a measured serving SQLite engine")
    for key in ("database_path", "config_dir"):
        actual = effective.get(key)
        expected = Path(slot[key]).resolve()
        if not actual or Path(actual).resolve() != expected:
            raise ValueError(f"worker effective {key} does not match isolated fixture path")
    if effective.get("storage_type", "").lower() != "local":
        raise ValueError("fixture profile requires effective local storage")


def source_locations(env: dict, root: Path) -> tuple[Path, Path]:
    from platformdirs import user_cache_dir
    from sqlalchemy.engine import make_url

    storage_type = env.get("LANGFLOW_STORAGE_TYPE", "local")
    if storage_type.lower() != "local":
        raise ValueError("controlled fixture supports SQLite and local storage only")
    storage = Path(env.get("LANGFLOW_CONFIG_DIR") or user_cache_dir("langflow", "langflow")).expanduser().resolve()
    configured = env.get("LANGFLOW_DATABASE_URL")
    if configured:
        url = make_url(configured)
        if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
            raise ValueError("controlled fixture requires a local SQLite database")
        db = Path(url.database).expanduser().resolve()
    else:
        # Match the repository settings validator's default without constructing it
        # (constructing settings could create source directories or copy a DB).
        package = root / "src/backend/base/langflow"
        if env.get("LANGFLOW_SAVE_DB_IN_CONFIG_DIR", "false").lower() == "true":
            package = storage
        from lfx.utils.version import get_version_info, is_pre_release

        is_pre = is_pre_release(get_version_info()["version"])
        db = package / ("langflow-pre.db" if is_pre else "langflow.db")
        if is_pre and not db.exists() and (package / "langflow.db").exists():
            db = package / "langflow.db"
    if not db.is_file() or not storage.is_dir():
        raise ValueError("cannot resolve existing source database/storage; specify source paths explicitly")
    return db, storage
