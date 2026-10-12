"""Build the offline HTML user guide from the Markdown sources.

    .venv\\Scripts\\python docs\\build_guide.py

docs/user-guide.md -> docs/user-guide.html and docs/PLUGINS.md -> docs/plugins.html: one page each, with all CSS
and script inline and the screenshots referenced from docs/img/, so the pages open offline by double-click
(packaging\\build.ps1 copies them next to the exe) and the app serves them at /guide/. Run it after editing either
Markdown file or regenerating the screenshots (docs\\screenshots.py), and commit the HTML with them.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from markdown_it import MarkdownIt

DOCS = Path(__file__).resolve().parent
PAGES = [("user-guide.md", "user-guide.html"), ("PLUGINS.md", "plugins.html")]
LINKS = {src: out for src, out in PAGES}           # links between the Markdown files point to the HTML pages


def slug(text: str, seen: dict) -> str:
    """GitHub's heading anchors, so the same #links work on GitHub and in the HTML page."""
    s = re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")
    n = seen.get(s, 0)
    seen[s] = n + 1
    return s if n == 0 else f"{s}-{n}"


def render(md_text: str) -> tuple[str, str, list]:
    """Markdown -> (title, body html, [(level, id, text)] of the h2/h3 headings)."""
    md = MarkdownIt("commonmark", {"html": False, "typographer": False}).enable("table")
    tokens = md.parse(md_text)
    seen: dict = {}
    toc, title = [], ""
    for i, t in enumerate(tokens):
        if t.type == "heading_open":
            text = "".join(c.content for c in tokens[i + 1].children or [] if c.type in ("text", "code_inline"))
            hid = slug(text, seen)
            t.attrSet("id", hid)
            level = int(t.tag[1])
            if level == 1 and not title:
                title = text
            elif level in (2, 3) and text != "Contents":
                toc.append((level, hid, text))
        if t.type == "inline":
            for c in t.children or []:
                if c.type == "link_open":
                    href = c.attrGet("href") or ""
                    base, _, frag = href.partition("#")
                    if base in LINKS:
                        c.attrSet("href", LINKS[base] + (f"#{frag}" if frag else ""))
                    elif href.startswith(("http://", "https://")):
                        c.attrSet("rel", "noopener")
                if c.type == "image":
                    c.attrSet("loading", "lazy")
    body = md.renderer.render(tokens, md.options, {})
    # the sidebar replaces the Markdown's own contents list
    body = re.sub(r'<h2 id="contents">Contents</h2>\s*<ol>.*?</ol>\s*', "", body, count=1, flags=re.S)
    # a paragraph holding only an image becomes a figure with its alt text as the caption
    body = re.sub(r'<p><img src="([^"]+)" alt="([^"]*)"([^>]*)></p>',
                  lambda m: f'<figure><a href="{m[1]}"><img src="{m[1]}" alt="{m[2]}"{m[3]}></a>'
                            f'<figcaption>{m[2]}</figcaption></figure>', body)
    return title, body, toc


def toc_html(toc: list) -> str:
    out, open_sub = [], False
    for level, hid, text in toc:
        if level == 2:
            if open_sub:
                out.append("</ul></li>")
                open_sub = False
            elif out:
                out.append("</li>")
            out.append(f'<li><a href="#{hid}">{html.escape(text)}</a>')
        else:
            if not open_sub:
                out.append("<ul>")
                open_sub = True
            out.append(f'<li><a href="#{hid}">{html.escape(text)}</a></li>')
    out.append("</ul></li>" if open_sub else "</li>")
    return "<ul>" + "".join(out) + "</ul>"


