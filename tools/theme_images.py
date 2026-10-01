#!/usr/bin/env python3
"""Make screenshots in the README and the wiki follow the reader's GitHub theme.

Every Markdown image ``![alt](.../name.png)`` that has a ``name-light.png`` next to it becomes a ``<picture>`` that shows the dark screenshot in GitHub's dark theme and the light one in the light theme. Images whose alt text names a theme ("Dark theme", "the light theme") show that theme on purpose and are left alone, as are images already converted, so running it again is harmless.

    python tools/theme_images.py            # rewrite README.md and docs/wiki/*.md
    python tools/theme_images.py --check    # exit 1 if anything still needs converting
"""
from __future__ import annotations

import argparse
import glob
import html
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES = os.path.join(ROOT, "docs", "wiki", "images")
MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(((?:[^)\s]*/)?images/([a-z0-9-]+)\.png)\)")


def picture(alt: str, path: str) -> str:
    light = path[:-4] + "-light.png"
    return f'<picture><source media="(prefers-color-scheme: light)" srcset="{light}"><img alt="{html.escape(alt, quote=True)}" src="{path}"></picture>'


def convert(text: str) -> tuple:
    count = 0

    def rep(m):
        nonlocal count
        alt, path, name = m.groups()
        # Images that show one theme on purpose (their alt text names it, e.g. "Dark theme") stay as they are.
        if name.endswith("-light") or re.search(r"\b(dark|light) theme\b", alt, re.I) or not os.path.exists(os.path.join(IMAGES, name + "-light.png")):
            return m.group(0)
        count += 1
        return picture(alt, path)
    return MD_IMAGE.sub(rep, text), count


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    total = 0
    for path in [os.path.join(ROOT, "README.md")] + sorted(glob.glob(os.path.join(ROOT, "docs", "wiki", "*.md"))):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        new, n = convert(text)
        if n:
            total += n
            print(f"{os.path.relpath(path, ROOT)}: {n} image(s)")
            if not args.check:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new)
    if args.check and total:
        return 1
    print(f"{total} image(s) {'to convert' if args.check else 'converted'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
