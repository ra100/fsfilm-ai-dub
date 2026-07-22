---
name: ai-dub-local-ui
description: Build or modify the FSFilm AI Dub local-first review application. Use when implementing the FastAPI backend, React/TypeScript UI, local project/media APIs, GPU job queue, video and waveform lip-sync review, candidate auditioning, actor-reference controls, or explicit speech-pause editing.
---

# AI Dub local UI workflow

Read `../../AGENTS.md` and `../../docs/LOCAL_UI_PLAN.md` before architecture or
feature changes. The UI is a local control surface for the existing pipeline,
not a replacement rendering system.

## Preserve the engine contract

- Use a Python/FastAPI backend and call `reusable_pipeline.py` as controlled
  subprocess jobs. Keep pipeline business logic and output semantics in the
  existing Python engine.
- Read/write the existing project artifacts for translations, timing,
  references, candidate selection, manifests, renders, and outputs. SQLite is
  for local UI convenience state only.
- Give every job an explicit command, project scope, lifecycle, log stream,
  result, and cancellation state. Permit at most one GPU render at once.
- Bind to loopback by default and serve only registered project paths after
  path validation. Do not upload or expose source media.

## Build review-first screens

Prioritise the edit-to-audition loop:

```text
edit approved lip-sync text -> apply -> render selected group ->
compare Czech/candidates against picture -> select -> move to next group
```

Support a synchronised video player, source/target audio switching at the same
playhead, waveform/timeline group navigation, subtitle boundaries, source and
generated durations, A/B/C candidate comparison, actor-reference playback,
and explicit QA warning display.

Do not make automatic scores look like editorial decisions. Preserve an easy
manual candidate override and show warnings for timing, transcript, endings,
and weak references.

## Pause controls

Store pause intent in `work/pause_overrides.json`, separate from clean
delivery-subtitle text. Give the editor a picture-time budget before render.

1. Prefer a natural pause in one continuous generated take.
2. Let an editor request a duration after a selected word.
3. Make any hard inserted silence or phrase split explicit, reviewable, and
   revalidated; never use either as a silent default.

Keep the delivery SRT free of pause markup and never time-compress speech to
fit an added pause.

## Implementation checks

- Validate API input and project-relative file resolution.
- Test job queuing, cancellation, a failed subprocess, and resume/reload.
- Test target-group render/select without touching unrelated candidates.
- Test video/audio synchronisation with frame stepping and looping.
- Test that final assembly preserves original target SRT timings and that UI
  changes remain usable from the CLI.
