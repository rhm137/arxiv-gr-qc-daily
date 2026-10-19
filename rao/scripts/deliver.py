#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
arXiv 日报投递器（v4：群推送画风 · 三层结构 · 6 天滚动窗口）
输入：data/digest_<date>.json、data/listing_<date>.json
输出：
  output/digest_<date>.pushplus.html   pushplus 简讯（链接置顶 + 每篇深度链接）
  site/<date>.html                     日报页（群推送扁平卡片风）
  site/index.html                      存档索引（最近 6 天卡片）
  存档/<date>.md                       Markdown 存档
行为：
  网页只保留最近 KEEP_DAYS（默认 6）天，每天运行时重渲窗口内全部页面并裁掉更旧的页面；
  默认向 pushplus 发送简讯；发送成功后将当日全部论文 ID 写入 data/seen_ids.json
用法：python scripts/deliver.py [--date 2026-09-16] [--no-push] [--force]
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

# ── 滚动窗口：网页只保留最近 N 天（与群推送 KEEP_DAYS=6 对齐）──
# 每天运行时重渲窗口内全部日报页与 index.html（digest/listing 都在 data/ 里，
# 重渲是纯函数），窗口外页面从 site/ 删除；部署为整站镜像，线上随之消失。
KEEP_DAYS = int(os.environ.get("RAO_KEEP_DAYS", "6"))

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


def date_cn(date: str) -> str:
    y, m, d = date.split("-")
    return f"{y}年{m}月{d}日"


def score_cls(score: float) -> str:
    if score >= 8:
        return "s8"
    if score >= 7:
        return "s7"
    if score >= 6:
        return "s6"
    return "s5"


def score_color(score: float) -> str:
    if score >= 8:
        return "#dc2626"
    if score >= 7:
        return "#d97706"
    if score >= 6:
        return "#059669"
    return "#6b7280"


REL_CLS = {"撞车预警": "rel-collision", "近邻": "rel-neighbor", "风向": "rel-field"}

# ---------------------------------------------------------------- 网页（群推送扁平卡片风 v4）

