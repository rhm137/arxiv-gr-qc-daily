#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
arXiv 日报投递器（v2：折叠卡片网页 + 简讯推送）
输入：data/digest_<date>.json（AI 精读结果，含 cn_title）、data/listing_<date>.json（当日全量）
输出：
  output/digest_<date>.pushplus.html   pushplus 简讯（统计 + 清单 + 网页链接）
  site/<date>.html                     折叠卡片日报页（群版风格：封面 + 两栏目录 + <details> 卡片）
  site/index.html                      存档索引
  存档/<date>.md                       Markdown 存档
行为：
  默认向 pushplus 发送简讯；发送成功后将当日全部论文 ID 写入 data/seen_ids.json
用法：python scripts/deliver.py [--date 2026-09-16] [--no-push]
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "output"
SITE = ROOT / "site"
ARCHIVE = ROOT / "存档"
for d in (OUT, SITE, ARCHIVE):
    d.mkdir(exist_ok=True)

BJ = timezone(timedelta(hours=8))
PUSHPLUS_URL = "https://www.pushplus.plus/send"

GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ",
    "sigma": "σ", "tau": "τ", "phi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "varphi": "φ", "vartheta": "θ", "varepsilon": "ε",
}
SYMS = {
    "geq": "≥", "leq": "≤", "neq": "≠", "times": "×", "pm": "±",
    "rightarrow": "→", "to": "→", "propto": "∝", "infty": "∞",
    "partial": "∂", "nabla": "∇", "simeq": "≃", "approx": "≈",
    "cdot": "·", "circ": "∘", "oplus": "⊕", "otimes": "⊗",
}


def latex_to_text(s: str) -> str:
    """微信/pushplus 纯文本环境的极简 LaTeX -> Unicode 转换。"""
    def unwrap(m):
        return m.group(1)
    s = re.sub(r"\$\$(.+?)\$\$", unwrap, s, flags=re.S)
    s = re.sub(r"\$(.+?)\$", unwrap, s)
    s = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", s)
    s = re.sub(r"\\sqrt\{([^{}]*)\}", r"√(\1)", s)
    s = re.sub(r"\\(?:left|right)\s*", "", s)
    for name, ch in {**GREEK, **SYMS}.items():
        s = re.sub(r"\\" + name + r"\b", ch, s)
    s = re.sub(r"\\(?:bar|dot|ddot|tilde|hat|vec|overline)\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\(?:mathrm|mathbf|mathcal|mathfrak|text|rm|bf|it)\{([^{}]*)\}", r"\1", s)
    s = s.replace("~", " ").replace("\\%", "%").replace("\\&", "&").replace("\\_", "_")
    s = re.sub(r"\^\{?2\}?", "²", s)
    s = re.sub(r"\^\{?3\}?", "³", s)
    s = re.sub(r"\^\{?([^{}]{1,3})\}?", r"^\1", s)
    s = re.sub(r"_\{?([^{}]{1,4})\}?", r"_\1", s)
    s = re.sub(r"\\[,;\s]", " ", s)
    s = re.sub(r"[{}]", "", s)
    return s


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def esc_keep_math(s: str) -> str:
    """HTML 转义，但保留 $...$ 段给 MathJax。"""
    parts = s.split("$")
    out = []
    for i, part in enumerate(parts):
        out.append(html.escape(part, quote=False) if i % 2 == 0 else f"${part}$")
    return "".join(out)


def nl2p(s: str) -> str:
    return "".join(f"<p>{esc_keep_math(p)}</p>" for p in s.split("\n") if p.strip())


def score_color(score: float) -> str:
    if score >= 8:
        return "#c0392b"
    if score >= 7:
        return "#d35400"
    if score >= 6:
        return "#b7950b"
    return "#7f8c8d"


def date_cn(date: str) -> str:
    y, m, d = date.split("-")
    return f"{y}年{m}月{d}日"


# ---------------------------------------------------------------- 网页（群版风格）

