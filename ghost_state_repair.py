"""
GHOST State File Repair

Validates and repairs Ghost's critical state files (config, databases, logs).
"""

import json
import logging
import os
import shutil
import sqlite3
import stat
import time
from pathlib import Path

log = logging.getLogger("quinely.state_repair")

GHOST_HOME = Path.home() / ".ghost"
BACKUP_DIR = GHOST_HOME / "state_backups"
SENSITIVE_FILES = (
    GHOST_HOME / "config.json",
    GHOST_HOME / "auth_profiles.json",
)
SENSITIVE_DIRS = (
    GHOST_HOME / "credentials",
)


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"{path.name}.{ts}.bak"
    shutil.copy2(path, dest)
    return dest


def repair_config(config_path: Path = None) -> dict:
    """Validate and repair config.json. Returns repair report."""
    config_path = config_path or (GHOST_HOME / "config.json")
    report = {"file": str(config_path), "status": "ok", "repairs": []}

    if not config_path.exists():
        report["status"] = "missing"
        report["repairs"].append("Config file does not exist — Ghost will use defaults")
        return report

    try:
        raw = config_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Config root must be a JSON object")
    except (json.JSONDecodeError, ValueError) as e:
        report["status"] = "corrupted"
        bak = _backup(config_path)
        report["repairs"].append(f"Backed up corrupted config to {bak}")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("{}", encoding="utf-8")
        report["repairs"].append("Reset config to empty object — Ghost will use defaults")
        return report

    required_keys = {"model": "google/gemini-2.0-flash-001"}
    for key, default in required_keys.items():
        if key not in data or not data[key]:
            data[key] = default
            report["repairs"].append(f"Restored missing key '{key}' = '{default}'")

    if report["repairs"]:
        _backup(config_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        report["status"] = "repaired"

    return report


def repair_sqlite_db(db_path: Path, label: str = "database") -> dict:
    """Validate and repair a SQLite database. Returns repair report."""
    report = {"file": str(db_path), "label": label, "status": "ok", "repairs": []}

    if not db_path.exists():
        report["status"] = "missing"
        report["repairs"].append(f"{label} does not exist — will be recreated on next use")
        return report

    try:
        conn = sqlite3.connect(str(db_path))
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if result[0] != "ok":
            report["status"] = "corrupted"
            bak = _backup(db_path)
            report["repairs"].append(f"Integrity check failed: {result[0]}")
            report["repairs"].append(f"Backed up corrupted DB to {bak}")
            try:
                conn.execute("REINDEX")
                conn.commit()
                re_check = conn.execute("PRAGMA integrity_check").fetchone()
                if re_check[0] == "ok":
                    report["status"] = "repaired"
                    report["repairs"].append("REINDEX fixed the database")
                else:
                    db_path.unlink()
                    report["repairs"].append("Database was unrecoverable — deleted for recreation")
            except Exception:
                db_path.unlink()
                report["repairs"].append("REINDEX failed — deleted database for recreation")
        else:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
        conn.close()
    except sqlite3.DatabaseError as e:
        report["status"] = "corrupted"
        bak = _backup(db_path)
        report["repairs"].append(f"Cannot open database: {e}")
        report["repairs"].append(f"Backed up corrupted file to {bak}")
        db_path.unlink(missing_ok=True)
        report["repairs"].append("Deleted corrupted file for recreation")
    except Exception as e:
        report["status"] = "error"
        report["repairs"].append(f"Unexpected error: {e}")

    return report


def repair_jsonl(jsonl_path: Path, label: str = "log") -> dict:
    """Validate and repair a JSONL file by dropping malformed lines."""
    report = {"file": str(jsonl_path), "label": label, "status": "ok", "repairs": []}

    if not jsonl_path.exists():
        report["status"] = "missing"
        return report

    try:
        raw_lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        report["status"] = "unreadable"
        report["repairs"].append(f"Cannot read file: {e}")
        return report

    valid_lines = []
    dropped = 0
    for i, line in enumerate(raw_lines):
        line = line.strip()
        if not line:
            continue
        try:
            json.loads(line)
            valid_lines.append(line)
        except json.JSONDecodeError:
            dropped += 1

    if dropped > 0:
        _backup(jsonl_path)
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        jsonl_path.write_text(("\n".join(valid_lines) + "\n") if valid_lines else "", encoding="utf-8")
        report["status"] = "repaired"
        report["repairs"].append(f"Dropped {dropped} malformed line(s) out of {len(raw_lines)}")

    return report


def _mode_label(path: Path) -> str:
    try:
        return oct(stat.S_IMODE(path.stat().st_mode))
    except OSError as exc:
        log.warning("Failed to read mode for %s: %s", path, exc)
        return "unknown"


def harden_sensitive_permissions() -> list[dict]:
    """Enforce owner-only permissions on sensitive Ghost state paths."""
    reports: list[dict] = []

    if os.name == "nt":
        reports.append({
            "file": str(GHOST_HOME),
            "status": "skipped",
            "repairs": ["Permission mode hardening skipped on Windows (ACL-based)."],
        })
        return reports

    targets: list[tuple[Path, int, str]] = []
    for directory in SENSITIVE_DIRS:
        if directory.exists() and directory.is_dir():
            targets.append((directory, 0o700, "directory"))
            for item in directory.glob("*"):
                if item.is_file():
                    targets.append((item, 0o600, "file"))

    for fpath in SENSITIVE_FILES:
        if fpath.exists() and fpath.is_file():
            targets.append((fpath, 0o600, "file"))

    for path, desired_mode, label in targets:
        report = {"file": str(path), "label": f"Sensitive {label}", "status": "ok", "repairs": []}
        try:
            current_mode = stat.S_IMODE(path.stat().st_mode)
        except OSError as exc:
            report["status"] = "error"
            report["repairs"].append(f"Failed to read current mode: {exc}")
            reports.append(report)
            continue

        if current_mode == desired_mode:
            reports.append(report)
            continue

        previous_mode = _mode_label(path)
        bak = _backup(path)
        report["repairs"].append(f"Backed up target to {bak}")
        report["previous_mode"] = previous_mode
        try:
            path.chmod(desired_mode)
            current_mode = _mode_label(path)
            report["current_mode"] = current_mode
            if current_mode == oct(desired_mode):
                report["status"] = "repaired"
                report["fixed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                report["repairs"].append(
                    f"Adjusted mode from {previous_mode} to {current_mode}"
                )
            else:
                report["status"] = "error"
                report["repairs"].append(
                    f"chmod attempted from {previous_mode} to {oct(desired_mode)} but observed {current_mode}"
                )
        except OSError as exc:
            report["status"] = "error"
            report["repairs"].append(f"chmod failed: {exc}")
        reports.append(report)

    return reports


def run_full_repair() -> list[dict]:
    """Run repair on all Ghost state files. Returns list of repair reports."""
    reports = []

    reports.append(repair_config())
    reports.extend(harden_sensitive_permissions())

    for db_name, label in [
        ("memory.db", "Memory database"),
        ("x_tracker.db", "X tracker database"),
    ]:
        reports.append(repair_sqlite_db(GHOST_HOME / db_name, label))

    debug_log = GHOST_HOME / "debug" / "tool_loop_debug.jsonl"
    if debug_log.exists():
        reports.append(repair_jsonl(debug_log, "Debug log"))

    evolve_history = GHOST_HOME / "evolve" / "history.jsonl"
    if evolve_history.exists():
        reports.append(repair_jsonl(evolve_history, "Evolution history"))

    issues = [r for r in reports if r["status"] not in ("ok", "missing")]
    if issues:
        log.warning("State repair found %d issue(s):", len(issues))
        for r in issues:
            log.warning("  %s: %s — %s", r.get("label", r["file"]), r["status"],
                        "; ".join(r["repairs"]))
    else:
        log.info("State repair: all files healthy")

    return reports


def build_state_repair_tools() -> list[dict]:
    """Build repair tools for the Ghost tool registry."""

    def repair_state_exec(**_extra):
        reports = run_full_repair()
        issues = [r for r in reports if r["status"] not in ("ok", "missing")]
        if not issues:
            return "All Ghost state files are healthy. No repairs needed."
        lines = [f"Repaired {len(issues)} issue(s):"]
        for r in issues:
            lines.append(f"  {r.get('label', r['file'])}: {r['status']}")
            for repair in r["repairs"]:
                lines.append(f"    → {repair}")
        return "\n".join(lines)

    return [
        {
            "name": "repair_state",
            "description": (
                "Validate and repair Ghost's state files (config, databases, logs). "
                "Checks integrity of SQLite databases, fixes corrupted JSON files, "
                "drops malformed JSONL lines. Creates backups before any repair."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
            "execute": repair_state_exec,
        }
    ]