SITE_CSS = r"""
:root {
    --bg:#fafaf8; --card-bg:#ffffff; --text:#2c2c2c; --text-secondary:#666;
    --accent:#2563eb; --accent-light:#eff6ff; --border:#e5e7eb;
    --tag-bg:#f3f4f6; --tag-text:#4b5563;
    --must:#dc2626; --s7:#d97706; --s6:#059669; --s5:#6b7280;
    --shadow:0 1px 3px rgba(0,0,0,0.08); --radius:8px;
    --font-sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans SC","PingFang SC","Microsoft YaHei",sans-serif;
    --font-mono:"SF Mono","Fira Code","Cascadia Code","Consolas",monospace;
}
* { box-sizing:border-box; margin:0; padding:0; }
html { scroll-behavior:smooth; }
body { font-family:var(--font-sans); background:var(--bg); color:var(--text);
       line-height:1.7; -webkit-font-smoothing:antialiased; }
.cover { max-width:800px; margin:64px auto 48px; text-align:center; padding:0 24px; }
.cover .badge { display:inline-block; background:var(--accent); color:#fff; font-size:13px;
    font-weight:600; letter-spacing:.08em; padding:6px 20px; border-radius:100px; margin-bottom:24px; }
.cover h1 { font-size:36px; font-weight:700; margin-bottom:8px; color:#111; }
.cover .date { font-size:18px; color:var(--text-secondary); margin-bottom:32px; }
.stats { display:flex; gap:16px; justify-content:center; flex-wrap:wrap; }
.stat-card { background:var(--card-bg); border:1px solid var(--border); border-radius:var(--radius);
    padding:20px 32px; min-width:140px; box-shadow:var(--shadow); }
.stat-card .num { font-size:32px; font-weight:700; color:var(--accent); }
.stat-card .label { font-size:13px; color:var(--text-secondary); margin-top:4px; }
.cover .src { margin-top:20px; font-size:13px; color:var(--text-secondary); }
.toc-section { max-width:800px; margin:0 auto 40px; padding:0 24px; }
.toc-section h2 { font-size:20px; font-weight:700; color:#111; margin-bottom:8px;
    padding-bottom:8px; border-bottom:2px solid var(--border); }
.toc-group { font-size:13px; font-weight:600; color:var(--text-secondary); letter-spacing:.06em; margin:16px 0 10px; }
.toc-group .en { font-weight:400; margin-left:6px; letter-spacing:.1em; }
.toc-list { display:grid; grid-template-columns:1fr 1fr; gap:6px 24px; list-style:none; }
.toc-list li { font-size:14px; line-height:1.6; }
.toc-list a { color:var(--accent); text-decoration:none; display:flex; align-items:baseline; gap:6px; }
.toc-list a:hover { text-decoration:underline; }
.toc-list .toc-num { font-family:var(--font-mono); font-size:12px; color:var(--text-secondary);
    min-width:28px; flex-shrink:0; }
.papers { max-width:800px; margin:0 auto 16px; padding:0 24px; }
.sec-head { max-width:800px; margin:48px auto 16px; padding:0 24px; }
.sec-head h2 { font-size:20px; font-weight:700; color:#111; padding-bottom:8px; border-bottom:2px solid var(--border); }
.sec-head .sub { font-size:13px; color:var(--text-secondary); margin-top:6px; }
.paper-card { background:var(--card-bg); border:1px solid var(--border); border-radius:var(--radius);
    margin-bottom:16px; box-shadow:var(--shadow); overflow:hidden; }
.paper-card summary { padding:20px 24px; cursor:pointer; list-style:none; display:flex;
    align-items:flex-start; gap:12px; user-select:none; }
.paper-card summary::-webkit-details-marker { display:none; }
.paper-card summary::before { content:"▶"; font-size:11px; color:var(--text-secondary);
    flex-shrink:0; margin-top:5px; transition:transform .2s; display:inline-block; }
.paper-card[open] summary::before { transform:rotate(90deg); }
.paper-card .card-body { flex:1; min-width:0; }
.paper-card .card-num { font-family:var(--font-mono); font-size:12px; color:var(--text-secondary); margin-bottom:4px; }
.paper-card .card-title { font-size:16px; font-weight:600; color:#111; margin-bottom:4px; line-height:1.5; }
.paper-card .card-title-cn { font-size:14px; font-weight:500; color:var(--text); margin-bottom:4px; }
.paper-card .card-authors { font-size:13px; color:var(--text-secondary); margin-bottom:4px; }
.paper-card .card-oneline { font-size:13px; color:var(--text-secondary); }
.paper-card .c-side { display:flex; flex-direction:column; align-items:flex-end; gap:6px; flex-shrink:0; padding-top:2px; }
.score { font-size:12px; font-weight:700; border:1px solid currentColor; border-radius:100px;
    padding:1px 10px; background:#fff; }
.s8 { color:var(--must); } .s7 { color:var(--s7); } .s6 { color:var(--s6); } .s5 { color:var(--s5); }
.must { background:var(--must); color:#fff; font-size:11px; font-weight:700; border-radius:4px; padding:1px 8px; }
.rel { font-size:11px; font-weight:700; border-radius:4px; padding:1px 8px; border:1px solid currentColor; }
.rel-collision { color:var(--must); } .rel-neighbor { color:var(--s7); } .rel-field { color:var(--s5); }
.kw { font-size:11px; font-weight:700; border-radius:4px; padding:1px 8px;
    border:1px solid var(--accent); color:var(--accent); }
.paper-card.collision { border-left:3px solid var(--must); }
.paper-card .detail { padding:0 24px 24px; border-top:1px solid var(--border); }
.paper-card .detail h4 { font-size:14px; font-weight:600; color:var(--accent); margin:20px 0 8px; }
.paper-card .detail p { font-size:14px; color:var(--text); line-height:1.8; margin-bottom:12px; }
.detail .action-box { background:#fef2f2; border:1px solid #fecaca; border-radius:var(--radius);
    padding:10px 14px; margin:12px 0; font-size:13.5px; }
.abs-details { margin:14px 0 4px; border:1px dashed var(--border); border-radius:var(--radius); padding:8px 14px; }
.abs-details summary { font-size:12.5px; color:var(--text-secondary); cursor:pointer; padding:0; display:block; border:none; }
.abs-details summary::before { content:""; }
.abs-details p { font-size:13px; color:var(--text-secondary); margin-top:8px; }
.src-link { margin-top:16px; font-size:13px; color:var(--text-secondary); }
.src-link a { color:var(--accent); text-decoration:none; }
.radar-note { max-width:800px; margin:0 auto 24px; padding:0 24px; font-size:13px; color:var(--text-secondary); }
.archives { max-width:800px; margin:24px auto 0; padding:0 24px; }
.archives-inner { background:var(--card-bg); border:1px solid var(--border); border-radius:var(--radius);
    box-shadow:var(--shadow); padding:20px 24px; text-align:center; }
.archives h3 { font-size:15px; font-weight:600; color:#111; margin-bottom:14px; }
.arc-list { display:flex; flex-wrap:wrap; gap:10px; justify-content:center; }
.arc-item { display:inline-block; font-size:14px; padding:8px 16px; border-radius:100px;
    text-decoration:none; color:var(--accent); background:var(--accent-light); border:1px solid #dbeafe; }
a.arc-item:hover { background:#dbeafe; }
.arc-item.arc-current { color:#fff; background:var(--accent); border-color:var(--accent); font-weight:600; }
.arc-count { font-size:12px; color:var(--text-secondary); margin-left:6px; font-weight:400; }
.arc-item.arc-current .arc-count { color:#bfdbfe; }
.footer { text-align:center; padding:40px 24px; font-size:13px; color:var(--text-secondary); }
.footer a { color:var(--accent); text-decoration:none; }
@media (max-width:600px) {
    .toc-list { grid-template-columns:1fr; }
    .cover h1 { font-size:26px; }
    .stats { gap:8px; }
    .stat-card { padding:14px 20px; min-width:100px; }
    .stat-card .num { font-size:24px; }
    .paper-card summary { padding:16px 18px; }
    .paper-card .detail { padding:0 18px 18px; }
}
"""