SITE_CSS = r"""
:root {
    --bg: #fafaf8; --card-bg: #ffffff; --text: #2c2c2c; --text-secondary: #666;
    --accent: #2563eb; --accent-light: #eff6ff; --border: #e5e7eb;
    --tag-bg: #f3f4f6; --tag-text: #4b5563; --shadow: 0 1px 3px rgba(0,0,0,0.08);
    --radius: 8px;
    --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
    --font-mono: "SF Mono", "Fira Code", "Cascadia Code", "Consolas", monospace;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: var(--font-sans); background: var(--bg); color: var(--text);
       line-height: 1.7; -webkit-font-smoothing: antialiased; }
.cover { max-width: 800px; margin: 64px auto 48px; text-align: center; padding: 0 24px; }
.cover .badge { display: inline-block; background: var(--accent); color: #fff; font-size: 13px;
    font-weight: 600; letter-spacing: 0.08em; padding: 6px 20px; border-radius: 100px; margin-bottom: 22px; }
.cover h1 { font-size: 32px; font-weight: 700; margin-bottom: 8px; color: #111; }
.cover .date-sub { font-size: 15px; color: var(--text-secondary); margin-bottom: 32px; }
.stats { display: flex; gap: 14px; justify-content: center; flex-wrap: wrap; }
.stats .stat-card { background: var(--card-bg); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 16px 26px; min-width: 110px; box-shadow: var(--shadow); }
.stats .stat-card .num { font-size: 28px; font-weight: 700; color: var(--accent); }
.stats .stat-card .label { font-size: 13px; color: var(--text-secondary); margin-top: 2px; }
.toc-section { max-width: 800px; margin: 0 auto 40px; padding: 0 24px; }
.toc-section h2 { font-size: 19px; font-weight: 700; margin-bottom: 14px; padding-bottom: 8px;
    border-bottom: 2px solid var(--border); }
.toc-list { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 24px; list-style: none; }
.toc-list li { font-size: 14px; line-height: 1.6; }
.toc-list a { color: var(--accent); text-decoration: none; display: inline; }
.toc-list a:hover { text-decoration: underline; }
.toc-list .toc-num { font-family: var(--font-mono); font-size: 12px; color: var(--text-secondary); margin-right: 4px; }
.papers { max-width: 800px; margin: 0 auto 72px; padding: 0 24px; }
.paper-card { background: var(--card-bg); border: 1px solid var(--border); border-radius: var(--radius);
    margin-bottom: 16px; box-shadow: var(--shadow); overflow: hidden; }
.paper-card summary { padding: 18px 22px; cursor: pointer; list-style: none; display: flex;
    align-items: flex-start; gap: 12px; user-select: none; }
.paper-card summary::-webkit-details-marker { display: none; }
.paper-card summary::before { content: "▶"; font-size: 11px; color: var(--text-secondary);
    flex-shrink: 0; margin-top: 3px; transition: transform 0.2s; display: inline-block; }
.paper-card[open] summary::before { transform: rotate(90deg); }
.paper-card .card-body { flex: 1; min-width: 0; }
.paper-card .card-num { font-family: var(--font-mono); font-size: 12px; color: var(--text-secondary); margin-bottom: 4px; }
.paper-card .card-title { font-size: 16px; font-weight: 600; color: #111; margin-bottom: 2px; }
.paper-card .card-title-cn { font-size: 14px; font-weight: 500; color: var(--text); margin-bottom: 4px; }
.paper-card .card-authors { font-size: 13px; color: var(--text-secondary); margin-bottom: 4px; }
.paper-card .card-oneline { font-size: 13px; color: var(--text-secondary); }
.paper-card .detail { padding: 0 22px 22px; border-top: 1px solid var(--border); }
.paper-card .detail h4 { font-size: 14px; font-weight: 600; color: var(--accent); margin: 16px 0 6px; }
.paper-card .detail p { font-size: 14px; color: var(--text); line-height: 1.8; margin-bottom: 10px; }
.paper-card .detail .src-link { margin-top: 14px; font-size: 13px; color: var(--text-secondary); }
.paper-card .detail .src-link a { color: var(--accent); text-decoration: none; }
.abs-details { margin: 12px 0; border: 1px dashed var(--border); border-radius: 6px; padding: 8px 14px; }
.abs-details summary { font-size: 13px; color: var(--text-secondary); cursor: pointer; }
.abs-details p { font-size: 13px; color: var(--text-secondary); margin-top: 8px; }
.score-pill { display: inline-block; border-radius: 4px; color: #fff; font-size: 11px;
    font-weight: 700; padding: 0 6px; margin-left: 6px; vertical-align: 1px; }
.sec-h2 { max-width: 800px; margin: 32px auto -8px; padding: 0 24px; font-size: 19px; font-weight: 700; }
.rel-chip { display: inline-block; border-radius: 4px; color: #fff; font-size: 11px;
    font-weight: 700; padding: 0 6px; margin-left: 6px; vertical-align: 1px; }
.rel-collision { background: #c0392b; }
.rel-neighbor { background: #d35400; }
.rel-field { background: #5b7a9d; }
.paper-card.radar-collision { border-left: 4px solid #c0392b; }
.radar-note { max-width: 800px; margin: 0 auto 40px; padding: 0 24px; font-size: 13px; color: var(--text-secondary); }
.rel-note { font-size: 13px; color: var(--accent); font-weight: 600; margin: 10px 0 2px; }
.runner { max-width: 800px; margin: -48px auto 72px; padding: 0 24px; }
.runner .box { background: var(--card-bg); border: 1px solid var(--border); border-radius: var(--radius);
    box-shadow: var(--shadow); padding: 16px 22px; font-size: 13px; }
.runner h2 { font-size: 15px; margin-bottom: 8px; }
.runner li { margin: 6px 0; }
.runner a { color: var(--accent); text-decoration: none; }
.footer { text-align: center; padding: 36px 24px; font-size: 13px; color: var(--text-secondary); }
.footer a { color: var(--accent); }
@media (max-width: 600px) {
    .toc-list { grid-template-columns: 1fr; }
    .cover h1 { font-size: 24px; }
    .stats { gap: 8px; }
    .stats .stat-card { padding: 12px 16px; min-width: 90px; }
    .stats .stat-card .num { font-size: 22px; }
}
"""

