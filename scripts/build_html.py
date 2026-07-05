#!/usr/bin/env python3
"""
Build a self-contained HTML report from arXiv gr-qc paper data.

Usage:
    python build_html.py <json_path> <output_html> <date_display>

    json_path   - Path to all-papers.json (with CN_Title, CN_Abstract, CN_Eval).
    output_html - Path to write the final HTML.
    date_display - Date string for display, e.g. "2026年06月29日".

The HTML includes:
- Cover page with statistics
- Two-column table of contents
- Collapsible paper cards with one-line summaries
- MathJax 3 for LaTeX rendering (with lazy typesetting for hidden sections)
"""

import json
import os
import re
import sys


# ── CSS (inline, self-contained) ───────────────────────────────────────────

CSS = r"""
:root {
    --bg: #fafaf8;
    --card-bg: #ffffff;
    --text: #2c2c2c;
    --text-secondary: #666;
    --accent: #2563eb;
    --accent-light: #eff6ff;
    --border: #e5e7eb;
    --tag-bg: #f3f4f6;
    --tag-text: #4b5563;
    --cross-bg: #fef3c7;
    --cross-text: #92400e;
    --shadow: 0 1px 3px rgba(0,0,0,0.08);
    --radius: 8px;
    --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
    --font-mono: "SF Mono", "Fira Code", "Cascadia Code", "Consolas", monospace;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: var(--font-sans);
    background: var(--bg);
    color: var(--text);
    line-height: 1.7;
    -webkit-font-smoothing: antialiased;
}

/* ── Cover ── */
.cover {
    max-width: 800px;
    margin: 80px auto 60px;
    text-align: center;
    padding: 0 24px;
}
.cover .badge {
    display: inline-block;
    background: var(--accent);
    color: #fff;
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.08em;
    padding: 6px 20px;
    border-radius: 100px;
    margin-bottom: 24px;
}
.cover h1 {
    font-size: 36px;
    font-weight: 700;
    margin-bottom: 8px;
    color: #111;
}
.cover .date {
    font-size: 18px;
    color: var(--text-secondary);
    margin-bottom: 40px;
}
.stats {
    display: flex;
    gap: 16px;
    justify-content: center;
    flex-wrap: wrap;
}
.stats .stat-card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px 32px;
    min-width: 140px;
    box-shadow: var(--shadow);
}
.stats .stat-card .num {
    font-size: 32px;
    font-weight: 700;
    color: var(--accent);
}
.stats .stat-card .label {
    font-size: 13px;
    color: var(--text-secondary);
    margin-top: 4px;
}

/* ── TOC ── */
.toc-section {
    max-width: 800px;
    margin: 0 auto 48px;
    padding: 0 24px;
}
.toc-section h2 {
    font-size: 20px;
    font-weight: 700;
    margin-bottom: 16px;
    padding-bottom: 8px;
    border-bottom: 2px solid var(--border);
}
.toc-list {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 6px 24px;
    list-style: none;
}
.toc-list li {
    font-size: 14px;
    line-height: 1.6;
}
.toc-list a {
    color: var(--accent);
    text-decoration: none;
    display: flex;
    align-items: baseline;
    gap: 6px;
}
.toc-list a:hover { text-decoration: underline; }
.toc-list .toc-num {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--text-secondary);
    min-width: 24px;
    flex-shrink: 0;
}
.toc-list .toc-cross {
    font-size: 11px;
    background: var(--cross-bg);
    color: var(--cross-text);
    padding: 1px 6px;
    border-radius: 4px;
    white-space: nowrap;
    flex-shrink: 0;
}

/* ── Papers ── */
.papers {
    max-width: 800px;
    margin: 0 auto 80px;
    padding: 0 24px;
}
.paper-card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    margin-bottom: 16px;
    box-shadow: var(--shadow);
    overflow: hidden;
}
.paper-card summary {
    padding: 20px 24px;
    cursor: pointer;
    list-style: none;
    display: flex;
    align-items: flex-start;
    gap: 12px;
    user-select: none;
}
.paper-card summary::-webkit-details-marker { display: none; }
.paper-card summary::before {
    content: "▶";
    font-size: 11px;
    color: var(--text-secondary);
    flex-shrink: 0;
    margin-top: 2px;
    transition: transform 0.2s;
    display: inline-block;
}
.paper-card[open] summary::before { transform: rotate(90deg); }

.paper-card .card-body {
    flex: 1;
    min-width: 0;
}
.paper-card .card-num {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--text-secondary);
    margin-bottom: 4px;
}
.paper-card .card-title {
    font-size: 16px;
    font-weight: 600;
    color: #111;
    margin-bottom: 4px;
}
.paper-card .card-authors {
    font-size: 13px;
    color: var(--text-secondary);
    margin-bottom: 4px;
}
.paper-card .card-title-cn {
    font-size: 14px;
    font-weight: 500;
    color: var(--text);
    margin-bottom: 4px;
}
.paper-card .card-oneline {
    font-size: 13px;
    color: var(--text-secondary);
}

.paper-card .detail {
    padding: 0 24px 24px;
    border-top: 1px solid var(--border);
}
.paper-card .detail h4 {
    font-size: 14px;
    font-weight: 600;
    color: var(--accent);
    margin: 20px 0 8px;
}
.paper-card .detail p {
    font-size: 14px;
    color: var(--text);
    line-height: 1.8;
    margin-bottom: 12px;
}
.paper-card .detail .cn-title {
    font-size: 15px;
    font-weight: 600;
    color: #111;
    margin: 16px 0 12px;
}

/* ── Footer ── */
.footer {
    text-align: center;
    padding: 40px 24px;
    font-size: 13px;
    color: var(--text-secondary);
}
.footer a { color: var(--accent); }

/* ── Responsive ── */
@media (max-width: 600px) {
    .toc-list { grid-template-columns: 1fr; }
    .cover h1 { font-size: 26px; }
    .stats { gap: 8px; }
    .stats .stat-card { padding: 14px 20px; min-width: 100px; }
    .stats .stat-card .num { font-size: 24px; }
}
"""