INDEX_CSS = r"""
* { box-sizing:border-box; margin:0; padding:0; }
:root { --bg:#f8f9fa; --text:#1a1a2e; --text-secondary:#6b7280; --accent:#2563eb;
  --accent-light:#eff6ff; --border:#e5e7eb;
  --font-sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans SC","PingFang SC","Microsoft YaHei",sans-serif; }
body { font-family:var(--font-sans); background:var(--bg); color:var(--text); min-height:100vh;
    display:flex; flex-direction:column; align-items:center; padding:60px 24px 40px; }
.header { text-align:center; margin-bottom:40px; }
.header h1 { font-size:32px; font-weight:700; margin-bottom:8px; color:#111; }
.header .date { font-size:16px; color:var(--text-secondary); }
.grid { display:flex; gap:20px; max-width:900px; width:100%; flex-wrap:wrap; justify-content:center; }
.card { background:#fff; border-radius:16px; box-shadow:0 4px 24px rgba(0,0,0,0.06);
    padding:28px 26px; flex:1; min-width:250px; max-width:280px; text-decoration:none;
    color:inherit; transition:transform .2s, box-shadow .2s; text-align:center; }
.card:hover { transform:translateY(-4px); box-shadow:0 8px 32px rgba(0,0,0,0.1); }
.card .emoji { font-size:40px; margin-bottom:12px; display:block; }
.card .cat-badge { display:inline-block; font-size:13px; font-weight:600; letter-spacing:.06em;
    padding:4px 14px; border-radius:100px; margin-bottom:12px; color:#fff; background:var(--accent); }
.card h2 { font-size:20px; font-weight:700; margin-bottom:8px; color:#111; }
.card .daycount { font-size:15px; font-weight:700; color:var(--accent); margin-bottom:6px; }
.card .must-line { font-size:13px; color:var(--must,#dc2626); font-weight:600; margin-bottom:6px; }
.card .desc { font-size:13px; color:var(--text-secondary); line-height:1.5; }
.footer { margin-top:48px; text-align:center; font-size:13px; color:var(--text-secondary); }
.footer a { color:var(--accent); text-decoration:none; }
@media (max-width:600px) { .header h1 { font-size:26px; } }
"""

MATHJAX_HEAD = r"""
<script>
window.MathJax = {
  tex: { inlineMath: [['$', '$'], ['\\(', '\\)']], displayMath: [['$$', '$$']] },
  options: { skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code', 'summary', 'details'] },
  startup: { ready() {
      MathJax.startup.defaultReady();
      document.querySelectorAll('details.paper-card').forEach(function(el) {
        el.addEventListener('toggle', function() {
          if (el.open) { MathJax.typesetPromise([el.querySelector('.detail')]); }
        });
      });
  } }
};
</script>
<script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
"""


def _abs_block(abstracts: dict, pid: str) -> str:
    if pid in abstracts and abstracts[pid]:
        return (f'<details class="abs-details"><summary>英文摘要原文</summary>'
                f'<p>{esc_keep_math(abstracts[pid])}</p></details>')
    return ""


def _group_detail(item: dict, abstracts: dict, show_action: bool) -> str:
    """群格式详情区：速览 + 评价（【创新点】【局限性】【对 Rao 的意义】）。"""
    parts = []
    if item.get("cn_summary"):
        parts.append(f"<h4>速览</h4><p>{esc_keep_math(item['cn_summary'])}</p>")
    if item.get("cn_review"):
        parts.append(f"<h4>评价</h4><p>{esc_keep_math(item['cn_review'])}</p>")
    if show_action and item.get("action") and item["action"] != "暂无":
        parts.append(f'<div class="action-box">⚠️ <b>建议行动</b>　{esc(item["action"])}</div>')
    return "\n".join(parts)


