from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

import local_ui
from local_ui import (
    JobRequest,
    PauseMarker,
    PipelineJobQueue,
    ProjectStore,
    candidate_audio,
    candidate_summaries,
    create_imported_project,
    generate_waveform,
    load_csv,
    project_snapshot,
    project_paths,
    role_summaries,
    update_group_role,
    update_pause_overrides,
    write_csv_atomic,
)
from reusable_pipeline import clean_generation, default_config, render_text_with_natural_pauses, validate_translation_text


class FakeUpload:
    """Small async upload double; Starlette's real UploadFile needs an ASGI portal."""

    def __init__(self, filename: str, data: bytes) -> None:
        self.filename = filename
        self.data = data
        self.offset = 0

    async def read(self, size: int = -1) -> bytes:
        if self.offset >= len(self.data):
            return b""
        end = len(self.data) if size < 0 else self.offset + size
        chunk = self.data[self.offset:end]
        self.offset += len(chunk)
        return chunk


def make_project(root: Path) -> Path:
    project = root / "demo"
    project.mkdir()
    (project / "work").mkdir()
    (project / "output").mkdir()
    (project / "pipeline.json").write_text(
        json.dumps(
            {
                "project": "demo",
                "input": {"audio": "dialogue.wav", "source_srt": "source.srt", "target_srt": "target.srt", "dialogue_script": "script.txt"},
                "layout": {"work_dir": "work", "output_dir": "output"},
                "languages": {"source": {"code": "cs"}, "target": {"code": "en"}},
            }
        ),
        encoding="utf-8",
    )
    (project / "work" / "turn_manifest.json").write_text(
        json.dumps(
            [
                {
                    "group": 1,
                    "role": "PUL",
                    "source_start": 1.0,
                    "source_end": 2.0,
                    "source_text": "Ahoj",
                    "lip_sync_text": "Hello",
                    "translation_state": "approved",
                    "script_indices": [0],
                    "render": {"candidates": [], "selection": None},
                }
            ]
        ),
        encoding="utf-8",
    )
    (project / "script.txt").write_text("PUL: Ahoj\n", encoding="utf-8")
    (project / "work" / "translation_review.csv").write_text(
        "group,role,lip_sync_text,approved,translator_notes\n1,PUL,Hello,yes,\n",
        encoding="utf-8",
    )
    return project


