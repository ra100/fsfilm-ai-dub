# Local-first dubbing UI plan

## Objective

Build a local browser UI for reviewing and driving the reusable dubbing
pipeline. It must make lip-sync, voice, timing, and candidate selection easy
to judge against picture while keeping `reusable_pipeline.py` as the
authoritative rendering engine.

The application runs only on the editor's workstation at `127.0.0.1`. Audio,
video, subtitle assets, actor references, models, and GPU inference remain
local; the app has no cloud dependency.

```text
Browser UI <-> local FastAPI service <-> reusable_pipeline.py <-> local GPU
                                      |
                                      +-> project files, media, and outputs
```

## Principles

- Do not rewrite the synthesis/QA pipeline in the UI.
- Preserve the existing portable project contract: `pipeline.json`, review
  CSVs, JSON overrides, manifests, renders, WAV outputs, and SRT outputs.
- The UI must be safely interchangeable with the command line. An editor can
  stop using the app and resume the project through `reusable_pipeline.py`.
- Never hide editorial changes or silently substitute a different model/take.
- Run one GPU render job at a time and retain all candidate takes.
- Keep source media local and serve only explicitly opened project files.

## Project and state model

Existing project artifacts remain the source of truth:

| Artifact | UI responsibility |
| --- | --- |
| `pipeline.json` | Show/edit registered source inputs, optional video input, roles, and references. |
| `work/translation_review.csv` | Edit and approve lip-sync translations. |
| `work/timing_overrides.json` | Review and apply human-approved timing changes. |
| `work/reference_selection.json` | Review extracted actor-reference choices. |
| `work/candidate_overrides.json` | Store a manual candidate choice without regenerating audio. |
| `work/renders/` | Present retained candidates and selected WAVs. |

Add a project-local `work/pause_overrides.json` for pause intent. It keeps
editorial controls out of clean subtitle text:

```json
{
  "13": [
    {
      "after_word": 2,
      "duration_ms": 450,
      "mode": "natural"
    }
  ]
}
```

The UI may use a small local SQLite database only for non-portable convenience
state: recent projects, job history/logs, UI settings, and candidate listening
history. It must not be required to reproduce a dub on another machine.

## Architecture

### Backend

Use Python and FastAPI. The existing ML, audio, subtitle, and rendering
ecosystem is Python, so the backend should invoke the established CLI as
controlled subprocess jobs rather than duplicate its logic.

Responsibilities:

- project discovery and validated path registration;
- read-only status, manifest, subtitles, waveform, candidate, and output APIs;
- explicit edit APIs for translations, references, selection/timing/pause
  overrides, and project configuration;
- a persistent single-GPU job queue with cancel support;
- streaming structured command progress and logs to the browser;
- project-scoped media delivery and generated low-resolution video proxies when
  useful for responsive preview.

Each job maps visibly to an existing command such as `build`,
`apply-translations`, `make-references`, `render --groups`, `select --groups`,
`assemble`, or `validate`. Destructive or broad actions require explicit
confirmation in the UI.

### Frontend

Use TypeScript/React. A custom frontend is justified by frame-accurate video,
waveform interaction, rapid A/B/C auditioning, keyboard control, and the
need to keep the playhead fixed while switching audio.

Use a waveform/timeline component such as WaveSurfer, with the native browser
video element for preview. A future Tauri shell may package the same local UI
as a desktop application, but it is not required for the initial release.

## UI areas

### Projects

- Create/open a project; register audio, optional video, source SRT, target
  SRT, and source dialogue script.
- Present preflight status, available GPU, missing actor references,
  untranslated/unapproved turns, QA warnings, and output state.
- Keep audition projects visibly separate from accepted masters to prevent an
  accidental final assembly from experimental takes.

### Dialogue and translation editor

Display one row per rendered dialogue group, not a pile of subtitle fragments.
For each group, show the source text, legacy target translation, editable
lip-sync text, speaker/role, source timing, target cue coverage, word budget,
glossary checks, pronunciation notes, and approval state.

An optional local LLM may create drafts, but it never changes approved text
without an editor action. The UI must surface wording, register, mandatory
phrases, and pronunciation concerns as reviewable information rather than
pretending lip-sync is a fully automatic score.