def _short_date(date: str) -> str:
    """'2026-10-16' → '10月16日'"""
    return f"{int(date[5:7])}月{int(date[8:10])}日"


def _archive_nav_html(days: list[dict], current_date: str) -> str:
    """日期切换导航（与群推送批次导航同款）：窗口内全部天数（新→旧），当前天高亮不可点。"""
    items = []
    for i, d in enumerate(days):
        txt = _short_date(d["date"]) + ("（今天）" if i == 0 else "")
        cnt = f'<span class="arc-count">{d["total"]} 篇</span>'
        if d["date"] == current_date:
            items.append(f'<span class="arc-item arc-current">{txt}{cnt}</span>')
        else:
            items.append(f'<a class="arc-item" href="{d["date"]}.html">{txt}{cnt}</a>')
    return ('<div class="archives"><div class="archives-inner">'
            f'<h3>📚 最近 {len(days)} 天（滚动保留 {KEEP_DAYS} 天）</h3>'
            f'<div class="arc-list">{"".join(items)}</div></div></div>')


def render_site_page(digest: dict, counts: dict, abstracts: dict,
                     days: list[dict] | None = None) -> str:
    date = digest["listing_date"]
    st = digest["stats"]
    radar = digest.get("radar", [])
    keyword = digest.get("keyword", [])
    n_core = st.get("core", len(digest["papers"]))
    n_must = sum(1 for p in digest["papers"] if p.get("must_read"))

    # ---- 核心层 ----
    toc_core, cards = [], []
    for i, p in enumerate(digest["papers"], 1):
        cn = p.get("cn_title", "")
        toc_core.append(
            f'<li><a href="#paper-{i}"><span class="toc-num">{i:02d}.</span>{esc(cn or p["title"])}</a></li>'
        )
        must = '<span class="must">必读</span>' if p.get("must_read") else ""
        cards.append(f"""
<details class="paper-card" id="paper-{i}">
<summary>
  <div class="card-body">
    <div class="card-num">#{i} · arXiv:{p['id']} · {' · '.join(p['categories'])}</div>
    <div class="card-title">{esc(p['title'])}</div>
    <div class="card-title-cn">{esc(cn)}</div>
    <div class="card-authors">{esc(p['authors'])}</div>
    <div class="card-oneline">💡 {esc(p['one_liner'])}</div>
  </div>
  <div class="c-side">
    <span class="score {score_cls(p['score'])}">{p['score']}</span>
    {must}
  </div>
</summary>
<div class="detail">
  <h4>速览</h4>
  <p>{esc_keep_math(p.get('cn_summary',''))}</p>
  <h4>评价</h4>
  <p>{esc_keep_math(p.get('cn_review',''))}</p>
  <h4>🎓 Rao 可以学到什么</h4>
  {nl2p(p['learn'])}
  <h4>💡 推荐课题</h4>
  {nl2p(p['topics'])}
  {_abs_block(abstracts, p['id'])}
  <p class="src-link">原文链接：<a href="{p['link']}" target="_blank" rel="noopener">arXiv:{p['id']}</a></p>
</div>
</details>""")

    # ---- 雷达层（群格式） ----
    toc_radar, rcards = [], []
    for j, r in enumerate(radar, 1):
        label = r.get("relation_label", "风向")
        toc_radar.append(
            f'<li><a href="#radar-{j}"><span class="toc-num">R{j}</span>{esc(r.get("cn_title") or r["title"])}</a></li>'
        )
        rcards.append(f"""
<details class="paper-card {'collision' if label=='撞车预警' else ''}" id="radar-{j}">
<summary>
  <div class="card-body">
    <div class="card-num">R{j} · arXiv:{r['id']} · {esc('/'.join(r.get('radar_fields', [])))}</div>
    <div class="card-title">{esc(r['title'])}</div>
    <div class="card-title-cn">{esc(r.get('cn_title',''))}</div>
    <div class="card-authors">{esc(r['authors'])}</div>
    <div class="card-oneline">💡 {esc(r['one_liner'])}</div>
  </div>
  <div class="c-side">
    <span class="score {score_cls(r['score'])}">{r['score']}</span>
    <span class="rel {REL_CLS.get(label,'rel-field')}">{label}</span>
  </div>
</summary>
<div class="detail">
  {_group_detail(r, abstracts, show_action=True)}
  <p class="src-link">原文链接：<a href="{r['link']}" target="_blank" rel="noopener">arXiv:{r['id']}</a></p>
</div>
</details>""")

    radar_block = ""
    if rcards:
        radar_block = ('<div class="sec-head"><h2>📡 领域动态 · 修正引力宇宙学</h2></div>'
                       '<div class="papers">' + "".join(rcards) + "</div>")
    elif digest.get("radar_note"):
        radar_block = f'<div class="radar-note">📡 领域动态：{esc(digest["radar_note"])}</div>'

    # ---- 名词推荐层（群格式） ----
    toc_kw, kcards = [], []
    for k, w in enumerate(keyword, 1):
        kws = " · ".join(w.get("matched_keywords", []))
        toc_kw.append(
            f'<li><a href="#kw-{k}"><span class="toc-num">K{k}</span>{esc(w.get("cn_title") or w["title"])}</a></li>'
        )
        kcards.append(f"""
<details class="paper-card" id="kw-{k}">
<summary>
  <div class="card-body">
    <div class="card-num">K{k} · arXiv:{w['id']} · {' · '.join(w['categories'])}</div>
    <div class="card-title">{esc(w['title'])}</div>
    <div class="card-title-cn">{esc(w.get('cn_title',''))}</div>
    <div class="card-authors">{esc(w['authors'])}</div>
    <div class="card-oneline">💡 {esc(w['one_liner'])}</div>
  </div>
  <div class="c-side">
    <span class="kw">关键词：{esc(kws)}</span>
  </div>
</summary>
<div class="detail">
  {_group_detail(w, abstracts, show_action=False)}
  <p class="src-link">原文链接：<a href="{w['link']}" target="_blank" rel="noopener">arXiv:{w['id']}</a></p>
</div>
</details>""")

    kw_block = ""
    if kcards:
        kw_labels = "、".join(sorted({kw for w in keyword for kw in w.get("matched_keywords", [])}))
        kw_block = (f'<div class="sec-head"><h2>🔑 名词推荐</h2><p class="sub">{esc(kw_labels)}</p></div>'
                    '<div class="papers">' + "".join(kcards) + "</div>")

    # ---- 目录组装 ----
    toc_radar_html = ""
    if toc_radar:
        toc_radar_html = ('<p class="toc-group">领域动态<span class="en">FIELD RADAR</span></p>'
                          '<ol class="toc-list">' + "".join(toc_radar) + "</ol>")
    toc_kw_html = ""
    if toc_kw:
        toc_kw_html = ('<p class="toc-group">名词推荐<span class="en">KEYWORD PICKS</span></p>'
                       '<ol class="toc-list">' + "".join(toc_kw) + "</ol>")

    stat_kw = ""
    if keyword:
        stat_kw = (f'<div class="stat-card"><div class="num">{len(keyword)}</div>'
                   f'<div class="label">名词推荐</div></div>')
    core_label = "核心精读" + (f" · ⭐{n_must} 必读" if n_must else "")

    nav_html = _archive_nav_html(days, date) if days and len(days) > 1 else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>arXiv 每日精选 — {date}</title>
