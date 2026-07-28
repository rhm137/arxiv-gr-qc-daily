#!/usr/bin/env python3
"""
Unified arXiv daily digest pipeline.

Usage:
    python run.py [--date YYYY-MM-DD] [--cats cat1 cat2 ...]

Fetch, translate, build HTML, and deploy — all in one go.
Eliminates all shell coordination issues.

Requires: pip install openai
Env vars: DEEPSEEK_API_KEY
"""

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from argparse import ArgumentParser
from urllib.request import urlopen, Request
from urllib.error import URLError

# ── Config ──
ARXIV_API = "http://export.arxiv.org/api/query"
ARXIV_MAX = 100
ARXIV_UA  = "WorkBuddy-arxiv-digest/2.0"
ARXIV_RETRY = 8
ARXIV_DELAY = 15

ASTRO_SUBS = ["astro-ph.CO", "astro-ph.HE"]

CATEGORY_INFO = {
    "gr-qc":    "引力与量子宇宙学",
    "hep-th":   "高能理论物理",
    "astro-ph": "天体物理学",
}

CATEGORY_PROMPTS = {
    "gr-qc": {
        "role": "a Chinese physicist specializing in general relativity and quantum cosmology",
        "abbr": "LIGO, GW, BH, GR, QPO, ISCO, FLRW",
    },
    "hep-th": {
        "role": "a Chinese theoretical physicist specializing in high energy physics and quantum field theory",
        "abbr": "QFT, SUSY, CFT, AdS/CFT, S-matrix, EFT, RG, SM, BSM, SUGRA, TQFT",
    },
    "astro-ph": {
        "role": "a Chinese astrophysicist specializing in astrophysics and cosmology",
        "abbr": "SNe, CMB, BAO, LSS, ISM, AGN, SMBH, GW, GRB, FRB, DM, DE",
    },
}

CATEGORY_META = {
    "gr-qc":    {"badge": "arXiv gr-qc",    "title": "引力与量子宇宙学", "label_primary": "主分类 gr-qc", "label_cross": "交叉列表"},
    "hep-th":   {"badge": "arXiv hep-th",   "title": "高能理论物理",     "label_primary": "主分类 hep-th", "label_cross": "交叉列表"},
    "astro-ph": {"badge": "arXiv astro-ph", "title": "天体物理学",       "label_primary": "主分类 astro-ph", "label_cross": "交叉列表"},
}

RETRY_ABBR = ("LIGO, GW, BH, GR, QPO, ISCO, FLRW, ADM, TOV, PBH, EHT, SKA, LISA, EGB, "
              "QFT, SUSY, CFT, EFT, RG, SUGRA, SNe, CMB, BAO, LSS, AGN, SMBH, GRB, FRB, DM, DE")


# ═══════════════════════════════════════════════════════════════════════
# FETCH
# ═══════════════════════════════════════════════════════════════════════

