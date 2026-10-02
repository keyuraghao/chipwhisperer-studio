"""Make README.md render on PyPI: relative image and file links become absolute GitHub URLs pinned to the release tag.

PyPI shows the README as the project description but has no copy of the repository, so ``docs/wiki/images/...`` and ``CHANGELOG.md`` would be broken there. The release job runs this on its checkout just before ``python -m build``; the README in git keeps its relative links.

    python tools/pypi_readme.py            # rewrite README.md in place for the version in src/cwstudio/__init__.py
    python tools/pypi_readme.py --check    # only report relative links that would remain (exit 1 if any)
"""
from __future__ import annotations

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "keyuraghao/chipwhisperer-studio"
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")

# src="x", srcset="x" and href="x" attributes, and Markdown link or image targets ](x) or ](x "title")
PATTERN = re.compile(r'((?:src|srcset|href)=")([^"]+)(")|(\]\()([^)\s]+)((?:\s+"[^"]*")?\))')


def version() -> str:
    text = open(os.path.join(ROOT, "src", "cwstudio", "__init__.py"), encoding="utf-8").read()
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


def absolute(target: str, tag: str) -> str:
    if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith(("#", "//")):
        return target  # already absolute (https:, mailto:) or an anchor on the same page
    path, _, frag = target.partition("#")
    path = path.lstrip("./")
    if path.lower().endswith(IMAGE_EXT):
        url = f"https://raw.githubusercontent.com/{REPO}/{tag}/{path}"
    else:
        kind = "tree" if os.path.isdir(os.path.join(ROOT, path)) else "blob"
        url = f"https://github.com/{REPO}/{kind}/{tag}/{path}"
    return url + ("#" + frag if frag else "")


def convert(text: str, tag: str) -> str:
    def repl(m: re.Match) -> str:
        if m.group(1):
            return m.group(1) + absolute(m.group(2), tag) + m.group(3)
        return m.group(4) + absolute(m.group(5), tag) + m.group(6)
    return PATTERN.sub(repl, text)


def relative_links(text: str) -> list:
    out = []
    for m in PATTERN.finditer(text):
        target = m.group(2) or m.group(5)
        if absolute(target, "x") != target:
            out.append(target)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="list the relative links that would be rewritten and verify the rewrite leaves none")
    ap.add_argument("--readme", default=os.path.join(ROOT, "README.md"))
    args = ap.parse_args(argv)
    tag = "v" + version()
    text = open(args.readme, encoding="utf-8").read()
    new = convert(text, tag)
    left = relative_links(new)
    if left:
        print("relative links left after the rewrite: " + ", ".join(left), file=sys.stderr)
        return 1
    if args.check:
        print(f"{len(relative_links(text))} relative links would point at {tag}")
        return 0
    with open(args.readme, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    print(f"README.md: {len(relative_links(text))} relative links now point at {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