### Actor references

- List roles and their dedicated/extracted voice references.
- Play source/reference comparison clips before use.
- Replace a weak or silent reference with a supplied actor recording.
- Configure optional dedicated emotion audio for difficult source turns.
- Store role-specific accent and pronunciation notes for the human reviewer.

### Video and timeline workspace

This is the primary lip-sync review surface:

```text
Video preview: frame step | loop selected group | playback-speed control
Audio: Czech source | selected English | candidate 1 / 2 / 3
Timeline: waveform | subtitle boundaries | speech range | pause markers
Editor: translation | timing/pause controls | render/select actions
```

Required behaviour:

- synchronised video/audio playback and sample/frame-aware seeking;
- loop a selected group with configurable pre-roll and post-roll;
- frame stepping and keyboard playback controls;
- switch among Czech, selected English, and A/B/C candidates at the same
  playhead; and
- show source speech range, grouped turn duration, generated duration,
  original subtitle boundaries, and QA/timing warnings.

### Candidate audition

- Retain and present all fresh candidate takes for a group.
- Compare source performance and candidates with matched start time and
  loudness.
- Display automatic duration/ASR/ending checks and explain review flags.
- Select a candidate manually or rerender three fresh variants without
  affecting other groups.
- Permit in-context preview with the preceding/following selected dialogue.

## Pause workflow

Pauses must be editorial controls, not merely punctuation guessed by a model.

1. **Natural pause** — render a comma, dash, ellipsis, or sentence break in
   one continuous take. This is the default and safest option.
2. **Requested pause** — let the editor place a marker after a word and set a
   duration. The UI reserves that duration in the group's timing budget.
3. **Hard pause** — after rendering and word alignment, insert an exact silence
   at the selected word boundary. Mark it as an intervention and re-run QA;
   later words move later but are never time-compressed.
4. **Phrase split** — a deliberately selected fallback for a substantial
   dramatic gap. It risks an audible seam and must never be the automatic
   default.

Before a render, show the consequence clearly:

```text
Picture time:       3.20 s
Estimated speech:   2.48 s
Requested pause:    0.45 s
Remaining margin:   0.27 s  OK
```

The delivery subtitle contains only normal approved text; pause metadata is
used for rendering/review and does not pollute the final SRT.

## Delivery phases

### Phase 1 — backend foundation

1. Scaffold FastAPI service, local launcher, project registry, and secure
   path/media serving.
2. Add project/status/manifest/read APIs.
3. Implement a one-GPU subprocess job queue, cancellation, retained logs, and
   browser progress stream.
4. Add safe write APIs for review CSVs and existing JSON override artifacts.

### Phase 2 — review MVP

1. Implement project/status and dialogue/translation screens.
2. Add targeted `apply-translations`, `render --groups`, and `select --groups`
   actions.
3. Implement A/B/C candidate audition with manual override selection.
4. Add actor-reference browsing and targeted reference generation.

### Phase 3 — picture and timing

1. Add optional video input, proxy generation, and synchronised preview.
2. Add waveform/timeline group navigation, looping, cue boundaries, and
   source/generated duration overlays.
3. Add timing proposal/approval controls and final QA visualisation.

### Phase 4 — pause support

1. Define and validate `pause_overrides.json`.
2. Add natural-pause prompts and budget previews.
3. Implement post-render word alignment and hard-pause insertion with QA.
4. Add deliberate phrase-split support only after auditioning its quality.

### Phase 5 — assembly and handoff

1. Add assemble/validate/export screens and a final video preview.
2. Export dialogue stem, original-timing delivery SRT, technical schedule SRT,
   and QA report.
3. Add end-to-end project tests, recovery/back-up behaviour, and local setup
   documentation.

## Definition of a successful MVP

An editor can open a local project, edit a group’s reviewed translation, see it
against the picture, render three fresh takes on the local GPU, compare all
three with the Czech source, choose one, and preserve that selection in the
same portable project files used by the CLI. No output is uploaded, silently
replaced, speed-adjusted, or assembled into a master without explicit action.