MATHJAX_HEAD = r"""
<script>
window.MathJax = {
  tex: { inlineMath: [['$', '$'], ['\\(', '\\)']], displayMath: [['$$', '$$']] },
  options: { skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code', 'summary', 'details'] },
  startup: {
    ready() {
      MathJax.startup.defaultReady();
      document.querySelectorAll('details.paper-card').forEach(function(el) {
        el.addEventListener('toggle', function() {
          if (el.open) { MathJax.typesetPromise([el.querySelector('.detail')]); }
        });
      });
    }
  }
};
</script>
<script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
"""


REL_CLASS = {"撞车预警": "rel-collision", "近邻": "rel-neighbor", "风向": "rel-field"}


def render_site_page(digest: dict, counts: dict, abstracts: dict) -> str:
    date = digest["listing_date"]
    st = digest["stats"]
    n_core = st.get("core", st.get("selected", len(digest["papers"])))
    radar = digest.get("radar", [])
    n_must = sum(1 for p in digest["papers"] if p.get("must_read"))

    toc_items = []
    cards = []
    for i, p in enumerate(digest["papers"], 1):
        star = "⭐" if p.get("must_read") else ""
        col = score_color(p["score"])
        cn = p.get("cn_title", "")
        toc_title = cn or p["title"]
        toc_items.append(
            f'<li><span class="toc-num">{i}.</span>{star}<a href="#paper-{i}">'
            f'{esc(toc_title[:48])}{"…" if len(toc_title) > 48 else ""}</a>'
            f'<span class="score-pill" style="background:{col}">{p["score"]}</span></li>'
        )
        abs_block = ""
        if p["id"] in abstracts:
            abs_block = (
                f'<details class="abs-details"><summary>英文摘要原文</summary>'
                f'<p>{esc_keep_math(abstracts[p["id"]])}</p></details>'
            )
        cards.append(f"""
<details class="paper-card" id="paper-{i}">
<summary>
    <div class="card-body">
        <div class="card-num">#{i} · arXiv:{p['id']} · {' · '.join(p['categories'])}{' · ⭐必读' if p.get('must_read') else ''}<span class="score-pill" style="background:{col}">{p['score']}</span></div>
        <div class="card-title">{esc(p['title'])}</div>
        <div class="card-title-cn">{esc(cn)}</div>
        <div class="card-authors">{esc(p['authors'])}</div>
        <div class="card-oneline">💡 {esc(p['one_liner'])}</div>
    </div>
</summary>
<div class="detail">
    <h4>📌 做了什么与评价</h4>
    {nl2p(p['did_and_eval'])}
    <h4>🎓 Rao 可以学到什么</h4>
    {nl2p(p['learn'])}
    <h4>💡 推荐课题</h4>
    {nl2p(p['topics'])}
    {abs_block}
    <p class="src-link">原文链接：<a href="{p['link']}" target="_blank" rel="noopener">arXiv:{p['id']}</a></p>
</div>
</details>""")

    # ---------- 领域动态雷达区 ----------
    radar_toc, radar_cards = [], []
    for j, r in enumerate(radar, 1):
        col = score_color(r["score"])
        label = r.get("relation_label", "风向")
        rel_cls = REL_CLASS.get(label, "rel-field")
        card_cls = "paper-card radar-collision" if label == "撞车预警" else "paper-card"
        fields = "/".join(r.get("radar_fields", []))
        toc_title = r.get("cn_title") or r["title"]
        radar_toc.append(
            f'<li><span class="toc-num">R{j}.</span><a href="#radar-{j}">'
            f'{esc(toc_title[:46])}{"…" if len(toc_title) > 46 else ""}</a>'
            f'<span class="rel-chip {rel_cls}">{label}</span></li>'
        )
        action_block = ""
        if r.get("action") and r["action"] != "暂无":
            action_block = f'<p class="rel-note">⚠️ 建议行动</p><p>{esc(r["action"])}</p>'
        abs_block = ""
        if r["id"] in abstracts:
            abs_block = (
                f'<details class="abs-details"><summary>英文摘要原文</summary>'
                f'<p>{esc_keep_math(abstracts[r["id"]])}</p></details>'
            )
        radar_cards.append(f"""
<details class="{card_cls}" id="radar-{j}">
<summary>
    <div class="card-body">
        <div class="card-num">R{j} · arXiv:{r['id']} · {esc(fields)}<span class="rel-chip {rel_cls}">{label}</span><span class="score-pill" style="background:{col}">{r['score']}</span></div>
        <div class="card-title">{esc(r['title'])}</div>
        <div class="card-title-cn">{esc(r.get('cn_title',''))}</div>
        <div class="card-authors">{esc(r['authors'])}</div>
        <div class="card-oneline">💡 {esc(r['one_liner'])}</div>
    </div>
</summary>
<div class="detail">
    <p class="rel-note">🔗 与 Rao 的关系</p>
    <p>{esc(r.get('relation_note','同领域'))}</p>
    <h4>简评</h4>
    {nl2p(r.get('brief','暂无'))}
    {action_block}
    {abs_block}
    <p class="src-link">原文链接：<a href="{r['link']}" target="_blank" rel="noopener">arXiv:{r['id']}</a></p>
</div>
</details>""")

    radar_section = ""
    if radar_cards:
        radar_section = (
            '<h2 class="sec-h2">📡 领域动态 · 修正引力宇宙学</h2>'
            '<div class="papers">' + "".join(radar_cards) + "</div>"
        )
    elif digest.get("radar_note"):
        radar_section = f'<div class="radar-note">📡 领域动态：{esc(digest["radar_note"])}</div>'

    runner = ""
    if digest.get("runner_ups"):
        lis = "".join(
            f'<li>{i}. <a href="https://arxiv.org/abs/{r["id"]}" target="_blank" rel="noopener">'
            f'{esc(r.get("cn_title") or r["title"])}</a> — {esc(r["one_liner"])}</li>'
            for i, r in enumerate(digest["runner_ups"], len(digest["papers"]) + 1)
        )
        runner = f'<div class="runner"><div class="box"><h2>📎 也值得关注</h2><ol style="padding-left:18px;margin:0;">{lis}</ol></div></div>'

    toc_radar = ""
    if radar_toc:
        toc_radar = '<h2 style="margin-top:20px;">📡 领域动态</h2><ol class="toc-list">' + "".join(radar_toc) + "</ol>"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>arXiv · {date}</title>
