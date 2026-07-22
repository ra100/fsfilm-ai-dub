# Reusable source-performance dubbing pipeline

`reusable_pipeline.py` is a repeatable source-performance dubbing workflow. It
produces a dialogue-only target-language stem. It does not
separate music/effects, make a final mix, or silently force speech to fit.

The primary renderer is IndexTTS2 in source-emotion voice-clone mode:

```text
source audio + approved actor reference + approved lip-sync text
                                  │
                                  ▼
                    fresh target-language A-only candidates
                                  │
                       ASR / duration / ending QA
                                  │
                                  ▼
                   no-speed dialogue stem + original-timing SRT
```

There is no hybrid stage. The pipeline never substitutes an old subtitle take,
another model, or a different style when a generated line fails. It reports the
line so the translation or reference can be fixed.

## Required inputs

For a new short, provide:

- a mono or stereo dialogue/final-mix WAV;
- optionally, a picture reference video for local UI lip-sync review;
- source-language SRT (Czech in the normal use case);
- a legacy target-language SRT. Its wording is a semantic starting point only;
- a source-language dialogue script where every spoken line is `ROLE: text`.

The source script is what assigns each subtitle/turn to an actor. It must be in
the same language as the source subtitles. Speaker IDs can be any concise label
(`PUL`, `NARRATOR`, `GUARD_2`, etc.).

Use only actor reference audio for which you have the performers' permission.
IndexTTS2's licence must also be checked for each distribution/use case.

## One-time environment

The preparation, review and selection commands use the normal environment:

```bash
ai_dub/.venv/bin/python
```

Rendering needs the isolated IndexTTS2 environment and a CUDA GPU with enough
VRAM (the 24 GB RTX 4090 used here is suitable):

```bash
ai_dub/third_party/index-tts/.venv/bin/python
```

`ffmpeg`, `ffprobe`, CUDA/PyTorch, `librosa`, `soundfile`, and
`faster-whisper` must be available. `draft-lipsync` additionally needs the
local Qwen instruction model; it is optional and its output is always a draft.

## Create a project

Keep each dub in its own small project folder. Inputs may remain in their
original locations; the config stores relative paths.

```bash
ai_dub/.venv/bin/python ai_dub/reusable_pipeline.py init \
  --project ai_dub/projects/my_short_en \
  --name my_short_en \
  --audio /path/to/dialogue-or-final-mix.wav \
  --video /path/to/picture-reference.mov \
  --source-srt /path/to/source.cs.srt \
  --target-srt /path/to/legacy.en.srt \
  --dialogue-script /path/to/dialogy.txt \
  --source-language cs \
  --target-language en
```

This creates `pipeline.json`, `work/`, and `output/`. Before doing anything
else, inspect `pipeline.json`:

- `roles`: add `reference_audio` for any actor with a clean dedicated take.
  This is essential for a weak, noisy or nearly silent source section such as
  a weak or off-screen transmission.
- `roles.<ROLE>.emotion_audio`: optional dedicated performance prompt. Use it
  only when the source turn itself contains no usable performance.
- `grouping.max_turn_seconds`: 30 seconds by default. Consecutive sentences by
  one character are rendered together up to this limit, yielding one natural
  turn file rather than a pile of clipped subtitle fragments.
- `grouping.max_gap_seconds`: 6 seconds by default so subtitle gaps inside one
  scripted turn do not create artificial separate takes. Lower it only when a
  long visual/action break should be rendered as a new turn.
- `grouping.merge_consecutive_same_role`: enabled by default. Adjacent script
  rows spoken by the same actor are rendered as one turn when they fit the gap
  and maximum-duration limits. The pipeline never merges across another
  character, because splitting a synthetic performance across interleaved
  dialogue would harm picture timing and acting continuity.
- `render.minimum_emotion_seconds`: source turns shorter than 1.2 seconds use
  the reviewed actor reference as the emotion anchor. Voice identity is thus
  stable even for a one-word reply; use `roles.<ROLE>.emotion_audio` when a
  specific short-line emotion needs a dedicated prompt.
