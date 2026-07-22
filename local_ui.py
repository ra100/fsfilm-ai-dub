"""Local-first FastAPI control surface for the FSFilm AI Dub pipeline.

The UI server deliberately delegates all pipeline work to ``reusable_pipeline``
subprocesses. Project CSV/JSON/WAV/SRT files remain the portable source of
truth; this module stores only local project registration and job history.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

import numpy as np
import soundfile as sf
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from reusable_pipeline import default_config


REPO_ROOT = Path(__file__).resolve().parent
PROJECTS_ROOT = REPO_ROOT / "projects"
PIPELINE_PATH = REPO_ROOT / "reusable_pipeline.py"
DEFAULT_STATE_DIR = REPO_ROOT / ".local-ui"
WEB_DIST_DIR = REPO_ROOT / "web" / "dist"
WAVEFORM_CACHE_VERSION = 1
TERMINAL_JOB_STATES = {"completed", "failed", "cancelled"}
ALLOWED_COMMANDS = {
    "preflight",
    "build",
    "apply-translations",
    "validate-translations",
    "refine-timing",
    "apply-timing",
    "make-references",
    "render",
    "select",
    "assemble",
    "validate",
    "status",
}
AUDIO_EXTENSIONS = {".wav", ".wave", ".flac", ".mp3", ".m4a", ".aac", ".ogg"}
VIDEO_EXTENSIONS = {".mov", ".mp4", ".mkv", ".avi", ".webm"}
SCRIPT_EXTENSIONS = {".txt", ".md"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def resolve_path(base: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def is_within(path: Path, roots: Iterable[Path]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


def read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read {path}: {error}") from error


def load_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"{path} has no CSV header")
        return reader.fieldnames, list(reader)


def write_csv_atomic(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def write_json_atomic(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_text_atomic(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def project_slug(name: str) -> str:
    value = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-_")
    if not value or len(value) > 64:
        raise ValueError("Project name must yield a 1–64 character lowercase folder name")
    return value


def upload_extension(upload: UploadFile, allowed: set[str], label: str) -> str:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{label} must use one of: {choices}")
    return suffix


async def save_upload(upload: UploadFile, path: Path) -> None:
    """Persist a browser-dropped file without exposing its original path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        while chunk := await upload.read(1024 * 1024):
            handle.write(chunk)


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    config_path: Path
    registered_at: str