<style>{SITE_CSS}</style>
{MATHJAX_HEAD}
</head>
<body>

<header class="cover">
  <div class="badge">GR-QC · HEP-TH · ASTRO-PH.CO</div>
  <h1>arXiv 每日精选</h1>
  <p class="date">{date_cn(date)}</p>
  <div class="stats">
    <div class="stat-card"><div class="num">{n_core}</div><div class="label">{core_label}</div></div>
    <div class="stat-card"><div class="num">{len(radar)}</div><div class="label">领域动态</div></div>
    {stat_kw}
  </div>
  <p class="src">来源：gr-qc {counts.get('gr-qc',0)} · hep-th {counts.get('hep-th',0)} · astro-ph.CO {counts.get('astro-ph.CO',0)}　·　按 Rao 的研究画像匹配</p>
</header>

<section class="toc-section">
  <h2>📋 目录</h2>
  <p class="toc-group">核心精读<span class="en">CORE</span></p>
  <ol class="toc-list">{''.join(toc_core)}</ol>
  {toc_radar_html}
  {toc_kw_html}
</section>

<div class="papers">{''.join(cards)}</div>

{radar_block}
{kw_block}

{nav_html}

<div class="footer">
  由 Kimi 生成 · <a href="index.html">存档索引</a> · {date_cn(date)} · 数据来自 <a href="https://arxiv.org" target="_blank" rel="noopener">arxiv.org</a>
</div>

