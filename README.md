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

This repository deliberately contains only portable code and documentation.
Models, Python environments, vendor checkouts, media, subtitle assets, actor
references, work directories, output WAVs, project configurations, and
credentials are ignored by design.