class ProjectStore:
    """Persist registered project configs and reject paths outside approved roots."""

    def __init__(self, state_dir: Path, allowed_roots: Iterable[Path]) -> None:
        self.state_dir = state_dir.resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = self.state_dir / "local_ui.sqlite3"
        self.allowed_roots = tuple(sorted({root.resolve() for root in allowed_roots}))
        if not self.allowed_roots:
            raise ValueError("At least one project root must be allowed")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    config_path TEXT UNIQUE NOT NULL,
                    registered_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    command_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    return_code INTEGER,
                    error TEXT,
                    log_path TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "UPDATE jobs SET state = 'failed', finished_at = ?, error = "
                "COALESCE(error, 'Local UI server stopped before this job finished.') "
                "WHERE state IN ('queued', 'running')",
                (utc_now(),),
            )

    def _normalise_config(self, value: str | Path) -> Path:
        candidate = Path(value).expanduser().resolve()
        config_path = candidate / "pipeline.json" if candidate.is_dir() else candidate
        if config_path.name != "pipeline.json":
            raise ValueError("Project path must be a pipeline.json file or its directory")
        if not config_path.is_file():
            raise ValueError(f"Project config does not exist: {config_path}")
        if not is_within(config_path, self.allowed_roots):
            raise PermissionError(f"Project config is outside registered roots: {config_path}")
        return config_path

    def register(self, value: str | Path) -> ProjectRecord:
        config_path = self._normalise_config(value)
        project_id = hashlib.sha256(str(config_path).encode("utf-8")).hexdigest()[:16]
        registered_at = utc_now()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO projects(project_id, config_path, registered_at) VALUES (?, ?, ?)",
                (project_id, str(config_path), registered_at),
            )
            row = connection.execute(
                "SELECT project_id, config_path, registered_at FROM projects WHERE config_path = ?",
                (str(config_path),),
            ).fetchone()
        assert row is not None
        return ProjectRecord(row["project_id"], Path(row["config_path"]), row["registered_at"])

    def discover(self, root: Path) -> list[ProjectRecord]:
        if not root.exists():
            return []
        records: list[ProjectRecord] = []
        for config_path in root.glob("*/pipeline.json"):
            try:
                records.append(self.register(config_path))
            except (PermissionError, ValueError):
                continue
        return records

    def get(self, project_id: str) -> ProjectRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT project_id, config_path, registered_at FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        config_path = Path(row["config_path"])
        try:
            config_path = self._normalise_config(config_path)
        except (PermissionError, ValueError) as error:
            raise KeyError(project_id) from error
        return ProjectRecord(row["project_id"], config_path, row["registered_at"])

    def all(self) -> list[ProjectRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT project_id, config_path, registered_at FROM projects ORDER BY registered_at DESC"
            ).fetchall()
        records: list[ProjectRecord] = []
        for row in rows:
            try:
                records.append(self.get(row["project_id"]))
            except KeyError:
                continue
        return records

    def create_job(
        self,
        *,
        job_id: str,
        project_id: str,
        command_name: str,
        arguments: dict[str, Any],
        log_path: Path,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs(job_id, project_id, command_name, arguments_json, state,
                                 created_at, log_path)
                VALUES (?, ?, ?, ?, 'queued', ?, ?)
                """,
                (job_id, project_id, command_name, json.dumps(arguments), utc_now(), str(log_path)),
            )

    def update_job(self, job_id: str, **values: Any) -> None:
        if not values:
            return
        columns = ", ".join(f"{key} = ?" for key in values)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {columns} WHERE job_id = ?",
                (*values.values(), job_id),
            )

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        value = dict(row)
        value["arguments"] = json.loads(value.pop("arguments_json"))
        return value

    def jobs_for_project(self, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["arguments"] = json.loads(value.pop("arguments_json"))
            values.append(value)
        return values


def project_paths(record: ProjectRecord) -> dict[str, Path]:
    config = read_json(record.config_path, {})
    if not isinstance(config, dict):
        raise ValueError(f"Project config is not an object: {record.config_path}")
    base = record.config_path.parent
    layout = config.get("layout", {})
    if not isinstance(layout, dict):
        layout = {}
    work = resolve_path(base, layout.get("work_dir", "work"))
    output = resolve_path(base, layout.get("output_dir", "output"))
    return {"config": record.config_path, "project": base, "work": work, "output": output}


def project_snapshot(record: ProjectRecord) -> dict[str, Any]:
    config = read_json(record.config_path, {})
    if not isinstance(config, dict):
        raise ValueError(f"Project config is not an object: {record.config_path}")
    paths = project_paths(record)
    manifest = read_json(paths["work"] / "turn_manifest.json", [])
    if not isinstance(manifest, list):
        manifest = []
    input_config = config.get("input", {}) if isinstance(config.get("input"), dict) else {}
    translation_path = paths["work"] / "translation_review.csv"
    selection_path = paths["work"] / "selection.csv"
    approved = sum(item.get("translation_state") == "approved" for item in manifest if isinstance(item, dict))
    rendered = sum(bool(item.get("render", {}).get("candidates")) for item in manifest if isinstance(item, dict))
    selected = sum(bool(item.get("render", {}).get("selection")) for item in manifest if isinstance(item, dict))
    return {
        "id": record.project_id,
        "name": config.get("project", record.config_path.parent.name),
        "config_path": str(record.config_path),
        "registered_at": record.registered_at,
        "languages": config.get("languages", {}),
        "has_video": bool(input_config.get("video")),
        "stage": "ready" if manifest else "initialized",
        "counts": {
            "turns": len(manifest),
            "approved_translations": approved,
            "rendered_turns": rendered,
            "selected_turns": selected,
        },
        "review_files": {
            "translation_review": translation_path.exists(),
            "selection": selection_path.exists(),
            "timing_overrides": (paths["work"] / "timing_overrides.json").exists(),
            "reference_selection": (paths["work"] / "reference_selection.json").exists(),
        },
    }


async def create_imported_project(
    store: ProjectStore,
    *,
    display_name: str,
    source_language: str,
    target_language: str,
    audio: UploadFile,
    source_srt: UploadFile,
    target_srt: UploadFile,
    dialogue_script: UploadFile,
    video: UploadFile | None,
) -> ProjectRecord:
    """Create an isolated project from browser uploads, never from browser paths."""
    name = display_name.strip()
    if not name or len(name) > 160:
        raise ValueError("Project name must be 1–160 characters")
    if not re.fullmatch(r"[a-z]{2,8}", source_language) or not re.fullmatch(r"[a-z]{2,8}", target_language):
        raise ValueError("Language codes must be 2–8 letters supported by the selected models")
    if not is_within(PROJECTS_ROOT, store.allowed_roots):
        raise PermissionError(f"Imported projects require an allowed root containing {PROJECTS_ROOT}")
    project = PROJECTS_ROOT / project_slug(name)
    if project.exists():
        raise FileExistsError(f"A project already exists at {project}; choose another name")
    audio_suffix = upload_extension(audio, AUDIO_EXTENSIONS, "Dialogue audio")
    source_suffix = upload_extension(source_srt, {".srt"}, "Source subtitle")
    target_suffix = upload_extension(target_srt, {".srt"}, "Target subtitle")
    script_suffix = upload_extension(dialogue_script, SCRIPT_EXTENSIONS, "Dialogue script")
    video_suffix = upload_extension(video, VIDEO_EXTENSIONS, "Video") if video else None
    try:
        project.mkdir(parents=True)
        inputs = project / "inputs"
        audio_path = inputs / f"dialogue{audio_suffix}"
        source_path = inputs / f"source{source_suffix}"
        target_path = inputs / f"target{target_suffix}"
        script_path = inputs / f"dialogue_script{script_suffix}"
        await save_upload(audio, audio_path)
        await save_upload(source_srt, source_path)
        await save_upload(target_srt, target_path)
        await save_upload(dialogue_script, script_path)
        video_path: Path | None = None
        if video and video_suffix:
            video_path = inputs / f"picture{video_suffix}"
            await save_upload(video, video_path)
        for folder in ("work", "output", "notes"):
            (project / folder).mkdir(exist_ok=True)
        config = default_config(
            project,
            name,
            audio_path,
            source_path,
            target_path,
            script_path,
            source_language,
            target_language,
            video=video_path,
        )
        write_json_atomic(project / "pipeline.json", config)
        (project / "notes" / "README.txt").write_text(
            "Imported through the local UI. Inputs are intentionally ignored by Git.\n"
            "Use work/ for reviewable changes and output/ for generated delivery artifacts.\n",
            encoding="utf-8",
        )
        return store.register(project)
    except Exception:
        if project.exists():
            shutil.rmtree(project)
        raise


async def attach_video(record: ProjectRecord, upload: UploadFile) -> dict[str, Any]:
    suffix = upload_extension(upload, VIDEO_EXTENSIONS, "Video")
    paths = project_paths(record)
    inputs = paths["project"] / "inputs"
    # A new immutable filename retains the prior picture asset if the editor
    # later needs to revert manually.
    destination = inputs / f"picture_{uuid.uuid4().hex[:8]}{suffix}"
    await save_upload(upload, destination)
    config = read_json(record.config_path, {})
    if not isinstance(config, dict):
        raise ValueError("Project config is not an object")
    input_config = config.setdefault("input", {})
    if not isinstance(input_config, dict):
        raise ValueError("Project config input is not an object")
    input_config["video"] = destination.relative_to(paths["project"]).as_posix()
    write_json_atomic(record.config_path, config)
    return project_snapshot(record)


def waveform_cache_path(cache_dir: Path, audio: Path, bins: int) -> Path:
    stat = audio.stat()
    identity = f"{WAVEFORM_CACHE_VERSION}:{audio}:{stat.st_size}:{stat.st_mtime_ns}:{bins}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.json"


def generate_waveform(audio: Path, bins: int) -> dict[str, Any]:
    """Return min/max PCM peaks without loading a feature-length track at once."""
    try:
        with sf.SoundFile(audio) as source:
            frames = int(source.frames)
            sample_rate = int(source.samplerate)
            if frames <= 0 or sample_rate <= 0:
                raise ValueError("Audio has no samples")
            actual_bins = min(max(64, bins), frames)
            samples_per_bin = max(1, (frames + actual_bins - 1) // actual_bins)
            effective_bins = (frames + samples_per_bin - 1) // samples_per_bin
            minimum = np.full(effective_bins, np.inf, dtype=np.float32)
            maximum = np.full(effective_bins, -np.inf, dtype=np.float32)
            offset = 0
            while True:
                block = source.read(65_536, dtype="float32", always_2d=True)
                if len(block) == 0:
                    break
                mono = block.mean(axis=1)
                indices = ((offset + np.arange(len(mono))) // samples_per_bin).astype(np.intp)
                np.minimum.at(minimum, indices, mono)
                np.maximum.at(maximum, indices, mono)
                offset += len(mono)
    except RuntimeError as error:
        raise ValueError(f"Cannot decode waveform audio {audio.name}: {error}") from error
    minimum[~np.isfinite(minimum)] = 0.0
    maximum[~np.isfinite(maximum)] = 0.0
    return {
        "duration": round(frames / sample_rate, 6),
        "sample_rate": sample_rate,
        "bins": effective_bins,
        "min": [round(float(value), 4) for value in minimum],
        "max": [round(float(value), 4) for value in maximum],
    }


def waveform_for_project(cache_dir: Path, record: ProjectRecord, bins: int) -> dict[str, Any]:
    audio = safe_asset_path(record, "audio")
    cache_path = waveform_cache_path(cache_dir, audio, bins)
    if cache_path.is_file():
        return read_json(cache_path, {})
    cache_dir.mkdir(parents=True, exist_ok=True)
    data = generate_waveform(audio, bins)
    write_json_atomic(cache_path, data)
    return data


def manifest_group(record: ProjectRecord, group_number: int) -> dict[str, Any]:
    manifest = read_json(project_paths(record)["work"] / "turn_manifest.json", [])
    if not isinstance(manifest, list):
        raise KeyError(group_number)
    group = next((item for item in manifest if isinstance(item, dict) and int(item.get("group", -1)) == group_number), None)
    if group is None:
        raise KeyError(group_number)
    return group


def render_asset_path(record: ProjectRecord, relative_path: str) -> Path:
    paths = project_paths(record)
    path = resolve_path(paths["project"], relative_path)
    render_root = (paths["work"] / "renders").resolve()
    if not path.is_file() or not path.is_relative_to(render_root):
        raise FileNotFoundError(relative_path)
    return path


def candidate_summaries(record: ProjectRecord, group_number: int) -> dict[str, Any]:
    group = manifest_group(record, group_number)
    render = group.get("render", {}) if isinstance(group.get("render"), dict) else {}
    selection = render.get("selection") if isinstance(render.get("selection"), dict) else {}
    analyzed = selection.get("candidates", []) if isinstance(selection, dict) else []
    analyzed_by_variant = {int(item["variant"]): item for item in analyzed if isinstance(item, dict) and "variant" in item}
    candidates = []
    for candidate in render.get("candidates", []):
        if not isinstance(candidate, dict):
            continue
        variant = int(candidate["variant"])
        detail = analyzed_by_variant.get(variant, {})
        candidates.append(
            {
                "variant": variant,
                "duration": detail.get("duration", candidate.get("duration")),
                "seed": candidate.get("seed"),
                "word_recall": detail.get("word_recall"),
                "ending_present": detail.get("ending_present"),
                "available_duration": detail.get("available_duration"),
                "overrun": detail.get("overrun"),
                "score": detail.get("score"),
                "transcript": detail.get("transcript"),
                "selected": bool(selection and int(selection.get("candidate", -1)) == variant),
            }
        )
    return {
        "group": group_number,
        "role": group.get("role"),
        "text": group.get("lip_sync_text"),
        "candidates": candidates,
        "selection": selection or None,
    }


def candidate_audio(record: ProjectRecord, group_number: int, asset: str) -> Path:
    group = manifest_group(record, group_number)
    render = group.get("render", {}) if isinstance(group.get("render"), dict) else {}
    if asset == "selected":
        selection = render.get("selection")
        if not isinstance(selection, dict) or not isinstance(selection.get("path"), str):
            raise FileNotFoundError(asset)
        return render_asset_path(record, selection["path"])
    match = re.fullmatch(r"candidate-(\d{1,2})", asset)
    if not match:
        raise FileNotFoundError(asset)
    wanted = int(match.group(1))
    candidate = next((item for item in render.get("candidates", []) if isinstance(item, dict) and int(item.get("variant", -1)) == wanted), None)
    if not candidate or not isinstance(candidate.get("path"), str):
        raise FileNotFoundError(asset)
    return render_asset_path(record, candidate["path"])


def validate_role(role: str) -> str:
    value = role.strip().upper()
    if not re.fullmatch(r"[A-Z0-9_]{1,32}", value):
        raise ValueError("Role must be 1–32 uppercase letters, digits, or underscores")
    return value


def script_role_names(record: ProjectRecord, config: dict[str, Any]) -> set[str]:
    """Return cast labels declared anywhere in the project dialogue script.

    The manifest only contains roles that the current subtitle alignment used.
    The script is the complete cast source, including a character whose line
    was skipped or has not been built yet.
    """
    input_config = config.get("input", {}) if isinstance(config.get("input"), dict) else {}
    script_value = input_config.get("dialogue_script") if isinstance(input_config, dict) else None
    if not isinstance(script_value, str):
        return set()
    script_path = resolve_path(project_paths(record)["project"], script_value)
    if not script_path.is_file():
        return set()
    roles: set[str] = set()
    for raw_line in script_path.read_text(encoding="utf-8-sig").splitlines():
        match = re.fullmatch(r"\s*([^:]{1,32}):\s*.+", raw_line)
        if not match:
            continue
        try:
            roles.add(validate_role(match.group(1)))
        except ValueError:
            continue
    return roles


def update_group_role(record: ProjectRecord, group_number: int, role: str) -> dict[str, Any]:
    """Correct a script-backed cast assignment and retire stale voice takes.

    ``script_indices`` index only non-empty dialogue rows, not physical file
    lines.  Updating the actual script keeps a later ``build`` reproducible;
    updating every manifest group that cites those rows prevents an old actor's
    rendered candidate from being selected accidentally.
    """
    role = validate_role(role)
    paths = project_paths(record)
    manifest_path = paths["work"] / "turn_manifest.json"
    manifest = read_json(manifest_path, [])
    if not isinstance(manifest, list):
        raise ValueError("turn_manifest.json must be a list")
    target = next(
        (item for item in manifest if isinstance(item, dict) and int(item.get("group", -1)) == group_number),
        None,
    )
    if target is None:
        raise KeyError(group_number)
    previous_role = str(target.get("role", "")).strip()
    if previous_role == role:
        return {
            "group": group_number,
            "role": role,
            "previous_role": previous_role,
            "affected_groups": [],
            "changed": False,
            "next_step": "This turn already has that character assignment.",
        }

    raw_indices = target.get("script_indices")
    if not isinstance(raw_indices, list) or not raw_indices:
        raise ValueError("This turn has no script row mapping; run build before correcting its character")
    try:
        script_indices = {int(index) for index in raw_indices}
    except (TypeError, ValueError) as error:
        raise ValueError("This turn has an invalid script row mapping") from error
    if any(index < 0 for index in script_indices):
        raise ValueError("This turn has an invalid script row mapping")

    config = read_json(record.config_path, {})
    input_config = config.get("input", {}) if isinstance(config, dict) else {}
    script_value = input_config.get("dialogue_script") if isinstance(input_config, dict) else None
    if not isinstance(script_value, str):
        raise ValueError("Project config has no dialogue script")
    script_path = resolve_path(paths["project"], script_value)
    if not script_path.is_file():
        raise ValueError(f"Dialogue script is unavailable: {script_path}")

    lines = script_path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    found: set[int] = set()
    row_index = -1
    for physical_index, raw_line in enumerate(lines):
        body = raw_line.rstrip("\r\n")
        stripped = body.strip()
        if not stripped:
            continue
        match = re.fullmatch(r"(\s*)[^:]{1,32}(:\s*.+)", body)
        if not match:
            raise ValueError(f"Dialogue script line {physical_index + 1} must be 'ROLE: dialogue'")
        row_index += 1
        if row_index not in script_indices:
            continue
        ending = raw_line[len(body):]
        lines[physical_index] = f"{match.group(1)}{role}{match.group(2)}{ending}"
        found.add(row_index)
    missing = sorted(script_indices - found)
    if missing:
        joined = ", ".join(str(index) for index in missing)
        raise ValueError(f"Dialogue script no longer contains mapped row(s): {joined}; run build to refresh the mapping")
    write_text_atomic(script_path, "".join(lines))

    affected: list[int] = []
    for item in manifest:
        if not isinstance(item, dict):
            continue
        item_indices = item.get("script_indices", [])
        if not isinstance(item_indices, list):
            continue
        try:
            shares_script_row = bool(script_indices & {int(index) for index in item_indices})
        except (TypeError, ValueError):
            continue
        if not shares_script_row:
            continue
        item["role"] = role
        item["role_override"] = {
            "from": previous_role,
            "to": role,
            "updated_at": utc_now(),
            "script_indices": sorted(script_indices),
        }
        item["render"] = {
            "candidates": [],
            "selection": None,
            "invalidated_reason": f"Character corrected from {previous_role or 'unknown'} to {role}",
        }
        group_id = item.get("group")
        if isinstance(group_id, int):
            affected.append(group_id)
    write_json_atomic(manifest_path, manifest)

    review_path = paths["work"] / "translation_review.csv"
    if review_path.is_file():
        headers, rows = load_csv(review_path)
        if "role" not in headers:
            headers.append("role")
            for row in rows:
                row.setdefault("role", "")
        affected_strings = {str(group) for group in affected}
        for row in rows:
            if row.get("group") in affected_strings:
                row["role"] = role
        write_csv_atomic(review_path, headers, rows)

    overrides_path = paths["work"] / "candidate_overrides.json"
    overrides = read_json(overrides_path, {})
    if isinstance(overrides, dict):
        for group in affected:
            overrides.pop(str(group), None)
        write_json_atomic(overrides_path, overrides)

    return {
        "group": group_number,
        "role": role,
        "previous_role": previous_role,
        "affected_groups": sorted(affected),
        "changed": True,
        "next_step": f"Add or prepare a {role} reference, then render fresh takes for the affected turn(s).",
    }


def role_summaries(record: ProjectRecord) -> list[dict[str, Any]]:
    config = read_json(record.config_path, {})
    paths = project_paths(record)
    configured = config.get("roles", {}) if isinstance(config, dict) and isinstance(config.get("roles"), dict) else {}
    manifest = read_json(paths["work"] / "turn_manifest.json", [])
    roles = script_role_names(record, config) if isinstance(config, dict) else set()
    for value in configured:
        try:
            roles.add(validate_role(str(value)))
        except ValueError:
            continue
    for item in manifest if isinstance(manifest, list) else []:
        if not isinstance(item, dict) or not item.get("role"):
            continue
        try:
            roles.add(validate_role(str(item["role"])))
        except ValueError:
            continue
    results = []
    for role in sorted(roles):
        entry = configured.get(role, {}) if isinstance(configured.get(role), dict) else {}
        generated = paths["work"] / "references" / f"{role}.wav"
        reference = entry.get("reference_audio")
        emotion = entry.get("emotion_audio")
        results.append(
            {
                "role": role,
                "configured_reference": bool(reference),
                "configured_emotion": bool(emotion),
                "generated_reference": generated.is_file(),
            }
        )
    return results


async def attach_role_audio(record: ProjectRecord, role: str, kind: str, upload: UploadFile) -> dict[str, Any]:
    role = validate_role(role)
    if kind not in {"reference", "emotion"}:
        raise ValueError("Audio kind must be reference or emotion")
    suffix = upload_extension(upload, AUDIO_EXTENSIONS, "Role audio")
    paths = project_paths(record)
    destination = paths["project"] / "inputs" / "references" / f"{role.lower()}_{kind}_{uuid.uuid4().hex[:8]}{suffix}"
    await save_upload(upload, destination)
    config = read_json(record.config_path, {})
    if not isinstance(config, dict):
        raise ValueError("Project config is not an object")
    roles = config.setdefault("roles", {})
    if not isinstance(roles, dict):
        raise ValueError("Project roles are not an object")
    entry = roles.setdefault(role, {})
    if not isinstance(entry, dict):
        raise ValueError(f"Project role {role} is not an object")
    entry["reference_audio" if kind == "reference" else "emotion_audio"] = destination.relative_to(paths["project"]).as_posix()
    write_json_atomic(record.config_path, config)
    return {"role": role, "kind": kind, "roles": role_summaries(record)}


def role_audio(record: ProjectRecord, role: str, kind: str) -> Path:
    role = validate_role(role)
    paths = project_paths(record)
    config = read_json(record.config_path, {})
    entry = config.get("roles", {}).get(role, {}) if isinstance(config, dict) else {}
    if kind == "reference":
        generated = paths["work"] / "references" / f"{role}.wav"
        if generated.is_file():
            return generated
        key = "reference_audio"
    elif kind == "emotion":
        key = "emotion_audio"
    else:
        raise FileNotFoundError(kind)
    source = entry.get(key) if isinstance(entry, dict) else None
    if not isinstance(source, str):
        raise FileNotFoundError(kind)
    path = resolve_path(paths["project"], source)
    if not path.is_file():
        raise FileNotFoundError(kind)
    return path


def pause_overrides(record: ProjectRecord) -> dict[str, list[dict[str, Any]]]:
    path = project_paths(record)["work"] / "pause_overrides.json"
    value = read_json(path, {})
    if not isinstance(value, dict):
        raise ValueError("pause_overrides.json must be an object")
    return value


def update_pause_overrides(record: ProjectRecord, group_number: int, markers: list[PauseMarker]) -> dict[str, Any]:
    group = manifest_group(record, group_number)
    words = re.findall(r"\b[\w']+\b", str(group.get("lip_sync_text", "")))
    if any(marker.after_word > len(words) for marker in markers):
        raise ValueError(f"Pause word position exceeds the {len(words)} words in group {group_number}")
    overrides = pause_overrides(record)
    overrides[str(group_number)] = [marker.model_dump() for marker in sorted(markers, key=lambda item: item.after_word)]
    write_json_atomic(project_paths(record)["work"] / "pause_overrides.json", overrides)
    config = read_json(record.config_path, {})
    translation = config.get("translation", {}) if isinstance(config, dict) and isinstance(config.get("translation"), dict) else {}
    words_per_second = float(translation.get("words_per_second", 2.35))
    speech_seconds = len(words) / max(0.1, words_per_second)
    pause_seconds = sum(marker.duration_ms for marker in markers) / 1000.0
    available = float(group.get("source_span", float(group.get("source_end", 0)) - float(group.get("source_start", 0))))
    return {
        "group": group_number,
        "markers": overrides[str(group_number)],
        "estimated_speech_seconds": round(speech_seconds, 3),
        "requested_pause_seconds": round(pause_seconds, 3),
        "available_seconds": round(available, 3),
        "remaining_seconds": round(available - speech_seconds - pause_seconds, 3),
    }


def group_summaries(record: ProjectRecord) -> list[dict[str, Any]]:
    manifest = read_json(project_paths(record)["work"] / "turn_manifest.json", [])
    if not isinstance(manifest, list):
        return []
    groups: list[dict[str, Any]] = []
    for item in manifest:
        if not isinstance(item, dict):
            continue
        render = item.get("render", {}) if isinstance(item.get("render"), dict) else {}
        groups.append(
            {
                "group": item.get("group"),
                "role": item.get("role"),
                "source_start": item.get("source_start"),
                "source_end": item.get("source_end"),
                "source_text": item.get("source_text"),
                "legacy_target_text": item.get("legacy_target_text"),
                "lip_sync_text": item.get("lip_sync_text"),
                "target_word_budget": item.get("target_word_budget"),
                "translation_state": item.get("translation_state"),
                "timing_state": item.get("timing_state"),
                "role_confidence": item.get("role_confidence"),
                "candidate_count": len(render.get("candidates", [])),
                "selection": render.get("selection"),
            }
        )
    return groups


class RegisterProjectRequest(BaseModel):
    config_path: str = Field(min_length=1)


class TranslationUpdate(BaseModel):
    lip_sync_text: str | None = Field(default=None, max_length=4000)
    approved: bool | None = None
    translator_notes: str | None = Field(default=None, max_length=4000)

    @field_validator("lip_sync_text")
    @classmethod
    def non_blank_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("lip_sync_text must not be blank")
        return value.strip() if value is not None else None


class CandidateOverride(BaseModel):
    variant: int = Field(ge=1, le=99)


class GroupRoleUpdate(BaseModel):
    role: str = Field(min_length=1, max_length=32)

    @field_validator("role")
    @classmethod
    def valid_role(cls, value: str) -> str:
        return validate_role(value)


class PauseMarker(BaseModel):
    after_word: int = Field(ge=1, le=500)
    duration_ms: int = Field(ge=50, le=5000)
    mode: str = "natural"

    @field_validator("mode")
    @classmethod
    def known_mode(cls, value: str) -> str:
        if value not in {"natural", "hard"}:
            raise ValueError("Pause mode must be natural or hard")
        return value


class PauseUpdate(BaseModel):
    markers: list[PauseMarker] = Field(default_factory=list, max_length=12)


class JobRequest(BaseModel):
    command: str = Field(min_length=1, max_length=64)
    groups: list[int] = Field(default_factory=list, max_length=200)
    roles: list[str] = Field(default_factory=list, max_length=100)
    variants: int = Field(default=3, ge=1, le=6)
    force: bool = False
    strict: bool = False
    confirm: bool = False

    @field_validator("groups")
    @classmethod
    def valid_groups(cls, groups: list[int]) -> list[int]:
        if any(group < 1 for group in groups):
            raise ValueError("Group IDs must be positive")
        return sorted(set(groups))

    @field_validator("roles")
    @classmethod
    def valid_roles(cls, roles: list[str]) -> list[str]:
        cleaned = sorted({role.strip() for role in roles if role.strip()})
        if any(not role.replace("_", "").isalnum() or len(role) > 32 for role in cleaned):
            raise ValueError("Roles must be 1–32 alphanumeric/underscore characters")
        return cleaned


@dataclass
class QueuedJob:
    job_id: str
    project: ProjectRecord
    request: JobRequest
    cancelled: bool = False
    process: asyncio.subprocess.Process | None = None


class PipelineJobQueue:
    """One-worker queue; rendering jobs cannot contend for the local GPU."""

    def __init__(self, store: ProjectStore, state_dir: Path) -> None:
        self.store = store
        self.state_dir = state_dir
        self.log_dir = state_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.queue: asyncio.Queue[QueuedJob | None] = asyncio.Queue()
        self.live_jobs: dict[str, QueuedJob] = {}
        self.worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.worker is None:
            self.worker = asyncio.create_task(self._run(), name="ai-dub-local-ui-jobs")

    async def stop(self) -> None:
        for job in self.live_jobs.values():
            job.cancelled = True
            if job.process and job.process.returncode is None:
                job.process.terminate()
        if self.worker:
            await self.queue.put(None)
            await self.worker
            self.worker = None

    def _interpreter(self, command: str) -> Path:
        if command == "render":
            configured = os.environ.get("AI_DUB_RENDER_PYTHON")
            fallback = REPO_ROOT / "third_party" / "index-tts" / ".venv" / "bin" / "python"
        else:
            configured = os.environ.get("AI_DUB_PYTHON")
            fallback = Path(sys.executable)
        interpreter = Path(configured).expanduser() if configured else fallback
        if not interpreter.is_file():
            raise ValueError(f"Python interpreter is unavailable for {command}: {interpreter}")
        return interpreter

    def _command(self, project: ProjectRecord, request: JobRequest) -> list[str]:
        command = request.command.strip()
        if command not in ALLOWED_COMMANDS:
            raise ValueError(f"Unsupported pipeline command: {command}")
        if command in {"render", "select"} and not request.groups:
            raise ValueError(f"{command} requires at least one targeted group")
        if command in {"assemble", "validate"} and not request.confirm:
            raise ValueError(f"{command} requires explicit confirmation")
        args = [str(self._interpreter(command)), str(PIPELINE_PATH), command, "--config", str(project.config_path)]
        if command in {"render", "select"}:
            args.extend(["--groups", ",".join(str(group) for group in request.groups)])
        if command == "render":
            args.extend(["--variants", str(request.variants)])
            if request.force:
                args.append("--force")
        if command == "make-references":
            if request.roles:
                args.extend(["--roles", ",".join(request.roles)])
            if request.force:
                args.append("--force")
        if command in {"validate-translations", "validate"} and request.strict:
            args.append("--strict")
        return args

    async def submit(self, project: ProjectRecord, request: JobRequest) -> dict[str, Any]:
        command = self._command(project, request)
        job_id = uuid.uuid4().hex
        log_path = self.log_dir / f"{job_id}.log"
        log_path.touch()
        self.store.create_job(
            job_id=job_id,
            project_id=project.project_id,
            command_name=request.command,
            arguments={"argv": command[2:], **request.model_dump()},
            log_path=log_path,
        )
        job = QueuedJob(job_id=job_id, project=project, request=request)
        self.live_jobs[job_id] = job
        await self.queue.put(job)
        return self.store.get_job(job_id)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        try:
            job_state = self.store.get_job(job_id)
        except KeyError as error:
            raise KeyError(job_id) from error
        if job_state["state"] in TERMINAL_JOB_STATES:
            return job_state
        live = self.live_jobs.get(job_id)
        if live is None:
            self.store.update_job(job_id, state="cancelled", finished_at=utc_now(), error="Cancelled before execution.")
            return self.store.get_job(job_id)
        live.cancelled = True
        if live.process and live.process.returncode is None:
            live.process.terminate()
        return self.store.get_job(job_id)

    def log_tail(self, job_id: str, limit: int = 32_000) -> str:
        job = self.store.get_job(job_id)
        path = Path(job["log_path"])
        if not path.is_file() or not path.is_relative_to(self.log_dir):
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]

    async def _run(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                if job is None:
                    return
                if job.cancelled:
                    self.store.update_job(job.job_id, state="cancelled", finished_at=utc_now(), error="Cancelled before execution.")
                    continue
                await self._execute(job)
            finally:
                if job is not None:
                    self.live_jobs.pop(job.job_id, None)
                self.queue.task_done()

    async def _execute(self, job: QueuedJob) -> None:
        try:
            command = self._command(job.project, job.request)
            self.store.update_job(job.job_id, state="running", started_at=utc_now())
            environment = os.environ.copy()
            environment.setdefault("HF_HOME", str(REPO_ROOT / "models"))
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=REPO_ROOT,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            job.process = process
            assert process.stdout is not None
            log_path = self.log_dir / f"{job.job_id}.log"
            with log_path.open("a", encoding="utf-8") as log:
                log.write("$ " + " ".join(command) + "\n")
                while True:
                    chunk = await process.stdout.readline()
                    if not chunk:
                        break
                    log.write(chunk.decode("utf-8", errors="replace"))
                    log.flush()
            return_code = await process.wait()
            if job.cancelled:
                self.store.update_job(
                    job.job_id,
                    state="cancelled",
                    finished_at=utc_now(),
                    return_code=return_code,
                    error="Cancelled by the editor.",
                )
            elif return_code == 0:
                self.store.update_job(job.job_id, state="completed", finished_at=utc_now(), return_code=return_code)
            else:
                self.store.update_job(
                    job.job_id,
                    state="failed",
                    finished_at=utc_now(),
                    return_code=return_code,
                    error=f"Pipeline exited with status {return_code}.",
                )
        except Exception as error:  # Keep a backend failure inspectable in job history.
            self.store.update_job(job.job_id, state="failed", finished_at=utc_now(), error=str(error))


def safe_asset_path(record: ProjectRecord, asset: str) -> Path:
    config = read_json(record.config_path, {})
    inputs = config.get("input", {}) if isinstance(config, dict) else {}
    asset_key = {"audio": "audio", "video": "video", "source-srt": "source_srt", "target-srt": "target_srt"}.get(asset)
    if not asset_key or not inputs.get(asset_key):
        raise FileNotFoundError(asset)
    path = resolve_path(record.config_path.parent, str(inputs[asset_key]))
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def create_app(
    *,
    state_dir: Path | None = None,
    allowed_roots: Iterable[Path] | None = None,
    initial_projects: Iterable[Path] = (),
) -> FastAPI:
    state = (state_dir or DEFAULT_STATE_DIR).resolve()
    roots = tuple(allowed_roots or (REPO_ROOT / "projects",))
    projects = ProjectStore(state, roots)
    projects.discover(PROJECTS_ROOT)
    for project in initial_projects:
        projects.register(project)
    jobs = PipelineJobQueue(projects, state)
    waveform_cache = state / "waveforms"

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await jobs.start()
        try:
            yield
        finally:
            await jobs.stop()

    app = FastAPI(
        title="FSFilm AI Dub Local UI",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )
    # Vite's development server is local too; production assets are served by
    # this same process after ``npm run build``.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["content-type"],
    )
    if (WEB_DIST_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=WEB_DIST_DIR / "assets"), name="ui-assets")
    app.state.projects = projects
    app.state.jobs = jobs

    def record_or_404(project_id: str) -> ProjectRecord:
        try:
            return projects.get(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Unknown or unavailable project") from error

    @app.get("/", include_in_schema=False)
    async def index():
        built_index = WEB_DIST_DIR / "index.html"
        if built_index.is_file():
            return FileResponse(built_index)
        return """<!doctype html><html><head><meta charset='utf-8'><title>FSFilm AI Dub</title></head>
<body><main><h1>FSFilm AI Dub local UI</h1><p>The backend is running locally.</p>
<p>Build the React review interface with <code>cd web &amp;&amp; pnpm install &amp;&amp; pnpm build</code>,
or use <a href='/api/docs'>the API documentation</a>.</p></main></body></html>"""

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": "fsfilm-ai-dub-local-ui", "version": app.version, "project_count": len(projects.all())}

    @app.get("/api/projects")
    async def list_projects() -> list[dict[str, Any]]:
        return [project_snapshot(record) for record in projects.all()]

    @app.post("/api/projects", status_code=201)
    async def register_project(request: RegisterProjectRequest) -> dict[str, Any]:
        try:
            record = projects.register(request.config_path)
            return project_snapshot(record)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/projects/import", status_code=201)
    async def import_project(
        project_name: str = Form(...),
        source_language: str = Form(...),
        target_language: str = Form(...),
        audio: UploadFile = File(...),
        source_srt: UploadFile = File(...),
        target_srt: UploadFile = File(...),
        dialogue_script: UploadFile = File(...),
        video: UploadFile | None = File(default=None),
    ) -> dict[str, Any]:
        try:
            record = await create_imported_project(
                projects,
                display_name=project_name,
                source_language=source_language.strip().lower(),
                target_language=target_language.strip().lower(),
                audio=audio,
                source_srt=source_srt,
                target_srt=target_srt,
                dialogue_script=dialogue_script,
                video=video,
            )
            return project_snapshot(record)
        except FileExistsError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/projects/{project_id}/video")
    async def upload_video(project_id: str, video: UploadFile = File(...)) -> dict[str, Any]:
        try:
            return await attach_video(record_or_404(project_id), video)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/projects/{project_id}")
    async def get_project(project_id: str) -> dict[str, Any]:
        return project_snapshot(record_or_404(project_id))

    @app.get("/api/projects/{project_id}/groups")
    async def get_groups(project_id: str) -> list[dict[str, Any]]:
        return group_summaries(record_or_404(project_id))

    @app.get("/api/projects/{project_id}/groups/{group_number}/candidates")
    async def get_candidates(project_id: str, group_number: int) -> dict[str, Any]:
        try:
            return candidate_summaries(record_or_404(project_id), group_number)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Unknown dialogue group") from error

    @app.get("/api/projects/{project_id}/groups/{group_number}/audio/{asset}")
    async def get_candidate_audio(project_id: str, group_number: int, asset: str) -> FileResponse:
        try:
            return FileResponse(candidate_audio(record_or_404(project_id), group_number, asset))
        except (KeyError, FileNotFoundError) as error:
            raise HTTPException(status_code=404, detail="Candidate audio is unavailable") from error

    @app.put("/api/projects/{project_id}/groups/{group_number}/candidate-override")
    async def set_candidate_override(project_id: str, group_number: int, update: CandidateOverride) -> dict[str, Any]:
        record = record_or_404(project_id)
        try:
            candidates = candidate_summaries(record, group_number)["candidates"]
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Unknown dialogue group") from error
        if update.variant not in {item["variant"] for item in candidates}:
            raise HTTPException(status_code=422, detail="Requested candidate does not exist for this group")
        path = project_paths(record)["work"] / "candidate_overrides.json"
        overrides = read_json(path, {})
        if not isinstance(overrides, dict):
            raise HTTPException(status_code=422, detail="candidate_overrides.json must be an object")
        overrides[str(group_number)] = update.variant
        write_json_atomic(path, overrides)
        return {"group": group_number, "variant": update.variant, "next_step": "Run select for this group to build selected.wav."}

    @app.put("/api/projects/{project_id}/groups/{group_number}/role")
    async def set_group_role(project_id: str, group_number: int, update: GroupRoleUpdate) -> dict[str, Any]:
        try:
            return update_group_role(record_or_404(project_id), group_number, update.role)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Unknown dialogue group") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/projects/{project_id}/roles")
    async def get_roles(project_id: str) -> list[dict[str, Any]]:
        return role_summaries(record_or_404(project_id))

    @app.get("/api/projects/{project_id}/roles/{role}/audio/{kind}")
    async def get_role_audio(project_id: str, role: str, kind: str) -> FileResponse:
        try:
            return FileResponse(role_audio(record_or_404(project_id), role, kind))
        except (ValueError, FileNotFoundError) as error:
            raise HTTPException(status_code=404, detail="Role audio is unavailable") from error

    @app.post("/api/projects/{project_id}/roles/{role}/audio/{kind}")
    async def upload_role_audio(project_id: str, role: str, kind: str, audio: UploadFile = File(...)) -> dict[str, Any]:
        try:
            return await attach_role_audio(record_or_404(project_id), role, kind, audio)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/projects/{project_id}/groups/{group_number}/pauses")
    async def get_pauses(project_id: str, group_number: int) -> dict[str, Any]:
        record = record_or_404(project_id)
        try:
            manifest_group(record, group_number)
            return {"group": group_number, "markers": pause_overrides(record).get(str(group_number), [])}
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Unknown dialogue group") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.put("/api/projects/{project_id}/groups/{group_number}/pauses")
    async def put_pauses(project_id: str, group_number: int, update: PauseUpdate) -> dict[str, Any]:
        try:
            return update_pause_overrides(record_or_404(project_id), group_number, update.markers)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Unknown dialogue group") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/projects/{project_id}/translation-review")
    async def get_translation_review(project_id: str) -> dict[str, Any]:
        path = project_paths(record_or_404(project_id))["work"] / "translation_review.csv"
        try:
            headers, rows = load_csv(path)
        except FileNotFoundError as error:
            raise HTTPException(status_code=409, detail="Run build before translation review") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"headers": headers, "rows": rows}

    @app.put("/api/projects/{project_id}/translation-review/{group}")
    async def update_translation_review(project_id: str, group: int, update: TranslationUpdate) -> dict[str, Any]:
        record = record_or_404(project_id)
        path = project_paths(record)["work"] / "translation_review.csv"
        try:
            headers, rows = load_csv(path)
        except FileNotFoundError as error:
            raise HTTPException(status_code=409, detail="Run build before translation review") from error
        if "group" not in headers:
            raise HTTPException(status_code=422, detail="Translation review CSV has no group column")
        target = next((row for row in rows if row.get("group") == str(group)), None)
        if target is None:
            raise HTTPException(status_code=404, detail=f"No translation row for group {group}")
        if update.lip_sync_text is not None:
            target["lip_sync_text"] = update.lip_sync_text
        if update.approved is not None:
            target["approved"] = "yes" if update.approved else ""
        if update.translator_notes is not None:
            if "translator_notes" not in headers:
                headers.append("translator_notes")
                for row in rows:
                    row.setdefault("translator_notes", "")
            target["translator_notes"] = update.translator_notes
        write_csv_atomic(path, headers, rows)
        return {"row": target, "path": str(path)}

    @app.get("/api/projects/{project_id}/media/{asset}")
    async def get_media(project_id: str, asset: str) -> FileResponse:
        try:
            path = safe_asset_path(record_or_404(project_id), asset)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="Registered media asset is unavailable") from error
        # Omit a download filename so the browser can use range requests for
        # inline audio/video playback in the later review interface.
        return FileResponse(path)

    @app.get("/api/projects/{project_id}/waveform")
    async def get_waveform(project_id: str, bins: int = Query(default=2400, ge=128, le=12_000)) -> dict[str, Any]:
        try:
            return waveform_for_project(waveform_cache, record_or_404(project_id), bins)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="Registered dialogue audio is unavailable") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/projects/{project_id}/jobs")
    async def list_jobs(project_id: str, limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
        record_or_404(project_id)
        return projects.jobs_for_project(project_id, limit)

    @app.post("/api/projects/{project_id}/jobs", status_code=202)
    async def submit_job(project_id: str, request: JobRequest) -> dict[str, Any]:
        record = record_or_404(project_id)
        try:
            return await jobs.submit(record, request)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        try:
            return projects.get_job(job_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Unknown job") from error

    @app.get("/api/jobs/{job_id}/log")
    async def get_job_log(job_id: str) -> dict[str, Any]:
        try:
            return {"job_id": job_id, "log": jobs.log_tail(job_id)}
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Unknown job") from error

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict[str, Any]:
        try:
            return await jobs.cancel(job_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Unknown job") from error

    @app.websocket("/api/jobs/{job_id}/events")
    async def job_events(websocket: WebSocket, job_id: str) -> None:
        await websocket.accept()
        try:
            while True:
                try:
                    job = projects.get_job(job_id)
                except KeyError:
                    await websocket.send_json({"error": "Unknown job"})
                    return
                await websocket.send_json({"job": job, "log": jobs.log_tail(job_id, 8_000)})
                if job["state"] in TERMINAL_JOB_STATES:
                    return
                await asyncio.sleep(0.5)
        except WebSocketDisconnect:
            return

    return app
