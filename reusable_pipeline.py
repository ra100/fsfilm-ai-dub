#!/usr/bin/env python3
"""Reusable, no-speed dialogue-dubbing pipeline for short-form productions.

The pipeline is deliberately conservative:

* source-language timing and performance drive the dub;
* supplied target subtitles are semantic reference only, never reused as an
  automatic audio fallback;
* all generated output is from one renderer/conditioning route (IndexTTS2
  with source-performance emotion prompts);
* a line that does not fit is flagged for translation revision, not sped up,
  truncated, or stitched to a different model.

It is designed for projects that provide a dialogue stem/final mix, source SRT,
legacy target SRT, and a source-language script whose lines start with
``ROLE: dialogue``.  It supports any source/target language that the selected
models support; word-integrity scoring is strongest for whitespace-separated
target languages.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SCRIPT_ROOT = Path(__file__).resolve().parent
INDEX_ROOT = SCRIPT_ROOT / "third_party/index-tts"
CONFIG_NAME = "pipeline.json"
SRT_TIME_RE = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})\s+-->\s+"
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})$"
)
ROLE_RE = re.compile(r"^([^:]{1,32}):\s*(.+)$")
APPROVED = {"yes", "y", "true", "approved", "ok"}
CJK_CODES = {"zh", "ja", "ko"}
LANGUAGE_NAMES = {
    "cs": "Czech", "sk": "Slovak", "pl": "Polish", "uk": "Ukrainian",
    "de": "German", "fr": "French", "es": "Spanish", "it": "Italian",
    "pt": "Portuguese", "en": "English", "ja": "Japanese", "ko": "Korean",
    "zh": "Chinese",
}


@dataclass(frozen=True)
class Cue:
    number: int
    start: float
    end: float
    text: str


def die(message: str) -> None:
    raise SystemExit(f"error: {message}")


def run(command: list[str]) -> None:
    print("+", " ".join(str(item) for item in command), flush=True)
    subprocess.run(command, check=True)


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load(path: Path) -> Any:
    if not path.exists():
        die(f"missing {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def read_srt(path: Path) -> list[Cue]:
    if not path.exists():
        die(f"missing subtitle file: {path}")
    raw = path.read_text(encoding="utf-8-sig").replace("\r", "").strip()
    if not raw:
        die(f"subtitle file is empty: {path}")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", raw):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if len(lines) < 3:
            die(f"malformed SRT block in {path}: {block!r}")
        try:
            number = int(lines[0])
        except ValueError:
            die(f"invalid SRT cue number in {path}: {lines[0]!r}")
        match = SRT_TIME_RE.match(lines[1])
        if not match:
            die(f"invalid SRT timing in {path}: {lines[1]!r}")
        values = [int(value) for value in match.groups()]
        start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000
        end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000
        if end <= start:
            die(f"non-positive SRT cue {number} in {path}")
        cues.append(Cue(number, start, end, " ".join(lines[2:]).strip()))
    return cues


def stamp(value: float) -> str:
    milliseconds = round(max(0.0, value) * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1_000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def write_srt(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    blocks = []
    for index, row in enumerate(rows, 1):
        blocks.append(
            f"{index}\n{stamp(float(row['start']))} --> {stamp(float(row['end']))}\n{row['text']}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def audio_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def config_path(value: str | Path) -> Path:
    path = Path(value)
    return path / CONFIG_NAME if path.is_dir() else path


def read_config(value: str | Path) -> tuple[Path, Path, dict[str, Any]]:
    path = config_path(value).resolve()
    config = load(path)
    if int(config.get("schema_version", 0)) != 1:
        die(f"{path} is not a schema_version 1 dubbing pipeline config")
    return path, path.parent, config


def resolve_project_path(project: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (project / path).resolve()


def rel_to_project(project: Path, value: Path) -> str:
    return os.path.relpath(value.resolve(), project.resolve())


def work_dir(project: Path, config: dict[str, Any]) -> Path:
    return project / config.get("layout", {}).get("work_dir", "work")


def output_dir(project: Path, config: dict[str, Any]) -> Path:
    return project / config.get("layout", {}).get("output_dir", "output")


def manifest_path(project: Path, config: dict[str, Any]) -> Path:
    return work_dir(project, config) / "turn_manifest.json"


def target_name(config: dict[str, Any]) -> str:
    return str(config["languages"]["target"].get("code", "target")).lower()


def source_name(config: dict[str, Any]) -> str:
    return str(config["languages"]["source"].get("code", "source")).lower()


def project_audio(project: Path, config: dict[str, Any]) -> Path:
    return resolve_project_path(project, config["input"]["audio"])


def source_srt(project: Path, config: dict[str, Any]) -> Path:
    return resolve_project_path(project, config["input"]["source_srt"])


def target_srt(project: Path, config: dict[str, Any]) -> Path:
    return resolve_project_path(project, config["input"]["target_srt"])


def dialogue_script(project: Path, config: dict[str, Any]) -> Path:
    return resolve_project_path(project, config["input"]["dialogue_script"])


def normalize(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", ascii_text.casefold()).strip()


def score_text(subtitle: str, script: str) -> float:
    left, right = normalize(subtitle), normalize(script)
    if not left or not right:
        return 0.0
    if left in right:
        return 1.0
    left_words = {word for word in left.split() if len(word) > 1}
    right_words = {word for word in right.split() if len(word) > 1}
    overlap = len(left_words & right_words) / max(1, len(left_words))
    return 0.72 * overlap + 0.28 * difflib.SequenceMatcher(None, left, right).ratio()


def script_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        die(f"missing dialogue script: {path}")
    rows = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not raw.strip():
            continue
        match = ROLE_RE.match(raw.strip())
        if not match:
            die(f"dialogue script line {line_no} must be 'ROLE: dialogue'")
        rows.append({"line": line_no, "role": match.group(1).strip(), "text": match.group(2).strip()})
    if not rows:
        die(f"no role-labelled dialogue lines in {path}")
    return rows


def assign_script_rows(source: list[Cue], script: list[dict[str, Any]], lookahead: int) -> list[dict[str, Any]]:
    """Assign source subtitle fragments to source-language script rows in order."""
    assigned = []
    cursor = 0
    for cue in source:
        candidates = [
            (index, score_text(cue.text, script[index]["text"]))
            for index in range(cursor, min(len(script), cursor + lookahead))
        ]
        if not candidates:
            die(f"script ran out while aligning source cue {cue.number}")
        best_index, best_score = max(candidates, key=lambda item: item[1])
        current_score = candidates[0][1]
        # Source SRT normally splits one script line into several fragments. Do
        # not advance to a later actor unless the evidence is clearly stronger.
        if best_index > cursor and best_score < current_score + 0.10:
            best_index, best_score = cursor, current_score
        cursor = best_index
        assigned.append(
            {
                "cue": cue.number,
                "start": cue.start,
                "end": cue.end,
                "text": cue.text,
                "script_index": best_index,
                "script_line": script[best_index]["line"],
                "role": script[best_index]["role"],
                "role_confidence": round(best_score, 3),
            }
        )
    return assigned


def map_target_to_source(source: list[Cue], target: list[Cue]) -> dict[int, list[Cue]]:
    """Map each legacy target subtitle to its source cue by timeline overlap."""
    mapped: dict[int, list[Cue]] = {cue.number: [] for cue in source}
    for target_cue in target:
        target_mid = (target_cue.start + target_cue.end) / 2
        ranked = []
        for source_cue in source:
            overlap = max(0.0, min(target_cue.end, source_cue.end) - max(target_cue.start, source_cue.start))
            source_mid = (source_cue.start + source_cue.end) / 2
            ranked.append((overlap, -abs(target_mid - source_mid), source_cue.number))
        _, _, cue_number = max(ranked)
        mapped[cue_number].append(target_cue)
    for values in mapped.values():
        values.sort(key=lambda item: (item.start, item.number))
    return mapped


def default_config(project: Path, name: str, audio: Path, source: Path, target: Path, script: Path, source_code: str, target_code: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project": name,
        "input": {
            "audio": rel_to_project(project, audio),
            "source_srt": rel_to_project(project, source),
            "target_srt": rel_to_project(project, target),
            "dialogue_script": rel_to_project(project, script),
        },
        "languages": {
            "source": {"code": source_code, "name": LANGUAGE_NAMES.get(source_code.casefold(), source_code)},
            "target": {"code": target_code, "name": LANGUAGE_NAMES.get(target_code.casefold(), target_code), "asr_language": target_code},
        },
        "layout": {"work_dir": "work", "output_dir": "output"},
        "grouping": {"script_lookahead": 10, "max_gap_seconds": 6.0, "max_turn_seconds": 30.0},
        "translation": {
            "words_per_second": 2.35,
            "accent": "Neutral General American English",
            "require_human_approval": True,
        },
        "render": {
            "engine": "indextts2_source_emotion",
            "variants": 2,
            "seed": 3200,
            "emotion_alpha": 0.90,
            "temperature": 0.80,
            "minimum_gap_seconds": 0.08,
        },
        "quality": {
            "minimum_role_confidence": 0.55,
            "minimum_word_recall": 0.82,
            "maximum_overrun_seconds": 0.35,
            "maximum_overlap_seconds": 0.05,
            "minimum_reference_active_db": -42.0,
        },
        "roles": {},
    }


def init(args: argparse.Namespace) -> None:
    project = Path(args.project).resolve()
    config_file = project / CONFIG_NAME
    if config_file.exists() and not args.force:
        die(f"{config_file} already exists (use --force to replace only the config)")
    inputs = [Path(args.audio).resolve(), Path(args.source_srt).resolve(), Path(args.target_srt).resolve(), Path(args.dialogue_script).resolve()]
    missing = [str(path) for path in inputs if not path.exists()]
    if missing:
        die("missing input(s): " + ", ".join(missing))
    project.mkdir(parents=True, exist_ok=True)
    for folder in ("work", "output", "notes"):
        (project / folder).mkdir(exist_ok=True)
    config = default_config(
        project, args.name or project.name, inputs[0], inputs[1], inputs[2], inputs[3], args.source_language, args.target_language
    )
    save(config_file, config)
    (project / "notes" / "README.txt").write_text(
        "Keep source assets outside this folder if desired.\n"
        "Edit pipeline.json only for project-wide choices; put reviewable changes in work/.\n",
        encoding="utf-8",
    )
    print(f"Created reusable dubbing project: {config_file}")


def preflight(args: argparse.Namespace) -> None:
    config_file, project, config = read_config(args.config)
    audio, source_path, target_path, script_path = project_audio(project, config), source_srt(project, config), target_srt(project, config), dialogue_script(project, config)
    missing = [str(path) for path in (audio, source_path, target_path, script_path) if not path.exists()]
    if missing:
        die("missing input(s): " + ", ".join(missing))
    source, target, script = read_srt(source_path), read_srt(target_path), script_rows(script_path)
    duration = audio_duration(audio)
    warnings = []
    if source[-1].end > duration + 0.25:
        warnings.append("source SRT ends after the audio track")
    if target[-1].end > duration + 0.25:
        warnings.append("target SRT ends after the audio track")
    report = {
        "config": str(config_file),
        "audio": str(audio),
        "audio_duration": round(duration, 3),
        "source_cues": len(source),
        "target_cues": len(target),
        "script_rows": len(script),
        "roles": sorted({row["role"] for row in script}),
        "warnings": warnings,
    }
    save(work_dir(project, config) / "preflight.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def build(args: argparse.Namespace) -> None:
    _, project, config = read_config(args.config)
    source = read_srt(source_srt(project, config))
    # Legacy target files often include title cards, credits, or a later cut.
    # Only dialogue that shares the source-dialogue timeline can seed a turn.
    all_target = read_srt(target_srt(project, config))
    target = [
        cue for cue in all_target
        if cue.end >= source[0].start - 0.5 and cue.start <= source[-1].end + 0.5
    ]
    if not target:
        die("no target subtitles overlap the source dialogue timeline")
    script = script_rows(dialogue_script(project, config))
    grouping = config.get("grouping", {})
    assigned = assign_script_rows(source, script, int(grouping.get("script_lookahead", 10)))
    target_map = map_target_to_source(source, target)
    target_by_number = {cue.number: cue for cue in target}
    max_gap = float(grouping.get("max_gap_seconds", 6.0))
    max_turn = float(grouping.get("max_turn_seconds", 30.0))
    groups: list[dict[str, Any]] = []
    for row in assigned:
        key = (row["role"], row["script_index"])
        new_turn = (
            not groups
            or groups[-1]["key"] != list(key)
            or row["start"] - float(groups[-1]["source_end"]) > max_gap
            or row["end"] - float(groups[-1]["source_start"]) > max_turn
        )
        if new_turn:
            groups.append(
                {
                    "group": len(groups) + 1,
                    "key": list(key),
                    "role": row["role"],
                    "script_line": row["script_line"],
                    "source_cues": [],
                    "source_start": round(row["start"], 3),
                    "source_end": round(row["end"], 3),
                    "source_text_parts": [],
                    "target_cues": [],
                    "role_confidence": [],
                    "timing_origin": "source_srt",
                }
            )
        group = groups[-1]
        group["source_end"] = round(row["end"], 3)
        group["source_cues"].append(row["cue"])
        group["source_text_parts"].append(row["text"])
        group["target_cues"].extend(item.number for item in target_map[row["cue"]])
        group["role_confidence"].append(row["role_confidence"])

    # A source subtitle track can omit an off-screen/radio line that still has
    # target-language captions. Detect an isolated skipped script row and use
    # its target-SRT gap as timing. This keeps an off-screen/radio transmission
    # as its own speaker turn instead of absorbing it into the next actor.
    used_script_indices = {int(group["key"][1]) for group in groups}
    missing_indices = [index for index in range(len(script)) if index not in used_script_indices]
    coverage_review: list[dict[str, Any]] = []
    inserted: list[dict[str, Any]] = []
    for missing_index in missing_indices:
        previous = [group for group in groups if int(group["key"][1]) < missing_index]
        following = [group for group in groups if int(group["key"][1]) > missing_index]
        previous_group = max(previous, key=lambda group: int(group["key"][1])) if previous else None
        following_group = min(following, key=lambda group: int(group["key"][1])) if following else None
        skipped_between = [
            index for index in missing_indices
            if (previous_group is None or int(previous_group["key"][1]) < index)
            and (following_group is None or index < int(following_group["key"][1]))
        ]
        if not previous_group or not following_group or len(skipped_between) != 1:
            coverage_review.append({
                "script_line": script[missing_index]["line"], "role": script[missing_index]["role"],
                "script_text": script[missing_index]["text"], "issue": "source script row has no isolated subtitle gap; add a manual timing override",
            })
            continue
        lower, upper = float(previous_group["source_end"]), float(following_group["source_start"])
        target_gap = [
            cue for cue in target
            if cue.start >= lower - 0.15 and cue.end <= upper + 0.15
        ]
        if not target_gap:
            coverage_review.append({
                "script_line": script[missing_index]["line"], "role": script[missing_index]["role"],
                "script_text": script[missing_index]["text"], "issue": "no target subtitle found in source subtitle gap",
            })
            continue
        target_numbers = {cue.number for cue in target_gap}
        for group in groups:
            group["target_cues"] = [number for number in group["target_cues"] if number not in target_numbers]
        inserted.append(
            {
                "group": 0,
                "key": [script[missing_index]["role"], missing_index],
                "role": script[missing_index]["role"],
                "script_line": script[missing_index]["line"],
                "source_cues": [],
                "source_start": round(min(cue.start for cue in target_gap), 3),
                "source_end": round(max(cue.end for cue in target_gap), 3),
                "source_text_parts": [script[missing_index]["text"]],
                "target_cues": sorted(target_numbers, key=lambda number: (target_by_number[number].start, number)),
                "role_confidence": [1.0],
                "timing_origin": "target_srt_for_missing_source",
            }
        )
    groups.extend(inserted)
    groups.sort(key=lambda group: (float(group["source_start"]), int(group["key"][1])))
    for number, group in enumerate(groups, 1):
        group["group"] = number
    wps = float(config.get("translation", {}).get("words_per_second", 2.35))
    for group in groups:
        group["target_cues"] = sorted(set(group["target_cues"]), key=lambda number: (target_by_number[number].start, number))
        source_text = " ".join(group.pop("source_text_parts"))
        legacy_text = " ".join(target_by_number[number].text for number in group["target_cues"]).strip()
        if not legacy_text:
            # A missing target overlap should be explicit rather than silently
            # creating a source-language synthesis request.
            legacy_text = ""
        span = float(group["source_end"]) - float(group["source_start"])
        confidence = min(group.pop("role_confidence"))
        group.update(
            {
                "source_span": round(span, 3),
                "source_text": source_text,
                "legacy_target_text": legacy_text,
                "lip_sync_text": legacy_text,
                "target_word_budget": max(1, int(math.floor(max(0.3, span) * wps))),
                "role_confidence": round(confidence, 3),
                "translation_state": "legacy_unreviewed",
                "timing_state": "source_srt_unreviewed" if group["timing_origin"] == "source_srt" else "target_srt_for_missing_source_review",
                "render": {"candidates": [], "selection": None},
            }
        )
    work = work_dir(project, config)
    save(manifest_path(project, config), groups)
    review_rows = [
        group for group in groups
        if group["role_confidence"] < float(config.get("quality", {}).get("minimum_role_confidence", 0.55))
        or not group["source_cues"]
    ]
    write_csv(
        work / "role_review.csv",
        review_rows,
        ["group", "role", "script_line", "source_cues", "source_text", "role_confidence"],
    )
    write_csv(work / "script_coverage_review.csv", coverage_review, ["script_line", "role", "script_text", "issue"])
    write_translation_sheet(project, config, groups)
    save(work / "timing_overrides.json", load(work / "timing_overrides.json") if (work / "timing_overrides.json").exists() else {})
    save(work / "glossary.json", load(work / "glossary.json") if (work / "glossary.json").exists() else {"required_terms": []})
    print(f"Built {len(groups)} speaker turns from {len(source)} source cues; {len(review_rows)} turn(s) need role review and {len(coverage_review)} script row(s) need timing coverage review.")


def write_csv(path: Path, rows: list[dict[str, Any]], headers: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            flattened = dict(row)
            for key, value in list(flattened.items()):
                if isinstance(value, (list, dict)):
                    flattened[key] = json.dumps(value, ensure_ascii=False)
            writer.writerow(flattened)


def write_translation_sheet(project: Path, config: dict[str, Any], groups: list[dict[str, Any]]) -> None:
    rows = []
    for group in groups:
        rows.append(
            {
                "group": group["group"],
                "role": group["role"],
                "source_start": group["source_start"],
                "source_end": group["source_end"],
                "target_word_budget": group["target_word_budget"],
                "source_text": group["source_text"],
                "legacy_target_text": group["legacy_target_text"],
                "candidate_lipsync_text": "",
                "lip_sync_text": group["lip_sync_text"],
                "approved": "",
                "translator_notes": "",
            }
        )
    write_csv(
        work_dir(project, config) / "translation_review.csv", rows,
        ["group", "role", "source_start", "source_end", "target_word_budget", "source_text", "legacy_target_text", "candidate_lipsync_text", "lip_sync_text", "approved", "translator_notes"],
    )


def draft_lipsync(args: argparse.Namespace) -> None:
    """Use a local instruction model to create review-only concise translations."""
    _, project, config = read_config(args.config)
    work = work_dir(project, config)
    sheet_path = Path(args.sheet) if args.sheet else work / "translation_review.csv"
    if not sheet_path.exists():
        die(f"missing translation sheet: {sheet_path}; run build first")
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        die(f"draft-lipsync needs torch and transformers: {exc}")
    if not torch.cuda.is_available():
        die("draft-lipsync requires CUDA")
    glossary = load(work / "glossary.json") if (work / "glossary.json").exists() else {"required_terms": []}
    terms = glossary.get("required_terms", [])
    rows = list(csv.DictReader(sheet_path.open(encoding="utf-8")))
    print(f"Loading {args.model} …", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    model = AutoModelForCausalLM.from_pretrained(args.model, torch_dtype=torch.bfloat16, device_map="auto")
    source_language = config["languages"]["source"].get("name", source_name(config))
    target_language = config["languages"]["target"].get("name", target_name(config))
    for row in rows:
        if args.groups and int(row["group"]) not in parse_groups(args.groups):
            continue
        term_notes = []
        for term in terms:
            if isinstance(term, dict) and str(term.get("source", "")).casefold() in row["source_text"].casefold():
                term_notes.append(f"Use this exact target phrase: {term.get('target', '')}")
        prompt = (
            f"Adapt one {source_language} film-dialogue turn into natural {target_language}. "
            "The legacy target subtitle is semantically useful but is not lip-synced. "
            "Keep plot-critical meaning, names, social register, and emotion. Do not add words. "
            "Use concise wording that can be spoken naturally in the available window; never suggest speeding up. "
            f"Output only the performable {target_language} dialogue, with no more than {row['target_word_budget']} words.\n"
            f"Role: {row['role']}\n"
            f"Source: {row['source_text']}\n"
            f"Legacy target: {row['legacy_target_text']}\n"
            + ("\n".join(term_notes) + "\n" if term_notes else "")
        )
        messages = [
            {"role": "system", "content": "You write concise, faithful, performable dubbed dialogue."},
            {"role": "user", "content": prompt},
        ]
        encoded = tokenizer.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt").to(model.device)
        with torch.inference_mode():
            generated = model.generate(
                encoded,
                max_new_tokens=max(24, min(128, int(row["target_word_budget"]) * 7)),
                do_sample=False,
                pad_token_id=tokenizer.eos_token_id,
            )
        text = clean_generation(tokenizer.decode(generated[0][encoded.shape[1]:], skip_special_tokens=True))
        row["candidate_lipsync_text"] = text
    with sheet_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader(); writer.writerows(rows)
    print(f"Wrote review-only drafts to {sheet_path}. Listen/read them, copy accepted text into lip_sync_text, then mark approved=yes.")


def clean_generation(text: str) -> str:
    text = text.strip().strip('"“”')
    text = re.sub(r"^(translation|english|line|target):\s*", "", text, flags=re.I)
    return " ".join(text.replace("\n", " ").split())


def parse_groups(value: str) -> set[int]:
    try:
        return {int(item.strip()) for item in value.split(",") if item.strip()}
    except ValueError:
        die(f"invalid group list: {value!r}")


def tokenise(text: str, target_code: str) -> list[str]:
    if target_code.casefold().split("-")[0] in CJK_CODES:
        return [character for character in text.casefold() if character.isalnum()]
    return re.findall(r"[\w]+(?:['’\-][\w]+)?", text.casefold(), flags=re.UNICODE)


def text_recall(expected: str, heard: str, target_code: str) -> tuple[float, bool]:
    expected_words, heard_words = tokenise(expected, target_code), tokenise(heard, target_code)
    matched = sum(block.size for block in difflib.SequenceMatcher(None, expected_words, heard_words).get_matching_blocks())
    recall = matched / max(1, len(expected_words))
    tail = expected_words[-min(2, len(expected_words)):]
    ending = all(
        any(difflib.SequenceMatcher(None, word, item).ratio() >= 0.67 for item in heard_words[-8:])
        for word in tail
    )
    return recall, ending


def validate_translation_text(text: str, config: dict[str, Any]) -> list[str]:
    issues = []
    if not text.strip():
        return ["empty translation"]
    target = target_name(config)
    if target == "en" and re.search(r"[^\x00-\x7F]", text):
        issues.append("non-ASCII characters in English text")
    if re.search(r"(?:translation|english|output)\s*:", text, flags=re.I):
        issues.append("model label leaked into dialogue")
    return issues


def apply_translations(args: argparse.Namespace) -> None:
    _, project, config = read_config(args.config)
    work = work_dir(project, config)
    sheet_path = Path(args.sheet) if args.sheet else work / "translation_review.csv"
    if not sheet_path.exists():
        die(f"missing translation sheet: {sheet_path}")
    rows = {int(row["group"]): row for row in csv.DictReader(sheet_path.open(encoding="utf-8"))}
    groups = load(manifest_path(project, config))
    issues = []
    require_approval = bool(config.get("translation", {}).get("require_human_approval", True)) and not args.accept_unapproved
    for group in groups:
        row = rows.get(int(group["group"]))
        if row is None:
            issues.append({"group": group["group"], "issue": "missing row in translation sheet"})
            continue
        text = clean_generation(row.get("lip_sync_text", ""))
        for issue in validate_translation_text(text, config):
            issues.append({"group": group["group"], "issue": issue})
        if require_approval and row.get("approved", "").strip().casefold() not in APPROVED:
            issues.append({"group": group["group"], "issue": "not marked approved"})
        group["lip_sync_text"] = text
        group["translation_state"] = "approved" if not require_approval or row.get("approved", "").strip().casefold() in APPROVED else "unapproved"
    write_csv(work / "translation_apply_issues.csv", issues, ["group", "issue"])
    if issues:
        die(f"translation sheet has {len(issues)} issue(s); see {work / 'translation_apply_issues.csv'}")
    save(manifest_path(project, config), groups)
    validate_translations_for_project(project, config, groups)
    print(f"Applied {len(groups)} approved lip-sync translations.")


def validate_translations_for_project(project: Path, config: dict[str, Any], groups: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    work = work_dir(project, config)
    groups = groups if groups is not None else load(manifest_path(project, config))
    glossary = load(work / "glossary.json") if (work / "glossary.json").exists() else {"required_terms": []}
    required_terms = glossary.get("required_terms", [])
    rows = []
    for group in groups:
        problems = validate_translation_text(group.get("lip_sync_text", ""), config)
        actual_words = len(tokenise(group.get("lip_sync_text", ""), target_name(config)))
        if actual_words > int(group["target_word_budget"]):
            problems.append("over word-budget guide")
        for item in required_terms:
            if not isinstance(item, dict):
                continue
            source_term, target_term = str(item.get("source", "")), str(item.get("target", ""))
            if source_term and source_term.casefold() in group["source_text"].casefold() and target_term.casefold() not in group["lip_sync_text"].casefold():
                problems.append(f"missing required term: {target_term}")
        rows.append({
            "group": group["group"], "role": group["role"], "word_budget": group["target_word_budget"],
            "word_count": actual_words, "translation_state": group["translation_state"],
            "issues": "; ".join(problems), "text": group.get("lip_sync_text", ""),
        })
    write_csv(work / "translation_qc.csv", rows, ["group", "role", "word_budget", "word_count", "translation_state", "issues", "text"])
    return rows


def validate_translations(args: argparse.Namespace) -> None:
    _, project, config = read_config(args.config)
    rows = validate_translations_for_project(project, config)
    problematic = [row for row in rows if row["issues"] or row["translation_state"] != "approved"]
    print(f"Translation QC: {len(rows) - len(problematic)}/{len(rows)} clean; report: {work_dir(project, config) / 'translation_qc.csv'}")
    if args.strict and problematic:
        raise SystemExit(2)


def rms_activity(path: Path) -> dict[str, float]:
    import librosa
    import numpy as np

    audio, sample_rate = librosa.load(path, sr=16000, mono=True)
    if not len(audio):
        return {"duration": 0.0, "active_ratio": 0.0, "active_db": -100.0}
    rms = librosa.feature.rms(y=audio, frame_length=1024, hop_length=256)[0]
    db = librosa.amplitude_to_db(np.maximum(rms, 1e-7), ref=1.0)
    threshold = max(-55.0, float(np.percentile(db, 15)) + 12.0, float(np.max(db)) - 35.0)
    active = db[db >= threshold]
    return {
        "duration": round(len(audio) / sample_rate, 3),
        "active_ratio": round(float(len(active)) / max(1, len(db)), 3),
        "active_db": round(float(np.median(active if len(active) else db)), 2),
    }


def refine_timing(args: argparse.Namespace) -> None:
    """Propose (never automatically apply) source-audio boundaries for each turn."""
    _, project, config = read_config(args.config)
    import librosa
    import numpy as np

    audio_path = project_audio(project, config)
    groups = load(manifest_path(project, config))
    audio, sample_rate = librosa.load(audio_path, sr=16000, mono=True)
    rms = librosa.feature.rms(y=audio, frame_length=1024, hop_length=160)[0]
    times = librosa.frames_to_time(range(len(rms)), sr=sample_rate, hop_length=160, n_fft=1024)
    db = librosa.amplitude_to_db(np.maximum(rms, 1e-7), ref=1.0)
    global_floor = float(np.percentile(db, 15))
    search = float(args.search_seconds)
    rows = []
    for group in groups:
        start, end = float(group["source_start"]), float(group["source_end"])
        mask = (times >= max(0.0, start - search)) & (times <= min(len(audio) / sample_rate, end + search))
        window_db, window_times = db[mask], times[mask]
        if not len(window_db):
            proposed_start, proposed_end, confidence = start, end, 0.0
        else:
            threshold = max(-48.0, global_floor + 12.0, float(np.max(window_db)) - 32.0)
            active_times = window_times[window_db >= threshold]
            if not len(active_times):
                proposed_start, proposed_end, confidence = start, end, 0.0
            else:
                proposed_start = max(0.0, float(active_times[0]) - 0.03)
                proposed_end = min(len(audio) / sample_rate, float(active_times[-1]) + 0.06)
                confidence = min(1.0, len(active_times) / max(1, len(window_times)) * 2.0)
        rows.append({
            "group": group["group"], "role": group["role"], "source_start": round(start, 3), "source_end": round(end, 3),
            "proposed_start": round(proposed_start, 3), "proposed_end": round(proposed_end, 3),
            "start_shift": round(proposed_start - start, 3), "end_shift": round(proposed_end - end, 3),
            "confidence": round(confidence, 3), "review": "listen before applying", "text": group["source_text"],
        })
    path = work_dir(project, config) / "timing_review.csv"
    write_csv(path, rows, ["group", "role", "source_start", "source_end", "proposed_start", "proposed_end", "start_shift", "end_shift", "confidence", "review", "text"])
    print(f"Wrote timing proposals to {path}. Copy only listened/approved values into timing_overrides.json, then run apply-timing.")


def apply_timing(args: argparse.Namespace) -> None:
    _, project, config = read_config(args.config)
    work = work_dir(project, config)
    overrides_path = Path(args.overrides) if args.overrides else work / "timing_overrides.json"
    overrides = load(overrides_path)
    groups = load(manifest_path(project, config))
    words_per_second = float(config.get("translation", {}).get("words_per_second", 2.35))
    for index, group in enumerate(groups):
        value = overrides.get(str(group["group"]))
        if not value:
            continue
        start, end = float(value["start"]), float(value["end"])
        if start < 0 or end <= start:
            die(f"invalid timing override for group {group['group']}")
        if index and start < float(groups[index - 1]["source_start"]):
            die(f"timing override for group {group['group']} breaks chronological order")
        span = end - start
        group["source_start"], group["source_end"] = round(start, 3), round(end, 3)
        group["source_span"], group["timing_state"] = round(span, 3), "human_approved"
        group["target_word_budget"] = max(1, int(math.floor(max(0.3, span) * words_per_second)))
        # A timing edit changes the performance budget. Preserve the wording
        # but force a visible re-approval before it can be rendered.
        group["translation_state"] = "timing_changed_reapproval_required"
    save(manifest_path(project, config), groups)
    write_translation_sheet(project, config, groups)
    print("Applied timing overrides. Re-check translation budgets because every changed span needs re-approval.")


def concatenate_reference(inputs: list[Path], output: Path) -> None:
    concat = output.with_suffix(".concat.txt")
    concat.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for path in inputs:
        escaped = str(path.resolve()).replace("'", r"'\\''")
        lines.append(f"file '{escaped}'\n")
    concat.write_text("".join(lines), encoding="utf-8")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat), "-ac", "1", "-ar", "48000", "-c:a", "pcm_s24le", str(output)])


def make_references(args: argparse.Namespace) -> None:
    """Extract reviewable source clips and create one checked reference per role."""
    _, project, config = read_config(args.config)
    work = work_dir(project, config)
    groups = load(manifest_path(project, config))
    audio = project_audio(project, config)
    candidates_dir = work / "reference_candidates"
    min_db = float(config.get("quality", {}).get("minimum_reference_active_db", -42.0))
    candidates: list[dict[str, Any]] = []
    for group in groups:
        path = candidates_dir / group["role"] / f"{int(group['group']):03d}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or args.force:
            run([
                "ffmpeg", "-y", "-v", "error", "-ss", f"{float(group['source_start']):.3f}",
                "-t", f"{float(group['source_span']):.3f}", "-i", str(audio),
                "-ac", "1", "-ar", "48000", "-c:a", "pcm_s24le", str(path),
            ])
        stats = rms_activity(path)
        candidates.append({
            "group": group["group"], "role": group["role"], "path": rel_to_project(project, path),
            **stats, "usable": stats["active_db"] >= min_db, "text": group["source_text"],
        })
    write_csv(work / "reference_candidates.csv", candidates, ["group", "role", "path", "duration", "active_ratio", "active_db", "usable", "text"])
    selection_path = work / "reference_selection.json"
    selected = load(selection_path) if selection_path.exists() else {}
    roles = sorted({group["role"] for group in groups})
    if not selected:
        for role in roles:
            best = sorted(
                [row for row in candidates if row["role"] == role and row["usable"]],
                key=lambda row: (float(row["active_ratio"]) * float(row["duration"]), float(row["duration"])), reverse=True,
            )[: args.clips_per_role]
            selected[role] = {"groups": [row["group"] for row in best], "review": "listen before use"}
        save(selection_path, selected)
        print(f"Wrote reference candidates and initial selections. Listen/edit {selection_path}, then rerun make-references.")
        return
    references = {}
    by_group = {int(row["group"]): row for row in candidates}
    for role in roles:
        role_cfg = config.get("roles", {}).get(role, {})
        configured_audio = role_cfg.get("reference_audio")
        chosen = selected.get(role, {})
        if configured_audio:
            inputs = [resolve_project_path(project, configured_audio)]
            origin = "configured dedicated reference"
        elif chosen.get("audio"):
            inputs = [resolve_project_path(project, chosen["audio"])]
            origin = "manual dedicated reference"
        else:
            requested = chosen.get("groups", [])
            unknown = [number for number in requested if int(number) not in by_group]
            if unknown:
                die(f"reference selection for {role} has unknown group(s): {unknown}")
            inputs = [resolve_project_path(project, by_group[int(number)]["path"]) for number in requested]
            if not inputs:
                die(f"no usable reference for role {role}; add roles.{role}.reference_audio to pipeline.json")
            origin = "source clips; human review required"
        missing = [str(path) for path in inputs if not path.exists()]
        if missing:
            die(f"reference input(s) missing for {role}: {', '.join(missing)}")
        output = work / "references" / f"{role}.wav"
        concatenate_reference(inputs, output)
        references[role] = {
            "path": rel_to_project(project, output),
            "source": origin,
            "inputs": [rel_to_project(project, path) for path in inputs],
        }
    save(work / "reference_map.json", references)
    print(f"Created {len(references)} actor references in {work / 'references'}.")


def verify_gpu() -> None:
    probe = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.free", "--format=csv,noheader"], capture_output=True, text=True)
    if probe.returncode:
        die(probe.stderr.strip() or "NVIDIA GPU is unavailable")
    print("GPU:", probe.stdout.strip(), flush=True)


def render(args: argparse.Namespace) -> None:
    """Render one fresh, source-conditioned take per complete speaker turn."""
    _, project, config = read_config(args.config)
    verify_gpu()
    try:
        import numpy as np
        import torch
        sys.path.insert(0, str(INDEX_ROOT))
        from indextts.infer_v2 import IndexTTS2
    except ImportError as exc:
        die(f"render requires the IndexTTS2 environment: {exc}")
    groups = load(manifest_path(project, config))
    bad = [str(group["group"]) for group in groups if group.get("translation_state") != "approved"]
    if bad:
        die("translations are not approved for group(s): " + ", ".join(bad))
    work = work_dir(project, config)
    references = load(work / "reference_map.json")
    requested = parse_groups(args.groups) if args.groups else None
    audio = project_audio(project, config)
    render_cfg = config.get("render", {})
    variants = int(args.variants or render_cfg.get("variants", 2))
    seed_base = int(render_cfg.get("seed", 3200))
    tts = IndexTTS2(
        cfg_path=str(INDEX_ROOT / "checkpoints/config.yaml"), model_dir=str(INDEX_ROOT / "checkpoints"),
        use_fp16=True, use_cuda_kernel=False, use_deepspeed=False,
    )
    for group in groups:
        if requested is not None and int(group["group"]) not in requested:
            continue
        role = group["role"]
        if role not in references:
            die(f"missing actor reference for role {role}; run make-references")
        group_dir = work / "renders" / f"{int(group['group']):03d}_{role}"
        group_dir.mkdir(parents=True, exist_ok=True)
        role_cfg = config.get("roles", {}).get(role, {})
        emotion_source = role_cfg.get("emotion_audio")
        emotion = group_dir / "source_emotion.wav"
        if emotion_source:
            source = resolve_project_path(project, emotion_source)
            run(["ffmpeg", "-y", "-v", "error", "-i", str(source), "-ac", "1", "-ar", "22050", str(emotion)])
        elif not emotion.exists() or args.force:
            run([
                "ffmpeg", "-y", "-v", "error", "-ss", f"{float(group['source_start']):.3f}",
                "-t", f"{float(group['source_span']):.3f}", "-i", str(audio),
                "-ac", "1", "-ar", "22050", str(emotion),
            ])
        speaker = resolve_project_path(project, references[role]["path"])
        candidates = []
        for variant in range(1, variants + 1):
            output = group_dir / f"candidate_{variant:02d}.wav"
            seed = seed_base + int(group["group"]) * 100 + variant
            if not output.exists() or args.force:
                random.seed(seed); np.random.seed(seed); torch.manual_seed(seed); torch.cuda.manual_seed_all(seed)
                print(f"A-lipsync {int(group['group']):03d} {role} variant {variant}/{variants}", flush=True)
                tts.infer(
                    spk_audio_prompt=str(speaker), text=group["lip_sync_text"], output_path=str(output),
                    emo_audio_prompt=str(emotion), emo_alpha=float(render_cfg.get("emotion_alpha", 0.90)),
                    do_sample=True, temperature=float(render_cfg.get("temperature", 0.80)), top_p=0.8, top_k=30,
                    num_beams=3, interval_silence=80, verbose=False,
                )
            candidates.append({"variant": variant, "seed": seed, "path": rel_to_project(project, output), "duration": round(audio_duration(output), 3)})
        group["render"] = {"method": "A_lipsync", "emotion": rel_to_project(project, emotion), "candidates": candidates, "selection": None}
        save(manifest_path(project, config), groups)
    print("Fresh source-conditioned A-only rendering complete. No legacy or alternative-model audio was used.")


def active_db(path: Path) -> float:
    return float(rms_activity(path)["active_db"])


def peak_db(path: Path) -> float:
    import numpy as np
    import soundfile as sf

    audio, _ = sf.read(path, dtype="float32", always_2d=False)
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    return 20.0 * math.log10(max(peak, 1e-7))


def normalize_to_source(source: Path, generated: Path, output: Path) -> float:
    wanted = active_db(source) - active_db(generated)
    safe = min(wanted, -1.0 - peak_db(generated))
    gain = max(-18.0, min(18.0, safe))
    run(["ffmpeg", "-y", "-v", "error", "-i", str(generated), "-af", f"volume={gain:.4f}dB", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s24le", str(output)])
    return round(gain, 3)


def select(args: argparse.Namespace) -> None:
    """Select strictly between fresh A-only candidates; never use old text/audio."""
    _, project, config = read_config(args.config)
    verify_gpu()
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        die(f"select requires faster-whisper: {exc}")
    groups = load(manifest_path(project, config))
    requested = parse_groups(args.groups) if args.groups else None
    whisper = WhisperModel(args.asr_model, device="cuda", compute_type="float16")
    target = config["languages"]["target"].get("asr_language", target_name(config))
    quality = config.get("quality", {})
    min_recall = float(quality.get("minimum_word_recall", 0.82))
    max_overrun = float(quality.get("maximum_overrun_seconds", 0.35))
    gap = float(config.get("render", {}).get("minimum_gap_seconds", 0.08))
    project_duration = audio_duration(project_audio(project, config))
    rows = []
    for index, group in enumerate(groups):
        if requested is not None and int(group["group"]) not in requested:
            continue
        candidates = group.get("render", {}).get("candidates", [])
        if not candidates:
            die(f"group {group['group']} has no candidates; run render")
        next_start = float(groups[index + 1]["source_start"]) if index + 1 < len(groups) else project_duration
        available = max(0.1, next_start - float(group["source_start"]) - gap)
        expected = group["lip_sync_text"]
        analyzed = []
        for candidate in candidates:
            path = resolve_project_path(project, candidate["path"])
            pieces, _ = whisper.transcribe(str(path), language=target, beam_size=5, vad_filter=False)
            transcript = " ".join(piece.text.strip() for piece in pieces).strip()
            recall, ending = text_recall(expected, transcript, target_name(config))
            duration = audio_duration(path)
            overrun = max(0.0, duration - available)
            target_span = max(0.35, float(group["source_span"]))
            score = (1.0 - recall) * 12.0 + (0.0 if ending else 5.0) + abs(duration - target_span) / target_span * 1.4 + overrun / target_span * 18.0
            analyzed.append({**candidate, "transcript": transcript, "word_recall": round(recall, 3), "ending_present": ending, "duration": round(duration, 3), "available_duration": round(available, 3), "overrun": round(overrun, 3), "score": round(score, 4)})
        best = min(analyzed, key=lambda item: item["score"])
        selected = resolve_project_path(project, best["path"]).parent / "selected.wav"
        gain = normalize_to_source(resolve_project_path(project, group["render"]["emotion"]), resolve_project_path(project, best["path"]), selected)
        review = []
        if float(best["word_recall"]) < min_recall:
            review.append("word-integrity review")
        if not bool(best["ending_present"]):
            review.append("ending review")
        if float(best["overrun"]) > max_overrun:
            review.append("timing review")
        group["render"]["selection"] = {
            "method": "A_lipsync", "candidate": best["variant"], "path": rel_to_project(project, selected),
            "duration": round(audio_duration(selected), 3), "gain_db": gain, "review": review,
            "candidates": analyzed,
        }
        rows.append({"group": group["group"], "role": group["role"], "candidate": best["variant"], "duration": group["render"]["selection"]["duration"], "available_duration": best["available_duration"], "word_recall": best["word_recall"], "ending_present": best["ending_present"], "overrun": best["overrun"], "review": "; ".join(review), "text": expected})
        save(manifest_path(project, config), groups)
    write_csv(work_dir(project, config) / "selection.csv", rows, ["group", "role", "candidate", "duration", "available_duration", "word_recall", "ending_present", "overrun", "review", "text"])
    print(f"A-only selection complete; {sum(bool(row['review']) for row in rows)} turn(s) need review.")


def assemble(args: argparse.Namespace) -> None:
    """Assemble selected A-only turns at source-picture starts without time stretching."""
    _, project, config = read_config(args.config)
    groups = load(manifest_path(project, config))
    output_root = output_dir(project, config)
    target = target_name(config)
    output = Path(args.output).resolve() if args.output else output_root / f"dialogue_{target}_a_lipsync.wav"
    subtitles = Path(args.srt).resolve() if args.srt else output_root / f"dialogue_{target}_a_lipsync.srt"
    duration = audio_duration(project_audio(project, config))
    max_overlap = float(config.get("quality", {}).get("maximum_overlap_seconds", 0.05))
    schedule, conflicts = [], []
    for index, group in enumerate(groups):
        selection = group.get("render", {}).get("selection")
        if not selection:
            die(f"group {group['group']} has no selection; run select")
        path = resolve_project_path(project, selection["path"])
        if not path.exists():
            die(f"selected audio missing for group {group['group']}: {path}")
        start = float(group["source_start"])
        end = start + float(selection["duration"])
        next_start = float(groups[index + 1]["source_start"]) if index + 1 < len(groups) else duration
        overlap = max(0.0, end - next_start)
        item = {"group": group["group"], "role": group["role"], "start": round(start, 3), "end": round(end, 3), "source_end": group["source_end"], "overlap": round(overlap, 3), "path": selection["path"], "text": group["lip_sync_text"]}
        schedule.append(item)
        if overlap > max_overlap:
            conflicts.append(item)
    if conflicts and not args.allow_overlap:
        save(work_dir(project, config) / "assembly_conflicts.json", conflicts)
        die(f"{len(conflicts)} turn(s) overrun the next source turn; revise their lip-sync text rather than speeding/cutting them. See {work_dir(project, config) / 'assembly_conflicts.json'}")
    if schedule and float(schedule[-1]["end"]) > duration + max_overlap:
        die("last generated turn exceeds the source audio duration; revise it before assembly")
    output.parent.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-t", f"{duration:.6f}", "-i", "anullsrc=r=48000:cl=mono"]
    labels, filters = ["[base]"], ["[0:a]aresample=48000[base]"]
    for index, item in enumerate(schedule, 1):
        command.extend(["-i", str(resolve_project_path(project, item["path"]))])
        label = f"g{index}"
        filters.append(f"[{index}:a]aresample=48000,adelay={round(float(item['start']) * 1000)}|{round(float(item['start']) * 1000)}[{label}]")
        labels.append(f"[{label}]")
    filters.append("".join(labels) + f"amix=inputs={len(labels)}:duration=first:normalize=0,atrim=duration={duration:.6f}[out]")
    command.extend(["-filter_complex", ";".join(filters), "-map", "[out]", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s24le", str(output)])
    run(command)
    write_srt(subtitles, schedule)
    save(work_dir(project, config) / "schedule.json", schedule)
    print(f"Wrote {output} and audio-aligned subtitles {subtitles}; overlaps allowed: {len(conflicts)}.")


def validate(args: argparse.Namespace) -> None:
    _, project, config = read_config(args.config)
    work = work_dir(project, config)
    groups = load(manifest_path(project, config))
    target = target_name(config)
    output = Path(args.output).resolve() if args.output else output_dir(project, config) / f"dialogue_{target}_a_lipsync.wav"
    schedule_path = work / "schedule.json"
    schedule = load(schedule_path) if schedule_path.exists() else []
    missing, reviews, non_a = [], [], []
    for group in groups:
        selected = group.get("render", {}).get("selection")
        if not selected:
            missing.append(group["group"]); continue
        if selected.get("method") != "A_lipsync":
            non_a.append(group["group"])
        if selected.get("review"):
            reviews.append({"group": group["group"], "review": selected["review"]})
    probe = None
    if output.exists():
        result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate,channels,bits_per_sample:format=duration", "-of", "json", str(output)], capture_output=True, text=True, check=True)
        probe = json.loads(result.stdout)
    report = {
        "turns": len(groups), "selected_a_lipsync_turns": len(groups) - len(non_a) - len(missing),
        "missing_selections": missing, "non_a_methods": non_a, "selection_reviews": reviews,
        "schedule_conflicts_over_threshold": [item["group"] for item in schedule if float(item.get("overlap", 0.0)) > float(config.get("quality", {}).get("maximum_overlap_seconds", 0.05))],
        "output": str(output), "output_probe": probe,
    }
    save(work / "final_validation.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.strict and (missing or non_a or reviews or not probe):
        raise SystemExit(2)


def status(args: argparse.Namespace) -> None:
    _, project, config = read_config(args.config)
    work = work_dir(project, config)
    manifest = manifest_path(project, config)
    if not manifest.exists():
        print("Project initialized; run preflight then build.")
        return
    groups = load(manifest)
    approved = sum(group.get("translation_state") == "approved" for group in groups)
    rendered = sum(bool(group.get("render", {}).get("candidates")) for group in groups)
    selected = sum(bool(group.get("render", {}).get("selection")) for group in groups)
    print(json.dumps({"turns": len(groups), "approved_translations": approved, "rendered_turns": rendered, "selected_turns": selected, "work": str(work), "output": str(output_dir(project, config))}, indent=2))


def add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True, help="pipeline.json file or its project directory")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init", help="create a new project config")
    p.add_argument("--project", required=True)
    p.add_argument("--audio", required=True)
    p.add_argument("--source-srt", required=True)
    p.add_argument("--target-srt", required=True)
    p.add_argument("--dialogue-script", required=True)
    p.add_argument("--source-language", default="cs")
    p.add_argument("--target-language", default="en")
    p.add_argument("--name")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=init)
    p = sub.add_parser("preflight", help="validate inputs and report counts")
    add_config_argument(p); p.set_defaults(func=preflight)
    p = sub.add_parser("build", help="build source-timed turns and review sheets")
    add_config_argument(p); p.set_defaults(func=build)
    p = sub.add_parser("draft-lipsync", help="make review-only local-LLM translation drafts")
    add_config_argument(p); p.add_argument("--model", default="Qwen/Qwen2.5-7B-Instruct"); p.add_argument("--sheet"); p.add_argument("--groups"); p.set_defaults(func=draft_lipsync)
    p = sub.add_parser("apply-translations", help="apply approved lip-sync translations from the review CSV")
    add_config_argument(p); p.add_argument("--sheet"); p.add_argument("--accept-unapproved", action="store_true"); p.set_defaults(func=apply_translations)
    p = sub.add_parser("validate-translations", help="write translation QC report")
    add_config_argument(p); p.add_argument("--strict", action="store_true"); p.set_defaults(func=validate_translations)
    p = sub.add_parser("refine-timing", help="propose source-audio timing corrections for review")
    add_config_argument(p); p.add_argument("--search-seconds", type=float, default=0.45); p.set_defaults(func=refine_timing)
    p = sub.add_parser("apply-timing", help="apply human-approved timing_overrides.json")
    add_config_argument(p); p.add_argument("--overrides"); p.set_defaults(func=apply_timing)
    p = sub.add_parser("make-references", help="extract and build actor-reference WAVs")
    add_config_argument(p); p.add_argument("--clips-per-role", type=int, default=3); p.add_argument("--force", action="store_true"); p.set_defaults(func=make_references)
    p = sub.add_parser("render", help="render fresh source-emotion A-only candidates")
    add_config_argument(p); p.add_argument("--groups"); p.add_argument("--variants", type=int); p.add_argument("--force", action="store_true"); p.set_defaults(func=render)
    p = sub.add_parser("select", help="select only among A-only candidates")
    add_config_argument(p); p.add_argument("--groups"); p.add_argument("--asr-model", default="large-v3-turbo"); p.set_defaults(func=select)
    p = sub.add_parser("assemble", help="assemble no-speed dialogue stem and audio-aligned SRT")
    add_config_argument(p); p.add_argument("--output"); p.add_argument("--srt"); p.add_argument("--allow-overlap", action="store_true"); p.set_defaults(func=assemble)
    p = sub.add_parser("validate", help="validate the A-only final master")
    add_config_argument(p); p.add_argument("--output"); p.add_argument("--strict", action="store_true"); p.set_defaults(func=validate)
    p = sub.add_parser("status", help="show current project stage")
    add_config_argument(p); p.set_defaults(func=status)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
