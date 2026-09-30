#!/usr/bin/env python3
"""Print the CHANGELOG.md section for one version, used as the GitHub release description.

    python tools/release_notes.py 0.2.0        # or v0.2.0
    python tools/release_notes.py --check 0.2.0  # also fail if it does not match cwstudio.__version__
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def section(version: str, text: str) -> str:
    version = version.lstrip("v")
    m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|^\[[^\]]+\]: |\Z)", text, re.S | re.M)
    if not m:
        raise SystemExit(f"CHANGELOG.md has no section for {version}")
    return m.group(1).strip() + "\n"


def main(argv):
    check = "--check" in argv
    args = [a for a in argv if a != "--check"]
    if len(args) != 1:
        raise SystemExit(__doc__)
    version = args[0].lstrip("v")
    with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
        notes = section(version, f.read())
    if check:
        sys.path.insert(0, os.path.join(ROOT, "src"))
        from cwstudio import __version__
        if __version__ != version:
            raise SystemExit(f"tag v{version} does not match cwstudio.__version__ {__version__}")
    sys.stdout.write(notes)


if __name__ == "__main__":
    main(sys.argv[1:])
