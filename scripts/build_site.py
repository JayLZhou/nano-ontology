"""Build the project website into ``_site/``.

    pip install markdown
    python scripts/build_site.py            # then open _site/index.html

The landing page is ``website/index.html``. The documentation pages are generated from the same
Markdown files the repository shows on GitHub (``docs/*.md``, ``benchmark/README.md``,
``CHANGELOG.md``), so there is one source for both. ``.github/workflows/pages.yml`` runs this and
publishes the result to GitHub Pages.
"""

from __future__ import annotations

import re
import shutil
from html import escape
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "_site"
REPO = "https://github.com/JayLZhou/nano-ontology"

# output name -> (source, title, navigation group)
PAGES = {
    "walkthrough": ("docs/walkthrough.md", "Walkthrough", "Start"),
    "walkthrough_cn": ("docs/walkthrough_cn.md", "完整流程（中文）", "Start"),
    "ontology-reference": ("docs/ontology-reference.md", "Ontology reference", "Model your domain"),
    "agents": ("docs/agents.md", "Connect an agent", "Run it"),
    "console": ("docs/console.md", "Console", "Run it"),
    "cli-reference": ("docs/cli-reference.md", "CLI reference", "Run it"),
    "architecture": ("docs/architecture.md", "Architecture", "Understand it"),
    "benchmark": ("benchmark/README.md", "Benchmark", "Understand it"),
    "changelog": ("CHANGELOG.md", "Changelog", "Understand it"),
}
BY_SOURCE = {source: name for name, (source, _, _) in PAGES.items()}


def slug(value: str, separator: str = "-") -> str:
    """Heading ids the way GitHub makes them, so the same ``#anchors`` work in both places."""
    value = re.sub(r"`", "", value.strip().lower())
    value = "".join(c for c in value if c.isalnum() or c in " -_")
    return value.replace(" ", separator)


def rewrite(target: str, source: Path) -> str:
    """Point a relative link in a Markdown file at the right place on the site (or on GitHub)."""
    if re.match(r"[a-z]+:|#", target):
        return target
    path, hash_, fragment = target.partition("#")
    resolved = (source.parent / path).resolve()
    try:
        relative = resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return target
    if relative in BY_SOURCE:
        return BY_SOURCE[relative] + ".html" + hash_ + fragment
    if relative.startswith("images/"):
        return "../" + relative
    if relative in ("README.md", "README_CN.md"):
        return "../index.html" if relative == "README.md" else f"{REPO}/blob/main/{relative}"
    kind = "tree" if resolved.is_dir() else "blob"
    return f"{REPO}/{kind}/main/{relative}{hash_}{fragment}"


def render(name: str) -> str:
    source_path, title, _ = PAGES[name]
    source = ROOT / source_path
    text = source.read_text(encoding="utf-8")
    text = re.sub(r"(\]\()([^)\s]+)(\))", lambda m: m.group(1) + rewrite(m.group(2), source) + m.group(3), text)
    text = re.sub(r'((?:href|src|srcset)=")([^"]+)(")', lambda m: m.group(1) + rewrite(m.group(2), source) + m.group(3), text)
    text = re.sub(r"^> \[!(NOTE|TIP|IMPORTANT|WARNING)\]\n", "", text, flags=re.M)
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "md_in_html", "toc", "sane_lists"],
                             extension_configs={"toc": {"slugify": slug}})
    groups: dict[str, list[str]] = {}
    for other, (_, label, group) in PAGES.items():
        groups.setdefault(group, []).append(f'<a href="{other}.html"{" class=on" if other == name else ""}>{escape(label)}</a>')
    nav = "".join(f"<h4>{escape(group)}</h4>{''.join(links)}" for group, links in groups.items())
    return f"""<!doctype html>
<html lang="{'zh' if name.endswith('_cn') else 'en'}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · nano-onto</title>
<link rel="stylesheet" href="../site.css">
</head>
<body>
<header class="top"><div class="wrap">
  <a class="logo" href="../index.html">nano-onto</a>
  <nav class="links"><a href="../index.html#console">Console</a><a href="../index.html#benchmark">Benchmark</a>
    <a href="walkthrough.html">Docs</a><a class="btn small cta" href="{REPO}">GitHub</a></nav>
</div></header>
<div class="wrap doc">
  <aside class="side">{nav}</aside>
  <article class="prose">{body}
    <p style="margin-top:48px;font-size:14px"><a href="{REPO}/blob/main/{source_path}">Edit this page on GitHub</a></p>
  </article>
</div>
</body>
</html>
"""


def main() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    (OUT / "docs").mkdir(parents=True)
    shutil.copytree(ROOT / "images", OUT / "images")
    for name in ("index.html", "site.css"):
        shutil.copy(ROOT / "website" / name, OUT / name)
    (OUT / ".nojekyll").write_text("")
    for name in PAGES:
        (OUT / "docs" / f"{name}.html").write_text(render(name), encoding="utf-8")
    print(f"built {len(PAGES) + 1} pages in {OUT}")


if __name__ == "__main__":
    main()
