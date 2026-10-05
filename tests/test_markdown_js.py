"""The Markdown renderer of the UI (static/js/markdown.js), run with Node.js: it must not use regex lookbehind (a syntax error in Safari before 16.4 that stops the whole UI from loading) and must render bare URLs and table rows as before."""
import json
import os
import re
import shutil
import subprocess

import pytest

JS = os.path.join(os.path.dirname(__file__), "..", "src", "cwstudio", "static", "js")


def test_no_regex_lookbehind_in_the_ui():
    for name in os.listdir(JS):
        if name.endswith(".js"):
            with open(os.path.join(JS, name), encoding="utf-8") as f:
                assert not re.search(r"\(\?<[!=]", f.read()), f"{name} uses regex lookbehind"


CASES = [
    ("http://a.com and www.b.org.", '<p><a href="http://a.com">http://a.com</a> and <a href="http://www.b.org">www.b.org</a>.</p>\n'),
    ("xhttp://no.link a/www.no.com", "<p>xhttp://no.link a/www.no.com</p>\n"),  # no link after a word character or a slash
    ("\\/http://x.com \\.http://y.com", '<p>/http://x.com .<a href="http://y.com">http://y.com</a></p>\n'),  # an escaped slash still counts as a slash
    ("| a | b |\n|---|---|\n| 1 \\| 2 | 3 |", "<table>\n<thead>\n<tr><th>a</th><th>b</th></tr>\n</thead>\n<tbody>\n<tr><td>1 | 2</td><td>3</td></tr>\n</tbody>\n</table>\n"),
]


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js is not installed")
def test_markdown_urls_and_tables(tmp_path):
    shutil.copyfile(os.path.join(JS, "markdown.js"), tmp_path / "markdown.mjs")
    script = tmp_path / "run.mjs"
    script.write_text("import { markdownToHtml } from './markdown.mjs';\n"
                      f"console.log(JSON.stringify({json.dumps([c[0] for c in CASES])}.map(markdownToHtml)));\n")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60, cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [c[1] for c in CASES]
