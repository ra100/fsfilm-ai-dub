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
import sqlite3
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field, field_validator


REPO_ROOT = Path(__file__).resolve().parent
PIPELINE_PATH = REPO_ROOT / "reusable_pipeline.py"
DEFAULT_STATE_DIR = REPO_ROOT / ".local-ui"
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
    projects.discover(REPO_ROOT / "projects")
    for project in initial_projects:
        projects.register(project)
    jobs = PipelineJobQueue(projects, state)

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
    app.state.projects = projects
    app.state.jobs = jobs

    def record_or_404(project_id: str) -> ProjectRecord:
        try:
            return projects.get(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Unknown or unavailable project") from error

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> str:
        return """<!doctype html><html><head><meta charset='utf-8'><title>FSFilm AI Dub</title></head>
<body><main><h1>FSFilm AI Dub local UI</h1><p>The backend is running locally.</p>
<p>Use <a href='/api/docs'>the API documentation</a> while the React review interface is built.</p></main></body></html>"""

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

    @app.get("/api/projects/{project_id}")
    async def get_project(project_id: str) -> dict[str, Any]:
        return project_snapshot(record_or_404(project_id))

    @app.get("/api/projects/{project_id}/groups")
    async def get_groups(project_id: str) -> list[dict[str, Any]]:
        return group_summaries(record_or_404(project_id))

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