CSS = """
:root {
  --bg: #f6f7f9; --surface: #ffffff; --ink: #18202a; --muted: #5b6673; --rule: #d9dee4;
  --accent: #1f6feb; --accent-soft: #e7f0fe; --code-bg: #eef1f4; --note-rule: #8fb4ee; --mat: #0d1117;
  --font-body: "Segoe UI", system-ui, -apple-system, Roboto, sans-serif;
  --font-mono: "Cascadia Mono", Consolas, "SF Mono", Menlo, monospace;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0f1318; --surface: #161b22; --ink: #e2e8ee; --muted: #9aa7b4; --rule: #2a323c;
    --accent: #58a6ff; --accent-soft: #15243a; --code-bg: #1d242d; --note-rule: #2f5b94; --mat: #05080b;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #0f1318; --surface: #161b22; --ink: #e2e8ee; --muted: #9aa7b4; --rule: #2a323c;
  --accent: #58a6ff; --accent-soft: #15243a; --code-bg: #1d242d; --note-rule: #2f5b94; --mat: #05080b;
  color-scheme: dark;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; scroll-padding-top: 16px; }
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }
body { margin: 0; background: var(--bg); color: var(--ink); font: 16px/1.6 var(--font-body); }
a { color: var(--accent); text-underline-offset: 2px; }
.layout { display: grid; grid-template-columns: 270px minmax(0, 1fr); max-width: 1320px; margin: 0 auto; }
nav.toc { position: sticky; top: 0; align-self: start; height: 100vh; overflow-y: auto; padding: 24px 16px 40px 20px;
  border-right: 1px solid var(--rule); font-size: 14px; }
nav.toc .brand { font-weight: 700; font-size: 17px; margin-bottom: 4px; }
nav.toc .brand a { color: var(--ink); text-decoration: none; }
nav.toc .sub { color: var(--muted); font-size: 13px; margin-bottom: 14px; }
nav.toc ul { list-style: none; margin: 0; padding: 0; }
nav.toc li { margin: 1px 0; }
nav.toc ul ul { padding-left: 12px; border-left: 1px solid var(--rule); margin: 2px 0 6px 6px; }
nav.toc a { display: block; padding: 3px 8px; border-radius: 6px; color: var(--ink); text-decoration: none; }
nav.toc ul ul a { color: var(--muted); font-size: 13px; padding: 2px 8px; }
nav.toc a:hover, nav.toc a.on { background: var(--accent-soft); color: var(--accent); }
nav.toc .tools { margin-top: 16px; display: flex; gap: 8px; }
button.theme { font: inherit; font-size: 13px; padding: 4px 10px; border: 1px solid var(--rule); border-radius: 6px;
  background: var(--surface); color: var(--ink); cursor: pointer; }
main { padding: 32px 48px 80px; min-width: 0; }
article { max-width: 940px; }
h1 { font-size: 34px; line-height: 1.2; margin: 0 0 12px; }
h2 { font-size: 26px; line-height: 1.25; margin: 56px 0 12px; padding-top: 12px; border-top: 1px solid var(--rule); }
h3 { font-size: 20px; margin: 32px 0 8px; }
h4 { font-size: 17px; margin: 24px 0 6px; }
h2 + h3 { margin-top: 16px; }
p, ul, ol, table, pre, blockquote, figure { margin: 0 0 16px; }
li { margin: 3px 0; }
li > p { margin: 0 0 6px; }
code { font-family: var(--font-mono); font-size: 0.88em; background: var(--code-bg); padding: 1px 5px; border-radius: 4px; }
pre { background: var(--code-bg); padding: 12px 14px; border-radius: 8px; overflow-x: auto; line-height: 1.45; }
pre code { background: none; padding: 0; font-size: 13.5px; }
table { border-collapse: collapse; width: 100%; font-size: 15px; display: block; overflow-x: auto; }
th, td { text-align: left; vertical-align: top; padding: 7px 10px; border-bottom: 1px solid var(--rule); }
th { background: var(--surface); font-weight: 600; border-bottom: 2px solid var(--rule); }
blockquote { background: var(--accent-soft); border-left: 4px solid var(--note-rule); padding: 10px 14px; border-radius: 0 8px 8px 0; }
blockquote p:last-child { margin-bottom: 0; }
figure { background: var(--mat); padding: 10px; border-radius: 10px; }
figure img { display: block; width: 100%; height: auto; border-radius: 4px; }
figcaption { color: #9aa7b4; font-size: 13px; padding: 8px 4px 0; }
img { max-width: 100%; }
hr { border: 0; border-top: 1px solid var(--rule); margin: 32px 0; }
footer { color: var(--muted); font-size: 13px; margin-top: 64px; padding-top: 16px; border-top: 1px solid var(--rule); }
@media (max-width: 900px) {
  .layout { display: block; }
  nav.toc { position: static; height: auto; border-right: 0; border-bottom: 1px solid var(--rule); padding: 16px; }
  nav.toc ul ul { display: none; }
  main { padding: 20px 16px 60px; }
  h1 { font-size: 28px; }
}
@media print {
  nav.toc { display: none; }
  .layout { display: block; }
  main { padding: 0; }
  figure { background: none; padding: 0; }
  h2 { break-after: avoid; }
}
"""

JS = """
(function () {
  var root = document.documentElement, btn = document.getElementById("theme");
  function get() { try { return localStorage.getItem("guide.theme"); } catch (e) { return null; } }
  function set(t) { try { localStorage.setItem("guide.theme", t); } catch (e) { /* file:// or private mode */ } }
  var t = get(); if (t) root.dataset.theme = t;
  btn.onclick = function () {
    var dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark"; set(root.dataset.theme);
  };
  // highlight the section in view in the contents
  var links = {}, current = null;
  document.querySelectorAll("nav.toc a[href^='#']").forEach(function (a) { links[a.getAttribute("href").slice(1)] = a; });
  var heads = Array.prototype.filter.call(document.querySelectorAll("article h2[id], article h3[id]"), function (h) { return links[h.id]; });
  function onScroll() {
    var y = window.scrollY + 80, hit = null;
    for (var i = 0; i < heads.length; i++) { if (heads[i].offsetTop <= y) hit = heads[i]; else break; }
    if (hit !== current) {
      if (current) links[current.id].classList.remove("on");
      if (hit) links[hit.id].classList.add("on");
      current = hit;
    }
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();
})();
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<div class="layout">
<nav class="toc" aria-label="Contents">
  <div class="brand"><a href="#top">{title}</a></div>
  <div class="sub">{sub}</div>
  {toc}
  <div class="tools"><button class="theme" id="theme" type="button">Light / dark</button></div>
</nav>
<main id="top">
<article>
{body}
</article>
<footer>Generated from {source} by docs/build_guide.py. {other}</footer>
</main>
</div>
<script>{js}</script>
</body>
</html>
"""


def build() -> None:
    for src, out in PAGES:
        title, body, toc = render((DOCS / src).read_text(encoding="utf-8"))
        others = [f'<a href="{o}">{"User guide" if o == "user-guide.html" else "Writing plugins"}</a>'
                  for s, o in PAGES if o != out]
        page = PAGE.format(title=html.escape(title), css=CSS, js=JS, body=body, toc=toc_html(toc), source=src,
                           sub="CANopener documentation", other="See also: " + ", ".join(others) + ".")
        (DOCS / out).write_text(page, encoding="utf-8", newline="\n")
        print(f"wrote docs/{out} ({len(page) // 1024} kB, {len(toc)} headings)")


if __name__ == "__main__":
    build()