# ── Helpers ─────────────────────────────────────────────────────────────────

def html_escape_safe(s: str) -> str:
    """HTML-escape text, but preserve $...$ math blocks for MathJax."""
    if not s:
        return ""
    parts = s.split("$")
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            part = (part
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                    .replace('"', "&quot;"))
        else:
            part = f"${part}$"
        result.append(part)
    return "".join(result)


def one_liner(eval_text: str) -> str:
    """Extract the first sentence from an evaluation."""
    if not eval_text:
        return ""
    m = re.match(r"^(.*?[。；])", eval_text)
    result = m.group(1) if m else eval_text[:80] + "…"
    # Replace "研究问题" with "速览" for the collapsed view
    result = result.replace("研究问题", "速览", 1)
    return result


def format_authors(paper: dict, max_authors: int = 4) -> str:
    """Format author list, truncating with 'et al.' if too long."""
    authors = paper.get("Authors", "")
    names = [a.strip() for a in authors.split(";") if a.strip()]
    if len(names) <= max_authors:
        return ", ".join(names)
    return ", ".join(names[:max_authors]) + f" et al. ({len(names)} authors)"


# ── Main ────────────────────────────────────────────────────────────────────

def build_html(json_path: str, output_path: str, date_display: str) -> None:
    """Read JSON, build HTML, write to output_path."""
    json_path = os.path.expanduser(json_path)
    output_path = os.path.expanduser(output_path)

    with open(json_path, "r", encoding="utf-8-sig") as f:
        papers = json.load(f)

    if not papers:
        print("[ERROR] No papers in JSON file.", file=sys.stderr)
        sys.exit(1)

    # Sort: primary (gr-qc) first, then cross-listed
    primary = [p for p in papers if p.get("PrimaryCat") == "gr-qc"]
    cross = [p for p in papers if p.get("PrimaryCat") != "gr-qc"]
    papers_ordered = primary + cross
    pc = len(primary)

    # Statistics
    cross_cats = {}
    for p in cross:
        c = p.get("PrimaryCat", "unknown")
        cross_cats[c] = cross_cats.get(c, 0) + 1
    cross_list_str = "、".join(
        f"{k} ({v})" for k, v in sorted(cross_cats.items(), key=lambda x: -x[1])
    )

    total = len(papers_ordered)
    date_iso = date_display.replace("年", "-").replace("月", "-").replace("日", "")

    # ── Build HTML pieces ──

    # TOC items
    toc_items = []
    for i, p in enumerate(papers_ordered, 1):
        cn_title = p.get("CN_Title", p.get("Title", ""))
        is_cross = p.get("PrimaryCat") != "gr-qc"
        cross_tag = ""
        if is_cross:
            cat = p.get("PrimaryCat", "")
            cross_tag = f'<span class="toc-cross">← {html_escape_safe(cat)}</span>'
        toc_items.append(
            f'<li><a href="#paper-{i}">'
            f'<span class="toc-num">{i}.</span>'
            f'{html_escape_safe(cn_title[:60])}{"…" if len(cn_title) > 60 else ""}'
            f'</a>{cross_tag}</li>'
        )

    # Paper cards
    paper_cards = []
    for i, p in enumerate(papers_ordered, 1):
        title = html_escape_safe(p.get("Title", ""))
        cn_title = html_escape_safe(p.get("CN_Title", ""))
        cn_abstract = html_escape_safe(p.get("CN_Abstract", ""))
        cn_eval = html_escape_safe(p.get("CN_Eval", ""))
        authors = html_escape_safe(format_authors(p))
        oneline = html_escape_safe(one_liner(p.get("CN_Eval", "")))
        paper_id = p.get("ID", "")
        is_cross = p.get("PrimaryCat") != "gr-qc"
        cross_badge = (
            f' [交叉: {html_escape_safe(p.get("PrimaryCat", ""))}]'
            if is_cross else ""
        )

        card = f"""
<details class="paper-card" id="paper-{i}">
<summary>
    <div class="card-body">
        <div class="card-num">#{i}{cross_badge}  ·  {html_escape_safe(paper_id)}</div>
        <div class="card-title">{title}</div>
        <div class="card-title-cn">{cn_title}</div>
        <div class="card-authors">{authors}</div>
        <div class="card-oneline">{oneline}</div>
    </div>
</summary>
<div class="detail">
    <h4>摘要</h4>
    <p>{cn_abstract}</p>
    <h4>评价</h4>
    <p>{cn_eval}</p>
    <p style="margin-top:12px;font-size:12px;color:var(--text-secondary);">
        arXiv: <a href="https://arxiv.org/abs/{html_escape_safe(paper_id)}"
        target="_blank" rel="noopener">{html_escape_safe(paper_id)}</a>
    </p>
</div>
</details>"""
        paper_cards.append(card)

    # ── Assemble HTML ──

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>arXiv gr-qc — {date_display}</title>
<style>{CSS}</style>
<script>
window.MathJax = {{
  tex: {{ inlineMath: [['$', '$']], displayMath: [['$$', '$$']] }},
  options: {{
    skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre',
                   'code', 'summary', 'details'],
    ignoreHtmlClass: 'no-math'
  }},
  startup: {{
    ready() {{
      MathJax.startup.defaultReady();
      // Lazy typeset when <details> is toggled open
      document.querySelectorAll('details.paper-card').forEach(function(el) {{
        el.addEventListener('toggle', function() {{
          if (el.open) {{
            MathJax.typesetPromise([el.querySelector('.detail')]);
          }}
        }});
      }});
    }}
  }}
}};
</script>
<script id="MathJax-script" async
  src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js">