<style>{SITE_CSS}</style>
{MATHJAX_HEAD}
</head>
<body>

<div class="cover">
    <div class="badge">arXiv</div>
    <h1>arXiv · {date_cn(date)}</h1>
    <p class="date-sub">gr-qc · hep-th · astro-ph.CO 每日精选　|　按 Rao 的研究画像匹配</p>
    <div class="stats">
        <div class="stat-card"><div class="num">{st['total_new']}</div><div class="label">新上线</div></div>
        <div class="stat-card"><div class="num">{st['candidates']}</div><div class="label">初筛入围</div></div>
        <div class="stat-card"><div class="num">{n_core}</div><div class="label">核心精读（⭐{n_must}）</div></div>
        <div class="stat-card"><div class="num">{len(radar)}</div><div class="label">领域动态</div></div>
    </div>
    <p style="margin-top:18px;font-size:13px;color:var(--text-secondary);">来源：gr-qc {counts.get('gr-qc',0)} · hep-th {counts.get('hep-th',0)} · astro-ph.CO {counts.get('astro-ph.CO',0)}</p>
</div>

<div class="toc-section">
    <h2>📋 核心精读</h2>
    <ol class="toc-list">
        {''.join(toc_items)}
    </ol>
    {toc_radar}
</div>

<div class="papers">
    {''.join(cards)}