</body>
</html>"""


def render_archive_index(days: list[dict]) -> str:
    """存档索引（群推送 Hub 同款画风）：最近 KEEP_DAYS 天的日期卡片。"""
    cards = []
    for i, d in enumerate(days):
        st = d["stats"]
        n_core = st.get("core", st.get("selected", 0))
        n_radar = st.get("radar", 0)
        n_kw = st.get("keyword", 0)
        cnt = f"核心 {n_core} · 动态 {n_radar}" + (f" · 名词 {n_kw}" if n_kw else "")
        must_line = ""
        if d.get("must_titles"):
            must_line = f'<div class="must-line">⭐ {"、".join(d["must_titles"][:2])}</div>'
        emoji = "📅" if i else "📰"
        cards.append(f"""<a href="{d['date']}.html" class="card">
            <span class="emoji">{emoji}</span>
            <span class="cat-badge">{"今天" if i == 0 else "历史"}</span>
            <h2>{_short_date(d['date'])}</h2>
            <div class="daycount">{cnt}</div>
            {must_line}
            <p class="desc">共 {d['total']} 篇 · {d['display']}</p>
        </a>""")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>arXiv 每日精选 · 存档索引</title>
<style>{INDEX_CSS}</style>
</head>
<body>

<div class="header">
    <h1>📚 arXiv 每日精选 · 存档</h1>
    <p class="date">最近 {len(days)} 天 · 滚动保留 {KEEP_DAYS} 天，循环覆盖</p>
</div>

<div class="grid">
    {''.join(cards)}
</div>

<div class="footer">
    由 Kimi 生成 · 按 Rao 的研究画像匹配 · 数据来自 <a href="https://arxiv.org" target="_blank" rel="noopener">arxiv.org</a>
</div>

</body>
</html>"""


# ---------------------------------------------------------------- pushplus 简讯（链接置顶 + 深度链接）

def render_pushplus(digest: dict, counts: dict, site_url: str = "") -> str:
    date = digest["listing_date"]
    st = digest["stats"]
    radar = digest.get("radar", [])
    keyword = digest.get("keyword", [])
    n_core = st.get("core", len(digest["papers"]))
    n_must = sum(1 for p in digest["papers"] if p.get("must_read"))

    stats_line = f'核心 {n_core}（⭐{n_must}）+ 动态 {len(radar)}' + (f' + 名词 {len(keyword)}' if keyword else '')
    head = (
        '<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:640px;margin:0 auto;color:#2c3e50;">'
        f'<div style="background:#2563eb;color:#fff;padding:12px 16px;border-radius:10px;">'
        f'<div style="font-size:17px;font-weight:700;">arXiv · {date}</div>'
        f'<div style="font-size:12px;opacity:.9;margin-top:3px;">{stats_line}　·　gr-qc {counts.get("gr-qc",0)} · hep-th {counts.get("hep-th",0)} · astro-ph.CO {counts.get("astro-ph.CO",0)}</div></div>'
    )
    link_top = ""
    if site_url:
        link_top = (
            f'<div style="text-align:center;margin:14px 0 6px;">'
            f'<a href="{site_url}" style="display:inline-block;background:#2563eb;color:#fff;font-size:15px;'
            f'font-weight:700;text-decoration:none;padding:10px 26px;border-radius:8px;">'
            f'👉 打开今日完整页面（折叠卡片）</a></div>'
        )

    items = []
    for i, p in enumerate(digest["papers"], 1):
        star = "⭐" if p.get("must_read") else ""
        col = score_color(p["score"])
        anchor = f"{site_url}#paper-{i}" if site_url else p["link"]
        items.append(
            f'<div style="margin:10px 0;padding:8px 12px;border-left:4px solid {col};background:#fafafa;border-radius:6px;">'
            f'<div style="font-size:14px;font-weight:700;">{star}{i}. '
            f'<a href="{anchor}" style="color:#2c3e50;text-decoration:none;">{esc(p.get("cn_title") or latex_to_text(p["title"]))}</a>'
            f'<span style="color:{col};font-size:12px;">　[{p["score"]}]</span></div>'
            f'<div style="font-size:12px;color:#666;margin-top:2px;">{esc(latex_to_text(p["title"]))}</div>'
            f'<div style="font-size:12px;color:#666;">{esc(p["authors"])}</div>'
            f'<div style="font-size:13px;margin-top:3px;">💡 {esc(p["one_liner"])}</div>'
            f'</div>'
        )

    def compact_section(entries, label, anchor_prefix, badge_of):
        if not entries:
            return ""
        rows = []
        for j, e in enumerate(entries, 1):
            anchor = f"{site_url}#{anchor_prefix}-{j}" if site_url else e["link"]
            badge = badge_of(e)
            rows.append(
                f'<div style="margin:6px 0;font-size:12px;">'
                f'<a href="{anchor}" style="color:#2c3e50;text-decoration:none;font-weight:700;">'
                f'{esc(e.get("cn_title") or latex_to_text(e["title"]))}</a>　{badge}<br>'
                f'<span style="color:#666;">💡 {esc(e["one_liner"])}</span></div>'
            )
        return (
            '<div style="margin:14px 0;padding:10px 12px;background:#f0f4fa;border-radius:8px;">'
            f'<div style="font-size:13px;font-weight:700;margin-bottom:6px;">{label}</div>'
            + "".join(rows) + "</div>"
        )

    radar_block = compact_section(
        radar, "📡 领域动态 · 修正引力宇宙学", "radar",
        lambda e: f'<span style="color:#dc2626;font-size:11px;">[{e.get("relation_label","风向")}]</span>')
    kw_block = compact_section(
        keyword, "🔑 名词推荐", "kw",
        lambda e: f'<span style="color:#2563eb;font-size:11px;">[关键词：{esc(" · ".join(e.get("matched_keywords", [])))}]</span>')
    radar_note_html = ""
    if not radar and digest.get("radar_note"):
        radar_note_html = f'<div style="font-size:11px;color:#999;margin:10px 0;">📡 {esc(digest["radar_note"])}</div>'

    return (
        head + link_top + "".join(items) + radar_block + radar_note_html + kw_block
        + '<div style="font-size:11px;color:#999;text-align:center;margin:12px 0;">'
          '每篇标题可点进网页对应卡片 · 由 Kimi 生成 · 按 Rao 的研究画像匹配</div></div>'
    )