</script>
</head>
<body>

<!-- ── Cover ── -->
<div class="cover">
    <div class="badge">arXiv gr-qc</div>
    <h1>引力与量子宇宙学</h1>
    <p class="date">{date_display}</p>
    <div class="stats">
        <div class="stat-card">
            <div class="num">{total}</div>
            <div class="label">论文总数</div>
        </div>
        <div class="stat-card">
            <div class="num">{pc}</div>
            <div class="label">主分类 gr-qc</div>
        </div>
        <div class="stat-card">
            <div class="num">{len(cross)}</div>
            <div class="label">交叉列表</div>
        </div>
    </div>
    {f'<p style="margin-top:20px;font-size:14px;color:var(--text-secondary);">交叉来源：{cross_list_str}</p>' if cross_list_str else ''}
</div>

<!-- ── TOC ── -->
<div class="toc-section">
    <h2>📋 目录</h2>
    <ol class="toc-list">
        {''.join(toc_items)}
    </ol>
</div>

<!-- ── Papers ── -->
<div class="papers">
    {''.join(paper_cards)}
</div>

<!-- ── Footer ── -->
<div class="footer">
    Generated by WorkBuddy · arXiv gr-qc Skill · {date_display}
    · Data from <a href="https://arxiv.org" target="_blank" rel="noopener">arxiv.org</a>
</div>

</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"HTML report saved to: {output_path}")
    print(f"  Papers: {total} (primary: {pc}, cross: {len(cross)})")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(f"Usage: {sys.argv[0]} <json_path> <output_html> <date_display>")
        print(f"Example: {sys.argv[0]} ~/arxiv/all-papers.json "
              f"~/arxiv/arxiv-gr-qc-2026-06-29.html '2026年06月29日'")
        sys.exit(1)

    build_html(sys.argv[1], sys.argv[2], sys.argv[3])
