#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
arXiv 日报投递器（v3：学术期刊风 · 三层结构 · 群格式雷达/名词卡）
输入：data/digest_<date>.json、data/listing_<date>.json
输出：
  output/digest_<date>.pushplus.html   pushplus 简讯（链接置顶 + 每篇深度链接）
  site/<date>.html                     学术期刊风日报页
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
        return "#9e2b25"
    if score >= 7:
        return "#b07c2a"
    if score >= 6:
        return "#6b7c5e"
    return "#8a8b99"


REL_CLS = {"撞车预警": "rel-collision", "近邻": "rel-neighbor", "风向": "rel-field"}

# ---------------------------------------------------------------- 网页（学术期刊风 v3）

SITE_CSS = r"""
:root {
    --bg:#f7f5f0; --card:#ffffff; --ink:#1c1c28; --ink2:#55566a; --muted:#8a8b99;
    --accent:#3b4a8c; --accent-ink:#2e3a70; --accent-soft:#eceef6;
    --border:#e4e1d6; --border-strong:#d3cfc0;
    --must:#9e2b25; --s7:#b07c2a; --s6:#6b7c5e; --s5:#8a8b99;
    --shadow:0 1px 2px rgba(28,28,40,.04), 0 4px 16px rgba(28,28,40,.06);
    --shadow-hover:0 2px 4px rgba(28,28,40,.06), 0 10px 28px rgba(28,28,40,.10);
    --serif:"Noto Serif SC","Source Han Serif SC","Songti SC","SimSun",serif;
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
    --mono:"SF Mono","Fira Code","Consolas",monospace;
}
[data-theme="dark"] {
    --bg:#14161d; --card:#1d2029; --ink:#e9e7de; --ink2:#b3b4c0; --muted:#7e8090;
    --accent:#93a3d9; --accent-ink:#b3c0ea; --accent-soft:#262a3a;
    --border:#2b2f3d; --border-strong:#3a3f50;
    --must:#d4706a; --s7:#d0a35c; --s6:#93a583; --s5:#7e8090;
    --shadow:0 1px 2px rgba(0,0,0,.25), 0 4px 16px rgba(0,0,0,.3);
    --shadow-hover:0 2px 4px rgba(0,0,0,.3), 0 10px 28px rgba(0,0,0,.4);
}
@media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
        --bg:#14161d; --card:#1d2029; --ink:#e9e7de; --ink2:#b3b4c0; --muted:#7e8090;
        --accent:#93a3d9; --accent-ink:#b3c0ea; --accent-soft:#262a3a;
        --border:#2b2f3d; --border-strong:#3a3f50;
        --must:#d4706a; --s7:#d0a35c; --s6:#93a583; --s5:#7e8090;
        --shadow:0 1px 2px rgba(0,0,0,.25), 0 4px 16px rgba(0,0,0,.3);
        --shadow-hover:0 2px 4px rgba(0,0,0,.3), 0 10px 28px rgba(0,0,0,.4);
    }
}
* { box-sizing:border-box; margin:0; padding:0; }
html { scroll-behavior:smooth; }
body { font-family:var(--sans); background:var(--bg); color:var(--ink);
       line-height:1.75; -webkit-font-smoothing:antialiased; transition:background .25s, color .25s; }
[data-theme="dark"] body { background:#14161d; color:#e9e7de; }
[data-theme="dark"] .card { background:#1d2029; border-color:#2b2f3d; }
[data-theme="dark"] .topnav { background:rgba(20,22,29,.84); }
.topnav { position:sticky; top:0; z-index:50; backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px);
    background:color-mix(in srgb, var(--bg) 84%, transparent); border-bottom:1px solid var(--border); }
.nav-inner { max-width:860px; margin:0 auto; padding:10px 24px; display:flex; justify-content:space-between; align-items:center; }
.brand { font-size:12px; letter-spacing:.24em; color:var(--muted); font-weight:600; }
.nav-right { display:flex; gap:14px; align-items:center; font-size:13px; }
.nav-right a { color:var(--accent); text-decoration:none; }
.nav-right a:hover { text-decoration:underline; }
#themeBtn { border:1px solid var(--border-strong); background:transparent; color:var(--ink2);
    border-radius:999px; padding:2px 12px; font-size:12px; cursor:pointer; font-family:var(--sans); }
#themeBtn:hover { border-color:var(--accent); color:var(--accent); }
.cover { max-width:860px; margin:0 auto; text-align:center; padding:60px 24px 36px; }
.kicker { font-size:12px; letter-spacing:.24em; color:var(--muted); font-weight:600; }
.cover h1 { font-family:var(--serif); font-size:34px; font-weight:600; margin:14px 0 8px; letter-spacing:.01em; }
.cover .sub { font-size:14px; color:var(--ink2); }
.stats { display:flex; justify-content:center; align-items:center; gap:26px; margin-top:30px; flex-wrap:wrap; }
.stat { text-align:center; }
.stat .num { display:block; font-family:var(--serif); font-size:30px; font-weight:600; color:var(--accent-ink); }
.stat .label { display:block; font-size:12px; color:var(--muted); margin-top:2px; letter-spacing:.08em; }
.stat-sep { width:1px; height:34px; background:var(--border-strong); }
.cover .src { margin-top:20px; font-size:12.5px; color:var(--muted); }
.toc { max-width:860px; margin:0 auto 8px; padding:0 24px; }
.toc h2 { font-size:12px; letter-spacing:.2em; color:var(--muted); font-weight:700; margin:26px 0 12px;
    padding-bottom:8px; border-bottom:1px solid var(--border); }
.toc h2 .en { font-weight:400; margin-left:8px; letter-spacing:.14em; }
.toc-list { display:grid; grid-template-columns:1fr 1fr; gap:8px 28px; list-style:none; }
.toc-list li { font-size:14px; line-height:1.6; }
.toc-list .n { font-family:var(--mono); font-size:11.5px; color:var(--muted); margin-right:6px; }
.toc-list a { color:var(--ink); text-decoration:none; font-family:var(--serif); }
.toc-list a:hover { color:var(--accent); text-decoration:underline; text-underline-offset:3px; }
.toc-list .toc-en { display:block; font-size:12px; color:var(--muted); margin-left:26px; margin-top:-2px; }
.divider { max-width:860px; margin:36px auto -6px; padding:0 24px; display:flex; align-items:center; gap:16px; }
.divider::before, .divider::after { content:""; flex:1; height:1px; background:var(--border-strong); }
.divider span { font-size:12px; letter-spacing:.2em; color:var(--muted); font-weight:700; white-space:nowrap; }
.papers { max-width:860px; margin:0 auto; padding:0 24px 40px; }
.card { background:var(--card); border:1px solid var(--border); border-radius:14px; margin:16px 0;
    box-shadow:var(--shadow); overflow:hidden;
    transition:transform .18s ease, box-shadow .18s ease, border-color .18s ease; }
.card:hover { transform:translateY(-2px); box-shadow:var(--shadow-hover); border-color:color-mix(in srgb, var(--accent) 40%, var(--border)); }
.card summary { list-style:none; cursor:pointer; display:grid; grid-template-columns:1fr auto;
    gap:16px; align-items:start; padding:20px 24px; user-select:none; }
.card summary::-webkit-details-marker { display:none; }
.c-kicker { font-family:var(--mono); font-size:11.5px; color:var(--muted); margin-bottom:6px; }
.c-title-en { font-size:13.5px; color:var(--ink2); line-height:1.5; }
.c-title-cn { font-family:var(--serif); font-size:17px; font-weight:600; color:var(--ink);
    margin:3px 0 7px; line-height:1.55; }
.c-authors { font-size:13px; color:var(--muted); }
.c-oneline { font-size:13.5px; color:var(--ink2); margin-top:7px; }
.c-side { display:flex; flex-direction:column; align-items:flex-end; gap:8px; padding-top:2px; }
.score { font-size:12px; font-weight:700; border:1px solid currentColor; border-radius:999px; padding:1px 10px; }
.s8 { color:var(--must); } .s7 { color:var(--s7); } .s6 { color:var(--s6); } .s5 { color:var(--s5); }
.must { background:var(--must); color:#fff; font-size:11px; font-weight:700; border-radius:4px; padding:1px 8px; }
.rel { font-size:11px; font-weight:700; border-radius:4px; padding:1px 8px; border:1px solid currentColor; }
.rel-collision { color:var(--must); } .rel-neighbor { color:var(--s7); } .rel-field { color:var(--s5); }
.kw { font-size:11px; font-weight:700; border-radius:4px; padding:1px 8px; border:1px solid var(--accent); color:var(--accent); }
.chev { font-size:16px; color:var(--muted); transition:transform .2s; line-height:1; }
.card[open] .chev { transform:rotate(90deg); color:var(--accent); }
.card.collision { border-left:3px solid var(--must); }
.detail { padding:2px 24px 22px; border-top:1px dashed var(--border-strong); }
.detail h4 { font-size:13px; letter-spacing:.06em; color:var(--accent-ink); font-weight:700; margin:18px 0 6px; }
.detail p { font-size:14.5px; line-height:1.85; color:var(--ink); margin-bottom:10px; }
.detail .rel-note { font-size:13px; color:var(--accent); font-weight:600; margin:14px 0 2px; }
.detail .action-box { background:color-mix(in srgb, var(--must) 8%, var(--card));
    border:1px solid color-mix(in srgb, var(--must) 30%, var(--border));
    border-radius:8px; padding:10px 14px; margin:12px 0; font-size:13.5px; }
.abs-details { margin:14px 0 4px; border:1px dashed var(--border-strong); border-radius:8px; padding:8px 14px; }
.abs-details summary { font-size:12.5px; color:var(--muted); cursor:pointer; padding:0; display:block; border:none; }
.abs-details p { font-size:13px; color:var(--ink2); margin-top:8px; }
.src-link { margin-top:16px; font-size:13px; color:var(--muted); }
.src-link a { color:var(--accent); text-decoration:none; border-bottom:1px solid color-mix(in srgb, var(--accent) 40%, transparent); }
.radar-note { max-width:860px; margin:0 auto 30px; padding:0 24px; font-size:13px; color:var(--muted); }
.footer { text-align:center; padding:36px 24px 48px; font-size:12.5px; color:var(--muted); }
.footer a { color:var(--accent); text-decoration:none; }
@media (max-width:640px) {
    .toc-list { grid-template-columns:1fr; }
    .cover h1 { font-size:26px; }
    .stats { gap:16px; }
    .stat-sep { display:none; }
    .card summary { padding:16px 18px; }
    .detail { padding:2px 18px 18px; }
}
"""

