#!/usr/bin/env python3
"""Launch the loopback-only FSFilm AI Dub local UI server."""
from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from local_ui import DEFAULT_STATE_DIR, REPO_ROOT, create_app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="loopback address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--allow-root", type=Path, action="append", default=[], help="additional project root")
    parser.add_argument("--project", type=Path, action="append", default=[], help="project config/directory to register")
    parser.add_argument("--allow-network", action="store_true", help="required to bind outside loopback")
    args = parser.parse_args()

    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    if args.host not in loopback_hosts and not args.allow_network:
        parser.error("Refusing non-loopback binding; pass --allow-network only when intentional")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    roots = [REPO_ROOT / "projects", *args.allow_root]
    roots.extend(project.resolve().parent for project in args.project)
    app = create_app(state_dir=args.state_dir, allowed_roots=roots, initial_projects=args.project)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
