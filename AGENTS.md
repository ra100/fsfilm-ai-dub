# FSFilm AI Dub agent guide

## Scope and orientation

This repository contains a reusable, source-performance dialogue-dubbing
pipeline. The maintained CLI entry point is `reusable_pipeline.py`.

Before changing pipeline behaviour, read `README.md` and
`REUSABLE_PIPELINE.md`. For local browser-app work, also read
`docs/LOCAL_UI_PLAN.md`.

Repository-local skills describe the two recurring workflows:

- Read `skills/ai-dub-project/SKILL.md` for project setup, translation/timing
  review, actor references, targeted renders, selection, assembly, or QA.
- Read `skills/ai-dub-local-ui/SKILL.md` for FastAPI/React local-app work,
  video/waveform review, job control, or pause controls.

## Source-of-truth and media rules

- Treat `pipeline.json`, review CSVs, manifests, override JSON, render
  directories, and output files as the project contract. Do not invent a
  parallel state format without a migration plan.
- The source audio and source-language subtitles are authoritative for spoken
  performance and timing. The legacy target subtitle is only a semantic draft.
- Do not speed speech to fit. Shorten/rewrite target dialogue or flag it for
  review instead.
- Preserve the delivery SRT's original target cue count and timings; only its
  reviewed dialogue text is substituted. Keep the technical audio schedule SRT
  separate.
- Retain raw candidate takes. Never silently replace a failed take with an old
  result, another model, or an unreviewed translation.
- Treat actor-reference audio as consent-sensitive source material. Use only
  approved recordings and do not commit them.

## Safe editing and pipeline operations

- Prefer narrowly targeted groups/roles for an audition. Do not assemble an
  experimental/audition project into a final master unless explicitly asked.
- Edit lip-sync wording in `work/translation_review.csv`; then run
  `apply-translations` and translation validation before synthesis.
- Keep human timing decisions in `work/timing_overrides.json`; timing analysis
  may propose boundaries but must not silently approve them.
- Build/listen to actor references before use. For weak, silent, or mixed
  source sections, require a clean dedicated role reference or emotion prompt.
- Candidate selection must remain reproducible through
  `work/candidate_overrides.json`; preserve all variants for later audition.
- Validate after behaviour changes with the smallest relevant command, then
  run broader checks before a release. Report warnings rather than hiding them.

## Repository hygiene

- This repository uses an allow-list `.gitignore`. Version only portable code,
  documentation, and intentional test fixtures.
- Never commit models, virtual environments, vendor checkouts, source media,
  project-specific configs, generated audio/video/subtitles, credentials, or
  actor references.
- Use `apply_patch` for tracked text changes. Preserve unrelated user changes
  in a dirty worktree.
- Before committing, run `git diff --check`, inspect `git status`, and verify
  staged paths contain no private media, paths, or credentials.

## Local UI rules

- Keep the app local-first: bind to `127.0.0.1` by default and do not upload
  media or project data.
- Use a Python/FastAPI backend to call the existing CLI as controlled
  subprocess jobs. Do not duplicate or fork rendering/QA logic in the UI.
- Run at most one GPU render job at a time. Stream logs/progress, retain job
  history, make cancellation explicit, and never claim a cancelled job
  succeeded.
- Serve only registered project files through validated, project-scoped paths.
- Keep UI edits interoperable with the CLI. SQLite may store convenience state
  only; the portable project artifacts remain authoritative.
- Store requested pauses separately from clean subtitle text. Natural pauses
  are the default; hard inserted pauses and phrase splits require visible QA
  because they may alter timing or introduce seams.
- Use `pnpm` for the React/Vite frontend. Commit its `pnpm-lock.yaml`; do not
  introduce an npm lockfile.