MATHJAX_HEAD = r"""
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource/noto-serif-sc@5.2.5/index.css">
<script>
window.MathJax = {
  tex: { inlineMath: [['$', '$'], ['\\(', '\\)']], displayMath: [['$$', '$$']] },
  options: { skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code', 'summary', 'details'] },
  startup: { ready() {
      MathJax.startup.defaultReady();
      document.querySelectorAll('details.card').forEach(function(el) {
        el.addEventListener('toggle', function() {
          if (el.open) { MathJax.typesetPromise([el.querySelector('.detail')]); }
        });
      });
  } }
};
</script>
<script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
"""

THEME_JS = r"""
(function(){
  var saved = localStorage.getItem('arxiv-theme');
  if (saved) { document.documentElement.setAttribute('data-theme', saved); }
  var btn = document.getElementById('themeBtn');
  function cur(){ return document.documentElement.getAttribute('data-theme') ||
    (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'); }
  function label(){ btn.textContent = cur() === 'dark' ? '☾ 深色' : '☀ 浅色'; }
  label();
  btn.onclick = function(){
    var t = cur() === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', t);
    localStorage.setItem('arxiv-theme', t); label();
  };
})();
"""


def _abs_block(abstracts: dict, pid: str) -> str:
    if pid in abstracts and abstracts[pid]:
        return (f'<details class="abs-details"><summary>英文摘要原文</summary>'
                f'<p>{esc_keep_math(abstracts[pid])}</p></details>')
    return ""