class LocalUiTests(unittest.TestCase):
    def test_import_creates_portable_input_video_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects_root = root / "projects"
            projects_root.mkdir()
            audio = io.BytesIO()
            sf.write(audio, np.zeros(800, dtype=np.float32), 8000, format="WAV")
            previous_root = local_ui.PROJECTS_ROOT
            local_ui.PROJECTS_ROOT = projects_root
            try:
                store = ProjectStore(root / "state", [projects_root])
                record = asyncio.run(
                    create_imported_project(
                        store,
                        display_name="Dropped short",
                        source_language="de",
                        target_language="ja",
                        audio=FakeUpload("source.wav", audio.getvalue()),  # type: ignore[arg-type]
                        source_srt=FakeUpload("source.srt", b"1\n00:00:00,000 --> 00:00:00,100\nAhoj\n"),  # type: ignore[arg-type]
                        target_srt=FakeUpload("target.srt", b"1\n00:00:00,000 --> 00:00:00,100\nHello\n"),  # type: ignore[arg-type]
                        dialogue_script=FakeUpload("dialogue.txt", b"PUL: Ahoj\n"),  # type: ignore[arg-type]
                        video=FakeUpload("picture.mp4", b"not decoded in import"),  # type: ignore[arg-type]
                    )
                )
            finally:
                local_ui.PROJECTS_ROOT = previous_root
            config = json.loads(record.config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["input"]["video"], "inputs/picture.mp4")
            self.assertEqual(config["languages"]["source"]["code"], "de")
            self.assertEqual(config["languages"]["target"]["code"], "ja")
            self.assertTrue((record.config_path.parent / config["input"]["audio"]).is_file())
            self.assertTrue((record.config_path.parent / config["input"]["video"]).is_file())

    def test_pipeline_language_helpers_follow_the_selected_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            config = default_config(
                project,
                "any-pair",
                project / "dialogue.wav",
                project / "source.srt",
                project / "target.srt",
                project / "dialogue.txt",
                "ar",
                "ja",
            )
            self.assertEqual(config["languages"]["source"]["name"], "Arabic")
            self.assertEqual(config["languages"]["target"]["name"], "Japanese")
            self.assertEqual(config["translation"]["accent"], "Neutral Japanese accent")
            self.assertEqual(clean_generation("Japanese: こんにちは", config), "こんにちは")
            self.assertEqual(validate_translation_text("こんにちは", config), [])
            self.assertIn("model label leaked", validate_translation_text("Japanese: こんにちは", config)[0])

    def test_waveform_decimates_pcm_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "tone.wav"
            samples = np.linspace(-0.5, 0.5, 1000, dtype=np.float32)
            sf.write(path, samples, 1000)
            waveform = generate_waveform(path, 128)
            self.assertEqual(waveform["duration"], 1.0)
            self.assertGreaterEqual(waveform["bins"], 64)
            self.assertEqual(len(waveform["min"]), len(waveform["max"]))
            self.assertLess(min(waveform["min"]), -0.45)
            self.assertGreater(max(waveform["max"]), 0.45)

    def test_candidate_assets_and_natural_pause_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = make_project(root)
            render_dir = project / "work" / "renders" / "001_PUL"
            render_dir.mkdir(parents=True)
            candidate_path = render_dir / "candidate_01.wav"
            sf.write(candidate_path, np.zeros(800, dtype=np.float32), 8000)
            manifest_path = project / "work" / "turn_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest[0]["render"] = {
                "candidates": [{"variant": 1, "path": "work/renders/001_PUL/candidate_01.wav", "duration": 0.1}],
                "selection": None,
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            store = ProjectStore(root / "state", [root])
            record = store.register(project)
            self.assertEqual(candidate_summaries(record, 1)["candidates"][0]["variant"], 1)
            self.assertEqual(candidate_audio(record, 1, "candidate-1"), candidate_path)
            summary = update_pause_overrides(record, 1, [PauseMarker(after_word=1, duration_ms=350)])
            rendered, markers = render_text_with_natural_pauses(project / "work", 1, "Hello there")
            self.assertEqual(rendered, "Hello... there")
            self.assertEqual(markers[0]["duration_ms"], 350)
            self.assertEqual(summary["requested_pause_seconds"], 0.35)

    def test_character_correction_updates_script_and_invalidates_voice_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = make_project(root)
            render_dir = project / "work" / "renders" / "001_PUL"
            render_dir.mkdir(parents=True)
            candidate_path = render_dir / "candidate_01.wav"
            sf.write(candidate_path, np.zeros(800, dtype=np.float32), 8000)
            manifest_path = project / "work" / "turn_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest[0]["render"] = {
                "candidates": [{"variant": 1, "path": "work/renders/001_PUL/candidate_01.wav"}],
                "selection": {"candidate": 1, "path": "work/renders/001_PUL/candidate_01.wav"},
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (project / "work" / "candidate_overrides.json").write_text('{"1": 1}\n', encoding="utf-8")
            store = ProjectStore(root / "state", [root])
            record = store.register(project)

            result = update_group_role(record, 1, "tal")

            self.assertTrue(result["changed"])
            self.assertEqual(result["role"], "TAL")
            self.assertEqual(result["affected_groups"], [1])
            self.assertEqual((project / "script.txt").read_text(encoding="utf-8"), "TAL: Ahoj\n")
            updated_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(updated_manifest[0]["role"], "TAL")
            self.assertEqual(updated_manifest[0]["render"]["candidates"], [])
            self.assertIsNone(updated_manifest[0]["render"]["selection"])
            self.assertEqual(load_csv(project / "work" / "translation_review.csv")[1][0]["role"], "TAL")
            self.assertEqual(json.loads((project / "work" / "candidate_overrides.json").read_text(encoding="utf-8")), {})

    def test_role_picker_includes_unassigned_script_cast(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = make_project(root)
            (project / "script.txt").write_text("PUL: Ahoj\nSHE: Neviditelna replika\n", encoding="utf-8")
            store = ProjectStore(root / "state", [root])
            record = store.register(project)

            self.assertEqual([item["role"] for item in role_summaries(record)], ["PUL", "SHE"])

    def test_project_status_and_translation_edit_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = make_project(root)
            store = ProjectStore(root / "state", [root])
            record = store.register(project)
            self.assertEqual(project_snapshot(record)["counts"]["approved_translations"], 1)
            sheet = project_paths(record)["work"] / "translation_review.csv"
            headers, rows = load_csv(sheet)
            rows[0]["lip_sync_text"] = "Hi there"
            rows[0]["approved"] = "yes"
            rows[0]["translator_notes"] = "shorter"
            write_csv_atomic(sheet, headers, rows)
            _, saved_rows = load_csv(sheet)
            self.assertEqual(saved_rows[0]["lip_sync_text"], "Hi there")
            self.assertEqual(saved_rows[0]["approved"], "yes")

    def test_job_command_requires_targeted_render_and_confirmed_assembly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = make_project(root)
            store = ProjectStore(root / "state", [root])
            record = store.register(project)
            queue = PipelineJobQueue(store, root / "state")
            with self.assertRaisesRegex(ValueError, "targeted group"):
                queue._command(record, JobRequest(command="render"))
            with self.assertRaisesRegex(ValueError, "explicit confirmation"):
                queue._command(record, JobRequest(command="assemble"))