</div>

{radar_section}

{runner}

<div class="footer">
    由 Kimi 生成 · <a href="index.html">存档索引</a> · {date_cn(date)} · 数据来自 <a href="https://arxiv.org" target="_blank" rel="noopener">arxiv.org</a>
</div>

</body>
</html>"""


def render_archive_index(days: list[dict]) -> str:
    rows = "".join(
        f'<tr><td><a href="{d["date"]}.html">{d["date"]}</a></td><td>{d["stats"]["total_new"]}</td>'
        f'<td>{d["stats"].get("core", d["stats"].get("selected", 0))}+{d["stats"].get("radar", 0)}</td><td>{esc("、".join(d.get("must_titles", []))[:100])}</td></tr>'
        for d in days
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>arXiv · 存档索引</title>
<style>{SITE_CSS}
table{{width:100%;border-collapse:collapse;background:var(--card-bg);border-radius:8px;overflow:hidden;box-shadow:var(--shadow)}}
td,th{{border:1px solid var(--border);padding:8px 12px;font-size:14px;text-align:left}}a{{color:var(--accent);text-decoration:none}}
.idx{{max-width:800px;margin:48px auto;padding:0 24px}}</style></head>
<body><div class="idx">
<h1 style="font-size:24px;margin-bottom:16px;">📚 arXiv 日报存档</h1>
<table><tr><th>日期</th><th>新上线</th><th>核心+动态</th><th>必读</th></tr>{rows}</table>
</div></body></html>"""


# ---------------------------------------------------------------- pushplus 简讯

