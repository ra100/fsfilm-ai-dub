---
name: ai-dub-project
description: Safely operate FSFilm AI Dub projects using the reusable Python pipeline. Use when creating or reviewing a dubbing project, matching source subtitles to a role-labelled script, editing lip-sync translations or timings, preparing actor references, rendering/selecting target-language candidates, assembling a dialogue stem, or validating SRT/audio outputs.
---

# AI Dub project workflow

Read `../../AGENTS.md` and `../../REUSABLE_PIPELINE.md` before acting. Treat
the project artifacts as portable source of truth; use the UI only as another
way to edit and run the same contract.

## Choose the smallest safe action

- Use `status` and `preflight` when diagnosing a project.
- Use `build` after a source script, source SRT, target SRT, or grouping change.
- For a wording edit, update `work/translation_review.csv`, explicitly approve
  it, run `apply-translations`, then validate translations.
- For a timing change, keep the human-approved values in
  `work/timing_overrides.json`; use `refine-timing` only to propose boundaries.
- For a weak/unknown role, prepare and listen to the actor reference before
  render. Prefer an approved dedicated reference over noisy source extraction.
- For an audition, render only requested `--groups` with several variants,
  inspect source performance/candidates, then run targeted `select` or set a
  manual candidate override.
- Assemble only an explicitly requested reviewed master. Validate it before
  calling it final.

## Rendering contract

- The source-language audio supplies performance/emotion; the approved actor
  reference supplies voice identity.
- Render whole same-role turns when grouping allows; do not chop a natural
  sentence into subtitle-fragment files merely to follow cue boundaries.
- Never speed target speech. Revise the translation or flag an overrun.
- Keep all raw candidates. Selection may choose only among current A-style
  candidates and must not silently use legacy audio or another model.
- Preserve the original target SRT timing/cue layout in delivery output. Keep
  technical audio timing in its separate schedule SRT.

## Review gates

Require human review for low-confidence role assignment, skipped script rows,
nearly silent/contaminated references, unapproved translations, timing
overruns, ASR/ending failures, hard pause insertion, and audible seams.

Do not overwrite an accepted master with work from an isolated audition
project. Report any warning that remains after a command instead of masking it.

## Useful command shape

Run normal preparation, review, and selection with:

```bash
ai_dub/.venv/bin/python ai_dub/reusable_pipeline.py <command> --config <project>
```

Run GPU rendering with the isolated IndexTTS environment:

```bash
ai_dub/third_party/index-tts/.venv/bin/python ai_dub/reusable_pipeline.py render --config <project> --groups <ids> --variants 3
```

Read the main runbook for every option and output contract; do not expand this
skill with project-specific paths, models, media, or credentials.
