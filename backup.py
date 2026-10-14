#!/usr/bin/env python3
"""Fast, versioned ZIP backups with ripgrep-compatible ignore handling.

Create ``request.json`` from ``request.json.example``, then run
``python backup.py``. Archives and checksums are always placed in
``data/<slug>/`` beside this script, where <slug> is derived from the
hash of request.json (slug != hash: slug is backup_name + hash prefix).
Version metadata lives in ``.bck/slugs/<slug>/``. Moving or cloning the
script therefore moves the complete backup repository; the working directory
from which it is invoked is never used for output.
Use --list-slugs to see all request slugs.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import fnmatch
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


# Loaded by main before any backup operation. Keeping machine paths outside
# this file makes the project safe to publish and clone anywhere.
CONFIG: dict[str, Any] = {}


SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DATA_DIR = SCRIPT_DIR / "data"
BASE_BCK_DIR = SCRIPT_DIR / ".bck"
# These are set per-request after hashing request.json (slug isolation)
DATA_DIR: Path = BASE_DATA_DIR
BCK_DIR: Path = BASE_BCK_DIR
STAGING_ROOT: Path = BCK_DIR / "tmp"
SNAPSHOTS_DIR: Path = BCK_DIR / "snapshots"
HEAD_FILE: Path = BCK_DIR / "HEAD"
INDEX_FILE: Path = BCK_DIR / "index.json"
HISTORY_FILE: Path = BCK_DIR / "history.jsonl"
LOCK_FILE: Path = BCK_DIR / "lock"
REQUEST_HASH: str = ""
REQUEST_SLUG: str = ""
DEFAULT_REQUEST = SCRIPT_DIR / "request.json"
SCHEMA_FILE = SCRIPT_DIR / "request.schema.json"
SLUGMAP_FILE = BASE_BCK_DIR / "slugs.json"


def _load_dotenv() -> None:
    # Minimal .env loader — no deps. Loaded before request so ${VAR} in request.json works.
    # Priority: existing env vars win over .env file.
    for env_path in (SCRIPT_DIR / ".env", Path.cwd() / ".env"):
        if not env_path.is_file():
            continue
        try:
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = os.path.expandvars(val)
        except Exception:
            pass
        break


_load_dotenv()


@dataclass(frozen=True)
class BackupItem:
    source: Path
    archive_path: PurePosixPath


def original_home() -> Path:
    """Return the invoking user's home even after sudo elevation."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and os.name != "nt":
        import pwd

        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass
    return Path.home()


def expand_path(value: str) -> Path:
    value = os.path.expandvars(value)
    if value == "~" or value.startswith("~/") or value.startswith("~\\"):
        value = str(original_home()) + value[1:]
    return Path(value).expanduser().resolve()


def safe_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    if not label:
        raise ValueError(f"Invalid empty archive label derived from {value!r}")
    return label


