#!/usr/bin/env python3
"""Convert a full dashboard HTML document into Artifact body-content.

The claude.ai Artifact host wraps the page in its own <!doctype html><head><body>
skeleton, so a published file must contain page *content* only — no doctype,
<html>, <head>, or <body> tags. This strips those from a dashboard rendered by
render_from_massive.py / dashboard.py, keeping the inline <script>, <style>, and
the page markup so the artifact renders identically.

Usage:
    python to_artifact.py dashboard.html artifact_dashboard.html
"""

from __future__ import annotations

import sys


def convert(src: str, dst: str) -> None:
    html = open(src).read()
    # keep the inline <script> + <style> from the head, and the <body> contents
    try:
        head = html[html.index("<script>"):html.index("</head>")]
    except ValueError:
        # no <script> before </head>; fall back to just the style block
        head = html[html.index("<style>"):html.index("</head>")]
    body = html[html.index("<body>") + len("<body>"):html.index("</body>")]
    with open(dst, "w") as fh:
        fh.write(head + "\n" + body)


def main(argv=None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 2:
        print("usage: python to_artifact.py <src.html> <dst.html>", file=sys.stderr)
        return 2
    convert(args[0], args[1])
    print(f"  wrote {args[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