def fetch_url(url: str, retries: int = ARXIV_RETRY) -> str:
    """Fetch URL with robust retries."""
    last_err = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": ARXIV_UA})
            with urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                wait = ARXIV_DELAY * (attempt + 1)
                print(f"  [RETRY {attempt+1}/{retries}] {e} — waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
    raise RuntimeError(f"arXiv API unreachable after {retries} attempts: {last_err}")


def fetch_category(cat: str, date_str: str) -> list[dict]:
    """Fetch papers for a category (handles astro-ph sub-categories)."""
    papers = []
    seen = set()

    if cat == "astro-ph":
        for sub in ASTRO_SUBS:
            query = f"cat:{sub}+AND+submittedDate:[{date_str}0000+TO+{date_str}2359]"
            url = (f"{ARXIV_API}?search_query={query}&sortBy=submittedDate"
                   f"&sortOrder=ascending&start=0&max_results={ARXIV_MAX}")
            print(f"  GET {sub}...")
            xml_text = fetch_url(url)
            new = _parse_xml(xml_text, seen, papers)
            print(f"    {sub}: {new} papers")
            time.sleep(5)
    else:
        query = f"cat:{cat}+AND+submittedDate:[{date_str}0000+TO+{date_str}2359]"
        url = (f"{ARXIV_API}?search_query={query}&sortBy=submittedDate"
               f"&sortOrder=ascending&start=0&max_results={ARXIV_MAX}")
        print(f"  GET {cat}...")
        xml_text = fetch_url(url)
        n = _parse_xml(xml_text, seen, papers)
        print(f"    {cat}: {n} papers")

    return papers


def _parse_xml(xml_text: str, seen: set, out: list) -> int:
    """Parse XML, dedup, append to out. Returns count of new papers."""
    ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    root = ET.fromstring(xml_text)
    new = 0
    for entry in root.findall(".//a:entry", ns):
        def txt(tag):
            el = entry.find(tag, ns)
            return el.text.strip() if el is not None and el.text else ""

        pid = txt("a:id").replace("http://arxiv.org/abs/", "")
        if pid in seen:
            continue
        seen.add(pid)

        authors = [a.find("a:name", ns).text.strip()
                   for a in entry.findall("a:author", ns) if a.find("a:name", ns) is not None]
        pc_el = entry.find("arxiv:primary_category", ns)
        primary = pc_el.attrib["term"] if pc_el is not None else "unknown"
        all_cats = [c.attrib["term"] for c in entry.findall("a:category", ns)]
        comment_el = entry.find("arxiv:comment", ns)
        comment = comment_el.text.strip() if (comment_el is not None and comment_el.text) else ""

        out.append({
            "ID": pid, "Title": txt("a:title"),
            "Authors": "; ".join(authors), "Summary": txt("a:summary"),
            "PrimaryCat": primary, "AllCats": ", ".join(all_cats), "Comment": comment,
        })
        new += 1
    return new


# ═══════════════════════════════════════════════════════════════════════
# TRANSLATE
# ═══════════════════════════════════════════════════════════════════════

def _build_prompt(cat: str) -> str:
    info = CATEGORY_PROMPTS.get(cat, CATEGORY_PROMPTS["gr-qc"])
    return f"""You are {info['role']}. Review the following arXiv paper and provide your response in Chinese.

## Paper Information
- arXiv ID: {{paper_id}}
- Title: {{title}}
- Authors: {{authors}}
- Abstract (English): {{abstract}}

## Instructions
Provide the following three items in Chinese. Output ONLY valid JSON, no other text.

1. "cn_title": Translate the title into Chinese. Preserve all technical abbreviations in English (e.g., {info['abbr']}).
2. "cn_abstract": Full Chinese translation of the abstract. Use $...$ for all LaTeX math symbols.
3. "cn_eval": A four-paragraph Chinese evaluation (~300 characters total):

Paragraph 1 — 研究问题
Paragraph 2 — 方法/框架
Paragraph 3 — 主要发现
Paragraph 4 — 评价与展望

CRITICAL: LaTeX math in $...$. Output ONLY valid JSON.
The JSON object must contain three keys: cn_title, cn_abstract, cn_eval."""


RETRY_PROMPT_TMPL = f"""The previous translation had quality issues. Re-translate MORE CAREFULLY.
- cn_title MUST be fully in Chinese (except: {RETRY_ABBR})
- cn_abstract MUST be >70% Chinese characters
- cn_eval MUST contain all four markers: 研究问题, 方法/框架, 主要发现, 评价与展望

## Paper Information
- arXiv ID: {{paper_id}}
- Title: {{title}}
- Authors: {{authors}}
- Abstract (English): {{abstract}}

Output ONLY valid JSON. No excuses."""


def translate_all(papers: list[dict], cat: str, api_key: str) -> int:
    """Translate all papers in-place. Returns count of flagged failures."""
    if not papers:
        return 0

    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    model = os.environ.get("LLM_MODEL", "deepseek-chat")
    prompt_tmpl = _build_prompt(cat)

    flagged = 0
    t0 = time.time()
    total = len(papers)

    for i, p in enumerate(papers):
        pid = p.get("ID", "?")
        title = p.get("Title", "")
        authors = p.get("Authors", "Unknown")
        abstract = p.get("Summary", "")

        prompt = prompt_tmpl.format(paper_id=pid, title=title, authors=authors, abstract=abstract)

        elapsed = time.time() - t0
        eta = (elapsed / max(i, 1)) * (total - i) if i > 0 else 0
        print(f"  [{i+1}/{total}] {pid}  (ETA {eta:.0f}s)")

        success = False
        for attempt in range(6):
            try:
                temp = 0.3 if attempt == 0 else 0.5
                resp = client.chat.completions.create(
                    model=model, temperature=temp, max_tokens=4096,
                    messages=[
                        {"role": "system", "content": "You are a Chinese physicist. Always respond with valid JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                )
                content = resp.choices[0].message.content.strip()
                if content.startswith("```"):
                    lines = content.split("\n")
                    content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
                result = json.loads(content)

                issues = _validate(result)
                if not issues:
                    p["CN_Title"] = str(result.get("cn_title", ""))
                    p["CN_Abstract"] = str(result.get("cn_abstract", ""))
                    p["CN_Eval"] = str(result.get("cn_eval", ""))
                    print("    OK")
                    success = True
                    break
                else:
                    print(f"    QA issues ({', '.join(issues[:2])}) — retry {attempt+1}/6")
                    if attempt < 5:
                        time.sleep(2 * (attempt + 1))
                        prompt = RETRY_PROMPT_TMPL.format(paper_id=pid, title=title, authors=authors, abstract=abstract)

            except json.JSONDecodeError:
                print(f"    JSON error — retry {attempt+1}/6")
                if attempt < 5:
                    time.sleep(3)
                    prompt = RETRY_PROMPT_TMPL.format(paper_id=pid, title=title, authors=authors, abstract=abstract)

            except Exception as e:
                ename = type(e).__name__
                print(f"    API: {ename}")
                w = 15 * (attempt + 1)
                if "rate" in str(e).lower() or "RateLimit" in ename:
                    w = 30 * (attempt + 1)
                print(f"    Waiting {w}s...")
                time.sleep(w)

        if not success:
            flagged += 1
            p["CN_Title"] = f"⚠ {title}"
            p["CN_Abstract"] = abstract
            p["CN_Eval"] = "⚠ 翻译校验未通过，请查看原文摘要。"
            print("    FLAGGED")

        time.sleep(6)  # pace between papers

    print(f"  [{cat}] {total} papers, {flagged} flagged ({time.time()-t0:.0f}s)")
    return flagged


def _validate(result: dict) -> list[str]:
    issues = []
    cn_title = result.get("cn_title", "")
    cn_abstract = result.get("cn_abstract", "")
    cn_eval = result.get("cn_eval", "")

    if not cn_title.strip():
        issues.append("title empty")

    if not cn_abstract.strip():
        issues.append("abstract empty")
    else:
        cn_chars = len(re.findall(r'[\u4e00-\u9fff]', cn_abstract))
        if len(cn_abstract) > 20 and cn_chars / max(len(cn_abstract), 1) < 0.15:
            issues.append("low CN ratio")

    markers = ["研究问题", "方法", "主要发现", "评价"]
    if sum(1 for m in markers if m in cn_eval) < 2:
        issues.append("missing markers")

    return issues


# ═══════════════════════════════════════════════════════════════════════
# BUILD HTML
# ═══════════════════════════════════════════════════════════════════════

HTML_CSS = r"""
:root {
    --bg: #fafaf8; --card-bg: #ffffff; --text: #2c2c2c; --text-secondary: #666;
    --accent: #2563eb; --accent-light: #eff6ff; --border: #e5e7eb;
    --tag-bg: #f3f4f6; --tag-text: #4b5563; --cross-bg: #fef3c7; --cross-text: #92400e;
    --shadow: 0 1px 3px rgba(0,0,0,0.08); --radius: 8px;
    --font-sans: -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans SC","PingFang SC","Microsoft YaHei",sans-serif;
    --font-mono: "SF Mono","Fira Code","Cascadia Code","Consolas",monospace;
}
* { box-sizing:border-box;margin:0;padding:0; }
body { font-family:var(--font-sans);background:var(--bg);color:var(--text);line-height:1.7;-webkit-font-smoothing:antialiased; }
.cover { max-width:800px;margin:80px auto 60px;text-align:center;padding:0 24px; }
.cover .badge { display:inline-block;background:var(--accent);color:#fff;font-size:13px;font-weight:600;letter-spacing:.08em;padding:6px 20px;border-radius:100px;margin-bottom:24px; }
.cover h1 { font-size:36px;font-weight:700;margin-bottom:8px;color:#111; }
.cover .date { font-size:18px;color:var(--text-secondary);margin-bottom:40px; }
.stats { display:flex;gap:16px;justify-content:center;flex-wrap:wrap; }
.stats .stat-card { background:var(--card-bg);border:1px solid var(--border);border-radius:var(--radius);padding:20px 32px;min-width:140px;box-shadow:var(--shadow); }
.stats .stat-card .num { font-size:32px;font-weight:700;color:var(--accent); }
.stats .stat-card .label { font-size:13px;color:var(--text-secondary);margin-top:4px; }
.toc-section { max-width:800px;margin:0 auto 48px;padding:0 24px; }
.toc-section h2 { font-size:20px;font-weight:700;margin-bottom:16px;padding-bottom:8px;border-bottom:2px solid var(--border); }
.toc-list { display:grid;grid-template-columns:1fr 1fr;gap:6px 24px;list-style:none; }
.toc-list li { font-size:14px;line-height:1.6; }
.toc-list a { color:var(--accent);text-decoration:none;display:flex;align-items:baseline;gap:6px; }
.toc-list a:hover { text-decoration:underline; }
.toc-list .toc-num { font-family:var(--font-mono);font-size:12px;color:var(--text-secondary);min-width:24px;flex-shrink:0; }
.toc-list .toc-cross { font-size:11px;background:var(--cross-bg);color:var(--cross-text);padding:1px 6px;border-radius:4px;white-space:nowrap;flex-shrink:0; }
.papers { max-width:800px;margin:0 auto 80px;padding:0 24px; }
.paper-card { background:var(--card-bg);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:16px;box-shadow:var(--shadow);overflow:hidden; }
.paper-card summary { padding:20px 24px;cursor:pointer;list-style:none;display:flex;align-items:flex-start;gap:12px;user-select:none; }
.paper-card summary::-webkit-details-marker { display:none; }
.paper-card summary::before { content:"▶";font-size:11px;color:var(--text-secondary);flex-shrink:0;margin-top:2px;transition:transform .2s;display:inline-block; }
.paper-card[open] summary::before { transform:rotate(90deg); }
.paper-card .card-body { flex:1;min-width:0; }
.paper-card .card-num { font-family:var(--font-mono);font-size:12px;color:var(--text-secondary);margin-bottom:4px; }
.paper-card .card-title { font-size:16px;font-weight:600;color:#111;margin-bottom:4px; }
.paper-card .card-authors { font-size:13px;color:var(--text-secondary);margin-bottom:4px; }
.paper-card .card-title-cn { font-size:14px;font-weight:500;color:var(--text);margin-bottom:4px; }
.paper-card .card-oneline { font-size:13px;color:var(--text-secondary); }
.paper-card .detail { padding:0 24px 24px;border-top:1px solid var(--border); }
.paper-card .detail h4 { font-size:14px;font-weight:600;color:var(--accent);margin:20px 0 8px; }
.paper-card .detail p { font-size:14px;color:var(--text);line-height:1.8;margin-bottom:12px; }
.paper-card .detail .cn-title { font-size:15px;font-weight:600;color:#111;margin:16px 0 12px; }
.footer { text-align:center;padding:40px 24px;font-size:13px;color:var(--text-secondary); }
.footer a { color:var(--accent); }
@media (max-width:600px) { .toc-list { grid-template-columns:1fr; } .cover h1 { font-size:26px; } .stats { gap:8px; } .stats .stat-card { padding:14px 20px;min-width:100px; } .stats .stat-card .num { font-size:24px; } }
"""

HUB_CSS = r"""
* { box-sizing:border-box;margin:0;padding:0; }
:root { --bg:#f8f9fa;--text:#1a1a2e;--text-secondary:#6b7280;
  --font-sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans SC","PingFang SC","Microsoft YaHei",sans-serif; }
body { font-family:var(--font-sans);background:var(--bg);color:var(--text);min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px 24px; }
.header { text-align:center;margin-bottom:48px; }
.header h1 { font-size:32px;font-weight:700;margin-bottom:8px;color:#111; }
.header .date { font-size:16px;color:var(--text-secondary); }
.grid { display:flex;gap:20px;max-width:900px;width:100%;flex-wrap:wrap;justify-content:center; }
.card { background:#fff;border-radius:16px;box-shadow:0 4px 24px rgba(0,0,0,0.06);padding:32px 28px;flex:1;min-width:260px;max-width:280px;text-decoration:none;color:inherit;transition:transform .2s,box-shadow .2s;text-align:center; }
.card:hover { transform:translateY(-4px);box-shadow:0 8px 32px rgba(0,0,0,0.1); }
.card .emoji { font-size:48px;margin-bottom:16px;display:block; }
.card .cat-badge { display:inline-block;font-size:13px;font-weight:600;letter-spacing:.06em;padding:4px 14px;border-radius:100px;margin-bottom:12px;color:#fff; }
.card h2 { font-size:20px;font-weight:700;margin-bottom:8px;color:#111; }
.card .count { font-size:28px;font-weight:700;margin-bottom:4px; }
.card .no-update { font-size:18px;font-weight:500;color:#9ca3af;margin-bottom:4px; }
.card .desc { font-size:13px;color:var(--text-secondary);line-height:1.5; }
.footer { margin-top:60px;text-align:center;font-size:13px;color:var(--text-secondary); }
.footer a { color:#2563eb; }
"""

HUB_INFO = {
    "gr-qc":    {"emoji": "🌀", "color": "#2563eb", "title": "引力与量子宇宙学", "desc": "广义相对论、量子引力、黑洞、引力波"},
    "hep-th":   {"emoji": "⚛️", "color": "#7c3aed", "title": "高能理论物理",     "desc": "量子场论、弦论、共形场论、超对称"},
    "astro-ph": {"emoji": "🌌", "color": "#059669", "title": "天体物理学",       "desc": "宇宙学、恒星演化、星系形成、高能天体物理"},
}


def _escape(s) -> str:
    """HTML-escape but preserve $...$ for MathJax."""
    if not s: return ""
    if not isinstance(s, str):
        s = json.dumps(s, ensure_ascii=False)
    parts = s.split("$")
    out = []
    for i, p in enumerate(parts):
        if i % 2 == 0:
            out.append(p.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;"))
        else:
            out.append(f"${p}$")
    return "".join(out)


def _one_liner(eval_text: str) -> str:
    if not eval_text: return ""
    m = re.match(r"^(.*?[。；])", eval_text)
    return (m.group(1) if m else eval_text[:80] + "…").replace("研究问题", "速览", 1)


def _format_authors(paper: dict, max_n: int = 4) -> str:
    authors = paper.get("Authors", "")
    names = [a.strip() for a in authors.split(";") if a.strip()]
    if len(names) <= max_n: return ", ".join(names)
    return ", ".join(names[:max_n]) + f" et al. ({len(names)} authors)"


def build_category_html(papers: list[dict], cat: str, out_dir: str, date_display: str, api_date: str):
    """Build date-stamped + latest HTML for a category."""
    meta = CATEGORY_META[cat]
    primary = [p for p in papers if p.get("PrimaryCat", "").startswith(cat if cat == "astro-ph" else cat)]
    cross = [p for p in papers if p not in primary]
    total = len(papers)

    # TOC
    toc = []
    for i, p in enumerate(papers, 1):
        cn_title = p.get("CN_Title", p.get("Title", ""))
        is_cross = p in cross
        cross_tag = f'<span class="toc-cross">← {_escape(p.get("PrimaryCat",""))}</span>' if is_cross else ""
        toc.append(f'<li><a href="#paper-{i}"><span class="toc-num">{i}.</span>'
                   f'{_escape(cn_title[:60])}{"…" if len(cn_title) > 60 else ""}</a>{cross_tag}</li>')

    # Cards
    cards = []
    for i, p in enumerate(papers, 1):
        pid = p.get("ID", "")
        ttl = _escape(p.get("Title", ""))
        cnt = _escape(p.get("CN_Title", ""))
        cabs = _escape(p.get("CN_Abstract", ""))
        ceval = _escape(p.get("CN_Eval", ""))
        auth = _escape(_format_authors(p))
        onel = _escape(_one_liner(p.get("CN_Eval", "")))
        is_x = p in cross
        xbadge = f" [交叉: {_escape(p.get('PrimaryCat',''))}]" if is_x else ""

        cards.append(f'<details class="paper-card" id="paper-{i}"><summary>'
            f'<div class="card-body"><div class="card-num">#{i}{xbadge}  ·  {_escape(pid)}</div>'
            f'<div class="card-title">{ttl}</div><div class="card-title-cn">{cnt}</div>'
            f'<div class="card-authors">{auth}</div><div class="card-oneline">{onel}</div></div></summary>'
            f'<div class="detail"><h4>摘要</h4><p>{cabs}</p><h4>评价</h4><p>{ceval}</p>'
            f'<p style="margin-top:12px;font-size:12px;color:var(--text-secondary);">'
            f'arXiv: <a href="https://arxiv.org/abs/{_escape(pid)}" target="_blank">{_escape(pid)}</a></p>'
            f'</div></details>')

    cross_cats = {}
    for p in cross:
        c = p.get("PrimaryCat", "?")
        cross_cats[c] = cross_cats.get(c, 0) + 1
    cross_str = "、".join(f"{k}({v})" for k, v in sorted(cross_cats.items(), key=lambda x: -x[1]))

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{meta['badge']} — {date_display}</title>
<style>{HTML_CSS}</style>
<script>window.MathJax={{tex:{{inlineMath:[['$','$']],displayMath:[['$$','$$']]}},
options:{{skipHtmlTags:['script','noscript','style','textarea','pre','code','summary','details']}},
startup:{{ready(){{MathJax.startup.defaultReady();
document.querySelectorAll('details.paper-card').forEach(function(el){{el.addEventListener('toggle',function(){{if(el.open)MathJax.typesetPromise([el.querySelector('.detail')]);}});}});}}}}}};</script>
<script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
</head><body>
<div class="cover"><div class="badge">{meta['badge']}</div><h1>{meta['title']}</h1><p class="date">{date_display}</p>
<div class="stats"><div class="stat-card"><div class="num">{total}</div><div class="label">论文总数</div></div>
<div class="stat-card"><div class="num">{len(primary)}</div><div class="label">{meta['label_primary']}</div></div>
<div class="stat-card"><div class="num">{len(cross)}</div><div class="label">{meta['label_cross']}</div></div></div>
{('<p style="margin-top:20px;font-size:14px;color:var(--text-secondary);">交叉来源：'+cross_str+'</p>') if cross_str else ''}
</div>
<div class="toc-section"><h2>📋 目录</h2><ol class="toc-list">{''.join(toc)}</ol></div>
<div class="papers">{''.join(cards)}</div>
<div class="footer">Generated by WorkBuddy · <a href="index.html">arXiv Daily Hub</a> · {date_display}
 · Data from <a href="https://arxiv.org" target="_blank">arxiv.org</a></div>
</body></html>"""

    date_file = os.path.join(out_dir, f"arxiv-{cat}-{api_date}.html")
    latest_file = os.path.join(out_dir, f"{cat}-latest.html")
    for p in [date_file, latest_file]:
        with open(p, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  Built {p}")


def build_hub_html(summary: dict, out_dir: str, date_display: str):
    """Build hub page."""
    cards = []
    for cat in ["gr-qc", "hep-th", "astro-ph"]:
        info = HUB_INFO[cat]
        count = summary.get(cat, 0)
        if count > 0:
            cnt_html = f'<div class="count" style="color:{info["color"]}">{count} 篇</div>'
            link = f'<a href="{cat}-latest.html" class="card">'
        else:
            cnt_html = '<div class="no-update">没更新</div>'
            link = '<div class="card" style="cursor:default">'
        cards.append(f'{link}<span class="emoji">{info["emoji"]}</span>'
                     f'<span class="cat-badge" style="background:{info["color"]}">arXiv {cat}</span>'
                     f'<h2>{info["title"]}</h2>{cnt_html}<p class="desc">{info["desc"]}</p>'
                     f'{"</a>" if count > 0 else "</div>"}')

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>arXiv Daily Hub — {date_display}</title><style>{HUB_CSS}</style></head><body>
<div class="header"><h1>📰 arXiv 今日论文速览</h1><p class="date">{date_display}</p></div>
<div class="grid">{''.join(cards)}</div>
<div class="footer">Generated by WorkBuddy · {date_display}
 · Data from <a href="https://arxiv.org" target="_blank">arxiv.org</a></div>
</body></html>"""

    path = os.path.join(out_dir, "latest.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  Hub: {path}")


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = ArgumentParser(description="arXiv unified daily digest")
    parser.add_argument("--date", default=None, help="Override date YYYY-MM-DD")
    parser.add_argument("--cats", nargs="+", default=["gr-qc", "hep-th", "astro-ph"])
    parser.add_argument("--out", default="./outputs-public", help="Output dir for HTML")
    args = parser.parse_args()

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("ERROR: DEEPSEEK_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    # Date calculation
    if args.date:
        from datetime import datetime, timedelta
        d = datetime.strptime(args.date, "%Y-%m-%d")
        qd = (d - timedelta(days=1)).strftime("%Y%m%d")
        display = d.strftime("%Y年%m月%d日")
    else:
        # Yesterday (Beijing time)
        os.environ["TZ"] = "Asia/Shanghai"
        from datetime import datetime, timedelta
        d = datetime.now() - timedelta(days=1)
        qd = d.strftime("%Y%m%d")
        display = (datetime.now() - timedelta(days=1)).strftime("%Y年%m月%d日")

    print(f"Query: {qd}  Display: {display}")
    os.makedirs(args.out, exist_ok=True)

    # ── STEP 1: Fetch ──
    all_data = {}
    summary = {}
    # Fetch gr-qc last — arXiv rate limits hit the first query hardest
    fetch_order = [c for c in args.cats if c != "gr-qc"] + (["gr-qc"] if "gr-qc" in args.cats else [])
    print(f"  Cooling {ARXIV_DELAY}s before first API call...")
    time.sleep(ARXIV_DELAY)
    for cat in fetch_order:
        print(f"\n--- Fetching {cat} ---")
        try:
            papers = fetch_category(cat, qd)
            all_data[cat] = papers
            summary[cat] = len(papers)
            # Stats
            primary = [p for p in papers if p["PrimaryCat"].startswith(cat if cat == "astro-ph" else cat)]
            cross = len(papers) - len(primary)
            print(f"  {cat}: {len(papers)} papers ({len(primary)} primary, {cross} cross)")
        except Exception as e:
            print(f"  [ERROR] {cat}: {e}", file=sys.stderr)
            all_data[cat] = []
            summary[cat] = 0

    # ── Second pass: retry any failed categories with extra patience ──
    failed = [c for c in fetch_order if summary.get(c, 0) == 0]
    if failed:
        print(f"\n--- Second pass for failed: {failed} ---")
        time.sleep(30)
        for cat in failed:
            print(f"\n--- Retrying {cat} ---")
            try:
                papers = fetch_category(cat, qd)
                if papers:
                    all_data[cat] = papers
                    summary[cat] = len(papers)
                    primary = [p for p in papers if p["PrimaryCat"].startswith(cat if cat == "astro-ph" else cat)]
                    cross = len(papers) - len(primary)
                    print(f"  {cat}: RECOVERED {len(papers)} papers ({len(primary)} primary, {cross} cross)")
                else:
                    print(f"  {cat}: still 0 papers")
            except Exception as e:
                print(f"  [ERROR] retry {cat}: {e}", file=sys.stderr)
            time.sleep(10)

    # ── STEP 2: Translate ──
    for cat in args.cats:
        papers = all_data[cat]
        if not papers:
            print(f"\n--- {cat}: 0 papers, skip translate ---")
            continue
        print(f"\n--- Translating {cat} ({len(papers)} papers) ---")
        try:
            translate_all(papers, cat, api_key)
        except Exception as e:
            print(f"  [ERROR] translate {cat}: {e}", file=sys.stderr)

    # ── STEP 3: Build HTML ──
    print(f"\n--- Building HTML ---")
    for cat in args.cats:
        papers = all_data[cat]
        if not papers:
            print(f"  {cat}: 0 papers, skip")
            continue
        build_category_html(papers, cat, args.out, display, qd)

    build_hub_html(summary, args.out, display)

    # ── STEP 4: Save summary ──
    spath = os.path.join(args.out, "summary.json")
    with open(spath, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    total = sum(summary.values())
    print(f"\n=== Done: {total} papers ===")
    for cat in args.cats:
        print(f"  {cat}: {summary[cat]} 篇")
    print(f"  HTML: {args.out}/")

    # Output summary for GitHub Actions workflow steps
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as gf:
            for cat, count in summary.items():
                safe_key = cat.replace("-", "_").replace(".", "_")
                gf.write(f"{safe_key}={count}\n")
            gf.write(f"display={display}\n")
            gf.write(f"total={total}\n")


if __name__ == "__main__":
    main()
