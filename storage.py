"""Centralized runtime storage paths for local dev and Railway deploys."""

from __future__ import annotations

import os
import json
import shutil
from pathlib import Path
from datetime import datetime


PROJECT_DIR = Path(__file__).resolve().parent


def _resolve_data_dir() -> Path:
    """Use Railway persistent storage when available, otherwise local project."""
    requested = Path(os.getenv("DATA_DIR", "/data"))
    try:
        requested.mkdir(parents=True, exist_ok=True)
        test_file = requested / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        return requested
    except OSError:
        PROJECT_DIR.mkdir(parents=True, exist_ok=True)
        return PROJECT_DIR


DATA_DIR = _resolve_data_dir()
for directory_name in ("generated_cards", "generated_prompts", "logs"):
    (DATA_DIR / directory_name).mkdir(parents=True, exist_ok=True)


JSON_DEFAULTS = {
    "picks.json": [],
    "results.json": {
        "daily": {},
        "last_7_days": {},
        "last_30_days": {},
        "season": {},
    },
    "posting_log.json": {},
    "daily_card.json": {},
    "model_reports.json": {},
    "props_lab.json": {},
    "approved_props.json": {},
    "model_weights.json": {},
}


def _timestamp() -> str:
    """Filesystem-safe timestamp for backups and write checks."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _write_json_default(path: Path, payload: object) -> None:
    """Create a missing JSON runtime file with a safe default."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _backup_corrupt_file(path: Path) -> Path | None:
    """Move a bad JSON file aside before recreating a safe default."""
    if not path.exists():
        return None
    if path.name == "results.json":
        backup_path = DATA_DIR / f"results_corrupt_{_timestamp()}.json"
    else:
        backup_path = DATA_DIR / f"{path.stem}_corrupt_backup_{_timestamp()}{path.suffix}"
    shutil.move(str(path), str(backup_path))
    return backup_path


def _json_type_valid(filename: str, payload: object) -> bool:
    """Validate the top-level JSON type expected by each runtime file."""
    if filename == "picks.json":
        return isinstance(payload, list)
    return isinstance(payload, dict)


def ensure_json_file(filename: str) -> dict[str, object]:
    """Ensure one runtime JSON file exists and is valid, repairing if needed."""
    default_payload = JSON_DEFAULTS[filename]
    path = DATA_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        _write_json_default(path, default_payload)
        return {"exists": True, "valid": True, "created": True, "backup": None}
    try:
        raw_text = path.read_text(encoding="utf-8").strip()
        if not raw_text:
            raise ValueError("empty JSON file")
        payload = json.loads(raw_text)
        if not _json_type_valid(filename, payload):
            raise ValueError("unexpected JSON type")
        return {"exists": True, "valid": True, "created": False, "backup": None}
    except (OSError, ValueError, json.JSONDecodeError):
        backup = _backup_corrupt_file(path)
        _write_json_default(path, default_payload)
        return {
            "exists": True,
            "valid": True,
            "created": True,
            "backup": str(backup) if backup else None,
        }


def ensure_runtime_storage() -> None:
    """Create every runtime directory/file expected by the bot."""
    for directory_name in ("generated_cards", "generated_prompts", "logs"):
        (DATA_DIR / directory_name).mkdir(parents=True, exist_ok=True)
    for filename, default_payload in JSON_DEFAULTS.items():
        ensure_json_file(filename)


def storage_write_test() -> tuple[bool, str]:
    """Return whether DATA_DIR is writable and the latest write-test timestamp."""
    timestamp = datetime.now().isoformat(timespec="seconds")
    test_path = DATA_DIR / "logs" / "storage_write_test.txt"
    try:
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(timestamp, encoding="utf-8")
        return True, timestamp
    except OSError:
        return False, timestamp


def storage_status() -> dict[str, object]:
    """Owner-facing storage diagnostics for Railway persistent volume checks."""
    ensure_runtime_storage()
    writable, timestamp = storage_write_test()
    picks_path = DATA_DIR / "picks.json"
    results_path = DATA_DIR / "results.json"
    picks_valid = False
    picks_count = 0
    results_valid = False
    try:
        picks_payload = json.loads(picks_path.read_text(encoding="utf-8"))
        picks_valid = isinstance(picks_payload, list)
        picks_count = len(picks_payload) if picks_valid else 0
    except (OSError, ValueError, json.JSONDecodeError):
        picks_valid = False
    try:
        results_payload = json.loads(results_path.read_text(encoding="utf-8"))
        results_valid = isinstance(results_payload, dict)
    except (OSError, ValueError, json.JSONDecodeError):
        results_valid = False
    results_database_healthy = (
        writable
        and picks_path.exists()
        and picks_valid
        and results_path.exists()
        and results_valid
    )
    try:
        disk = shutil.disk_usage(DATA_DIR)
        disk_usage = {
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
            "free_mb": round(disk.free / (1024 * 1024), 1),
        }
    except OSError:
        disk_usage = {"total": None, "used": None, "free": None, "free_mb": None}
    return {
        "data_dir": str(DATA_DIR),
        "writable": writable,
        "last_write_test": timestamp,
        "picks_path": str(picks_path),
        "picks_exists": (DATA_DIR / "picks.json").exists(),
        "picks_valid": picks_valid,
        "picks_count": picks_count,
        "results_path": str(results_path),
        "results_exists": (DATA_DIR / "results.json").exists(),
        "results_valid": results_valid,
        "results_database_healthy": results_database_healthy,
        "posting_log_exists": (DATA_DIR / "posting_log.json").exists(),
        "daily_card_exists": (DATA_DIR / "daily_card.json").exists(),
        "model_reports_exists": (DATA_DIR / "model_reports.json").exists(),
        "props_lab_exists": (DATA_DIR / "props_lab.json").exists(),
        "approved_props_exists": (DATA_DIR / "approved_props.json").exists(),
        "model_weights_exists": (DATA_DIR / "model_weights.json").exists(),
        "generated_cards_path": str(DATA_DIR / "generated_cards"),
        "generated_cards_exists": (DATA_DIR / "generated_cards").exists(),
        "logs_path": str(DATA_DIR / "logs"),
        "logs_exists": (DATA_DIR / "logs").exists(),
        "disk_usage": disk_usage,
    }


ensure_runtime_storage()


def data_file(name: str) -> Path:
    """Return a runtime file or directory path under DATA_DIR."""
    path = DATA_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
