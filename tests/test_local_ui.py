from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_ui import JobRequest, PipelineJobQueue, ProjectStore, load_csv, project_snapshot, project_paths, write_csv_atomic


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
                    "render": {"candidates": [], "selection": None},
                }
            ]
        ),
        encoding="utf-8",
    )
    (project / "work" / "translation_review.csv").write_text(
        "group,role,lip_sync_text,approved,translator_notes\n1,PUL,Hello,yes,\n",
        encoding="utf-8",
    )
    return project


class LocalUiTests(unittest.TestCase):
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