def _group_detail(item: dict, abstracts: dict, show_action: bool) -> str:
    """群格式详情区：中文摘要 + 四段式（末段为对 Rao 的意义）。"""
    parts = []
    if item.get("cn_abstract"):
        parts.append(f"<h4>摘要</h4><p>{esc_keep_math(item['cn_abstract'])}</p>")
    for field, label in [("eval_problem", "研究问题"), ("eval_method", "方法/框架"),
                         ("eval_finding", "主要发现"), ("eval_significance", "对 Rao 的意义")]:
        if item.get(field):
            parts.append(f"<h4>{label}</h4><p>{esc_keep_math(item[field])}</p>")
    if show_action and item.get("action") and item["action"] != "暂无":
        parts.append(f'<div class="action-box">⚠️ <b>建议行动</b>　{esc(item["action"])}</div>')
    parts.append(_abs_block(abstracts, item["id"]))
    return "\n".join(parts)


def render_site_page(digest: dict, counts: dict, abstracts: dict, site_url: str = "") -> str:
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
            f'<li><span class="n">{i:02d}</span><a href="#paper-{i}">{esc(cn or p["title"])}</a>'
            f'<span class="toc-en">{esc(p["title"][:70])}{"…" if len(p["title"])>70 else ""}</span></li>'
        )
        must = '<span class="must">必读</span>' if p.get("must_read") else ""
        cards.append(f"""
<details class="card" id="paper-{i}">
<summary>
  <div>
    <div class="c-kicker">#{i} · arXiv:{p['id']} · {' · '.join(p['categories'])}</div>
    <div class="c-title-cn">{esc(cn)}</div>
    <div class="c-title-en">{esc(p['title'])}</div>
    <div class="c-authors">{esc(p['authors'])}</div>
    <div class="c-oneline">💡 {esc(p['one_liner'])}</div>
  </div>
  <div class="c-side">
    <span class="score {score_cls(p['score'])}">{p['score']}</span>
    {must}
    <span class="chev">▸</span>
  </div>
</summary>
<div class="detail">
  <h4>📌 做了什么与评价</h4>
  {nl2p(p['did_and_eval'])}
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
            f'<li><span class="n">R{j}</span><a href="#radar-{j}">{esc(r.get("cn_title") or r["title"])}</a>'
            f'<span class="toc-en">{esc(r["title"][:70])}{"…" if len(r["title"])>70 else ""}</span></li>'
        )
        rcards.append(f"""
<details class="card {'collision' if label=='撞车预警' else ''}" id="radar-{j}">
<summary>
  <div>
    <div class="c-kicker">R{j} · arXiv:{r['id']} · {esc('/'.join(r.get('radar_fields', [])))}</div>
    <div class="c-title-cn">{esc(r.get('cn_title',''))}</div>
    <div class="c-title-en">{esc(r['title'])}</div>
    <div class="c-authors">{esc(r['authors'])}</div>
    <div class="c-oneline">💡 {esc(r['one_liner'])}</div>
  </div>
  <div class="c-side">
    <span class="rel {REL_CLS.get(label,'rel-field')}">{label}</span>
    <span class="score {score_cls(r['score'])}">{r['score']}</span>
    <span class="chev">▸</span>
  </div>
</summary>
<div class="detail">
  {_group_detail(r, abstracts, show_action=True)}
  <p class="src-link">原文链接：<a href="{r['link']}" target="_blank" rel="noopener">arXiv:{r['id']}</a></p>
</div>
</details>""")

    radar_block = ""
    if rcards:
        radar_block = ('<div class="divider"><span>领域动态 · 修正引力宇宙学</span></div>'
                       '<div class="papers">' + "".join(rcards) + "</div>")
    elif digest.get("radar_note"):
        radar_block = f'<div class="radar-note">📡 领域动态：{esc(digest["radar_note"])}</div>'

    # ---- 名词推荐层（群格式） ----
    toc_kw, kcards = [], []
    for k, w in enumerate(keyword, 1):
        kws = " · ".join(w.get("matched_keywords", []))
        toc_kw.append(
            f'<li><span class="n">K{k}</span><a href="#kw-{k}">{esc(w.get("cn_title") or w["title"])}</a>'
            f'<span class="toc-en">{esc(w["title"][:70])}{"…" if len(w["title"])>70 else ""}</span></li>'
        )
        kcards.append(f"""
<details class="card" id="kw-{k}">
<summary>
  <div>
    <div class="c-kicker">K{k} · arXiv:{w['id']} · {' · '.join(w['categories'])}</div>
    <div class="c-title-cn">{esc(w.get('cn_title',''))}</div>
    <div class="c-title-en">{esc(w['title'])}</div>
    <div class="c-authors">{esc(w['authors'])}</div>
    <div class="c-oneline">💡 {esc(w['one_liner'])}</div>
  </div>
  <div class="c-side">
    <span class="kw">关键词：{esc(kws)}</span>
    <span class="chev">▸</span>
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
        kw_block = (f'<div class="divider"><span>名词推荐 · {esc(kw_labels)}</span></div>'
                    '<div class="papers">' + "".join(kcards) + "</div>")

    # ---- 目录组装 ----
    toc_radar_html = ""
    if toc_radar:
        toc_radar_html = ('<h2>领域动态<span class="en">FIELD RADAR</span></h2>'
                          '<ol class="toc-list">' + "".join(toc_radar) + "</ol>")
    toc_kw_html = ""
    if toc_kw:
        toc_kw_html = ('<h2>名词推荐<span class="en">KEYWORD PICKS</span></h2>'
                       '<ol class="toc-list">' + "".join(toc_kw) + "</ol>")

    stat_kw = ""
    if keyword:
        stat_kw = ('<div class="stat-sep"></div>'
                   f'<div class="stat"><span class="num">{len(keyword)}</span><span class="label">名词推荐</span></div>')

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

<nav class="topnav"><div class="nav-inner">
  <span class="brand">ARXIV DAILY</span>
  <span class="nav-right">
    <a href="index.html">存档索引</a>
    <button id="themeBtn" type="button">◐</button>
  </span>
</div></nav>

<header class="cover">
  <div class="kicker">GR-QC · HEP-TH · ASTRO-PH.CO</div>
  <h1>arXiv · {date_cn(date)}</h1>
  <p class="sub">每日精选 · 按 Rao 的研究画像匹配</p>
  <div class="stats">
    <div class="stat"><span class="num">{n_core}</span><span class="label">核心精读 · ⭐{n_must}</span></div>
    <div class="stat-sep"></div>
    <div class="stat"><span class="num">{len(radar)}</span><span class="label">领域动态</span></div>
    {stat_kw}
  </div>
  <p class="src">来源：gr-qc {counts.get('gr-qc',0)} · hep-th {counts.get('hep-th',0)} · astro-ph.CO {counts.get('astro-ph.CO',0)}</p>
</header>

<section class="toc">
  <h2>核心精读<span class="en">CORE</span></h2>
  <ol class="toc-list">{''.join(toc_core)}</ol>
  {toc_radar_html}
  {toc_kw_html}
</section>

<div class="papers">{''.join(cards)}</div>

{radar_block}
{kw_block}

<div class="footer">
  由 Kimi 生成 · <a href="index.html">存档索引</a> · {date_cn(date)} · 数据来自 <a href="https://arxiv.org" target="_blank" rel="noopener">arxiv.org</a>
</div>

<script>{THEME_JS}</script>
</body>
</html>"""


def render_archive_index(days: list[dict]) -> str:
    rows = "".join(
        f'<tr><td><a href="{d["date"]}.html">{d["date"]}</a></td>'
        f'<td>{d["stats"].get("core", d["stats"].get("selected", 0))} / {d["stats"].get("radar", 0)} / {d["stats"].get("keyword", 0)}</td>'
        f'<td>{esc("、".join(d.get("must_titles", []))[:100])}</td></tr>'
        for d in days
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>arXiv · 存档索引</title>
<style>{SITE_CSS}
table{{width:100%;border-collapse:collapse;background:var(--card);border-radius:12px;overflow:hidden;box-shadow:var(--shadow)}}
td,th{{border:1px solid var(--border);padding:9px 12px;font-size:14px;text-align:left}}
a{{color:var(--accent);text-decoration:none}}
.idx{{max-width:860px;margin:56px auto;padding:0 24px}}
.idx h1{{font-family:var(--serif);font-size:26px;margin-bottom:18px}}</style></head>
<body><div class="idx">
<h1>📚 arXiv 日报存档</h1>
<table><tr><th>日期</th><th>核心/动态/名词</th><th>必读</th></tr>{rows}</table>
</div></body></html>"""


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
        f'<div style="background:#3b4a8c;color:#fff;padding:12px 16px;border-radius:10px;">'
        f'<div style="font-size:17px;font-weight:700;">arXiv · {date}</div>'
        f'<div style="font-size:12px;opacity:.9;margin-top:3px;">{stats_line}　·　gr-qc {counts.get("gr-qc",0)} · hep-th {counts.get("hep-th",0)} · astro-ph.CO {counts.get("astro-ph.CO",0)}</div></div>'
    )
    link_top = ""
    if site_url:
        link_top = (
            f'<div style="text-align:center;margin:14px 0 6px;">'
            f'<a href="{site_url}" style="display:inline-block;background:#3b4a8c;color:#fff;font-size:15px;'
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
        lambda e: f'<span style="color:#9e2b25;font-size:11px;">[{e.get("relation_label","风向")}]</span>')
    kw_block = compact_section(
        keyword, "🔑 名词推荐", "kw",
        lambda e: f'<span style="color:#3b4a8c;font-size:11px;">[关键词：{esc(" · ".join(e.get("matched_keywords", [])))}]</span>')
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
    if item.get("cn_abstract"):
        lines += ["**摘要**", "", item["cn_abstract"], ""]
    for field, label in [("eval_problem", "研究问题"), ("eval_method", "方法/框架"),
                         ("eval_finding", "主要发现"), ("eval_significance", "对 Rao 的意义")]:
        if item.get(field):
            lines += [f"**{label}**：{item[field]}", ""]
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

    (SITE / f"{date}.html").write_text(render_site_page(digest, counts, abstracts, site_url), encoding="utf-8")
    (ARCHIVE / f"{date}.md").write_text(render_markdown(digest, counts), encoding="utf-8")

    days = []
    for f in sorted(SITE.glob("2*.html"), reverse=True):
        d = f.stem
        if d.startswith("preview-"):
            continue
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