def render_pushplus(digest: dict, counts: dict, site_url: str = "") -> str:
    date = digest["listing_date"]
    st = digest["stats"]
    n_core = st.get("core", st.get("selected", len(digest["papers"])))
    radar = digest.get("radar", [])
    n_must = sum(1 for p in digest["papers"] if p.get("must_read"))
    items = []
    for i, p in enumerate(digest["papers"], 1):
        star = "⭐" if p.get("must_read") else ""
        col = score_color(p["score"])
        items.append(
            f'<div style="margin:10px 0;padding:8px 12px;border-left:4px solid {col};background:#fafafa;border-radius:6px;">'
            f'<div style="font-size:14px;font-weight:700;">{star}{i}. {esc(p.get("cn_title") or latex_to_text(p["title"]))}'
            f'<span style="color:{col};font-size:12px;">　[{p["score"]}]</span></div>'
            f'<div style="font-size:12px;color:#666;margin-top:2px;">{esc(latex_to_text(p["title"]))}</div>'
            f'<div style="font-size:12px;color:#666;">{esc(p["authors"])}</div>'
            f'<div style="font-size:13px;margin-top:3px;">💡 {esc(p["one_liner"])}</div>'
            f'</div>'
        )
    radar_block = ""
    if radar:
        ris = []
        for j, r in enumerate(radar, 1):
            label = r.get("relation_label", "风向")
            ris.append(
                f'<div style="margin:6px 0;font-size:12px;">'
                f'<b>R{j}. {esc(r.get("cn_title") or latex_to_text(r["title"]))}</b>　'
                f'<span style="color:#c0392b;">[{label}]</span><br>'
                f'<span style="color:#666;">💡 {esc(r["one_liner"])}</span></div>'
            )
        radar_block = (
            '<div style="margin:14px 0;padding:10px 12px;background:#f0f4fa;border-radius:8px;">'
            '<div style="font-size:13px;font-weight:700;margin-bottom:6px;">📡 领域动态 · 修正引力宇宙学</div>'
            + "".join(ris) + "</div>"
        )
    elif digest.get("radar_note"):
        radar_block = f'<div style="font-size:11px;color:#999;margin:10px 0;">📡 {esc(digest["radar_note"])}</div>'
    link_block = ""
    if site_url:
        link_block = (
            f'<div style="text-align:center;margin:16px 0;">'
            f'<a href="{site_url}" style="display:inline-block;background:#2563eb;color:#fff;font-size:15px;'
            f'font-weight:700;text-decoration:none;padding:10px 26px;border-radius:8px;">'
            f'👉 打开今日完整页面（折叠卡片）</a></div>'
            f'<div style="font-size:11px;color:#999;text-align:center;">核心层的「做了什么与评价 / Rao 可以学到什么 / 推荐课题」与雷达层简评都在网页里，点开卡片即读</div>'
        )
    return (
        '<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:640px;margin:0 auto;color:#2c3e50;">'
        f'<div style="background:#2563eb;color:#fff;padding:12px 16px;border-radius:10px;">'
        f'<div style="font-size:17px;font-weight:700;">arXiv · {date}</div>'
        f'<div style="font-size:12px;opacity:.9;margin-top:3px;">gr-qc {counts.get("gr-qc",0)} · hep-th {counts.get("hep-th",0)} · astro-ph.CO {counts.get("astro-ph.CO",0)}'
        f'　→ 新上线 {st["total_new"]} → 初筛 {st["candidates"]} → 核心 {n_core}（⭐{n_must}）+ 动态 {len(radar)}</div></div>'
        + "".join(items)
        + radar_block
        + link_block
        + '<div style="font-size:11px;color:#999;text-align:center;margin:12px 0;">由 Kimi 生成 · 按 Rao 的研究画像匹配</div></div>'
    )


# ---------------------------------------------------------------- Markdown 存档

