"""
Versioned Full Profile Backup & Restore (.capri-backup.zip) for Closed Beta.

Design guarantees:
- Safe SQLite online backup (`sqlite3.Connection.backup`) so WAL transactions are consistent.
- Never includes `.env` or secret API keys (config.yaml is always scrubbed via `scrub_config_secrets`).
- Manifest v1 (`manifest.json`) records schema_version, capri_version, timestamp, and SHA-256 digests.
- Restore validates manifest, blocks zip-slip path traversal, verifies SHA-256 digests, checks
  SQLite `PRAGMA integrity_check`, and saves a pre-restore safety snapshot before overwriting.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from .. import __version__
from .config import USER_CONFIG_DIR, scrub_config_secrets

BACKUP_SCHEMA_VERSION = 1

PROFILE_JSON_FILES: tuple[str, ...] = (
    "memory.json",
    "emotion.json",
    "context.json",
    "initiative.json",
)
ALLOWED_ARCHIVE_FILES: frozenset[str] = frozenset(
    {"manifest.json", "sessions.db", "config.yaml", *PROFILE_JSON_FILES}
)


@dataclass
class BackupResult:
    archive_path: Path
    included_files: list[str]
    schema_version: int = BACKUP_SCHEMA_VERSION


@dataclass
class RestoreResult:
    restored_files: list[str]
    pre_restore_snapshot: Optional[Path]
    schema_version: int


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sqlite_safe_snapshot(src_db: Path, dst_db: Path) -> None:
    """Create a consistent SQLite snapshot using the built-in online backup API."""
    with sqlite3.connect(str(src_db), timeout=5.0) as src_con:
        with sqlite3.connect(str(dst_db), timeout=5.0) as dst_con:
            src_con.backup(dst_con)


def _verify_sqlite_integrity(db_path: Path) -> bool:
    try:
        with sqlite3.connect(str(db_path), timeout=5.0) as con:
            row = con.execute("PRAGMA integrity_check;").fetchone()
            return bool(row and str(row[0]).lower() == "ok")
    except Exception:
        return False


def export_profile_backup(
    output_path: Optional[Path] = None,
    profile_dir: Optional[Path] = None,
    config_file: Optional[Path] = None,
) -> BackupResult:
    """Export a versioned, secret-free Capri profile archive (.capri-backup.zip)."""
    src_dir = profile_dir or USER_CONFIG_DIR
    src_dir.mkdir(parents=True, exist_ok=True)

    if output_path is None:
        backups_dir = src_dir / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        output_path = backups_dir / f"capri_profile_v{__version__}_{stamp}.capri-backup.zip"

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    files_checksums: dict[str, str] = {}
    included: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        staged: dict[str, Path] = {}

        # 1. SQLite sessions database (online consistent backup)
        sessions_src = src_dir / "sessions.db"
        if sessions_src.exists():
            snap_db = tmp_dir / "sessions.db"
            _sqlite_safe_snapshot(sessions_src, snap_db)
            staged["sessions.db"] = snap_db

        # 2. JSON state files (memory, emotion, context, initiative)
        for fname in PROFILE_JSON_FILES:
            fpath = src_dir / fname
            if fpath.exists() and fpath.is_file():
                staged[fname] = fpath

        # 3. Scrubbed config.yaml (NEVER raw secrets, NEVER .env)
        cfg_src = config_file if config_file is not None else (src_dir / "config.yaml")
        if cfg_src.exists() and cfg_src.is_file():
            raw_cfg = yaml.safe_load(cfg_src.read_text(encoding="utf-8")) or {}
            clean_cfg = scrub_config_secrets(raw_cfg if isinstance(raw_cfg, dict) else {})
            staged_cfg = tmp_dir / "config.yaml"
            staged_cfg.write_text(
                yaml.safe_dump(clean_cfg, allow_unicode=True, default_flow_style=False),
                encoding="utf-8",
            )
            staged["config.yaml"] = staged_cfg

        # 4. Write zip with manifest.json & SHA-256 digests
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for arcname, file_path in sorted(staged.items()):
                data = file_path.read_bytes()
                files_checksums[arcname] = _sha256_bytes(data)
                zf.writestr(arcname, data)
                included.append(arcname)

            manifest = {
                "schema_version": BACKUP_SCHEMA_VERSION,
                "capri_version": __version__,
                "created_at": time.time(),
                "files": files_checksums,
            }
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))

    return BackupResult(archive_path=output_path, included_files=included, schema_version=BACKUP_SCHEMA_VERSION)


def restore_profile_backup(
    archive_path: Path,
    profile_dir: Optional[Path] = None,
    create_pre_restore_snapshot: bool = True,
) -> RestoreResult:
    """
    Validate and restore a versioned Capri profile backup archive.
    Raises ValueError on corrupted, tampered, unsafe, or incompatible archives.
    """
    archive_path = Path(archive_path)
    if not archive_path.exists():
        raise ValueError(f"Архив резервной копии не найден: {archive_path}")

    target_dir = profile_dir or USER_CONFIG_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        try:
            zf = zipfile.ZipFile(archive_path, "r")
        except zipfile.BadZipFile as e:
            raise ValueError(f"Повреждённый ZIP-архив: {e}") from e

        with zf:
            names = zf.namelist()
            # Path traversal / unexpected entry protection
            for name in names:
                if name.startswith("/") or ".." in Path(name).parts or "\\" in name:
                    raise ValueError(f"Небезопасный путь в архиве: {name}")
                if name not in ALLOWED_ARCHIVE_FILES:
                    raise ValueError(f"Недопустимый файл в архиве профиля: {name}")

            if "manifest.json" not in names:
                raise ValueError("Архив не содержит manifest.json.")

            try:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            except Exception as e:
                raise ValueError(f"Некорректный manifest.json: {e}") from e

            schema_v = int(manifest.get("schema_version", 0))
            if schema_v != BACKUP_SCHEMA_VERSION:
                raise ValueError(
                    f"Неподдерживаемая версия схемы резервной копии: {schema_v} (ожидалась {BACKUP_SCHEMA_VERSION})."
                )

            expected_files = manifest.get("files")
            if not isinstance(expected_files, dict):
                raise ValueError("manifest.json не содержит карту контрольных сумм 'files'.")

            # Extract & verify SHA-256 + content validity before touching target_dir
            extracted: dict[str, Path] = {}
            for fname, expected_hash in expected_files.items():
                if fname not in ALLOWED_ARCHIVE_FILES or fname == "manifest.json":
                    raise ValueError(f"Недопустимая запись в manifest.json: {fname}")
                if fname not in names:
                    raise ValueError(f"Файл '{fname}' указан в manifest.json, но отсутствует в архиве.")
                raw_bytes = zf.read(fname)
                actual_hash = _sha256_bytes(raw_bytes)
                if actual_hash != expected_hash:
                    raise ValueError(f"Нарушена контрольная сумма SHA-256 для '{fname}'.")

                staged_file = tmp_dir / fname
                if fname == "config.yaml":
                    # Always scrub on restore too — never allow secrets back into YAML
                    parsed = yaml.safe_load(raw_bytes.decode("utf-8")) or {}
                    clean = scrub_config_secrets(parsed if isinstance(parsed, dict) else {})
                    staged_file.write_text(
                        yaml.safe_dump(clean, allow_unicode=True, default_flow_style=False),
                        encoding="utf-8",
                    )
                else:
                    staged_file.write_bytes(raw_bytes)

                if fname == "sessions.db" and not _verify_sqlite_integrity(staged_file):
                    raise ValueError("Файл sessions.db в архиве не прошёл SQLite PRAGMA integrity_check.")
                if fname.endswith(".json"):
                    try:
                        json.loads(staged_file.read_text(encoding="utf-8"))
                    except Exception as e:
                        raise ValueError(f"Повреждённый JSON '{fname}' в архиве: {e}") from e

                extracted[fname] = staged_file

        # Create safety snapshot of current profile before overwriting
        pre_snap: Optional[Path] = None
        if create_pre_restore_snapshot and any((target_dir / f).exists() for f in ("sessions.db", *PROFILE_JSON_FILES)):
            pre_dir = target_dir / "backups"
            pre_dir.mkdir(parents=True, exist_ok=True)
            pre_path = pre_dir / f"pre_restore_{int(time.time())}.capri-backup.zip"
            pre_snap = export_profile_backup(output_path=pre_path, profile_dir=target_dir).archive_path

        # Atomic-style replacement into target_dir
        restored: list[str] = []
        for fname, staged_file in extracted.items():
            dst = target_dir / fname
            if fname == "sessions.db":
                # Clean stale WAL/SHM sidecars before swapping DB
                for sidecar in (target_dir / "sessions.db-wal", target_dir / "sessions.db-shm"):
                    if sidecar.exists():
                        try:
                            sidecar.unlink()
                        except OSError:
                            pass
            shutil.copy2(staged_file, dst)
            restored.append(fname)

    return RestoreResult(restored_files=restored, pre_restore_snapshot=pre_snap, schema_version=schema_v)