# ---------------------------------------------------------------- Markdown 存档

def _md_group(item: dict) -> list[str]:
    lines = [f"💡 {item['one_liner']}", ""]
    if item.get("cn_summary"):
        lines += ["**速览**", "", item["cn_summary"], ""]
    if item.get("cn_review"):
        lines += ["**评价**", "", item["cn_review"], ""]
    if item.get("action") and item["action"] != "暂无":
        lines += [f"⚠️ 建议行动：{item['action']}", ""]
    return lines


def render_markdown(digest: dict, counts: dict) -> str:
    date = digest["listing_date"]
    st = digest["stats"]
    radar = digest.get("radar", [])
    keyword = digest.get("keyword", [])
    n_core = st.get("core", len(digest["papers"]))
    lines = [
        f"# arXiv · {date}",
        "",
        f"> 核心 {n_core} 篇 + 领域动态 {len(radar)} 篇" + (f" + 名词推荐 {len(keyword)} 篇" if keyword else "")
        + f"　|　来源 gr-qc {counts.get('gr-qc',0)} · hep-th {counts.get('hep-th',0)} · astro-ph.CO {counts.get('astro-ph.CO',0)}",
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
            "**速览**",
            "",
            p.get("cn_summary", ""),
            "",
            "**评价**",
            "",
            p.get("cn_review", ""),
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
            ] + _md_group(r) + ["---", ""]
    elif digest.get("radar_note"):
        lines += [f"📡 领域动态：{digest['radar_note']}", ""]
    if keyword:
        lines += ["## 🔑 名词推荐", ""]
        for k, w in enumerate(keyword, 1):
            lines += [
                f"### K{k}. [{w.get('cn_title') or w['title']}]({w['link']})　【关键词：{' · '.join(w.get('matched_keywords', []))}】",
                "",
                f"**{w['authors']}**",
                "",
            ] + _md_group(w) + ["---", ""]
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


def _counts_abstracts(listing: dict) -> tuple[dict, dict]:
    counts = {"gr-qc": 0, "hep-th": 0, "astro-ph.CO": 0}
    abstracts = {}
    for p in listing["papers"]:
        for c in p["source"].split("+"):
            if c in counts:
                counts[c] += 1
        abstracts[p["id"]] = p.get("abstract", "")
    return counts, abstracts


def _day_meta(d: str, dg: dict | None = None) -> dict | None:
    """一天的索引元数据；digest 缺失/损坏返回 None。"""
    try:
        dg = dg if dg is not None else load_json(DATA / f"digest_{d}.json")
        st = dg["stats"]
        total = st.get("core", len(dg.get("papers", []))) + st.get("radar", 0) + st.get("keyword", 0)
        return {
            "date": d,
            "display": date_cn(d),
            "stats": st,
            "must_titles": [p.get("cn_title") or p["title"] for p in dg.get("papers", []) if p.get("must_read")],
            "total": total,
        }
    except Exception:  # noqa: BLE001
        return None