def render_markdown(digest: dict, counts: dict) -> str:
    date = digest["listing_date"]
    st = digest["stats"]
    n_core = st.get("core", st.get("selected", len(digest["papers"])))
    radar = digest.get("radar", [])
    lines = [
        f"# arXiv · {date}",
        "",
        f"> gr-qc {counts.get('gr-qc',0)} · hep-th {counts.get('hep-th',0)} · astro-ph.CO {counts.get('astro-ph.CO',0)}"
        f"　→ 新上线 {st['total_new']} 篇 → 初筛 {st['candidates']} 篇 → 核心 {n_core} 篇 + 领域动态 {len(radar)} 篇",
        "",
        "## 核心精读",
        "",
    ]
    for i, p in enumerate(digest["papers"], 1):
        star = "⭐必读 " if p.get("must_read") else ""
        cn = p.get("cn_title", "")
        lines += [
            f"### {star}{i}. [{p['title']}]({p['link']})",
            "",
            (f"**{cn}**　｜　" if cn else "") + f"**{p['authors']}**　|　{' · '.join(p['categories'])}　|　相关度 **{p['score']}**",
            "",
            f"💡 {p['one_liner']}",
            "",
            "**做了什么与评价**",
            "",
            p["did_and_eval"],
            "",
            "**Rao 可以学到什么**",
            "",
            p["learn"],
            "",
            "**推荐课题**",
            "",
            p["topics"],
            "",
            "---",
            "",
        ]
    if radar:
        lines += ["## 📡 领域动态 · 修正引力宇宙学", ""]
        for j, r in enumerate(radar, 1):
            lines += [
                f"### R{j}. [{r.get('cn_title') or r['title']}]({r['link']})　【{r.get('relation_label','风向')}】",
                "",
                f"**{r['authors']}**　|　{'/'.join(r.get('radar_fields', []))}　|　相关度 **{r['score']}**",
                "",
                f"💡 {r['one_liner']}",
                "",
                f"🔗 与 Rao 的关系：{r.get('relation_note','同领域')}",
                "",
                r.get("brief", "暂无"),
                "",
            ]
            if r.get("action") and r["action"] != "暂无":
                lines += [f"⚠️ 建议行动：{r['action']}", ""]
            lines += ["---", ""]
    elif digest.get("radar_note"):
        lines += [f"📡 领域动态：{digest['radar_note']}", ""]
    if digest.get("runner_ups"):
        lines.append("## 📎 也值得关注")
        lines.append("")
        for r in digest["runner_ups"]:
            lines.append(f"- [{r.get('cn_title') or r['title']}](https://arxiv.org/abs/{r['id']})：{r['one_liner']}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程

def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def get_secret(name: str, default=None):
    """优先读环境变量，回落到本地 config.local.json（GitHub Actions 中用 Secrets 注入环境变量）。"""
    v = os.environ.get(name)
    if v:
        return v
    p = ROOT / "config.local.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")).get(name, default)
        except Exception:  # noqa: BLE001
            return default
    return default


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()
    date = args.date or (datetime.now(BJ) - timedelta(hours=8)).date().isoformat()

    digest_path = DATA / f"digest_{date}.json"
    if not digest_path.exists():
        print(json.dumps({"ok": True, "pushed": False, "note": "无 digest（昨日无更新或无候选），静默跳过"}, ensure_ascii=False))
        return 0
    digest = load_json(digest_path)
    listing = load_json(DATA / f"listing_{date}.json")
    cfg = load_json(ROOT / "config.json")

    counts = {"gr-qc": 0, "hep-th": 0, "astro-ph.CO": 0}
    abstracts = {}
    for p in listing["papers"]:
        for c in p["source"].split("+"):
            if c in counts:
                counts[c] += 1
        abstracts[p["id"]] = p.get("abstract", "")

    site_base = cfg.get("site_base_url", "").rstrip("/")
    site_url = f"{site_base}/{date}.html" if site_base else ""

    pp_html = render_pushplus(digest, counts, site_url)
    (OUT / f"digest_{date}.pushplus.html").write_text(pp_html, encoding="utf-8")

    (SITE / f"{date}.html").write_text(render_site_page(digest, counts, abstracts), encoding="utf-8")
    (ARCHIVE / f"{date}.md").write_text(render_markdown(digest, counts), encoding="utf-8")

    days = []
    for f in sorted(SITE.glob("2*.html"), reverse=True):
        d = f.stem
        try:
            dg = load_json(DATA / f"digest_{d}.json")
            days.append({
                "date": d,
                "stats": dg["stats"],
                "must_titles": [p.get("cn_title") or p["title"] for p in dg["papers"] if p.get("must_read")],
            })
        except Exception:  # noqa: BLE001
            continue
    (SITE / "index.html").write_text(render_archive_index(days), encoding="utf-8")

    if len(pp_html) > cfg.get("pushplus_max_chars", 19000):
        print(json.dumps({"ok": False, "error": f"pushplus html too long: {len(pp_html)} chars"}, ensure_ascii=False))
        return 1

    if args.no_push:
        print(json.dumps({"ok": True, "pushed": False, "chars": len(pp_html), "site_url": site_url}, ensure_ascii=False))
        return 0

    token = get_secret("PUSHPLUS_TOKEN")
    if not token:
        print(json.dumps({"ok": False, "error": "缺少 PUSHPLUS_TOKEN"}, ensure_ascii=False))
        return 1
    st = digest["stats"]
    n_core = st.get("core", st.get("selected", len(digest["papers"])))
    title = f"arXiv · {date}｜新{st['total_new']}篇→核心{n_core}+动态{st.get('radar', 0)}篇"
    resp = requests.post(PUSHPLUS_URL, json={
        "token": token, "title": title[:95], "content": pp_html, "template": "html",
    }, timeout=40)
    try:
        rj = resp.json()
    except Exception:  # noqa: BLE001
        rj = {"code": -1, "raw": resp.text[:200]}
    if rj.get("code") != 200:
        print(json.dumps({"ok": False, "pushed": False, "response": rj}, ensure_ascii=False))
        return 1

    seen_path = DATA / "seen_ids.json"
    seen = set(json.loads(seen_path.read_text(encoding="utf-8"))) if seen_path.exists() else set()
    seen.update(p["id"] for p in listing["papers"])
    seen_path.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=0), encoding="utf-8")

    print(json.dumps({"ok": True, "pushed": True, "chars": len(pp_html), "seen_total": len(seen)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