def _request_hash_and_slug(request: dict[str, Any]) -> tuple[str, str]:
    # Slug isolates different *source sets*, not delivery method.
    # Hash only backup_name + sources + files + environment (stable across delivery/ archive tweaks).
    # delivery, require_elevation, archive are intentionally excluded so enabling rclone
    # doesn't create a new slug / version history.
    slim = {
        "backup_name": request.get("backup_name"),
        "sources": request.get("sources"),
        "files": request.get("files"),
        "environment": request.get("environment"),
    }
    canonical = json.dumps(slim, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    h = hashlib.sha256(canonical).hexdigest()
    slug = f"{safe_label(request.get('backup_name', 'backup'))}-{h[:8]}"
    return h, slug


def _setup_request_dirs(request: dict[str, Any], request_path: Path) -> tuple[str, str]:
    global DATA_DIR, BCK_DIR, STAGING_ROOT, SNAPSHOTS_DIR, HEAD_FILE, INDEX_FILE, HISTORY_FILE, LOCK_FILE, REQUEST_HASH, REQUEST_SLUG
    h, slug = _request_hash_and_slug(request)
    REQUEST_HASH = h
    REQUEST_SLUG = slug
    DATA_DIR = BASE_DATA_DIR / slug
    BCK_DIR = BASE_BCK_DIR / "slugs" / slug
    STAGING_ROOT = BCK_DIR / "tmp"
    SNAPSHOTS_DIR = BCK_DIR / "snapshots"
    HEAD_FILE = BCK_DIR / "HEAD"
    INDEX_FILE = BCK_DIR / "index.json"
    HISTORY_FILE = BCK_DIR / "history.jsonl"
    LOCK_FILE = BCK_DIR / "lock"
    # maintain slug map for listing / reuse (slug != hash, it's backup_name + hash prefix)
    try:
        BASE_BCK_DIR.mkdir(parents=True, exist_ok=True)
        existing: dict[str, Any] = {}
        if SLUGMAP_FILE.exists():
            try:
                existing = json.loads(SLUGMAP_FILE.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        # store by slug
        existing[slug] = {
            "slug": slug,
            "hash": h,
            "backup_name": request.get("backup_name"),
            "request_path": str(request_path),
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        # also index by hash for quick lookup
        tmp = SLUGMAP_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(existing, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
        os.replace(tmp, SLUGMAP_FILE)
    except Exception:
        pass  # non-critical
    return h, slug


def load_request(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(
            f"Request file not found: {path}. Copy request.json.example to request.json first."
        )
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in {path}: line {error.lineno}, column {error.colno}: {error.msg}") from error
    if not isinstance(request, dict):
        raise ValueError("The request root must be a JSON object")

    required_sections = {"backup_name", "sources", "files", "environment", "archive", "delivery"}
    missing = required_sections - request.keys()
    if missing:
        raise ValueError(f"Request is missing required keys: {', '.join(sorted(missing))}")
    if not isinstance(request["sources"], list) or not isinstance(request["files"], list):
        raise ValueError("sources and files must be JSON arrays")
    if not isinstance(request["delivery"], list):
        raise ValueError("delivery must be a JSON array")
    if not isinstance(request["environment"], dict) or not isinstance(request["archive"], dict):
        raise ValueError("environment and archive must be JSON objects")

    names: set[str] = set()
    for kind in ("sources", "files"):
        for position, entry in enumerate(request[kind]):
            if not isinstance(entry, dict):
                raise ValueError(f"{kind}[{position}] must be an object")
            for field in ("name", "path", "enabled"):
                if field not in entry:
                    raise ValueError(f"{kind}[{position}] is missing {field!r}")
            if entry["name"] in names:
                raise ValueError(f"Target name must be unique: {entry['name']!r}")
            names.add(entry["name"])
            if kind == "sources":
                entry.setdefault("include_git", False)
                if entry.get("include_git"):
                    # Opt-in: git objects ride along; drop the object-dir
                    # exclusion everywhere downstream (rg globs, walk prune,
                    # pattern matching all read cfg["exclude"]).
                    entry["exclude"] = [
                        pattern
                        for pattern in entry.get("exclude", [])
                        if pattern not in ("**/.git/**", "**/.git")
                    ]
                for field in ("path_regex_include", "path_regex_exclude", "content_regex_include", "content_regex_exclude"):
                    for expression in entry.get(field, []):
                        try:
                            re.compile(expression)
                        except re.error as error:
                            raise ValueError(f"Invalid {field} in {entry['name']!r}: {expression!r}: {error}") from error

    environment = request["environment"]
    for field in ("include_regex", "exclude_regex", "redact_regex"):
        for expression in environment.get(field, []):
            try:
                re.compile(expression)
            except re.error as error:
                raise ValueError(f"Invalid environment.{field}: {expression!r}: {error}") from error

    archive = request["archive"]
    archive.setdefault("compression_level", 6)
    archive.setdefault("workers", 0)
    archive.setdefault("staging_mode", "copy")
    archive.setdefault("checksum", True)
    archive.setdefault("keep_last", 10)
    archive.setdefault("fail_on_unreadable", True)
    if archive["staging_mode"] not in ("copy", "auto", "hardlink"):
        raise ValueError("archive.staging_mode must be copy, auto, or hardlink")
    if not 0 <= int(archive["compression_level"]) <= 9:
        raise ValueError("archive.compression_level must be between 0 and 9")
    archive.setdefault("encryption", {})
    encryption = archive["encryption"]
    if not isinstance(encryption, dict):
        raise ValueError("archive.encryption must be a JSON object")
    encryption.setdefault("enabled", False)
    encryption.setdefault("method", "7z-aes")
    encryption.setdefault("passphrase_from", "prompt")
    encryption.setdefault("keyring_service", "bck-backup")
    encryption.setdefault("keyring_user", "")
    if encryption.get("method") != "7z-aes":
        raise ValueError("archive.encryption.method must be '7z-aes' (WinZip AES-256 via 7z)")
    if encryption.get("passphrase_from") not in ("prompt", "env", "keyring"):
        raise ValueError("archive.encryption.passphrase_from must be prompt, env, or keyring")
    request.setdefault("require_elevation", True)
    # notes: non-transferrable machine knowledge (software inventory digest,
    # PATH tooling, user custom text, command outputs). Optional; missing
    # section means disabled. Never affects the request slug/hash.
    request.setdefault("notes", {})
    notes = request["notes"]
    if not isinstance(notes, dict):
        raise ValueError("notes must be a JSON object")
    notes.setdefault("enabled", False)
    notes.setdefault("include_software", True)
    notes.setdefault("include_path", True)
    notes.setdefault("custom", [])
    notes.setdefault("capture_commands", [])
    if not isinstance(notes.get("custom", []), list) or not isinstance(
        notes.get("capture_commands", []), list
    ):
        raise ValueError("notes.custom and notes.capture_commands must be arrays")
    request.setdefault("git_state", {})
    git_state = request["git_state"]
    if not isinstance(git_state, dict):
        raise ValueError("git_state must be a JSON object")
    git_state.setdefault("enabled", False)
    request.setdefault("restore", {})
    restore = request["restore"]
    if not isinstance(restore, dict):
        raise ValueError("restore must be a JSON object")
    restore.setdefault("path_aliases", [])
    for alias in restore["path_aliases"]:
        if not isinstance(alias, dict) or not alias.get("from") or not alias.get("to"):
            raise ValueError("restore.path_aliases entries need {from, to}")
    request.setdefault("symlinks", {})
    symlinks_cfg = request["symlinks"]
    if not isinstance(symlinks_cfg, dict):
        raise ValueError("symlinks must be a JSON object")
    symlinks_cfg.setdefault("enabled", False)
    return request


def is_elevated() -> bool:
    if os.name == "nt":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0


def selected_environment_names() -> list[str]:
    cfg = CONFIG["environment"]
    names = set(cfg.get("names", []))
    prefixes = tuple(cfg.get("prefixes", []))
    include_regex = [re.compile(pattern) for pattern in cfg.get("include_regex", [])]
    if cfg.get("include_all") or prefixes or include_regex:
        names.update(
            name
            for name in os.environ
            if cfg.get("include_all")
            or name.startswith(prefixes)
            or any(pattern.search(name) for pattern in include_regex)
        )
    return sorted(
        name
        for name in names
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) and name != "BACKUP_PASSPHRASE"
    )


def request_elevation(argv: list[str], request_path: Path) -> None:
    if is_elevated() or not CONFIG.get("require_elevation", False) or "--no-elevate" in argv:
        return
    if "--elevated" in argv:
        raise PermissionError("Elevation was requested but administrator privileges were not granted")

    forwarded = [
        str(Path(__file__).resolve()),
        *argv,
        "--request",
        str(request_path),
        "--elevated",
    ]
    if os.name == "nt":
        import subprocess as sp

        params = sp.list2cmdline(forwarded)
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, str(SCRIPT_DIR), 1
        )
        if result <= 32:
            raise PermissionError("Windows administrator elevation was cancelled or failed")
        raise SystemExit(0)

    sudo = shutil.which("sudo")
    if not sudo:
        raise RuntimeError("Elevation is required, but sudo is not installed")
    command = [sudo]
    preserve = selected_environment_names()
    # Carry a prompted passphrase into the elevated child (memory only, and
    # capture_environment()/selected_environment_names() exclude it below).
    if os.environ.get("BACKUP_PASSPHRASE") and "BACKUP_PASSPHRASE" not in preserve:
        preserve.append("BACKUP_PASSPHRASE")
    if preserve:
        command.append(f"--preserve-env={','.join(preserve)}")
    command.extend([sys.executable, *forwarded])
    os.execv(sudo, command)


def matches_patterns(relative: str, includes: list[str], excludes: list[str]) -> bool:
    relative = relative.replace(os.sep, "/")
    included = not includes or any(
        fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch("/" + relative, pattern)
        for pattern in includes
    )
    excluded = any(
        fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch("/" + relative, pattern)
        for pattern in excludes
    )
    return included and not excluded


def matches_regexes(value: str, includes: list[str], excludes: list[str]) -> bool:
    included = not includes or any(re.search(pattern, value) for pattern in includes)
    excluded = any(re.search(pattern, value) for pattern in excludes)
    return included and not excluded


def matches_file_selection(path: Path, root: Path, cfg: dict[str, Any]) -> bool:
    relative = path.relative_to(root).as_posix()
    if not matches_patterns(relative, cfg.get("include", []), cfg.get("exclude", [])):
        return False
    if not matches_regexes(
        relative,
        cfg.get("path_regex_include", []),
        cfg.get("path_regex_exclude", []),
    ):
        return False
    content_includes = cfg.get("content_regex_include", [])
    content_excludes = cfg.get("content_regex_exclude", [])
    if not content_includes and not content_excludes:
        return True
    try:
        content = path.read_text(encoding=cfg.get("content_encoding", "utf-8"), errors="replace")
    except OSError:
        return False
    return matches_regexes(content, content_includes, content_excludes)


def is_internal_output(path: Path) -> bool:
    for internal in (BASE_DATA_DIR, BASE_BCK_DIR):
        try:
            path.resolve().relative_to(internal.resolve())
            return True
        except ValueError:
            pass
    return False


def enumerate_with_rg(root: Path, cfg: dict[str, Any]) -> list[Path]:
    rg = shutil.which("rg")
    if not rg:
        raise RuntimeError(
            f"{cfg['name']!r} requires ripgrep because respect_gitignore is enabled"
        )
    command = [rg, "--files", "--hidden", "--no-require-git", "--null"]
    if cfg.get("follow_symlinks", False):
        command.append("--follow")
    for pattern in cfg.get("include", []):
        if pattern != "**":
            command.extend(["--glob", pattern])
    for pattern in cfg.get("exclude", []):
        command.extend(["--glob", "!" + pattern.lstrip("!")])
    result = subprocess.run(command, cwd=root, capture_output=True, check=False)
    stderr = result.stderr.decode(errors="replace").strip()
    # rg may exit non-zero when some dirs are unreadable (e.g. docker volumes).
    # Keep whatever it listed and surface the error as a warning unless
    # it produced no output at all.
    if result.returncode not in (0, 1) and not result.stdout:
        raise RuntimeError(f"ripgrep failed for {root}: {stderr}")
    paths: set[Path] = set()
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        path = root / os.fsdecode(raw)
        if not is_internal_output(path) and matches_file_selection(path, root, cfg):
            paths.add(path)
    # stash stderr warning for caller (via per_target error bubbling)
    if stderr and result.returncode not in (0, 1):
        # keep but don't fail if we got files; caller will show it
        cfg["_rg_stderr"] = stderr

    force_include = cfg.get("force_include", [])
    if force_include:
        forced_command = [rg, "--files", "--hidden", "--no-require-git", "--no-ignore", "--null"]
        if cfg.get("follow_symlinks", False):
            forced_command.append("--follow")
        for pattern in force_include:
            forced_command.extend(["--glob", pattern])
        for pattern in cfg.get("exclude", []):
            forced_command.extend(["--glob", "!" + pattern.lstrip("!")])
        forced = subprocess.run(forced_command, cwd=root, capture_output=True, check=False)
        f_stderr = forced.stderr.decode(errors="replace").strip()
        if forced.returncode not in (0, 1) and not forced.stdout:
            raise RuntimeError(f"ripgrep force_include failed for {root}: {f_stderr}")
        if f_stderr and forced.returncode not in (0, 1):
            cfg["_rg_stderr"] = (cfg.get("_rg_stderr", "") + "\n" + f_stderr).strip()
        for raw in forced.stdout.split(b"\0"):
            if not raw:
                continue
            path = root / os.fsdecode(raw)
            if not is_internal_output(path) and matches_file_selection(path, root, cfg):
                paths.add(path)
    return sorted(paths)


def enumerate_without_ignores(root: Path, cfg: dict[str, Any]) -> list[Path]:
    includes = cfg.get("include", [])
    excludes = cfg.get("exclude", [])
    follow = cfg.get("follow_symlinks", False)
    paths: list[Path] = []
    visited_directories: set[Path] = set()
    for current, directories, filenames in os.walk(root, followlinks=follow):
        current_path = Path(current)
        resolved_current = current_path.resolve()
        if resolved_current in visited_directories:
            directories[:] = []
            continue
        visited_directories.add(resolved_current)
        directories[:] = [
            name
            for name in directories
            if not is_internal_output(current_path / name)
            and (follow or not (current_path / name).is_symlink())
            and matches_patterns(
                str((current_path / name).relative_to(root)) + "/",
                ["**"],
                excludes,
            )
        ]
        for filename in filenames:
            path = current_path / filename
            relative = str(path.relative_to(root))
            if not path.exists() and path.is_symlink():
                continue
            if (
                not is_internal_output(path)
                and (follow or not path.is_symlink())
                and matches_file_selection(path, root, cfg)
            ):
                paths.append(path)
    return paths


def enumerate_items(only: set[str]) -> tuple[list[BackupItem], list[str], dict[str, dict[str, Any]]]:
    items: list[BackupItem] = []
    warnings: list[str] = []
    per_target: dict[str, dict[str, Any]] = {}

    for cfg in CONFIG.get("sources", []):
        if not cfg.get("enabled") or (only and cfg["name"] not in only):
            continue
        root = expand_path(cfg["path"])
        if not root.is_dir():
            raise FileNotFoundError(f"Source directory does not exist: {root}")
        cfg.pop("_rg_stderr", None)
        try:
            paths = (
                enumerate_with_rg(root, cfg)
                if cfg.get("respect_gitignore")
                else enumerate_without_ignores(root, cfg)
            )
            error = cfg.pop("_rg_stderr", None)
            if error:
                warnings.append(f"{cfg['name']}: {error}")
        except Exception as exc:
            paths = []
            error = str(exc)
            warnings.append(f"{cfg['name']}: {error}")
        label = safe_label(cfg["name"])
        for path in paths:
            items.append(BackupItem(path, PurePosixPath("sources", label, path.relative_to(root).as_posix())))
        is_hard_fail = error is not None and len(paths) == 0
        per_target[cfg["name"]] = {
            "kind": "source",
            "path": str(root),
            "file_count": len(paths),
            "bytes": sum((p.stat().st_size for p in paths if p.exists()), 0),
            "status": "failed" if is_hard_fail else ("ok" if not error else "ok_with_warnings"),
            "error": error,
        }

    for cfg in CONFIG.get("files", []):
        if not cfg.get("enabled") or (only and cfg["name"] not in only):
            continue
        expanded = str(expand_path(cfg["path"]))
        matches = [Path(path).resolve() for path in glob.glob(expanded, recursive=True)]
        matches = [path for path in matches if path.is_file() and not is_internal_output(path)]
        if not matches:
            message = f"File target {cfg['name']!r} matched nothing: {cfg['path']}"
            if cfg.get("required"):
                raise FileNotFoundError(message)
            warnings.append(message)
            per_target[cfg["name"]] = {"kind": "files", "path": cfg["path"], "file_count": 0, "bytes": 0, "status": "empty", "error": message}
            continue
        label = safe_label(cfg["name"])
        common = Path(os.path.commonpath([str(path.parent) for path in matches]))
        for path in matches:
            relative = path.relative_to(common) if len(matches) > 1 else Path(path.name)
            items.append(BackupItem(path, PurePosixPath("files", label, relative.as_posix())))
        per_target[cfg["name"]] = {
            "kind": "files",
            "path": cfg["path"],
            "file_count": len(matches),
            "bytes": sum(p.stat().st_size for p in matches),
            "status": "ok",
            "error": None,
        }

    by_archive_path: dict[PurePosixPath, BackupItem] = {}
    for item in items:
        previous = by_archive_path.get(item.archive_path)
        if previous and previous.source != item.source:
            raise ValueError(
                f"Archive path collision: {previous.source} and {item.source} "
                f"both map to {item.archive_path}"
            )
        by_archive_path[item.archive_path] = item
    deduped = sorted(by_archive_path.values(), key=lambda item: str(item.archive_path))
    # recompute per_target counts after dedup
    return deduped, warnings, per_target


def capture_environment() -> dict[str, str]:
    cfg = CONFIG["environment"]
    if not cfg.get("enabled"):
        return {}
    redaction_enabled = cfg.get("redaction_enabled", True)
    excludes = cfg.get("exclude", [])
    redacts = cfg.get("redact", [])
    exclude_regex = [re.compile(pattern) for pattern in cfg.get("exclude_regex", [])]
    redact_regex = [re.compile(pattern) for pattern in cfg.get("redact_regex", [])]
    captured: dict[str, str] = {}
    for name in selected_environment_names():
        if name == "BACKUP_PASSPHRASE":
            continue  # never persist the archive passphrase
        if (
            name not in os.environ
            or any(fnmatch.fnmatch(name, pattern) for pattern in excludes)
            or any(pattern.search(name) for pattern in exclude_regex)
        ):
            continue
        captured[name] = (
            "<redacted>"
            if redaction_enabled
            and (
                any(fnmatch.fnmatch(name, pattern) for pattern in redacts)
                or any(pattern.search(name) for pattern in redact_regex)
            )
            else os.environ[name]
        )
    return captured


def _run_note_command(command: str) -> dict[str, Any]:
    """Run one notes.capture_commands entry without ever raising."""
    import shlex

    try:
        argv = shlex.split(command)
    except Exception as error:
        return {"returncode": None, "stdout": "", "stderr": f"parse failed: {error}"}
    if not argv:
        return {"returncode": None, "stdout": "", "stderr": "empty command"}
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
        return {
            "returncode": result.returncode,
            "stdout": (result.stdout or "")[:20000],
            "stderr": (result.stderr or "")[-2000:],
        }
    except Exception as error:
        return {"returncode": None, "stdout": "", "stderr": f"failed: {error}"}


def collect_notes() -> dict[str, Any]:
    """Gather non-transferrable machine knowledge (notes feature).

    Unlike sources/files, notes are facts about this machine that cannot be
    restored elsewhere: installed software digest, PATH tooling, operator
    notes, and captured command outputs. Stored as _backup/notes.json plus
    human-readable _backup/NOTES.txt. Never affects the request slug/hash.
    """
    cfg = CONFIG.get("notes", {}) or {}
    if not cfg.get("enabled"):
        return {}
    notes: dict[str, Any] = {
        "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if cfg.get("include_software", True):
        try:
            inventory = collect_software_inventory()
            notes["software"] = {
                "platform": inventory.get("platform", {}),
                "system_package_counts": {
                    manager: len(packages)
                    for manager, packages in inventory.get("system_packages", {}).items()
                },
                "desktop_application_count": len(inventory.get("desktop_applications", [])),
                "developer_tools": {
                    tool: (len(value) if isinstance(value, (dict, list)) else value)
                    for tool, value in inventory.get("developer_tools", {}).items()
                },
                "user_path_executable_count": len(inventory.get("user_path_executables", [])),
                "user_path_executables": [
                    entry.get("name", "") for entry in inventory.get("user_path_executables", [])
                ][:500],
            }
            # Full per-package detail already ships as _backup/software_inventory.json.
        except Exception as error:
            notes["software_error"] = str(error)
    if cfg.get("include_path", True):
        path_dirs = os.environ.get("PATH", "").split(os.pathsep)
        notes["path_dirs"] = path_dirs
        notes["path_dir_status"] = [
            {"dir": entry, "exists": Path(entry).is_dir() if entry else False}
            for entry in path_dirs
        ]
    notes["custom"] = list(cfg.get("custom", []))
    commands: dict[str, Any] = {}
    for command in cfg.get("capture_commands", []):
        if isinstance(command, str) and command.strip():
            commands[command] = _run_note_command(command)
    notes["commands"] = commands
    return notes


def resolve_passphrase(purpose: str) -> str:
    """Resolve the archive passphrase for backup/restore.

    BACKUP_PASSPHRASE env wins in every mode (automation/CI, and the channel
    used to pass a prompted passphrase through sudo elevation). Otherwise the
    request's passphrase_from selects: prompt (getpass, needs a tty), env
    (must be set), or keyring (OS credential store via the optional
    `keyring` package). The secret is never logged, never captured into
    environment.json, and never written to manifests/snapshots.
    """
    enc = CONFIG.get("archive", {}).get("encryption", {}) or {}
    if not enc.get("enabled"):
        raise ValueError("archive.encryption is not enabled")
    from_env = os.environ.get("BACKUP_PASSPHRASE")
    if from_env:
        return from_env
    mode = enc.get("passphrase_from", "prompt")
    if mode == "env":
        raise RuntimeError(
            "archive.encryption.passphrase_from=env but BACKUP_PASSPHRASE is not set"
        )
    if mode == "keyring":
        try:
            import keyring
        except ImportError:
            raise RuntimeError(
                "passphrase_from=keyring needs the 'keyring' package "
                "(pip install keyring), or use env/prompt instead"
            )
        service = enc.get("keyring_service", "bck-backup") or "bck-backup"
        user = enc.get("keyring_user") or platform.node()
        try:
            secret = keyring.get_password(service, user)
        except Exception as error:
            raise RuntimeError(
                f"keyring lookup failed ({service}/{user}): {error}. "
                "Store one first: python backup.py --store-passphrase"
            )
        if not secret:
            raise RuntimeError(
                f"no passphrase in keyring for {service}/{user}; "
                "store one first: python backup.py --store-passphrase"
            )
        return secret
    import getpass

    if not sys.stdin.isatty():
        raise RuntimeError(
            "no tty for passphrase prompt; set BACKUP_PASSPHRASE or use passphrase_from=keyring"
        )
    secret = getpass.getpass(f"Backup passphrase ({purpose}): ")
    if not secret:
        raise ValueError("empty passphrase")
    return secret


def do_store_passphrase(args: argparse.Namespace) -> int:
    """Prompt twice and store the passphrase in the OS keyring. No backup runs."""
    import getpass

    try:
        import keyring
    except ImportError:
        raise RuntimeError(
            "storing needs the 'keyring' package (pip install keyring); "
            "otherwise export BACKUP_PASSPHRASE instead"
        )
    if not sys.stdin.isatty():
        raise RuntimeError("no tty for passphrase prompt")
    enc = CONFIG.get("archive", {}).get("encryption", {}) or {}
    service = enc.get("keyring_service", "bck-backup") or "bck-backup"
    user = enc.get("keyring_user") or platform.node()
    first = getpass.getpass("New backup passphrase: ")
    second = getpass.getpass("Repeat backup passphrase: ")
    if not first:
        raise ValueError("empty passphrase")
    if first != second:
        raise ValueError("passphrases do not match")
    keyring.set_password(service, user, first)
    print(f"Stored passphrase in keyring: service={service} user={user}")
    return 0


def _git_repo_state(repo: Path) -> dict[str, Any] | None:
    if not shutil.which("git"):
        return None

    def run(*argv: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo), *argv],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except Exception:
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    head = run("rev-parse", "HEAD")
    if not head:
        return None
    upstream = run("rev-parse", "--abbrev-ref", "@{upstream}")
    ahead: int | None = None
    behind: int | None = None
    if upstream:
        counts = run("rev-list", "--left-right", "--count", "HEAD...@{upstream}")
        if counts:
            try:
                ahead_s, behind_s = counts.split()
                ahead, behind = int(ahead_s), int(behind_s)
            except ValueError:
                pass
    remotes: dict[str, str] = {}
    for line in (run("remote", "-v") or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in remotes:
            remotes[parts[0]] = parts[1]
    dirty_raw = run("status", "--porcelain=v1", "--untracked-files=no")
    dirty = len(dirty_raw.splitlines()) if dirty_raw else 0
    return {
        "path": str(repo),
        "branch": run("rev-parse", "--abbrev-ref", "HEAD") or "?",
        "head": head,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "remotes": remotes,
        "dirty": dirty,
    }


def collect_git_state(only: set[str]) -> list[dict[str, Any]]:
    """Record git identity (not objects) for repos under in-scope sources.

    `.git` object dirs stay excluded by default (see source `include_git`);
    this captures remote URLs, branches, HEAD SHAs and ahead/behind counts so
    a receiver can re-create history with plain git commands. Read-only.
    """
    cfg = CONFIG.get("git_state", {}) or {}
    if not cfg.get("enabled") or not shutil.which("git"):
        return []
    repos: list[dict[str, Any]] = []
    seen: set[str] = set()
    skip_dirs = {".bck", "data", "node_modules", ".venv", "target", "dist", "build"}
    for source in CONFIG.get("sources", []):
        if not source.get("enabled") or (only and source["name"] not in only):
            continue
        root = expand_path(source["path"])
        if not root.is_dir():
            continue
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            if depth > 4:
                continue
            try:
                children = list(current.iterdir())
            except OSError:
                continue
            if any(child.name == ".git" for child in children):
                key = str(current.resolve())
                if key not in seen:
                    seen.add(key)
                    state = _git_repo_state(current)
                    if state:
                        repos.append(state)
                continue  # do not descend into the repo itself
            if depth < 4:
                stack.extend(
                    (child, depth + 1)
                    for child in children
                    if child.is_dir()
                    and not child.is_symlink()
                    and child.name not in skip_dirs
                )
    return sorted(repos, key=lambda item: item["path"])


def collect_symlinks(only: set[str], cap_per_source: int = 500) -> dict[str, dict[str, Any]]:
    """Inventory symlinks under in-scope sources for serve display.

    Links are never backed up as items (follow_symlinks=false skips them);
    this records {rel, target, is_dir, exists} so report/plan/load can show
    them. Prunes each source's own excludes (node_modules et al stay out).
    Capped per source; count is always exact. Read-only.
    Blocked entirely unless the request sets symlinks.enabled.
    """
    if not (CONFIG.get("symlinks", {}) or {}).get("enabled"):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for cfg in CONFIG.get("sources", []):
        if not cfg.get("enabled") or (only and cfg["name"] not in only):
            continue
        root = expand_path(cfg["path"])
        if not root.is_dir():
            continue
        excludes = cfg.get("exclude", [])
        if cfg.get("include_git"):
            excludes = [e for e in excludes if e not in ("**/.git/**", "**/.git")]
        links: list[dict[str, Any]] = []
        total = 0
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                children = list(current.iterdir())
            except OSError:
                continue
            for child in children:
                try:
                    if child.is_symlink():
                        total += 1
                        if len(links) < cap_per_source:
                            try:
                                target = os.readlink(child)
                            except OSError:
                                target = "?"
                            links.append({
                                "rel": child.relative_to(root).as_posix(),
                                "target": target,
                                "is_dir": child.is_dir(),
                                "exists": child.exists(),
                            })
                        continue
                    if child.is_dir():
                        rel = child.relative_to(root).as_posix() + "/"
                        if matches_patterns(rel, ["**"], excludes):
                            stack.append(child)
                except OSError:
                    continue
        out[cfg["name"]] = {
            "root": str(root), "count": total, "items": links,
            "truncated": total > len(links),
        }
    return out


def check_all_dependencies(request: dict[str, Any]) -> str:
    """Validate all required and delivery dependencies upfront.
    Returns the resolved path to 7z/7zz/7za or raises RuntimeError with actionable instructions."""
    missing: list[str] = []
    
    # 1. Check 7-Zip (mandatory)
    seven_zip: str | None = None
    for executable in ("7zz", "7z", "7za"):
        found = shutil.which(executable)
        if found:
            seven_zip = found
            break
    if not seven_zip:
        if os.name == "nt":
            inst = "Install 7-Zip from https://www.7-zip.org/ and ensure 7z.exe is in PATH."
        elif platform.system() == "Darwin":
            inst = "Install with: brew install sevenzip (or brew install p7zip)"
        else:
            inst = "Install with: sudo apt install 7zip  (or: sudo dnf install 7zip / sudo pacman -S 7zip)"
        missing.append(f"• 7-Zip (7z/7zz/7za) [Required for compression]:\n    {inst}")
        
    # 2. Check ripgrep (mandatory if any source has respect_gitignore=True)
    needs_rg = any(s.get("enabled") and s.get("respect_gitignore") for s in request.get("sources", []))
    if needs_rg and not shutil.which("rg"):
        if os.name == "nt":
            inst = "Install with: winget install BurntSushi.ripgrep.MSVC  (or: choco install ripgrep / scoop install ripgrep)"
        elif platform.system() == "Darwin":
            inst = "Install with: brew install ripgrep"
        else:
            inst = "Install with: sudo apt install ripgrep  (or: sudo dnf install ripgrep / sudo pacman -S ripgrep)"
        missing.append(f"• ripgrep (rg) [Required for .gitignore traversal]:\n    {inst}")
        
    # 3. Check rclone (required if delivery has enabled rclone action)
    needs_rclone = any(d.get("enabled") and d.get("type") == "rclone" for d in request.get("delivery", []))
    if needs_rclone and not shutil.which("rclone"):
        if os.name == "nt":
            inst = "Install with: winget install Rclone.Rclone  (or: scoop install rclone / download from https://rclone.org/downloads/)"
        elif platform.system() == "Darwin":
            inst = "Install with: brew install rclone"
        else:
            inst = "Install with: sudo apt install rclone  (or: curl https://rclone.org/install.sh | sudo bash)"
        missing.append(f"• rclone [Required for cloud delivery/Google Drive upload]:\n    {inst}")

    if missing:
        msg = "\n" + "=" * 60 + "\n"
        msg += "MISSING SYSTEM DEPENDENCIES DETECTED:\n"
        msg += "=" * 60 + "\n\n"
        msg += "\n\n".join(missing) + "\n\n"
        msg += "=" * 60
        raise RuntimeError(msg)
        
    return seven_zip  # type: ignore[return-value]


def collect_software_inventory() -> dict[str, Any]:
    """Capture raw snapshot of installed packages, desktop apps, global dev tools, and user binaries.
    Fast, cross-platform (Linux, macOS, Windows), pure Python & light CLI calls (no heavy profilers)."""
    inv: dict[str, Any] = {
        "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python": platform.python_version(),
            "node": platform.node(),
        },
        "system_packages": {},
        "desktop_applications": [],
        "developer_tools": {},
        "user_path_executables": [],
    }

    # 1. System Package Managers
    # --- Debian / Ubuntu (dpkg) ---
    if shutil.which("dpkg-query"):
        try:
            r = subprocess.run(
                ["dpkg-query", "-W", "-f=${Package}\t${Version}\t${Architecture}\t${Status}\n"],
                capture_output=True, text=True, check=False
            )
            if r.returncode == 0:
                pkgs = []
                for line in r.stdout.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 3 and "installed" in parts[-1]:
                        pkgs.append({"name": parts[0], "version": parts[1], "arch": parts[2]})
                inv["system_packages"]["dpkg"] = pkgs
        except Exception:
            pass

    # --- RedHat / Fedora / CentOS (rpm) ---
    if shutil.which("rpm"):
        try:
            r = subprocess.run(["rpm", "-qa", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\n"], capture_output=True, text=True, check=False)
            if r.returncode == 0:
                pkgs = []
                for line in r.stdout.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        pkgs.append({"name": parts[0], "version": parts[1], "arch": parts[2] if len(parts) > 2 else ""})
                inv["system_packages"]["rpm"] = pkgs
        except Exception:
            pass

    # --- Arch Linux (pacman) ---
    if shutil.which("pacman"):
        try:
            r = subprocess.run(["pacman", "-Q"], capture_output=True, text=True, check=False)
            if r.returncode == 0:
                pkgs = []
                for line in r.stdout.splitlines():
                    parts = line.split()
                    if len(parts) >= 2:
                        pkgs.append({"name": parts[0], "version": parts[1]})
                inv["system_packages"]["pacman"] = pkgs
        except Exception:
            pass

    # --- Snap packages (Linux) ---
    if shutil.which("snap"):
        try:
            r = subprocess.run(["snap", "list"], capture_output=True, text=True, check=False)
            if r.returncode == 0:
                lines = r.stdout.splitlines()
                if len(lines) > 1:
                    snaps = []
                    for line in lines[1:]:
                        parts = line.split()
                        if len(parts) >= 3:
                            snaps.append({"name": parts[0], "version": parts[1], "rev": parts[2], "tracking": parts[3] if len(parts) > 3 else ""})
                    inv["system_packages"]["snap"] = snaps
        except Exception:
            pass

    # --- Flatpak packages (Linux) ---
    if shutil.which("flatpak"):
        try:
            r = subprocess.run(["flatpak", "list", "--app", "--columns=application,version,branch,origin"], capture_output=True, text=True, check=False)
            if r.returncode == 0:
                flatpaks = []
                for line in r.stdout.splitlines():
                    parts = line.split("\t")
                    if parts and parts[0].strip():
                        flatpaks.append({
                            "app_id": parts[0],
                            "version": parts[1] if len(parts) > 1 else "",
                            "branch": parts[2] if len(parts) > 2 else "",
                            "origin": parts[3] if len(parts) > 3 else "",
                        })
                inv["system_packages"]["flatpak"] = flatpaks
        except Exception:
            pass

    # --- macOS Homebrew (brew formulae & casks) ---
    if shutil.which("brew"):
        try:
            r = subprocess.run(["brew", "info", "--json=v2", "--installed"], capture_output=True, text=True, check=False)
            if r.returncode == 0:
                data = json.loads(r.stdout)
                inv["system_packages"]["homebrew_formulae"] = [
                    {"name": f["name"], "full_name": f.get("full_name"), "version": f.get("installed", [{}])[0].get("version", "")}
                    for f in data.get("formulae", [])
                ]
                inv["system_packages"]["homebrew_casks"] = [
                    {"name": c.get("token"), "version": c.get("version")} for c in data.get("casks", [])
                ]
        except Exception:
            pass

    # --- Windows (winget, scoop, choco) & Windows Registry ---
    if os.name == "nt":
        # Windows Registry Uninstall keys (works on Windows 7, 8, 10, 11)
        try:
            import winreg
            reg_apps = []
            roots = [
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            ]
            for hkey, subkey in roots:
                try:
                    with winreg.OpenKey(hkey, subkey) as key:
                        count, _, _ = winreg.QueryInfoKey(key)
                        for i in range(count):
                            try:
                                name = winreg.EnumKey(key, i)
                                with winreg.OpenKey(key, name) as app_key:
                                    try:
                                        display_name, _ = winreg.QueryValueEx(app_key, "DisplayName")
                                        if display_name:
                                            version, _ = winreg.QueryValueEx(app_key, "DisplayVersion") if "DisplayVersion" in [winreg.EnumValue(app_key, j)[0] for j in range(winreg.QueryInfoKey(app_key)[1])] else ("", None)
                                            publisher, _ = winreg.QueryValueEx(app_key, "Publisher") if "Publisher" in [winreg.EnumValue(app_key, j)[0] for j in range(winreg.QueryInfoKey(app_key)[1])] else ("", None)
                                            reg_apps.append({"name": str(display_name), "version": str(version), "publisher": str(publisher)})
                                    except OSError:
                                        pass
                            except OSError:
                                pass
                except OSError:
                    pass
            # dedup
            seen_reg = set()
            deduped_reg = []
            for item in reg_apps:
                key = (item["name"], item["version"])
                if key not in seen_reg:
                    seen_reg.add(key)
                    deduped_reg.append(item)
            inv["system_packages"]["windows_registry_apps"] = deduped_reg
        except Exception:
            pass

        if shutil.which("winget"):
            try:
                r = subprocess.run(["winget", "list", "--accept-source-agreements"], capture_output=True, text=True, check=False)
                if r.returncode == 0:
                    inv["system_packages"]["winget_raw"] = [l for l in r.stdout.splitlines() if l.strip()]
            except Exception:
                pass

        if shutil.which("scoop"):
            try:
                r = subprocess.run(["scoop", "list"], capture_output=True, text=True, check=False)
                if r.returncode == 0:
                    inv["system_packages"]["scoop"] = [l for l in r.stdout.splitlines() if l.strip()]
            except Exception:
                pass

    # 2. Desktop GUI Applications
    # --- Linux Desktop files ---
    if platform.system() == "Linux":
        desktop_dirs = [
            Path("/usr/share/applications"),
            Path("/usr/local/share/applications"),
            expand_path("~/.local/share/applications"),
            Path("/var/lib/flatpak/exports/share/applications"),
            expand_path("~/.local/share/flatpak/exports/share/applications"),
            Path("/var/lib/snapd/desktop/applications"),
        ]
        desktop_apps = []
        seen_desktops = set()
        for d in desktop_dirs:
            if d.is_dir():
                for f in d.glob("*.desktop"):
                    if f.name not in seen_desktops:
                        seen_desktops.add(f.name)
                        app_name = f.stem
                        try:
                            # fast extract Name= line without parsing whole INI
                            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                                if line.startswith("Name="):
                                    app_name = line[5:].strip()
                                    break
                        except Exception:
                            pass
                        desktop_apps.append({"file": f.name, "name": app_name, "path": str(f)})
        inv["desktop_applications"] = sorted(desktop_apps, key=lambda x: x["name"].lower())

    # --- macOS Applications ---
    elif platform.system() == "Darwin":
        mac_apps = []
        for app_dir in [Path("/Applications"), Path("/System/Applications"), expand_path("~/Applications")]:
            if app_dir.is_dir():
                for app in app_dir.glob("*.app"):
                    mac_apps.append({"name": app.name, "path": str(app)})
        inv["desktop_applications"] = sorted(mac_apps, key=lambda x: x["name"].lower())

    # 3. Global Language & Developer Tools
    # --- Node / npm global ---
    if shutil.which("npm"):
        try:
            r = subprocess.run(["npm", "list", "-g", "--depth=0", "--json"], capture_output=True, text=True, check=False)
            if r.returncode == 0:
                data = json.loads(r.stdout)
                inv["developer_tools"]["npm_global"] = {
                    k: v.get("version", "") for k, v in data.get("dependencies", {}).items()
                }
        except Exception:
            pass

    # --- Python pipx ---
    if shutil.which("pipx"):
        try:
            r = subprocess.run(["pipx", "list", "--json"], capture_output=True, text=True, check=False)
            if r.returncode == 0:
                data = json.loads(r.stdout)
                inv["developer_tools"]["pipx"] = {
                    k: v.get("main_package", {}).get("package_version", "")
                    for k, v in data.get("venvs", {}).items()
                }
        except Exception:
            pass

    # --- Rust Cargo global binaries ---
    if shutil.which("cargo"):
        cargo_bin = expand_path("~/.cargo/bin")
        if cargo_bin.is_dir():
            inv["developer_tools"]["cargo_binaries"] = sorted(
                f.name for f in cargo_bin.iterdir() if f.is_file() and os.access(f, os.X_OK)
            )

    # --- Go global binaries ---
    go_bin = expand_path("~/go/bin")
    if go_bin.is_dir():
        inv["developer_tools"]["go_binaries"] = sorted(
            f.name for f in go_bin.iterdir() if f.is_file() and os.access(f, os.X_OK)
        )

    # --- Bun global ---
    bun_bin = expand_path("~/.bun/bin")
    if bun_bin.is_dir():
        inv["developer_tools"]["bun_binaries"] = sorted(
            f.name for f in bun_bin.iterdir() if f.is_file() and os.access(f, os.X_OK)
        )

    # 4. User Path Executables (captures loose/extracted binaries in non-system directories)
    # E.g. tools dropped in ~/.local/bin, ~/bin, /opt/bin
    system_prefixes = ("/usr/bin", "/usr/sbin", "/bin", "/sbin", "/usr/games", "/usr/local/games")
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    user_executables = []
    seen_execs = set()
    for entry in path_entries:
        if not entry:
            continue
        p = Path(entry).resolve()
        if not p.is_dir():
            continue
        # check if it is a user/custom directory
        is_user_dir = not any(str(p).startswith(sys_prefix) for sys_prefix in system_prefixes) or str(p).startswith(str(original_home()))
        if is_user_dir:
            try:
                for item in p.iterdir():
                    if item.is_file() and os.access(item, os.X_OK) and item.name not in seen_execs:
                        seen_execs.add(item.name)
                        stat_res = item.stat()
                        user_executables.append({
                            "name": item.name,
                            "path": str(item),
                            "size": stat_res.st_size,
                            "is_symlink": item.is_symlink(),
                            "symlink_target": str(item.resolve()) if item.is_symlink() else None,
                        })
            except Exception:
                pass
    inv["user_path_executables"] = sorted(user_executables, key=lambda x: x["name"].lower())

    return inv


def acquire_lock() -> None:
    BCK_DIR.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        try:
            details = LOCK_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            details = "unknown process"
        raise RuntimeError(f"Another backup may be running ({details}); remove {LOCK_FILE} if stale") from error
    with os.fdopen(descriptor, "w", encoding="utf-8") as lock:
        lock.write(f"pid={os.getpid()} started={dt.datetime.now(dt.timezone.utc).isoformat()}\n")


def release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def hash_item(item: BackupItem, staging: Path) -> tuple[str, dict[str, Any]]:
    staged_path = staging / Path(*item.archive_path.parts)
    info = staged_path.stat()
    digest = hashlib.sha256()
    with staged_path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return str(item.archive_path), {
        "source": str(item.source),
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }


def build_index(
    items: list[BackupItem], environment: dict[str, str], staging: Path, workers: int
) -> tuple[dict[str, Any], str]:
    files: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="backup-hash") as executor:
        futures = [executor.submit(hash_item, item, staging) for item in items]
        for future in as_completed(futures):
            archive_path, details = future.result()
            files[archive_path] = details
    environment_hash = hashlib.sha256(
        json.dumps(environment, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    index = {"files": dict(sorted(files.items())), "environment_sha256": environment_hash}
    canonical = json.dumps(index, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return index, hashlib.sha256(canonical).hexdigest()


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Backup repository metadata is corrupt: {path}: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"Backup repository metadata must be a JSON object: {path}")
    return value


def latest_snapshot_for_scope(scope: list[str]) -> dict[str, Any] | None:
    if not HISTORY_FILE.exists():
        return None
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            record = json.loads(line)
            if record.get("scope") == scope:
                return read_json(SCRIPT_DIR / record["snapshot"])
    except (OSError, json.JSONDecodeError, KeyError) as error:
        raise RuntimeError(f"Backup history is corrupt: {HISTORY_FILE}: {error}") from error
    return None


def next_version() -> int:
    if not HEAD_FILE.exists():
        return 1
    match = re.search(r"^version=(\d+)$", HEAD_FILE.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise RuntimeError(f"Backup repository HEAD is corrupt: {HEAD_FILE}")
    return int(match.group(1)) + 1


def calculate_changes(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, list[str]]:
    previous_files = previous.get("index", {}).get("files", {}) if previous else {}
    current_files = current["files"]
    previous_names = set(previous_files)
    current_names = set(current_files)
    return {
        "added": sorted(current_names - previous_names),
        "modified": sorted(
            name
            for name in current_names & previous_names
            if current_files[name]["sha256"] != previous_files[name]["sha256"]
        ),
        "deleted": sorted(previous_names - current_names),
    }


def relative_repository_path(path: Path) -> str:
    return path.relative_to(SCRIPT_DIR).as_posix()


def atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def record_version(
    version: int,
    backup_id: str,
    created_at: str,
    scope: list[str],
    archive: Path,
    archive_hash: str,
    index: dict[str, Any],
    changes: dict[str, list[str]],
    symlink_inventory: dict[str, Any],
) -> None:
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = SNAPSHOTS_DIR / f"v{version:06d}-{backup_id}.json"
    snapshot = {
        "format": 2,
        "version": version,
        "backup_id": backup_id,
        "request_hash": REQUEST_HASH,
        "request_slug": REQUEST_SLUG,
        "created_at": created_at,
        "scope": scope,
        "archive": relative_repository_path(archive),
        "archive_sha256": archive_hash,
        "changes": changes,
        "index": index,
        "symlink_inventory": symlink_inventory,
    }
    atomic_write(snapshot_path, json.dumps(snapshot, indent=2, ensure_ascii=True) + "\n")
    record = {
        key: snapshot[key]
        for key in ("version", "backup_id", "created_at", "scope", "archive", "archive_sha256", "changes")
    }
    record["snapshot"] = relative_repository_path(snapshot_path)
    with HISTORY_FILE.open("a", encoding="utf-8") as history:
        history.write(json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n")
        history.flush()
        os.fsync(history.fileno())
    atomic_write(INDEX_FILE, json.dumps(snapshot, indent=2, ensure_ascii=True) + "\n")
    atomic_write(
        HEAD_FILE,
        f"version={version}\nid={backup_id}\nsnapshot={relative_repository_path(snapshot_path)}\n",
    )


def stage_one(item: BackupItem, staging: Path, mode: str) -> int:
    destination = staging / Path(*item.archive_path.parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_stat = item.source.stat()
    if mode in ("auto", "hardlink"):
        try:
            os.link(item.source, destination)
            return source_stat.st_size
        except OSError:
            if mode == "hardlink":
                raise
    shutil.copy2(item.source, destination)
    return source_stat.st_size


def stage_items(
    items: list[BackupItem], staging: Path, workers: int
) -> tuple[int, list[tuple[BackupItem, str]]]:
    mode = CONFIG["archive"].get("staging_mode", "auto")
    failures: list[tuple[BackupItem, str]] = []
    total_bytes = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="backup-stage") as executor:
        futures = {executor.submit(stage_one, item, staging, mode): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                total_bytes += future.result()
            except Exception as error:
                failures.append((item, str(error)))
    return total_bytes, failures


def write_metadata(
    staging: Path,
    items: list[BackupItem],
    environment: dict[str, str],
    notes: dict[str, Any],
    git_state: list[dict[str, Any]],
    symlinks: dict[str, Any],
    warnings: list[str],
    version: int,
    backup_id: str,
    changes: dict[str, list[str]],
) -> None:
    metadata = staging / "_backup"
    metadata.mkdir(parents=True, exist_ok=True)
    # manifest is the restore-tree: archive_path -> original source
    manifest = {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "version": version,
        "backup_id": backup_id,
        "request_hash": REQUEST_HASH,
        "request_slug": REQUEST_SLUG,
        "host": platform.node(),
        "platform": platform.platform(),
        "file_count": len(items),
        "warnings": warnings,
        "changes": changes,
        "notes": {
            "enabled": bool(notes),
            "custom_count": len(notes.get("custom", [])) if notes else 0,
            "command_count": len(notes.get("commands", {})) if notes else 0,
        },
        "encryption": {
            "enabled": bool(CONFIG.get("archive", {}).get("encryption", {}).get("enabled")),
            "method": "7z-aes"
            if CONFIG.get("archive", {}).get("encryption", {}).get("enabled")
            else None,
        },
        "git_state": {
            "enabled": bool(CONFIG.get("git_state", {}).get("enabled")),
            "repo_count": len(git_state),
        },
        "symlinks": {
            "enabled": bool(CONFIG.get("symlinks", {}).get("enabled")),
            "sources": len(symlinks),
            "links": sum(v.get("count", 0) for v in symlinks.values()),
        },
        "symlink_inventory": symlinks,
        "files": [
            {"source": str(item.source), "archive_path": str(item.archive_path)}
            for item in items
        ],
    }
    (metadata / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )
    # tree.json is a flat map used by --restore to place files back
    tree = {
        str(item.archive_path): str(item.source) for item in items
    }
    (metadata / "tree.json").write_text(
        json.dumps(tree, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8"
    )
    # software inventory snapshot
    try:
        software_inv = collect_software_inventory()
        (metadata / "software_inventory.json").write_text(
            json.dumps(software_inv, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
        )
        # human readable software overview
        lines = [
            f"# Installed Software & Tools Snapshot",
            f"Collected at: {software_inv['collected_at']}",
            f"Platform: {software_inv['platform']['system']} {software_inv['platform']['release']} ({software_inv['platform']['machine']})",
            "",
            "## System Packages Count:",
        ]
        for mgr, pkgs in software_inv["system_packages"].items():
            lines.append(f"- {mgr}: {len(pkgs)} packages/apps")
        lines.append(f"\n## Desktop Applications ({len(software_inv['desktop_applications'])}):")
        for app in software_inv["desktop_applications"][:40]:
            lines.append(f"- {app['name']}")
        if len(software_inv["desktop_applications"]) > 40:
            lines.append(f"- ... and {len(software_inv['desktop_applications']) - 40} more (see software_inventory.json)")
        lines.append("\n## Developer & Language Tools:")
        for tool, items_val in software_inv["developer_tools"].items():
            if isinstance(items_val, dict):
                lines.append(f"- {tool}: {len(items_val)} global packages")
            elif isinstance(items_val, list):
                lines.append(f"- {tool}: {len(items_val)} binaries")
        lines.append(f"\n## Custom & Loose User Binaries ({len(software_inv['user_path_executables'])}):")
        for uexec in software_inv["user_path_executables"][:40]:
            lines.append(f"- {uexec['name']} ({uexec['path']})")
        if len(software_inv["user_path_executables"]) > 40:
            lines.append(f"- ... and {len(software_inv['user_path_executables']) - 40} more (see software_inventory.json)")
        (metadata / "SOFTWARE.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        warnings.append(f"software inventory collection warning: {e}")
    # human-readable restore instructions
    rel_data = DATA_DIR.relative_to(SCRIPT_DIR).as_posix()
    (metadata / "RESTORE.txt").write_text(
        f"To restore this backup to original locations:\n"
        f"  python {Path(__file__).name} --restore --archive {rel_data}/<this-archive>.zip\n"
        f"To preview without writing:\n"
        f"  python {Path(__file__).name} --restore --archive {rel_data}/<this-archive>.zip --dry-run\n"
        f"Tree map: _backup/tree.json  (archive_path -> absolute source)\n"
        f"Full manifest: _backup/manifest.json\n"
        f"Machine notes: _backup/notes.json & _backup/NOTES.txt (non-transferrable)\n"
        f"Software snapshot: _backup/software_inventory.json & _backup/SOFTWARE.txt\n"
        f"Slug: {REQUEST_SLUG}  Hash: {REQUEST_HASH}\n",
        encoding="utf-8",
    )
    if CONFIG["environment"].get("enabled"):
        (metadata / "environment.json").write_text(
            json.dumps(environment, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
    if notes:
        (metadata / "notes.json").write_text(
            json.dumps(notes, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        nlines = [
            "# Machine Notes (non-transferrable knowledge)",
            f"Collected at: {notes.get('collected_at', '?')}",
            "",
            "## Software summary:",
        ]
        for manager, count in sorted(
            notes.get("software", {}).get("system_package_counts", {}).items()
        ):
            nlines.append(f"- {manager}: {count} packages/apps")
        devtools = notes.get("software", {}).get("developer_tools", {})
        if devtools:
            nlines.append("")
            nlines.append("## Developer tools:")
            for tool, count in sorted(devtools.items(), key=lambda kv: str(kv[0])):
                nlines.append(f"- {tool}: {count}")
        exes = notes.get("software", {}).get("user_path_executables", [])
        if exes:
            nlines.append("")
            nlines.append(f"## PATH executables ({len(exes)}):")
            nlines.append(", ".join(exes[:200]))
            if len(exes) > 200:
                nlines.append(f"... and {len(exes) - 200} more (see notes.json)")
        if notes.get("path_dirs"):
            nlines.append("")
            nlines.append("## PATH directories:")
            for entry in notes["path_dirs"]:
                nlines.append(f"- {entry}")
        for custom in notes.get("custom", []):
            nlines.append("")
            nlines.append("## Operator note:")
            nlines.append(str(custom))
        commands = notes.get("commands", {})
        if commands:
            nlines.append("")
            nlines.append("## Captured command outputs:")
            for command, result in commands.items():
                nlines.append(f"### $ {command}  (exit {result.get('returncode')})")
                out = str(result.get("stdout", ""))
                if len(out) > 3000:
                    out = out[:3000] + f"\n... truncated ({len(out)} chars, see notes.json)"
                nlines.append(out.rstrip() or "(no output)")
                err = str(result.get("stderr", "")).strip()
                if err:
                    nlines.append(f"[stderr] {err[-500:]}")
        (metadata / "NOTES.txt").write_text("\n".join(nlines) + "\n", encoding="utf-8")
    if CONFIG.get("git_state", {}).get("enabled"):
        (metadata / "git-state.json").write_text(
            json.dumps(git_state, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        glines = [
            "# Git history is NOT in this archive (.git excluded unless a source sets include_git).",
            "# Re-create history per repo, then restore working-tree files over it:",
            "#   git clone <remote-url> <path>",
            "#   or: git init <path> && git remote add origin <url> && git fetch origin && git reset --hard <sha>",
            f"# Recorded at: {dt.datetime.now(dt.timezone.utc).isoformat()}",
            "",
        ]
        for repo in git_state:
            remotes = ", ".join(
                f"{name}={url}" for name, url in sorted(repo.get("remotes", {}).items())
            ) or "NO-REMOTE"
            glines.append(f"## {repo.get('path', '?')}")
            glines.append(
                f"# branch={repo.get('branch')} head={repo.get('head')} "
                f"upstream={repo.get('upstream')} ahead={repo.get('ahead')} "
                f"behind={repo.get('behind')} dirty={repo.get('dirty')} remotes: {remotes}"
            )
        (metadata / "GIT.txt").write_text("\n".join(glines) + "\n", encoding="utf-8")


def create_zip(
    staging: Path, archive: Path, workers: int, level: int, seven_zip: str,
    passphrase: str | None = None,
) -> str:
    command = [
        seven_zip,
        "a",
        "-tzip",
        f"-mx={level}",
        f"-mmt={workers}",
        "-bd",
        "-y",
    ]
    if passphrase:
        # WinZip AES-256. Caveat: the passphrase is briefly visible in the
        # process list while 7z runs; 7z offers no pass-file option.
        command += ["-mem=AES256", f"-p{passphrase}"]
    command += [str(archive), "."]
    subprocess.run(command, cwd=staging, check=True)
    return Path(seven_zip).name


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _append_slug_if_needed(destination: str, slug: str) -> str:
    # If destination already ends with slug (or ${REQUEST_SLUG}), do not duplicate.
    # Otherwise append /<slug> so every remote / drive stores files under <destination>/<slug>/
    dest = destination.rstrip("/\\")
    if not slug:
        return dest
    # check if path's last segment is slug
    if dest.endswith(f"/{slug}") or dest.endswith(f"\\{slug}") or dest.endswith(f":{slug}") or dest.endswith(f":/{slug}"):
        return dest
    # handle rclone remote format 'remote:' -> 'remote:slug', 'remote:dir' -> 'remote:dir/slug'
    if ":" in dest:
        remote, path = dest.split(":", 1)
        if not path or path == "/":
            return f"{remote}:{slug}"
        return f"{remote}:{path.rstrip('/')}/{slug}"
    # local path
    return f"{dest}/{slug}"


def deliver(files: list[Path]) -> list[str]:
    completed: list[str] = []
    errors: list[str] = []
    for action in CONFIG.get("delivery", []):
        if not action.get("enabled"):
            continue
        selected = files if action.get("include_checksum", True) else files[:1]
        # expand env vars first, and support ${REQUEST_SLUG} explicitly
        os.environ["REQUEST_SLUG"] = REQUEST_SLUG
        os.environ["REQUEST_HASH"] = REQUEST_HASH
        destination = os.path.expandvars(action["destination"])
        # auto append slug so remote/drive is <destination>/<slug>/
        destination = _append_slug_if_needed(destination, REQUEST_SLUG)
        raw_args = [os.path.expandvars(str(a)) for a in action.get("args", [])]
        if action["type"] == "rclone" and os.environ.get("RCLONE_ARGS"):
            import shlex

            raw_args = [*shlex.split(os.environ["RCLONE_ARGS"]), *raw_args]
        try:
            if action["type"] == "copy":
                target = expand_path(destination)
                if os.environ.get("BACKUP_COPY_REQUIRE_MOUNT") == "1":
                    check = target if target.exists() else target.parent
                    while not check.exists() and check != check.parent:
                        check = check.parent
                    is_mount = False
                    try:
                        is_mount = os.path.ismount(check)
                    except Exception:
                        pass
                    if not is_mount:
                        raise RuntimeError(
                            f"copy delivery: target {target} is not on a mounted drive (BACKUP_COPY_REQUIRE_MOUNT=1). "
                            f"Mount your drive first (e.g. /media/<user>/<label> or /mnt/backup)."
                        )
                target.mkdir(parents=True, exist_ok=True)
                for path in selected:
                    dest_file = target / path.name
                    if dest_file.exists() and dest_file.resolve() == path.resolve():
                        continue  # DATA_DIR itself lives under the destination; skip self-copy
                    temporary = target / (path.name + ".partial")
                    shutil.copy2(path, temporary)
                    os.replace(temporary, target / path.name)
                completed.append(f"copied to {target}")
            elif action["type"] == "rclone":
                rclone = shutil.which("rclone")
                if not rclone:
                    raise RuntimeError(
                        "rclone delivery is enabled, but rclone was not found. Install: https://rclone.org/install/ "
                        "(curl https://rclone.org/install.sh | sudo bash) or apt/brew install rclone"
                    )
                if ":" not in destination:
                    raise ValueError(
                        f"rclone destination must be remote:path like gdrive:Backups, got {destination!r}. "
                        f"Run `rclone config` and see .env.example / docs/google-drive-upload-research.md"
                    )
                for src in selected:
                    command = [rclone, "copy", str(src), destination, *raw_args]
                    subprocess.run(command, check=True)
                completed.append(f"uploaded to {destination}")
            else:
                raise ValueError(f"Unknown delivery type: {action['type']!r}")
        except Exception as e:
            errors.append(f"{action['type']}→{destination}: {e}")
            print(f"Delivery error ({action['type']}): {e}", file=sys.stderr)
    if errors and not completed:
        raise RuntimeError("; ".join(errors))
    if errors:
        deliver._last_errors = errors  # type: ignore[attr-defined]
    else:
        deliver._last_errors = []  # type: ignore[attr-defined]
    return completed


def prune_archives(current: Path) -> list[Path]:
    keep = int(CONFIG["archive"].get("keep_last", 0))
    if keep <= 0:
        return []
    prefix = safe_label(CONFIG["backup_name"]) + "-"
    archives = sorted(DATA_DIR.glob(prefix + "*.zip"), key=lambda path: path.stat().st_mtime)
    removed: list[Path] = []
    for archive in archives[:-keep]:
        if archive == current:
            continue
        archive.unlink(missing_ok=True)
        archive.with_suffix(archive.suffix + ".sha256").unlink(missing_ok=True)
        removed.append(archive)
    return removed


# ---- restore helpers ----

def _list_versions(slug: str | None = None) -> list[dict[str, Any]]:
    # if slug is set, list only that slug's history, else current REQUEST_SLUG
    hist = HISTORY_FILE if slug is None else BASE_BCK_DIR / "slugs" / slug / "history.jsonl"
    if not hist.exists():
        return []
    out = []
    for line in hist.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _list_all_slugs() -> dict[str, Any]:
    if SLUGMAP_FILE.exists():
        try:
            return json.loads(SLUGMAP_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    # fallback scan filesystem
    slugs_dir = BASE_BCK_DIR / "slugs"
    if not slugs_dir.exists():
        return {}
    out: dict[str, Any] = {}
    for p in slugs_dir.iterdir():
        if p.is_dir():
            out[p.name] = {"slug": p.name}
    return out


def _resolve_archive(args: argparse.Namespace) -> Path:
    # honour explicit --slug if given
    effective_slug = getattr(args, "slug", None) or REQUEST_SLUG
    effective_data = BASE_DATA_DIR / effective_slug if effective_slug else DATA_DIR
    effective_history = BASE_BCK_DIR / "slugs" / effective_slug / "history.jsonl" if effective_slug else HISTORY_FILE
    if args.archive:
        p = args.archive
        if not p.is_absolute():
            cand = (Path.cwd() / p).resolve()
            if cand.exists():
                p = cand
            else:
                cand2 = (effective_data / p).resolve()
                if cand2.exists():
                    p = cand2
                else:
                    # also try BASE_DATA_DIR/<slug>/ plus bare name
                    cand3 = (BASE_DATA_DIR / effective_slug / p.name).resolve() if effective_slug else None
                    if cand3 and cand3.exists():
                        p = cand3
                    else:
                        p = cand.resolve() if p.is_absolute() else (Path.cwd() / p).resolve()
        if not p.exists():
            raise FileNotFoundError(f"Archive not found: {p}")
        return p
    if args.version:
        v = args.version.strip().lower().lstrip("v")
        try:
            vi = int(v)
        except ValueError:
            raise ValueError(f"Invalid --version {args.version!r}, expected e.g. 1 or v000001")
        hist = effective_history
        if hist.exists():
            for line in reversed(hist.read_text(encoding="utf-8").splitlines()):
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec.get("version") == vi:
                    cand = SCRIPT_DIR / rec["archive"]
                    if not cand.exists():
                        matches = list(effective_data.glob(f"*-v{vi:06d}-*.zip"))
                        if matches:
                            return sorted(matches, key=lambda x: x.stat().st_mtime)[-1]
                    return cand
        raise FileNotFoundError(f"No archive found for version {vi} in slug {effective_slug}")
    # latest
    hist = effective_history
    if hist.exists():
        lines = [l for l in hist.read_text(encoding="utf-8").splitlines() if l.strip()]
        if lines:
            rec = json.loads(lines[-1])
            cand = SCRIPT_DIR / rec["archive"]
            if cand.exists():
                return cand
    zips = sorted(effective_data.glob("*.zip"), key=lambda p: p.stat().st_mtime) if effective_data.exists() else []
    if zips:
        return zips[-1]
    raise FileNotFoundError(f"No archive found in {effective_data} (slug {effective_slug})")


def _read_tree_manifest(zip_path: Path) -> tuple[dict[str, str], dict[str, Any], bool]:
    """Read tree/manifest from an archive.

    Returns (tree, manifest, encrypted). Encrypted archives raise RuntimeError
    mentioning encryption/password on read; that is detected (not fatal) so
    the caller can switch to the 7z extraction path. Manifests always carry
    an `encryption` flag, never the passphrase.
    """
    import zipfile

    tree_name = "_backup/tree.json"
    manifest_name = "_backup/manifest.json"
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = set(z.namelist())
            if tree_name in names:
                tree = json.loads(z.read(tree_name).decode("utf-8"))
            elif manifest_name in names:
                manifest = json.loads(z.read(manifest_name).decode("utf-8"))
                tree = {f["archive_path"]: f["source"] for f in manifest.get("files", [])}
                return tree, manifest, bool(manifest.get("encryption", {}).get("enabled"))
            else:
                raise RuntimeError(f"Archive has no _backup/tree.json or manifest: {zip_path}")
            manifest: dict[str, Any] = {}
            if manifest_name in names:
                manifest = json.loads(z.read(manifest_name).decode("utf-8"))
            return tree, manifest, bool(manifest.get("encryption", {}).get("enabled"))
    except RuntimeError as error:
        if "encrypted" in str(error).lower() or "password" in str(error).lower():
            return {}, {}, True
        raise


def _extract_encrypted_to(archive: Path, dest: Path) -> None:
    seven_zip = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")
    if not seven_zip:
        raise RuntimeError("encrypted archive requires 7z/7zz/7za for extraction")
    passphrase = resolve_passphrase("restore")
    result = subprocess.run(
        [seven_zip, "x", f"-p{passphrase}", f"-o{dest}", "-bd", "-y", str(archive)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        hint = (result.stderr or "").strip()[-300:]
        raise RuntimeError(f"7z extraction failed (wrong passphrase?): {hint}")


def _apply_path_aliases(dest: str, extra: list[str]) -> str:
    """Rewrite a restore destination for a different machine/OS.

    Sources: request restore.path_aliases plus --map FROM=TO flags,
    longest-prefix-first. Stored paths are POSIX strings; parsing uses
    stdlib semantics only (no third-party path library needed — see skill).
    /mnt/* (reserved for mounts) has no meaningful Windows equivalent, so an
    unmapped Linux mount path on Windows is a hard error with the fix.
    """
    aliases: list[tuple[str, str]] = []
    for entry in (CONFIG.get("restore", {}) or {}).get("path_aliases", []) or []:
        if isinstance(entry, dict) and entry.get("from") and entry.get("to"):
            aliases.append((entry["from"], entry["to"]))
    for raw in extra or []:
        if "=" in raw:
            frm, to = raw.split("=", 1)
            if frm and to:
                aliases.append((frm, to))
    for frm, to in sorted(aliases, key=lambda pair: -len(pair[0])):
        if dest.startswith(frm):
            return to + dest[len(frm):]
    if os.name == "nt" and (dest == "/mnt" or dest.startswith("/mnt/")):
        raise RuntimeError(
            f"Cannot restore Linux mount path {dest!r} on Windows without a mapping; "
            "pass --map /mnt/<name>=<Letter>:/... (e.g. --map /mnt/devspace=D:/data) "
            "or set restore.path_aliases in the request"
        )
    return dest


def do_restore(args: argparse.Namespace) -> int:
    import tempfile
    import zipfile

    archive = _resolve_archive(args)
    print(f"Restore source: {archive}")
    tree, manifest, encrypted = _read_tree_manifest(archive)
    if encrypted:
        print("Archive is encrypted (7z-aes); file list needs the passphrase")
    only = set(args.only) if args.only else None
    # filter by target prefix if --only given: archive_path starts with sources/<name>/ or files/<name>/
    if only and not encrypted:
        tree = {k: v for k, v in tree.items() if k.split("/")[1] in only} if tree else {}
        if not tree:
            print(f"No files match --only {','.join(sorted(only))} in this archive", file=sys.stderr)
            return 1
    # group for summary
    from collections import Counter
    groups: Counter = Counter()
    for ap in tree:
        groups[ap.split("/")[1] if "/" in ap else ap] += 1
    # dry-run summary
    if args.dry_run and not encrypted:
        print(f"Would restore {len(tree)} files from {archive.name}")
        for label, count in sorted(groups.items()):
            print(f"  {label}: {count} files -> ...")
        if manifest.get("version"):
            print(f"  version: v{manifest['version']:06d}  id: {manifest.get('backup_id','')}")
        return 0
    # actual restore: stage to temp then copy to destinations.
    # Encrypted archives (names readable, contents not) are pulled out with
    # 7z first — this also loads tree/manifest, so --only works there too and
    # dry-run below reflects the decrypted file list.
    tmp = Path(tempfile.mkdtemp(prefix="bck-restore-"))
    errors: list[str] = []
    restored = 0
    try:
        if encrypted:
            _extract_encrypted_to(archive, tmp)
            tree_file = tmp / "_backup" / "tree.json"
            manifest_file = tmp / "_backup" / "manifest.json"
            if not tree_file.is_file():
                raise RuntimeError(f"Decrypted archive has no _backup/tree.json: {archive}")
            tree = json.loads(tree_file.read_text(encoding="utf-8"))
            manifest = (
                json.loads(manifest_file.read_text(encoding="utf-8"))
                if manifest_file.is_file()
                else {}
            )
            if only:
                tree = {k: v for k, v in tree.items() if k.split("/")[1] in only} if tree else {}
                if not tree:
                    print(f"No files match --only {','.join(sorted(only))} in this archive", file=sys.stderr)
                    return 1
            groups = Counter()
            for ap in tree:
                groups[ap.split("/")[1] if "/" in ap else ap] += 1
            if args.dry_run:
                print(f"Would restore {len(tree)} files from {archive.name}")
                for label, count in sorted(groups.items()):
                    print(f"  {label}: {count} files -> ...")
                if manifest.get("version"):
                    print(f"  version: v{manifest['version']:06d}  id: {manifest.get('backup_id','')}")
                return 0
        else:
            with zipfile.ZipFile(archive) as z:
                # stage only needed members; _backup/* stays out of destinations
                for ap in tree:
                    if ap not in z.namelist():
                        continue
                    target = tmp / Path(*PurePosixPath(ap).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(ap) as src, target.open("wb") as out:
                        shutil.copyfileobj(src, out, 8 * 1024 * 1024)
        for ap, dest in tree.items():
            staged = tmp / Path(*PurePosixPath(ap).parts)
            if not staged.is_file():
                errors.append(f"missing in archive: {ap}")
                continue
            dest_path = Path(_apply_path_aliases(dest, args.map))
            try:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                errors.append(f"{dest}: mkdir failed: {e}")
                continue
            if dest_path.exists() and not args.force:
                errors.append(f"exists (use --force to overwrite): {dest}")
                continue
            try:
                shutil.copy2(staged, dest_path)
                restored += 1
            except Exception as e:
                errors.append(f"{dest}: write failed: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # per-directory summary
    for label, count in sorted(groups.items()):
        print(f"  {label}: {count} files")
    print(f"Restored: {restored}/{len(tree)} files")
    if errors:
        print("Errors:", file=sys.stderr)
        for e in errors[:20]:
            print(f"  {e}", file=sys.stderr)
        if len(errors) > 20:
            print(f"  ... and {len(errors)-20} more", file=sys.stderr)
        return 1 if restored == 0 else 2
    return 0


def _resolve_serve_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "version", None):
        raw = args.version.strip().lower().lstrip("v")
        try:
            wanted = int(raw)
        except ValueError:
            raise ValueError(f"Invalid --version {args.version!r}, expected e.g. 1 or v000001")
        matches = sorted(SNAPSHOTS_DIR.glob(f"v{wanted:06d}-*.json")) if SNAPSHOTS_DIR.exists() else []
        if not matches:
            raise FileNotFoundError(f"No snapshot found for version {wanted} in slug {REQUEST_SLUG}")
        snapshot = read_json(matches[-1])
        if snapshot is None:
            raise RuntimeError(f"Snapshot metadata is corrupt: {matches[-1]}")
        return snapshot
    snapshot = read_json(INDEX_FILE)
    if snapshot is None:
        raise FileNotFoundError(
            f"No backup versions yet for slug {REQUEST_SLUG}; run a backup first"
        )
    return snapshot


def _render_report_html(
    slug: str,
    snapshot: dict[str, Any],
    versions: list[dict[str, Any]],
    rows: list[dict[str, str | int]],
    targets: list[str],
    symlinks: dict[str, Any],
) -> str:
    import html as _html

    files_json = json.dumps(rows, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")
    changes = snapshot.get("changes", {})
    options = "\n".join(
        f'      <option value="{_html.escape(t)}">{_html.escape(t)}</option>' for t in targets
    )
    version_rows = "\n".join(
        f"<tr><td>v{r.get('version', 0):06d}</td>"
        f"<td>{_html.escape(str(r.get('created_at', '')))}</td>"
        f"<td>{_html.escape(str(r.get('archive', '')))}</td>"
        f"<td>+{len(r.get('changes', {}).get('added', []))} "
        f"~{len(r.get('changes', {}).get('modified', []))} "
        f"-{len(r.get('changes', {}).get('deleted', []))}</td></tr>"
        for r in versions
    )
    total_bytes = sum(int(r["b"]) for r in rows)
    sym_json = json.dumps(symlinks, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backup report — {_html.escape(slug)}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 1.5rem; max-width: 1100px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 0.85rem; }}
th, td {{ border: 1px solid #ccc; padding: 4px 8px; text-align: left; }}
th {{ background: #f0f0f0; position: sticky; top: 0; }}
.toolbar {{ display: flex; gap: 0.5rem; flex-wrap: wrap; margin: 1rem 0; align-items: center; }}
.mono {{ font-family: monospace; font-size: 0.8rem; }}
</style>
</head>
<body>
<h1>Backup report — {_html.escape(slug)}</h1>
<p>Version <b>v{snapshot.get('version', 0):06d}</b> · id {_html.escape(str(snapshot.get('backup_id', '')))} ·
created {_html.escape(str(snapshot.get('created_at', '')))}<br>
Archive: <span class="mono">{_html.escape(str(snapshot.get('archive', '')))}</span><br>
Files: <b>{len(rows)}</b> · bytes: <b>{total_bytes:,}</b> ·
changes: +{len(changes.get('added', []))} ~{len(changes.get('modified', []))} -{len(changes.get('deleted', []))}</p>
<h2>Versions</h2>
<table><tr><th>version</th><th>created</th><th>archive</th><th>changes</th></tr>
{version_rows}
</table>
<p>Tip: re-run with <span class="mono">--serve --version N</span> to inspect an older version.
Full detail (software, notes, environment) lives inside the archive under <span class="mono">_backup/</span>.</p>
<h2>Files</h2>
<div class="toolbar">
<input id="q" type="search" placeholder="filter by path…" size="40">
<select id="target"><option value="">all targets</option>
{options}
</select>
<button id="all">Select shown</button>
<button id="none">Clear</button>
<button id="copy">Copy selected</button>
<button id="dl">Download JSON</button>
<span id="count"></span>
</div>
<table><thead><tr><th></th><th>archive path</th><th>size</th><th>source</th></tr></thead>
<tbody id="rows"></tbody></table>
<p id="trunc"></p>
<h2>Symlinks (recorded, not backed up)</h2>
<div class="toolbar">
<input id="lq" type="search" placeholder="filter symlinks…" size="40">
<span id="lcount"></span>
</div>
<table><thead><tr><th>source</th><th>link</th><th>target</th><th>target ok</th></tr></thead>
<tbody id="lrows"></tbody></table>
<script>
const FILES = {files_json};
const LIM = 10000;
const sel = new Set();
const q = document.getElementById('q'), tgt = document.getElementById('target');
const rowsEl = document.getElementById('rows'), countEl = document.getElementById('count');
function filtered() {{
  const needle = q.value.toLowerCase(), t = tgt.value;
  return FILES.filter(f => (!t || f.p.split('/')[1] === t) && (!needle || f.p.toLowerCase().includes(needle)));
}}
function render() {{
  const list = filtered(), shown = list.slice(0, LIM);
  rowsEl.innerHTML = shown.map(f => '<tr><td><input type="checkbox" data-p="' + f.p.replace(/"/g, '&quot;') + '"' + (sel.has(f.p) ? ' checked' : '') + '></td><td class="mono">' + f.p + '</td><td>' + f.b.toLocaleString() + '</td><td class="mono">' + f.s + '</td></tr>').join('');
  countEl.textContent = 'showing ' + shown.length + ' of ' + list.length + ' matching (' + sel.size + ' selected)';
  document.getElementById('trunc').textContent = list.length > LIM ? 'First ' + LIM + ' rows shown — refine the filter to see more.' : '';
  rowsEl.querySelectorAll('input[type=checkbox]').forEach(cb => cb.onchange = () => {{ cb.checked ? sel.add(cb.dataset.p) : sel.delete(cb.dataset.p); render(); }});
}}
q.oninput = render; tgt.onchange = render;
document.getElementById('all').onclick = () => {{ filtered().slice(0, LIM).forEach(f => sel.add(f.p)); render(); }};
document.getElementById('none').onclick = () => {{ sel.clear(); render(); }};
document.getElementById('copy').onclick = () => navigator.clipboard.writeText([...sel].join('\\n'));
document.getElementById('dl').onclick = () => {{
  const blob = new Blob([JSON.stringify([...sel], null, 2)], {{type: 'application/json'}});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'selection.json'; a.click();
}};
render();
const SYM = {sym_json};
const lq = document.getElementById('lq'), lrowsEl = document.getElementById('lrows'), lcountEl = document.getElementById('lcount');
function lrender() {{
  const needle = lq.value.toLowerCase();
  let html = '', shown = 0, total = 0;
  for (const [src, info] of Object.entries(SYM)) {{
    for (const l of info.items) {{
      total++;
      if (needle && !(src.toLowerCase().includes(needle) || l.rel.toLowerCase().includes(needle) || l.target.toLowerCase().includes(needle))) continue;
      shown++;
      html += '<tr><td class="mono">' + src + '</td><td class="mono">' + l.rel + '</td><td class="mono">' + l.target + '</td><td>' + (l.exists ? 'yes' : 'NO') + '</td></tr>';
    }}
    if (info.truncated) html += '<tr><td colspan="4">… capped at ' + info.items.length + ' of ' + info.count + ' in ' + src + ' (full count in manifest)</td></tr>';
  }}
  lrowsEl.innerHTML = html;
  lcountEl.textContent = shown + ' of ' + total + ' links';
}}
lq.oninput = lrender;
lrender();
</script>
</body>
</html>
"""


def _open_in_browser(output: Path, url: str, use_http: bool) -> None:
    """Open the report in the user's browser, and say honestly what happened.

    Two silent-failure modes are handled: (1) desktop openers must get a
    plain filesystem path — handed a file:// URI, xdg-open looks up an
    "x-scheme-handler/file" handler that does not exist and exits 0 having
    opened nothing; (2) stdlib webbrowser.open() returns True right after
    *spawning* the browser, never knowing the child crashed (e.g. no display
    in that terminal). So every strategy is return-code checked with stderr
    captured, and total failure prints the path plus fallbacks.
    """
    import subprocess

    target = url if use_http else str(output)
    errors: list[str] = []
    strategies: list[tuple[str, list[str]]] = []
    if os.name == "nt":
        strategies.append(("os.startfile", []))
    elif sys.platform == "darwin":
        strategies.append(("open", ["open", target]))
    else:
        strategies.append(("xdg-open", ["xdg-open", target]))
        strategies.append(("gio-open", ["gio", "open", target]))
        strategies.append(("sensible-browser", ["sensible-browser", target]))
    for label, command in strategies:
        if label == "os.startfile":
            try:
                os.startfile(str(output))  # type: ignore[attr-defined]
                print(f"Opened in default browser: {output}")
                return
            except Exception as error:
                errors.append(f"os.startfile: {error}")
                continue
        if not shutil.which(command[0]):
            continue
        try:
            result = subprocess.run(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, timeout=30, start_new_session=True,
                check=False,
            )
        except Exception as error:
            errors.append(f"{label}: launch failed: {error}")
            continue
        stderr = (result.stderr or b"").decode(errors="replace").strip()
        if result.returncode == 0 and "x-scheme-handler" not in stderr:
            print(f"Opened in default browser: {target}")
            return
        errors.append(f"{label}: exit={result.returncode} {stderr[:200]}".rstrip())
    try:
        import webbrowser

        if webbrowser.open(url):
            print(f"Opened in default browser (fallback): {url}")
            return
        errors.append("webbrowser.open returned False (no runnable browser)")
    except Exception as error:
        errors.append(f"webbrowser: {error}")
    print(
        "Could not open a browser automatically "
        f"({'; '.join(errors) if errors else 'no launcher found'}).\n"
        f"Open this file manually: {output}\n"
        "Or serve it over HTTP: add --serve-port 8080 (then visit the printed URL)."
    )


def _open_report(output: Path, args: argparse.Namespace) -> int:
    url = output.as_uri()
    server = None
    if getattr(args, "serve_port", None):
        import functools
        import threading
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(DATA_DIR))
        server = ThreadingHTTPServer(("127.0.0.1", args.serve_port), handler)
        url = f"http://127.0.0.1:{args.serve_port}/{output.name}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"Serving {DATA_DIR} at {url} (Ctrl-C to stop)")
    if not getattr(args, "no_browser", False):
        _open_in_browser(output, url, bool(getattr(args, "serve_port", None)))
    if server is not None:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        server.shutdown()
    return 0


def do_serve(args: argparse.Namespace) -> int:
    mode = getattr(args, "serve", "report") or "report"
    if mode == "plan":
        return do_serve_plan(args)
    if mode == "load":
        return do_serve_load(args)
    return do_serve_report(args)


def do_serve_report(args: argparse.Namespace) -> int:
    snapshot = _resolve_serve_snapshot(args)
    versions = _list_versions()
    files = snapshot.get("index", {}).get("files", {})
    rows = [
        {"p": archive_path, "s": meta.get("source", ""), "b": meta.get("size", 0)}
        for archive_path, meta in sorted(files.items())
    ]
    targets = sorted({str(r["p"]).split("/")[1] for r in rows if "/" in str(r["p"])})
    symlinks = snapshot.get("symlink_inventory", {}) or {}
    page = _render_report_html(REQUEST_SLUG, snapshot, versions, rows, targets, symlinks)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output = DATA_DIR / f"{REQUEST_SLUG}-report.html"
    output.write_text(page, encoding="utf-8")
    print(f"Report: {output} ({len(rows)} files, v{snapshot.get('version', 0):06d})")
    return _open_report(output, args)


def _plan_stats(only: set[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Live per-target enumeration for `serve plan`.

    Ignores `enabled` so the planner shows live file counts for disabled
    targets too. Read-only: takes no lock, writes nothing, never elevates.
    """
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for cfg in CONFIG.get("sources", []):
        if only and cfg["name"] not in only:
            continue
        root = expand_path(cfg["path"])
        if not root.is_dir():
            rows.append({
                "kind": "source", "name": cfg["name"], "path": str(root),
                "enabled": bool(cfg.get("enabled")),
                "gitignore": bool(cfg.get("respect_gitignore")),
                "file_count": 0, "bytes": 0, "status": "missing",
                "error": "directory does not exist",
            })
            continue
        cfg.pop("_rg_stderr", None)
        try:
            paths = (
                enumerate_with_rg(root, cfg)
                if cfg.get("respect_gitignore")
                else enumerate_without_ignores(root, cfg)
            )
            error = cfg.pop("_rg_stderr", None)
        except Exception as exc:
            paths = []
            error = str(exc)
        status = "ok" if error is None else ("failed" if not paths else "ok_with_warnings")
        rows.append({
            "kind": "source", "name": cfg["name"], "path": str(root),
            "enabled": bool(cfg.get("enabled")),
            "gitignore": bool(cfg.get("respect_gitignore")),
            "file_count": len(paths),
            "bytes": sum((p.stat().st_size for p in paths if p.exists()), 0),
            "status": status, "error": error,
        })
        if error:
            warnings.append(f"{cfg['name']}: {error}")
    for cfg in CONFIG.get("files", []):
        if only and cfg["name"] not in only:
            continue
        expanded = str(expand_path(cfg["path"]))
        matches = [Path(path).resolve() for path in glob.glob(expanded, recursive=True)]
        matches = [path for path in matches if path.is_file() and not is_internal_output(path)]
        rows.append({
            "kind": "files", "name": cfg["name"], "path": cfg["path"],
            "enabled": bool(cfg.get("enabled")), "gitignore": False,
            "file_count": len(matches),
            "bytes": sum((p.stat().st_size for p in matches if p.exists()), 0),
            "status": "ok" if matches else "empty",
            "error": None if matches else "glob matched nothing",
        })
    link_map = collect_symlinks(only)
    for row in rows:
        row["symlinks"] = link_map.get(
            row["name"],
            {"root": row["path"], "count": 0, "items": [], "truncated": False},
        )
    return rows, warnings


def _render_plan_html(
    slug: str,
    rows: list[dict[str, Any]],
    warnings: list[str],
    request: dict[str, Any],
    total_files: int,
    total_bytes: int,
) -> str:
    import html as _html

    rows_json = json.dumps(rows, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")
    request_json = json.dumps(request, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")
    warn_html = "".join(f"<li>{_html.escape(w)}</li>" for w in warnings) or "<li>none</li>"
    env = request.get("environment", {})
    nts = request.get("notes", {})
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backup plan — {_html.escape(slug)}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 1.5rem; max-width: 1100px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 0.85rem; }}
th, td {{ border: 1px solid #ccc; padding: 4px 8px; text-align: left; }}
th {{ background: #f0f0f0; position: sticky; top: 0; }}
.toolbar {{ display: flex; gap: 0.5rem; flex-wrap: wrap; margin: 1rem 0; align-items: center; }}
.mono {{ font-family: monospace; font-size: 0.8rem; }}
tr.off {{ opacity: 0.55; }}
</style>
</head>
<body>
<h1>Backup plan — {_html.escape(slug)}</h1>
<p>Live enumeration of the request file: <b>{len(rows)}</b> targets ·
<b>{total_files:,}</b> files · <b>{total_bytes:,}</b> bytes.<br>
environment: {'<b>on</b>' if env.get('enabled') else 'off'} ·
notes: {'<b>on</b>' if nts.get('enabled') else 'off'} ·
counts respect each target's gitignore/rg filters.</p>
<h2>Warnings</h2>
<ul>{warn_html}</ul>
<h2>Targets</h2>
<div class="toolbar">
<input id="q" type="search" placeholder="filter by name or path…" size="40">
<button id="all">Select all</button>
<button id="none">Clear</button>
<button id="copy">Copy --only command</button>
<button id="dl">Download trimmed request.json</button>
<span id="count"></span>
</div>
<table><thead><tr><th></th><th>name</th><th>kind</th><th>path</th><th>gitignore</th><th>files</th><th>bytes</th><th>status</th></tr></thead>
<tbody id="rows"></tbody></table>
<h2>Symlinks (recorded, not backed up)</h2>
<div class="toolbar">
<input id="lq" type="search" placeholder="filter symlinks…" size="40">
<span id="lcount"></span>
</div>
<table><thead><tr><th>target</th><th>link</th><th>points to</th><th>target ok</th></tr></thead>
<tbody id="lrows"></tbody></table>
<script>
const ROWS = {rows_json};
const REQ = {request_json};
const lq = document.getElementById('lq'), lrowsEl = document.getElementById('lrows'), lcountEl = document.getElementById('lcount');
function lrender() {{
  const needle = lq.value.toLowerCase();
  let html = '', shown = 0, total = 0;
  for (const r of ROWS) {{
    const info = r.symlinks || {{items: [], count: 0}};
    for (const l of info.items) {{
      total++;
      if (needle && !(r.name.toLowerCase().includes(needle) || l.rel.toLowerCase().includes(needle) || l.target.toLowerCase().includes(needle))) continue;
      shown++;
      html += '<tr><td class="mono">' + r.name + '</td><td class="mono">' + l.rel + '</td><td class="mono">' + l.target + '</td><td>' + (l.exists ? 'yes' : 'NO') + '</td></tr>';
    }}
    if (info.truncated) html += '<tr><td colspan="4">… capped at ' + info.items.length + ' of ' + info.count + ' in ' + r.name + '</td></tr>';
  }}
  lrowsEl.innerHTML = html;
  lcountEl.textContent = shown + ' of ' + total + ' links';
}}
lq.oninput = lrender;
lrender();
const sel = new Set(ROWS.filter(r => r.enabled).map(r => r.name));
const q = document.getElementById('q'), rowsEl = document.getElementById('rows'), countEl = document.getElementById('count');
function render() {{
  const needle = q.value.toLowerCase();
  const list = ROWS.filter(r => !needle || r.name.toLowerCase().includes(needle) || r.path.toLowerCase().includes(needle));
  rowsEl.innerHTML = list.map(r => '<tr class="' + (sel.has(r.name) ? '' : 'off') + '"><td><input type="checkbox" data-n="' + r.name + '"' + (sel.has(r.name) ? ' checked' : '') + '></td><td class="mono">' + r.name + '</td><td>' + r.kind + '</td><td class="mono">' + r.path + '</td><td>' + (r.gitignore ? 'yes' : 'no') + '</td><td>' + r.file_count.toLocaleString() + '</td><td>' + r.bytes.toLocaleString() + '</td><td>' + r.status + (r.error ? ' (' + r.error + ')' : '') + '</td></tr>').join('');
  const selBytes = ROWS.filter(r => sel.has(r.name)).reduce((a, r) => a + r.bytes, 0);
  countEl.textContent = sel.size + ' selected · ' + selBytes.toLocaleString() + ' bytes';
  rowsEl.querySelectorAll('input[type=checkbox]').forEach(cb => cb.onchange = () => {{ cb.checked ? sel.add(cb.dataset.n) : sel.delete(cb.dataset.n); render(); }});
}}
q.oninput = render;
document.getElementById('all').onclick = () => {{ ROWS.forEach(r => sel.add(r.name)); render(); }};
document.getElementById('none').onclick = () => {{ sel.clear(); render(); }};
document.getElementById('copy').onclick = () => navigator.clipboard.writeText('python backup.py ' + [...sel].map(n => '--only ' + n).join(' '));
document.getElementById('dl').onclick = () => {{
  const out = JSON.parse(JSON.stringify(REQ));
  for (const t of [...(out.sources || []), ...(out.files || [])]) t.enabled = sel.has(t.name);
  const blob = new Blob([JSON.stringify(out, null, 2)], {{type: 'application/json'}});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'request.selection.json'; a.click();
}};
render();
</script>
</body>
</html>
"""


def do_serve_plan(args: argparse.Namespace) -> int:
    print("Enumerating targets for plan view (can take a few minutes on full requests)...", flush=True)
    only = set(args.only)
    rows, warnings = _plan_stats(only)
    total_files = sum(r["file_count"] for r in rows)
    total_bytes = sum(r["bytes"] for r in rows)
    page = _render_plan_html(REQUEST_SLUG, rows, warnings, CONFIG, total_files, total_bytes)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output = DATA_DIR / f"{REQUEST_SLUG}-plan.html"
    output.write_text(page, encoding="utf-8")
    print(f"Plan: {output} ({len(rows)} targets, {total_files:,} files, {total_bytes:,} bytes)")
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    return _open_report(output, args)


def _load_meta_for_load(
    args: argparse.Namespace, archive: Path
) -> tuple[dict[str, str], dict[str, Any], dict[str, Any], dict[str, Any], bool]:
    """Read tree/manifest/notes/software from an archive for `serve load`.

    Works for plain and encrypted archives (encrypted meta needs the
    passphrase even though only listings are read). Never writes to
    destinations. Returns (tree, manifest, notes, software, encrypted).
    """
    import tempfile
    import zipfile

    tree, manifest, encrypted = _read_tree_manifest(archive)
    notes: dict[str, Any] = {}
    software: dict[str, Any] = {}
    if encrypted:
        seven_zip = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")
        if not seven_zip:
            raise RuntimeError("encrypted archive needs 7z/7zz/7za to inspect")
        passphrase = os.environ.get("BACKUP_PASSPHRASE")
        if not passphrase:
            try:
                passphrase = resolve_passphrase("load")
            except ValueError:
                # Local request has encryption disabled (fresh machine) but the
                # archive is encrypted: prompt directly instead of failing.
                import getpass

                if not sys.stdin.isatty():
                    raise RuntimeError("archive is encrypted; set BACKUP_PASSPHRASE")
                passphrase = getpass.getpass("Archive passphrase (load): ")
                if not passphrase:
                    raise ValueError("empty passphrase")
        tmp = Path(tempfile.mkdtemp(prefix="bck-load-"))
        try:
            wanted = [
                "_backup/tree.json", "_backup/manifest.json",
                "_backup/notes.json", "_backup/software_inventory.json",
            ]
            result = subprocess.run(
                [seven_zip, "x", f"-p{passphrase}", f"-o{tmp}", "-bd", "-y",
                 str(archive), *wanted],
                capture_output=True, text=True, check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"7z meta extraction failed (wrong passphrase?): "
                    f"{(result.stderr or '').strip()[-200:]}"
                )
            base = tmp / "_backup"
            tree = json.loads((base / "tree.json").read_text(encoding="utf-8"))
            manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
            if (base / "notes.json").is_file():
                notes = json.loads((base / "notes.json").read_text(encoding="utf-8"))
            if (base / "software_inventory.json").is_file():
                software = json.loads(
                    (base / "software_inventory.json").read_text(encoding="utf-8")
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        with zipfile.ZipFile(archive) as z:
            names = set(z.namelist())
            for key in ("_backup/notes.json", "_backup/software_inventory.json"):
                if key in names:
                    try:
                        parsed = json.loads(z.read(key).decode("utf-8"))
                    except Exception:
                        parsed = {}
                    if key.endswith("notes.json"):
                        notes = parsed
                    else:
                        software = parsed
    return tree, manifest, notes, software, encrypted


def _render_load_html(
    slug: str,
    archive_name: str,
    archive_abs: str,
    version: Any,
    created: str,
    encrypted: bool,
    targets: list[dict[str, Any]],
    notes_custom: list[str],
    syscounts: dict[str, int],
    dev_tools: list[str],
    path_exes: list[str],
    path_exes_total: int,
    desktop_apps: list[str],
    desktop_total: int,
    request: dict[str, Any],
) -> str:
    import html as _html

    t_json = json.dumps(targets, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")
    req_json = json.dumps(request, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")
    apps_json = json.dumps(
        {"dev": dev_tools, "exes": path_exes, "desk": desktop_apps},
        ensure_ascii=True, separators=(",", ":"),
    ).replace("</", "<\\/")
    notes_html = "".join(f"<li>{_html.escape(str(c))}</li>" for c in notes_custom) or "<li>none</li>"
    sys_html = "".join(
        f"<li>{_html.escape(str(m))}: {c} packages</li>" for m, c in sorted(syscounts.items())
    ) or "<li>unknown</li>"
    target_rows = "\n".join(
        f"<tr><td class=\"mono\">{_html.escape(str(t['label']))}</td>"
        f"<td>{t['kind']}</td><td>{t['files']:,}</td><td>{t['symlinks']}</td>"
        f"<td class=\"mono\">{_html.escape(str(t['orig']))}</td>"
        f"<td><input data-from=\"{_html.escape(str(t['orig']))}\" value=\"{_html.escape(str(t['proposed']))}\" size=\"42\"></td></tr>"
        for t in targets
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Load plan — {_html.escape(slug)}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 1.5rem; max-width: 1100px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 0.85rem; }}
th, td {{ border: 1px solid #ccc; padding: 4px 8px; text-align: left; }}
th {{ background: #f0f0f0; }}
.toolbar {{ display: flex; gap: 0.5rem; flex-wrap: wrap; margin: 1rem 0; align-items: center; }}
.mono, pre {{ font-family: monospace; }}
pre {{ background: #f6f6f6; padding: 1rem; white-space: pre-wrap; font-size: 0.8rem; }}
input[type=text], input:not([type]) {{ font-family: monospace; font-size: 0.8rem; }}
details {{ margin: 0.5rem 0; }}
</style>
</head>
<body>
<h1>Load plan — {_html.escape(slug)}</h1>
<p>Archive: <span class="mono">{_html.escape(archive_name)}</span><br>
Version: {_html.escape(str(version))} · created {_html.escape(created)} ·
encrypted: {'<b>yes (7z-aes)</b>' if encrypted else 'no'}<br>
Human workflow: edit the <b>load to</b> paths below, tick the apps to install, then
<b>Download request.load.json</b> and hand its path back to your agent. The agent runs the
shown command — it must not invent paths (review step).</p>
<h2>1. Destinations</h2>
<div class="toolbar">
<label><input id="dry" type="checkbox" checked> dry-run first</label>
<button id="copy">Copy command</button>
<button id="dlreq">Download request.load.json</button>
<button id="dlbrief">Download load-brief.md</button>
</div>
<pre id="cmd"></pre>
<table><thead><tr><th>target</th><th>kind</th><th>files</th><th>symlinks*</th><th>original root</th><th>load to</th></tr></thead>
<tbody>{target_rows}</tbody></table>
<p>* symlinks are recorded, not archived — re-create the ones you need by hand.</p>
<h2>2. Applications to install</h2>
<p>Tick what this machine needs. The order panel is heuristic (drivers → runtimes → managers →
apps); anything it cannot script is flagged MANUAL for you.</p>
<div class="toolbar"><input id="aq" type="search" placeholder="filter apps…" size="30"></div>
<details open><summary>Developer tools ({len(dev_tools)})</summary><div id="g-dev"></div></details>
<details><summary>PATH binaries ({path_exes_total}, showing {len(path_exes)})</summary><div id="g-exe"></div></details>
<details><summary>Desktop applications ({desktop_total}, showing {len(desktop_apps)})</summary><div id="g-desk"></div></details>
<h3>Install order</h3>
<ol id="plan"></ol>
<h3>Manual steps (escalated to you)</h3>
<ul id="manual"></ul>
<h2>3. Operator notes from the source machine</h2>
<ul>{notes_html}</ul>
<h2>4. Source-machine software summary</h2>
<ul>{sys_html}</ul>
<h2>5. LLM brief (paste to your agent)</h2>
<pre id="brief"></pre>
<script>
const TARGETS = {t_json};
const REQ = {req_json};
const APPS = {apps_json};
const ARCH = {json.dumps(archive_abs, ensure_ascii=True)};
const RULES = [
  {{re: "nvidia|cuda", phase: 0, manual: "Install the proprietary GPU driver for your OS first — cannot be scripted reliably."}},
  {{re: "wsl", phase: 0, manual: "Windows: enable Virtualization in BIOS, then install WSL2."}},
  {{re: "docker", phase: 3, manual: "Windows needs WSL2 + BIOS virtualization first; on Linux add your user to the docker group after install."}},
  {{re: "python|pipx|uv|conda", phase: 1, manual: ""}},
  {{re: "node|npm|bun|deno", phase: 1, manual: ""}},
  {{re: "go$|golang|cargo|rustup|rustc|jdk|java|maven|gradle|dotnet", phase: 1, manual: ""}},
  {{re: "snapd|^snap$", phase: 2, manual: "Needs snapd service running (systemd)."}},
  {{re: "brew|flatpak", phase: 2, manual: ""}}
];
function rows() {{
  return [...document.querySelectorAll('tbody input')].map(i => ({{from: i.dataset.from, to: i.value}}));
}}
function cmd() {{
  let c = 'python backup.py --restore --archive ' + ARCH;
  for (const r of rows()) if (r.to !== r.from) c += ' --map ' + r.from + '=' + r.to;
  if (document.getElementById('dry').checked) c += ' --dry-run';
  return c;
}}
function checkedApps() {{
  return [...document.querySelectorAll('.app:checked')].map(c => c.value);
}}
function cat(name) {{
  for (const r of RULES) if (new RegExp(r.re, 'i').test(name)) return r;
  return {{phase: 4, manual: ""}};
}}
function renderPlan() {{
  const sel = checkedApps().sort();
  const manual = [], items = sel.map(n => ({{n, c: cat(n)}}));
  items.sort((a, b) => a.c.phase - b.c.phase);
  for (const it of items) if (it.c.manual) manual.push(it.n + ': ' + it.c.manual);
  document.getElementById('plan').innerHTML = items.map(it => '<li>[phase ' + it.c.phase + '] ' + it.n + '</li>').join('') || '<li>nothing selected</li>';
  const um = sel.length ? '' : '';
  document.getElementById('manual').innerHTML = (manual.map(m => '<li>' + m + '</li>').join('') || '<li>none</li>') + (um);
}}
function brief() {{
  let b = 'LOAD BRIEF (generated in load.html; paths are human-reviewed)\\n';
  b += 'archive: ' + ARCH + '\\ncommand: ' + cmd() + '\\n\\ntargets:\\n';
  for (const r of rows()) b += '- ' + r.from + '  ->  ' + r.to + '\\n';
  b += '\\napps to install (ordered):\\n';
  const sel = checkedApps().sort();
  for (const n of sel) b += '- ' + n + '\\n';
  b += '\\noperator notes:\\n';
  return b;
}}
function render() {{
  document.getElementById('cmd').textContent = cmd();
  renderPlan();
  document.getElementById('brief').textContent = brief();
}}
function appBox(group, name) {{
  return '<label><input class="app" type="checkbox" value="' + name.replace(/"/g, '&quot;') + '"> ' + name + '</label><br>';
}}
function renderApps() {{
  const needle = document.getElementById('aq').value.toLowerCase();
  const f = l => l.filter(n => !needle || n.toLowerCase().includes(needle));
  document.getElementById('g-dev').innerHTML = f(APPS.dev).map(n => appBox('dev', n)).join('') || '(none)';
  document.getElementById('g-exe').innerHTML = f(APPS.exes).map(n => appBox('exe', n)).join('') || '(none)';
  document.getElementById('g-desk').innerHTML = f(APPS.desk).map(n => appBox('desk', n)).join('') || '(none)';
  document.querySelectorAll('.app').forEach(c => c.onchange = render);
}}
document.getElementById('aq').oninput = renderApps;
document.querySelectorAll('tbody input').forEach(i => i.oninput = render);
document.getElementById('dry').onchange = render;
document.getElementById('copy').onclick = () => navigator.clipboard.writeText(cmd());
document.getElementById('dlreq').onclick = () => {{
  const out = JSON.parse(JSON.stringify(REQ));
  out.restore = out.restore || {{}};
  out.restore.path_aliases = rows().filter(r => r.to !== r.from).map(r => ({{from: r.from, to: r.to}}));
  const blob = new Blob([JSON.stringify(out, null, 2)], {{type: 'application/json'}});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'request.load.json'; a.click();
}};
document.getElementById('dlbrief').onclick = () => {{
  const blob = new Blob([brief()], {{type: 'text/markdown'}});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'load-brief.md'; a.click();
}};
renderApps();
render();
</script>
</body>
</html>
"""


def do_serve_load(args: argparse.Namespace) -> int:
    archive = _resolve_archive(args)
    tree, manifest, notes, software, encrypted = _load_meta_for_load(args, archive)
    if not tree:
        raise RuntimeError(f"No restorable files found in {archive}")
    groups: dict[str, dict[str, Any]] = {}
    for archive_path, source in sorted(tree.items()):
        parts = PurePosixPath(archive_path).parts
        label = parts[1] if len(parts) > 1 else parts[0]
        entry = groups.setdefault(
            label, {"kind": parts[0], "files": 0, "srcs": []}
        )
        entry["files"] += 1
        entry["srcs"].append(source)
    sym_inv = manifest.get("symlink_inventory", {}) or {}
    targets = []
    for label, entry in sorted(groups.items()):
        try:
            orig = os.path.commonpath(entry["srcs"])
        except ValueError:
            orig = os.path.dirname(entry["srcs"][0])
        try:
            proposed = _apply_path_aliases(orig, args.map)
        except RuntimeError:
            proposed = orig  # unmapped (e.g. /mnt on Windows): human maps it in the page
        targets.append({
            "label": label, "kind": entry["kind"], "files": entry["files"],
            "orig": orig, "proposed": proposed,
            "symlinks": sym_inv.get(label, {}).get("count", 0),
        })
    dev_tools = sorted((software.get("developer_tools", {}) or {}).keys())
    exes_all = [
        e.get("name", "") for e in (software.get("user_path_executables", {}) or []) or []
        if isinstance(e, dict) and e.get("name")
    ]
    desk_all = [
        a.get("name", "") for a in (software.get("desktop_applications", {}) or []) or []
        if isinstance(a, dict) and a.get("name")
    ]
    syscounts = {
        manager: len(packages)
        for manager, packages in (software.get("system_packages", {}) or {}).items()
    }
    page = _render_load_html(
        REQUEST_SLUG, archive.name, str(archive.resolve()),
        manifest.get("version", "?"), str(manifest.get("created_at", "?")), encrypted,
        targets, list((notes.get("custom", {}) or []) if isinstance(notes.get("custom"), list) else []),
        syscounts, dev_tools, exes_all[:150], len(exes_all), desk_all[:150], len(desk_all),
        CONFIG,
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output = DATA_DIR / f"{REQUEST_SLUG}-load.html"
    output.write_text(page, encoding="utf-8")
    print(f"Load: {output} ({len(targets)} targets, {len(tree)} files)")
    return _open_report(output, args)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--request",
        type=Path,
        default=DEFAULT_REQUEST,
        help="request JSON file (default: request.json beside this script)",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="NAME",
        help="back up only a named source/file target; repeatable (also filters --restore)",
    )
    parser.add_argument("--dry-run", action="store_true", help="preview without writing (backup or restore)")
    parser.add_argument("--list-targets", action="store_true", help="show configured target status")
    parser.add_argument("--list-versions", action="store_true", help="list available archives/versions for current slug")
    parser.add_argument("--list-slugs", action="store_true", help="list all slugs (hash != slug, slug is data/[slug]/)")
    parser.add_argument("--slug", type=str, help="override slug (default: derived from request.json hash)")
    parser.add_argument("--no-delivery", action="store_true", help="skip copy/upload actions")
    parser.add_argument("--no-elevate", action="store_true", help="do not request sudo/UAC")
    parser.add_argument("--keep-staging", action="store_true", help="retain staging data for diagnosis")
    parser.add_argument("--workers", type=int, help="override configured worker count")
    parser.add_argument("--level", type=int, choices=range(0, 10), help="ZIP level from 0 to 9")
    parser.add_argument("--output-name", help="override the generated ZIP filename")
    parser.add_argument("--restore", action="store_true", help="restore files to original locations from an archive")
    parser.add_argument("--serve", nargs="?", const="report", default=None, choices=["report", "plan", "load"], metavar="MODE", help="serve MODE and open it in the default browser: report (backup contents), plan (live request planner), or load (extraction-time mapping planner). Bare --serve means report.")
    parser.add_argument("--serve-port", type=int, default=None, help="with --serve, also serve the report over HTTP on this port")
    parser.add_argument("--no-browser", action="store_true", help="with --serve, do not auto-open the browser")
    parser.add_argument("--store-passphrase", action="store_true", help="prompt twice and store the archive passphrase in the OS keyring (no backup runs)")
    parser.add_argument("--map", action="append", default=[], metavar="FROM=TO", help="rewrite restore destination prefix (repeatable); e.g. --map /mnt/devspace=D:/data. Combines with restore.path_aliases, longest prefix wins.")
    parser.add_argument("--archive", type=Path, help="zip archive to restore (default: latest in data/<slug>/)")
    parser.add_argument("--version", type=str, help="version to restore, e.g. v000001 or 1")
    parser.add_argument("--force", action="store_true", help="overwrite existing files on restore")
    parser.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def show_targets() -> None:
    print(f"repository: {SCRIPT_DIR}")
    print(f"slug: {REQUEST_SLUG}  (hash {REQUEST_HASH[:12]}...)")
    print(f"archives: {DATA_DIR}")
    print(f"versions: {BCK_DIR}")
    for kind in ("sources", "files"):
        print(f"{kind}:")
        for cfg in CONFIG.get(kind, []):
            status = "enabled" if cfg.get("enabled") else "disabled"
            ignore = (
                f", gitignore={bool(cfg.get('respect_gitignore'))}" if kind == "sources" else ""
            )
            symlinks = (
                f", follow_symlinks={bool(cfg.get('follow_symlinks'))}"
                if kind == "sources"
                else ""
            )
            print(f"  {cfg['name']}: {cfg['path']} ({status}{ignore}{symlinks})")
    environment = "enabled" if CONFIG["environment"].get("enabled") else "disabled"
    print(f"environment: {environment}")
    notes_cfg = CONFIG.get("notes", {}) or {}
    print(f"notes: {'enabled' if notes_cfg.get('enabled') else 'disabled'}")
    enc_cfg = CONFIG.get("archive", {}).get("encryption", {}) or {}
    print(f"encryption: {'7z-aes ON' if enc_cfg.get('enabled') else 'off'}")
    print(f"git_state: {'enabled' if CONFIG.get('git_state', {}).get('enabled') else 'disabled'}")
    print(f"symlinks: {'blocked' if not CONFIG.get('symlinks', {}).get('enabled') else 'recorded'}")
    for action in CONFIG.get("delivery", []):
        status = "enabled" if action.get("enabled") else "disabled"
        print(f"delivery: {action['type']} -> {action['destination']} ({status})")


def show_versions() -> None:
    recs = _list_versions()
    if not recs:
        print(f"No versions yet for slug {REQUEST_SLUG} ({REQUEST_HASH[:8]}).")
        return
    print(f"slug: {REQUEST_SLUG}  hash: {REQUEST_HASH}")
    for r in recs:
        print(f"v{r['version']:06d}  {r['backup_id']}  {r['created_at']}  {r['archive']}  scope={','.join(r.get('scope',[]))}")
        ch = r.get("changes", {})
        print(f"  +{len(ch.get('added',[]))} ~{len(ch.get('modified',[]))} -{len(ch.get('deleted',[]))}")


def show_slugs() -> None:
    m = _list_all_slugs()
    if not m:
        print("No slugs yet. Run a backup first.")
        return
    print(f"repository: {SCRIPT_DIR}")
    for slug, info in sorted(m.items()):
        # enrich with filesystem counts if available
        data_p = BASE_DATA_DIR / slug
        bck_p = BASE_BCK_DIR / "slugs" / slug
        hist = bck_p / "history.jsonl"
        count = 0
        if hist.exists():
            count = sum(1 for l in hist.read_text(encoding="utf-8").splitlines() if l.strip())
        zcount = len(list(data_p.glob("*.zip"))) if data_p.exists() else 0
        print(f"  {slug}  hash={info.get('hash','?')[:12]}  backup_name={info.get('backup_name','?')}  versions={count} zips={zcount}")


def main(argv: list[str] | None = None) -> int:
    global CONFIG

    argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(argv)
    request_path = args.request
    if not request_path.is_absolute():
        request_path = (Path.cwd() / request_path).resolve()
    CONFIG = load_request(request_path)
    # derive slug/data/bck from request hash (slug != hash, it's backup_name + hash prefix)
    _setup_request_dirs(CONFIG, request_path)
    # allow explicit --slug override (for listing/restoring another slug)
    if args.slug:
        # override DATA_DIR/BCK_DIR to requested slug
        global DATA_DIR, BCK_DIR, STAGING_ROOT, SNAPSHOTS_DIR, HEAD_FILE, INDEX_FILE, HISTORY_FILE, LOCK_FILE, REQUEST_SLUG
        REQUEST_SLUG = args.slug
        DATA_DIR = BASE_DATA_DIR / args.slug
        BCK_DIR = BASE_BCK_DIR / "slugs" / args.slug
        STAGING_ROOT = BCK_DIR / "tmp"
        SNAPSHOTS_DIR = BCK_DIR / "snapshots"
        HEAD_FILE = BCK_DIR / "HEAD"
        INDEX_FILE = BCK_DIR / "index.json"
        HISTORY_FILE = BCK_DIR / "history.jsonl"
        LOCK_FILE = BCK_DIR / "lock"
    if args.list_slugs:
        show_slugs()
        return 0
    # restore does not require 7z for extraction (stdlib), but check if available for info
    if args.restore:
        request_elevation(argv, request_path)
        return do_restore(args)
    if args.list_versions:
        show_versions()
        return 0
    if args.serve:
        return do_serve(args)
    if args.store_passphrase:
        return do_store_passphrase(args)
    seven_zip = check_all_dependencies(CONFIG)
    if args.list_targets:
        show_targets()
        print(f"zip engine: {seven_zip}")
        return 0

    archive_cfg = CONFIG["archive"]
    # Resolve before elevation: prompt needs this process's tty, and the
    # secret rides into the elevated child via --preserve-env (memory only).
    # On the elevated re-exec, BACKUP_PASSPHRASE is already set so this
    # returns without prompting.
    passphrase: str | None = None
    if archive_cfg.get("encryption", {}).get("enabled", False) and not args.dry_run:
        passphrase = resolve_passphrase("backup")
    request_elevation(argv, request_path)
    # env overrides (from .env) — CLI still wins
    if not args.workers and os.environ.get("BACKUP_WORKERS"):
        try:
            args.workers = int(os.environ["BACKUP_WORKERS"])
        except ValueError:
            pass
    if args.level is None and os.environ.get("BACKUP_LEVEL"):
        try:
            args.level = int(os.environ["BACKUP_LEVEL"])
        except ValueError:
            pass
    workers = args.workers or int(archive_cfg.get("workers", 0)) or (os.cpu_count() or 4)
    if workers < 1:
        raise ValueError("workers must be at least 1")
    level = args.level if args.level is not None else int(archive_cfg.get("compression_level", 6))
    only = set(args.only)
    known = {cfg["name"] for kind in ("sources", "files") for cfg in CONFIG.get(kind, [])}
    unknown = only - known
    if unknown:
        raise ValueError(f"Unknown --only target(s): {', '.join(sorted(unknown))}")

    items, warnings, per_target = enumerate_items(only)
    environment = capture_environment()
    notes = collect_notes()
    git_state = collect_git_state(only)
    symlinks = collect_symlinks(only)
    if not items and not environment and not notes and not git_state and not symlinks:
        raise RuntimeError("Nothing is enabled or selected for backup")
    total_size = sum(item.source.stat().st_size for item in items if item.source.exists())
    if args.dry_run:
        print(f"Would back up {len(items)} files ({total_size:,} bytes)")
        for name, info in sorted(per_target.items()):
            status = info["status"]
            flag = "✓" if status == "ok" else ("✗" if status == "failed" else "○")
            print(f"  {flag} {name}: {info['file_count']} files ({info['bytes']:,} bytes) [{status}]  {info['path']}")
            if info["error"]:
                print(f"    error: {info['error']}", file=sys.stderr)
        # also show any enumerated warning not tied to per_target?
        for w in warnings:
            # avoid duplicate if already shown
            if not any(w.startswith(n + ":") for n in per_target):
                print(f"WARNING: {w}", file=sys.stderr)
        if environment:
            print(f"Would capture {len(environment)} environment variables")
        if notes:
            print(
                f"Would record notes: {len(notes.get('custom', []))} custom, "
                f"{len(notes.get('commands', {}))} commands, "
                f"software={'yes' if 'software' in notes else 'no'}"
            )
        enc_cfg = CONFIG["archive"].get("encryption", {}) or {}
        if enc_cfg.get("enabled"):
            print(
                "Would encrypt archive with 7z-aes "
                f"(passphrase from {enc_cfg.get('passphrase_from', 'prompt')})"
            )
        if CONFIG.get("git_state", {}).get("enabled"):
            print(f"Would record git state for {len(git_state)} repos")
        if CONFIG.get("symlinks", {}).get("enabled"):
            link_total = sum(v.get("count", 0) for v in symlinks.values())
            print(f"Would record {link_total} symlinks across {len(symlinks)} sources")
        return 0

    enabled_scope = sorted(
        cfg["name"]
        for kind in ("sources", "files")
        for cfg in CONFIG.get(kind, [])
        if cfg.get("enabled") and (not only or cfg["name"] in only)
    )
    if CONFIG["environment"].get("enabled"):
        enabled_scope.append("@environment")
    if CONFIG.get("notes", {}).get("enabled"):
        enabled_scope.append("@notes")
    if CONFIG.get("git_state", {}).get("enabled"):
        enabled_scope.append("@gitstate")
    if CONFIG.get("symlinks", {}).get("enabled"):
        enabled_scope.append("@symlinks")

    acquire_lock()
    staging: Path | None = None
    archive: Path | None = None
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STAGING_ROOT.mkdir(parents=True, exist_ok=True)
        version = next_version()
        created_at = dt.datetime.now(dt.timezone.utc).isoformat()
        timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        started = time.monotonic()
        staging = Path(tempfile.mkdtemp(prefix=timestamp + "-", dir=STAGING_ROOT))
        staged_bytes, failures = stage_items(items, staging, workers)
        if failures:
            failure_messages = [f"{item.source}: {error}" for item, error in failures]
            message = "Unreadable files:\n" + "\n".join(
                f"  {failure}" for failure in failure_messages
            )
            if archive_cfg.get("fail_on_unreadable", True):
                raise PermissionError(message)
            failed_paths = {item.archive_path for item, _ in failures}
            items = [item for item in items if item.archive_path not in failed_paths]
            warnings.extend(failure_messages)
            print("WARNING: " + message, file=sys.stderr)
        index, full_id = build_index(items, environment, staging, workers)
        backup_id = full_id[:12]
        previous = latest_snapshot_for_scope(enabled_scope)
        changes = calculate_changes(previous, index)
        default_name = (
            f"{safe_label(CONFIG['backup_name'])}-v{version:06d}-{backup_id}-{timestamp}.zip"
        )
        output_name = args.output_name or default_name
        if Path(output_name).name != output_name or not output_name.lower().endswith(".zip"):
            raise ValueError("--output-name must be a plain filename ending in .zip")
        archive = DATA_DIR / output_name
        if archive.exists():
            raise FileExistsError(f"Refusing to overwrite existing archive: {archive}")
        write_metadata(staging, items, environment, notes, git_state, symlinks, warnings, version, backup_id, changes)
        engine = create_zip(staging, archive, workers, level, seven_zip, passphrase)
        archive_hash = sha256_file(archive)
        outputs = [archive]
        if archive_cfg.get("checksum", True):
            checksum = archive.with_suffix(archive.suffix + ".sha256")
            checksum.write_text(f"{archive_hash}  {archive.name}\n", encoding="ascii")
            outputs.append(checksum)
        record_version(
            version,
            backup_id,
            created_at,
            enabled_scope,
            archive,
            archive_hash,
            index,
            changes,
            symlinks,
        )
        removed = prune_archives(archive)
        deliveries = []
        delivery_error: Exception | None = None
        if not args.no_delivery:
            try:
                deliveries = deliver(outputs)
                # partial failures (e.g. rclone missing but copy succeeded) are stored on function
                if getattr(deliver, "_last_errors", []):
                    delivery_error = RuntimeError("; ".join(deliver._last_errors))  # type: ignore
            except Exception as error:
                delivery_error = error
    except Exception:
        if archive is not None:
            archive.unlink(missing_ok=True)
            archive.with_suffix(archive.suffix + ".sha256").unlink(missing_ok=True)
        raise
    finally:
        if staging is not None and not args.keep_staging:
            shutil.rmtree(staging, ignore_errors=True)
        release_lock()

    elapsed = time.monotonic() - started
    rate = staged_bytes / elapsed / (1024 * 1024) if elapsed else 0
    print(f"Created: {archive}")
    print(f"Version: v{version:06d}; ID: {backup_id}")
    print(
        "Changes: "
        f"{len(changes['added'])} added, {len(changes['modified'])} modified, "
        f"{len(changes['deleted'])} deleted"
    )
    for name, info in sorted(per_target.items()):
        flag = "✓" if info["status"] == "ok" else ("✗" if info["status"] == "failed" else "○")
        print(f"  {flag} {name}: {info['file_count']} files ({info['bytes']:,} bytes) [{info['status']}]")
        if info["error"]:
            print(f"    error: {info['error']}", file=sys.stderr)
    if warnings:
        for w in warnings:
            if not any(w.startswith(n + ":") for n in per_target):
                print(f"WARNING: {w}", file=sys.stderr)
    print(f"Total: {len(items):,} files; {staged_bytes:,} bytes; {elapsed:.1f}s @ {rate:.1f} MiB/s [{engine}/{workers} workers]")
    for result in deliveries:
        print(f"Delivery: {result}")
    if removed:
        print(f"Retention: removed {len(removed)} old archive(s)")
    if delivery_error is not None:
        print(
            f"ERROR: local backup succeeded, but delivery failed: {delivery_error}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Backup cancelled", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
