# FSFilm AI Dub

A reusable, source-performance dialogue-dubbing pipeline for short-form film.
It takes a source voice track, source subtitles, legacy target subtitles, and a
role-labelled source script; it produces a no-speed target-language dialogue
stem plus subtitles that preserve the original target-SRT cue timing.

The maintained entry point is `reusable_pipeline.py`. Read
[REUSABLE_PIPELINE.md](REUSABLE_PIPELINE.md) for setup, safety gates, and the
complete runbook.

The planned local browser UI is documented in
[docs/LOCAL_UI_PLAN.md](docs/LOCAL_UI_PLAN.md). It will drive this same
pipeline without replacing its portable project artifacts or rendering logic.

## Local UI backend (early implementation)

The first local-only backend is available now. It discovers projects under
`projects/`, exposes status/review APIs, and queues only whitelisted pipeline
commands. It binds to loopback only by default:

```bash
ai_dub/.venv/bin/python ai_dub/run_local_ui.py
```

Open `http://127.0.0.1:7860` for the local service and
`http://127.0.0.1:7860/api/docs` for its temporary API interface. Use
`--project /path/to/project` and, where needed, `--allow-root /path/to` to
register a project outside this repository. Build the bundled React interface
with `cd ai_dub/web && pnpm install && pnpm build`; the FastAPI server will
then serve it from the same loopback address.

This repository deliberately contains only portable code and documentation.
Models, Python environments, vendor checkouts, media, subtitle assets, actor
references, work directories, output WAVs, project configurations, and
credentials are ignored by design.