def rebuild_site_window(today: str, digest: dict, listing: dict) -> list[dict]:
    """6 天滚动窗口：重渲窗口内全部日报页 + index.html，裁掉窗口外页面。

    与群推送一致：窗口 = 今天 + 过去 5 天 = 6 天；每天重渲窗口内全部页面
    （渲染是纯函数，数据都在 data/ 里），所以所有页面的日期导航永远只指向
    窗口内存活的页面，不会产生死链；窗口外页面从 site/ 删除，部署为整站
    镜像后线上随之消失。重跑同一天幂等：页面覆盖写、窗口按日期去重。

    窗口以 data/ 里的 digest 历史为准，而不是 site/ 里现有页面：data/ 由
    workflow 每日提交回仓库，CI 每次 checkout 都能拿到完整历史；site/ 不入库，
    只是渲染产物（群推送用入库的 archive/ 做账本，这里用 data/ 做账本，效果相同）。
    """
    data_dates = [m.group(1) for f in DATA.glob("digest_2*.json")
                  if (m := re.fullmatch(r"digest_(\d{4}-\d{2}-\d{2})", f.stem))]
    window = sorted(set(data_dates) | {today}, reverse=True)[:KEEP_DAYS]

    # 先算窗口元数据（导航与索引都依赖它），再渲染页面
    days = []
    for d in window:
        meta = _day_meta(d, digest if d == today else None)
        if meta:
            days.append(meta)
    days.sort(key=lambda m: m["date"], reverse=True)

    for d in window:
        try:
            dg = digest if d == today else load_json(DATA / f"digest_{d}.json")
            lst = listing if d == today else load_json(DATA / f"listing_{d}.json")
            counts, abstracts = _counts_abstracts(lst)
            (SITE / f"{d}.html").write_text(
                render_site_page(dg, counts, abstracts, days=days), encoding="utf-8")
            print(f"  site: {d}.html 重渲完成")
        except Exception as e:  # noqa: BLE001 —— 历史页渲染失败则保留旧文件，不阻塞今天
            print(f"  [WARN] {d}.html 重渲失败（保留旧文件）: {e}", file=sys.stderr)

    kept = set(window)
    for f in sorted(SITE.glob("2*.html")):
        if f.stem not in kept:
            f.unlink()
            print(f"  滚动窗口：裁掉 {f.name}")

    (SITE / "index.html").write_text(render_archive_index(days), encoding="utf-8")
    print(f"  site: index.html（{len(days)} 天窗口）")
    return days


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--force", action="store_true", help="跳过幂等检查，强制重推（测试用）")
    args = ap.parse_args()
    date = args.date
    if not date:
        latest = DATA / "latest.json"
        if latest.exists():
            try:
                date = json.loads(latest.read_text(encoding="utf-8")).get("listing_date")
            except Exception:  # noqa: BLE001
                date = None
    date = date or (datetime.now(BJ) - timedelta(hours=8)).date().isoformat()

    digest_path = DATA / f"digest_{date}.json"
    if not digest_path.exists():
        print(json.dumps({"ok": True, "pushed": False, "note": "无 digest（昨日无更新或无候选），静默跳过"}, ensure_ascii=False))
        return 0
    digest = load_json(digest_path)
    listing = load_json(DATA / f"listing_{date}.json")
    cfg = load_json(ROOT / "config.json")

    counts, abstracts = _counts_abstracts(listing)

    site_base = cfg.get("site_base_url", "").rstrip("/")
    site_url = f"{site_base}/{date}.html" if site_base else ""

    pp_html = render_pushplus(digest, counts, site_url)
    (OUT / f"digest_{date}.pushplus.html").write_text(pp_html, encoding="utf-8")

    (ARCHIVE / f"{date}.md").write_text(render_markdown(digest, counts), encoding="utf-8")

    rebuild_site_window(date, digest, listing)

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

    # 幂等闸门：该批次全部 ID 已在 seen_ids 中 => 今天已推过，跳过（重跑/补部署不产生重复消息）
    seen_path = DATA / "seen_ids.json"
    seen = set(json.loads(seen_path.read_text(encoding="utf-8"))) if seen_path.exists() else set()
    ids = [p["id"] for p in listing["papers"]]
    if ids and all(i in seen for i in ids) and not args.force:
        print(json.dumps({"ok": True, "pushed": False, "note": "该批次已全部推送过，幂等跳过"}, ensure_ascii=False))
        return 0

    st = digest["stats"]
    title = f"arXiv · {date}｜核心{st.get('core', len(digest['papers']))}+动态{st.get('radar', 0)}"
    if st.get("keyword"):
        title += f"+名词{st['keyword']}"
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