- `translation.accent`: metadata for the translator/reviewer. Source-conditioned
  cloning preserves some source accent; it does not guarantee General American
  pronunciation. Do a small audition before committing to a language/accent.

## Standard runbook

Run these from the repository root. Substitute your project directory.

```bash
PIPE=ai_dub/projects/my_short_en
PY=ai_dub/.venv/bin/python
INDEXPY=ai_dub/third_party/index-tts/.venv/bin/python

$PY ai_dub/reusable_pipeline.py preflight --config "$PIPE"
$PY ai_dub/reusable_pipeline.py build --config "$PIPE"
```

`build` writes these reviewable source-of-truth artifacts:

| File | Purpose |
| --- | --- |
| `work/turn_manifest.json` | source-timed speaker turns; do not hand-edit except for diagnosis |
| `work/role_review.csv` | low-confidence script-to-speaker assignments |
| `work/script_coverage_review.csv` | scripted lines with no usable source-SRT timing |
| `work/translation_review.csv` | the editable lip-sync translation sheet |
| `work/timing_overrides.json` | human-approved timing edits only |
| `work/glossary.json` | mandatory source-to-target phrases/names |

### 1. Speaker and timing review

Correct `role_review.csv` problems by fixing the source script, then run
`build` again. Do not guess actor identity from the target subtitle.

If an off-screen/radio line is absent from the source SRT but present in the
target SRT, `build` detects the skipped source-script row and creates a
target-timed turn for it. That turn is deliberately placed in `role_review.csv`:
listen to the supplied time range and add a dedicated actor reference if the
source track is weak. If it cannot find an isolated target-SRT gap, it leaves a
row in `script_coverage_review.csv` instead of guessing a timing.

Generate proposed boundaries, listen to them, and copy only approved values to
`work/timing_overrides.json`:

```bash
$PY ai_dub/reusable_pipeline.py refine-timing --config "$PIPE"
$PY ai_dub/reusable_pipeline.py apply-timing --config "$PIPE"
```

The timing analyser only proposes boundaries. This is intentional: a final mix
can have music/effects that look like speech to energy VAD. Source audio stays
authoritative, but a human confirms its actual start/end.

### 2. Lip-sync translation review

The legacy target subtitles have been copied to `lip_sync_text` as an initial
semantic draft. They are not yet approved, and synthesis refuses to run.

Optionally make local LLM drafts:

```bash
$PY ai_dub/reusable_pipeline.py draft-lipsync --config "$PIPE"
```

For every row in `work/translation_review.csv`:

1. Use the source text, legacy translation, word budget and picture to write a
   concise, speakable target line.
2. Put the selected wording in `lip_sync_text`.
3. Put `yes` in `approved` only after a bilingual/creative review.
4. Add non-negotiable wording to `work/glossary.json`, for example:

```json
{
  "required_terms": [
    {"source": "speciální vojenská operace", "target": "special military operation"}
  ]
}
```

Apply and inspect the QC report:

```bash
$PY ai_dub/reusable_pipeline.py apply-translations --config "$PIPE"
$PY ai_dub/reusable_pipeline.py validate-translations --config "$PIPE" --strict
```

The word budget is a warning, not a licence to speed up the performance. If a
natural take overruns, revise the text creatively. Keep intended register and
idiom: softened insults, political references, technical names, and quotes
must be reviewed as dialogue—not blindly literal text.

### 3. Build actor references

```bash
$PY ai_dub/reusable_pipeline.py make-references --config "$PIPE"
```

This first pass extracts candidates and creates
`work/reference_selection.json`; it stops there. Listen to every selection.
Source audio may be a final mix, so a long clip can still contain music,
another actor or effects. Edit the JSON to select clean group IDs, or use a
dedicated recording:

```json
{
  "CAPTAIN": {"groups": [4, 12, 27], "review": "listened"},
  "ALIEN": {"audio": "../../references/alien_clean.wav", "review": "dedicated recording"}
}
```

Then run the same command once more to create `work/references/<ROLE>.wav`.
Never accept an almost-silent automated reference: add a clean dedicated source
through `roles.<ROLE>.reference_audio` in `pipeline.json`.

When `roles.<ROLE>.reference_audio` is configured, the command creates that
role's reference immediately; it does not require a source-clip review first.
For an isolated actor refresh without touching other roles, use:

```bash
$PY ai_dub/reusable_pipeline.py make-references --config "$PIPE" --roles TAL
$INDEXPY ai_dub/reusable_pipeline.py render --config "$PIPE" --groups 16,22,26 --variants 3
```

`--roles` and `--groups` are intentionally separate: the former prepares voice
identity, while the latter controls which complete dialogue turns are rendered.
The source-SRT/script alignment also detects identical consecutive short cues
at a role handoff, so a reply from a second actor is not silently merged into
the first actor's turn.

### 4. Render, select and review

Render one complete, source-conditioned A-style turn at a time. It uses the
actor reference for identity and the Czech/source turn for emotion and melody.

```bash
$INDEXPY ai_dub/reusable_pipeline.py render --config "$PIPE" --variants 3
$PY ai_dub/reusable_pipeline.py select --config "$PIPE"
```

You can audition/revise only specific groups without touching the rest:

```bash
$INDEXPY ai_dub/reusable_pipeline.py render --config "$PIPE" --groups 7,31 --variants 3 --force
$PY ai_dub/reusable_pipeline.py select --config "$PIPE" --groups 7,31
```

Every raw take is retained at `work/renders/<turn>_<ROLE>/candidate_01.wav`,
`candidate_02.wav`, and `candidate_03.wav`; selection never deletes or replaces
them. `work/candidate_audition.csv` lists every candidate, its transcript,
duration and automatic score.

Read `work/selection.csv` and listen to every row with a review flag. The
selector checks target-language ASR recall, ending presence and fit to the next
source turn. It chooses only from the newly rendered A-style candidates; it
cannot fall back to legacy target audio/text.

To choose a take yourself later, create/edit
`work/candidate_overrides.json` and rerun `select`—no generation is needed:

```json
{
  "7": 3,
  "31": 2
}
```

The keys are turn IDs and values are retained candidate numbers. The selected
WAV is rebuilt at the correct loudness, then `assemble` can be run again.

### 5. Assemble and validate

```bash
$PY ai_dub/reusable_pipeline.py assemble --config "$PIPE"
$PY ai_dub/reusable_pipeline.py validate --config "$PIPE" --strict
```

The output folder contains:

- `dialogue_<target>_a_lipsync.wav` — 48 kHz, mono, 24-bit dialogue stem;
- `dialogue_<target>_a_lipsync.srt` — the original target SRT's cue count,
  starts, and ends, with the approved dialogue text substituted. When a
  revised grouped line needs reflowing, only its text is redistributed across
  its existing cue slots; no subtitle timing is changed.
- `dialogue_<target>_a_lipsync_audio_schedule.srt` — an optional technical
  view based on the selected rendered takes' actual starts and ends. This is
  useful for auditing audio, not for replacing the source subtitle layout.

`assemble` refuses turns that overlap the following source turn by more than
the configured threshold. Fix their translation or timing; do **not** use
`--allow-overlap` for final delivery. That escape hatch exists only for
diagnostic listening, because overlapping dialogue produces the stitched and
unnatural result we explicitly want to avoid.

## What is automated vs. deliberately reviewed

| Stage | Automatic | Human decision |
| --- | --- | --- |
| Speaker assignment | ordered fuzzy alignment to source script | low-confidence roles |
| Timing | energy-based suggestions | every applied change |
| Translation | optional local draft | final lip-sync wording and terms |
| References | candidate extraction/ranking | clean actor-only clips/dedicated takes |
| Synthesis | multiple fresh A candidates | re-render any weak acting/accent |
| Selection | transcript + ending + duration score | review flags, final listening |
| Assembly | exact source starts, no rate change | resolve any overlap before delivery |

This split is intentional. The reliable parts are automated, while the
judgment-sensitive parts—meaning, performance, lip articulation, actor identity
and lines with very short windows—stay visible and reversible.
