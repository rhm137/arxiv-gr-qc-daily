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
from datetime import datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError

# ── Config ──
ARXIV_API = "https://export.arxiv.org/api/query"
LISTING_BASE = "https://arxiv.org/list"   # 权威批次页面 /list/{cat}/new
LISTING_SHOW = 2000                       # listing 页大小（合法值 25..2000）
META_CHUNK = 50                           # id_list 元数据分块大小
ARXIV_UA  = "WorkBuddy-arxiv-digest/2.0"
ARXIV_RETRY = 8
ARXIV_DELAY = 30                          # 重试退避基数（秒）：30/60/.../递增，fetch_url 内封顶 300s
ARXIV_GAP = 8                             # 相邻 arXiv 请求间隔（秒），防 429 限流
DEFAULT_SUMMARY_URL = "https://rhm137.github.io/arxiv-gr-qc-daily/summary.json"

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
# BATCH DATE / LISTING
# ═══════════════════════════════════════════════════════════════════════
# arXiv 公告机制（官方时刻表 info.arxiv.org/help/availability.html + 2026-09 实测修正）：
#   - 提交截止：每个工作日美东 14:00；截止后提交进入下一个公告窗口。
#   - 公告：美东 Sun/Mon/Tue/Wed/Thu 的 20:00 发布（Fri/Sat 无公告）。
#   - ⚠️ 批次标签 = 公告的【次日】（官方原话 "Mailed Thursday night / Friday morning"）。
#     例：Mon 20:00 ET 公告 → 批次标签 Tuesday；listing 页在当天夜里
#     （约美东午夜前后）翻页到新批次。实测：每天 00:00–01:00 ET 时
#     listing 已显示【当天】日期标签的批次（Mon–Fri 才有批次，Sat/Sun 无）。
#   - 少量论文会因审核挂起延迟数日才进入批次（submittedDate 无法推算），
#     因此批次成员以 listing 页面为唯一权威来源。

def _expected_label() -> str:
    """当前时刻理应已发布的最新批次标签（YYYY-MM-DD，批次标签上的日期）。

    批次标签为 Mon–Fri 的 ET 日期；标签为 D 的批次在 D-1 的 20:00 ET 公告，
    实测在 D 天午夜 ET 之前 listing 已完成翻页（2026-09-14/15/17 三天的
    00:00 ET 探测均已看到当天批次）。因此规则极简单：取当前 ET 日期，
    Sat/Sun 回退到周五。若偶尔翻页延迟（listing 仍显示昨天），
    main() 的等待循环会兜底，无需在此处留缓冲——2026-09-17 的教训：
    缓冲导致期望=昨天、listing=今天，每次运行白等 50 分钟并诱发限流。
    """
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now = datetime.utcnow() - timedelta(hours=4)
    d = now.date()
    while d.weekday() in (5, 6):  # Sat/Sun 无批次 → 回退到周五
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def scrape_listing(cat_query: str) -> dict:
    """
    解析 https://arxiv.org/list/{cat}/new —— 当前已公告的权威批次。

    Returns {"label": "YYYY-MM-DD", "new": [ids], "cross": [ids]}
    listing 的 New/Cross submissions 与官网展示完全一致（含延迟发布论文）。
    """
    url = f"{LISTING_BASE}/{cat_query}/new?skip=0&show={LISTING_SHOW}"
    html = fetch_url(url)

    m = re.search(r"Showing new listings for \w+day, (\d{1,2}) (\w+) (\d{4})", html)
    if not m:
        raise RuntimeError(f"cannot parse listing label from /list/{cat_query}/new")
    label = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y").strftime("%Y-%m-%d")

    new_ids, cross_ids = [], []
    section, counts = None, {}
    for chunk in re.split(r"(<h3[^>]*>.*?</h3>)", html):
        hm = re.search(r"<h3[^>]*>(.*?)</h3>", chunk, re.S)
        if hm:
            head = re.sub(r"<[^>]+>", "", hm.group(1))
            if "New submissions" in head:
                section = "new"
            elif "Cross submissions" in head:
                section = "cross"
            else:
                section = None
            cm = re.search(r"showing (?:first )?(\d+) of (\d+) entries", head)
            if section and cm:
                counts[section] = int(cm.group(2))
            continue
        if section:
            # 论文 ID 只出现在 <dt> 条目内（区段附注中可能引用其他论文链接）
            for dt in re.findall(r"<dt>(.*?)</dt>", chunk, re.S):
                m2 = re.search(r"abs/(\d{4}\.\d+)", dt)
                if not m2:
                    continue
                pid = m2.group(1)
                lst = new_ids if section == "new" else cross_ids
                if pid not in lst:
                    lst.append(pid)

    for name, ids in (("new", new_ids), ("cross", cross_ids)):
        expect = counts.get(name)
        if expect is not None and len(ids) != expect:
            raise RuntimeError(
                f"{cat_query} {name}: parsed {len(ids)} ids but listing says {expect} — page truncated or layout changed")
    return {"label": label, "new": new_ids, "cross": cross_ids}


def _live_summary_date() -> str:
    """线上已推送批次的日期（读取 GitHub Pages 上的 summary.json）；失败返回 ''。"""
    url = os.environ.get("PAGES_SUMMARY_URL", DEFAULT_SUMMARY_URL)
    try:
        text = fetch_url(url, retries=2)
        return str(json.loads(text).get("date", ""))
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════
# FETCH
# ═══════════════════════════════════════════════════════════════════════

def fetch_url(url: str, retries: int = ARXIV_RETRY) -> str:
    """Fetch URL with robust retries.

    arXiv 对 GitHub Actions 共享 IP 的限流/拦截较常见（429，或 WAF 式的 406），
    退避需要足够长：30/60/90/... 秒递增，封顶 300s；并带上浏览器风格的
    Accept 头（裸 urllib 默认不带 Accept，易触发 406）。
    """
    last_err = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={
                "User-Agent": ARXIV_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })
            with urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                wait = min(ARXIV_DELAY * (attempt + 1), 300)
                print(f"  [RETRY {attempt+1}/{retries}] {e} — waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
    raise RuntimeError(f"arXiv unreachable after {retries} attempts: {last_err}")


def fetch_category(cat: str) -> tuple[list[dict], str]:
    """
    抓取 `cat` 当前已公告的批次（与 arXiv 官网 /new 完全一致）。

    Returns (papers, label_date)。paper dict 带 "Section": "new"|"cross"。
    astro-ph = astro-ph.CO + astro-ph.HE 两个子分区合并。
    """
    queries = ASTRO_SUBS if cat == "astro-ph" else [cat]
    label = None
    sections: dict[str, str] = {}
    order: list[str] = []
    for q in queries:
        print(f"  Scraping /list/{q}/new ...")
        listing = scrape_listing(q)
        if label is None:
            label = listing["label"]
        elif listing["label"] != label:
            print(f"  [WARN] {q} label {listing['label']} != {label}")
        for pid in listing["new"]:
            if pid not in sections:
                sections[pid] = "new"
                order.append(pid)
        for pid in listing["cross"]:
            if pid not in sections:
                sections[pid] = "cross"
                order.append(pid)
        print(f"    {q}: {len(listing['new'])} new + {len(listing['cross'])} cross ({listing['label']})")
        if len(queries) > 1:
            time.sleep(ARXIV_GAP)

    papers = _fetch_meta(order)
    by_id = {p["ID"]: p for p in papers}
    out = []
    for pid in order:
        p = by_id.get(pid)
        if p is None:
            print(f"  [WARN] {pid} on listing but missing from API metadata")
            continue
        p["Section"] = sections[pid]
        out.append(p)
    return out, label or ""


def _fetch_meta(ids: list[str]) -> list[dict]:
    """按 id_list 分块拉取论文元数据。"""
    papers, seen = [], set()
    for i in range(0, len(ids), META_CHUNK):
        chunk = ids[i:i + META_CHUNK]
        url = f"{ARXIV_API}?id_list={','.join(chunk)}&max_results={len(chunk)}"
        _parse_xml(fetch_url(url), seen, papers)
        if i + META_CHUNK < len(ids):
            time.sleep(ARXIV_GAP)
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
        pid = re.sub(r"v\d+$", "", pid)   # 去掉版本号，与 listing ID 对齐
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
            "Published": txt("a:published"),
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
Provide the following four items in Chinese. Output ONLY valid JSON, no other text.

1. "cn_title": Translate the title into Chinese. Preserve all technical abbreviations in English (e.g., {info['abbr']}).
2. "cn_oneliner": 一句话简介（30–50 字），概括**这篇论文**做了什么、得到什么最重要的结果。
   句式如"用 X 方法研究/证明了 Y，发现 Z"。以论文为主语，不写领域背景，
   不简单重复标题。这张卡片折叠时只显示这句话，必须独立成立、一眼看懂。
3. "cn_summary": 速览 — 中文 250–350 字，分两段：
   第一段（2–3 句）背景与动机：这个领域已知什么、还缺什么、为什么这个问题值得做，
   让没读过相关文献的读者也能进入语境。
   第二段（3–4 句）内容总结：方法 → 关键结果，必须保留摘要中的具体数字、
   置信度、样本量、对象名称等硬信息，不要泛泛而谈。
   Use $...$ for all LaTeX math symbols.
4. "cn_review": 评价 — 中文 200–250 字，务必精炼，分三段，每段以【创新点】【局限性】【意义与读者】开头：

   【创新点】1–2 句：与已有工作相比新在哪（具体说明，引用论文中的关键结果）。
   【局限性】1–2 句：方法或假设最主要的弱点/适用范围；摘要未体现就明说。
   【意义与读者】1 句：什么样的读者值得读原文。

   硬性要求：直接、具体、不重复速览已说过的内容；
   禁止使用"具有重要意义""提供了新思路""有望推动"等空泛套话。

CRITICAL: LaTeX math in $...$. Output ONLY valid JSON.
The JSON object must contain four keys: cn_title, cn_oneliner, cn_summary, cn_review."""


RETRY_PROMPT_TMPL = f"""The previous translation had quality issues. Re-translate MORE CAREFULLY.
- cn_title MUST be fully in Chinese (except: {RETRY_ABBR})
- cn_oneliner MUST be a single 30–50 character sentence stating what THIS paper did and its key result (no background, not a copy of the title)
- cn_summary MUST be >70% Chinese characters, start with 2–3 sentences of background/motivation, then methods and concrete results (numbers required)
- cn_review MUST contain all three markers: 【创新点】, 【局限性】, 【意义与读者】, be CONCISE (200–250 Chinese characters), and cite concrete results (no empty boilerplate)

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
                    p["CN_Oneliner"] = str(result.get("cn_oneliner", ""))
                    p["CN_Summary"] = str(result.get("cn_summary", ""))
                    p["CN_Review"] = str(result.get("cn_review", ""))
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
            p["CN_Oneliner"] = ""
            p["CN_Summary"] = abstract
            p["CN_Review"] = "⚠ 翻译校验未通过，请查看原文摘要。"
            print("    FLAGGED")

        time.sleep(6)  # pace between papers

    print(f"  [{cat}] {total} papers, {flagged} flagged ({time.time()-t0:.0f}s)")
    return flagged


def _validate(result: dict) -> list[str]:
    issues = []
    cn_title = result.get("cn_title", "")
    cn_oneliner = result.get("cn_oneliner", "")
    cn_summary = result.get("cn_summary", "")
    cn_review = result.get("cn_review", "")

    if not cn_title.strip():
        issues.append("title empty")

    ol = cn_oneliner.strip()
    if not ol:
        issues.append("oneliner empty")
    elif len(ol) > 90:
        issues.append("oneliner too long")

    if not cn_summary.strip():
        issues.append("summary empty")
    else:
        cn_chars = len(re.findall(r'[\u4e00-\u9fff]', cn_summary))
        if len(cn_summary) > 20 and cn_chars / max(len(cn_summary), 1) < 0.15:
            issues.append("low CN ratio")

    if len(re.findall(r'[\u4e00-\u9fff]', cn_review)) < 120:
        issues.append("review too short")
    markers = ["创新点", "局限性", "意义与读者"]
    if sum(1 for m in markers if m in cn_review) < 2:
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


def _one_liner(summary_text: str) -> str:
    if not summary_text: return ""
    m = re.match(r"^(.*?[。；])", summary_text)
    return m.group(1) if m else summary_text[:80] + "…"


def _format_authors(paper: dict, max_n: int = 4) -> str:
    authors = paper.get("Authors", "")
    names = [a.strip() for a in authors.split(";") if a.strip()]
    if len(names) <= max_n: return ", ".join(names)
    return ", ".join(names[:max_n]) + f" et al. ({len(names)} authors)"


def build_category_html(papers: list[dict], cat: str, out_dir: str, date_display: str, api_date: str):
    """Build date-stamped + latest HTML for a category."""
    meta = CATEGORY_META[cat]
    # 排序：主分类论文在前、交叉列表在后；交叉再按来源主分类分组（组内保持 listing 顺序，
    # 组间按论文数量降序，数量相同按分类名），相同交叉分区的论文尽量排在一起
    # （Section 来自 listing 页面的 New/Cross 分区，比 PrimaryCat 前缀更准确）
    is_primary = lambda p: (p.get("Section") == "new") if p.get("Section") \
        else p.get("PrimaryCat", "").startswith(cat if cat == "astro-ph" else cat)
    primary = [p for p in papers if is_primary(p)]
    cross = [p for p in papers if not is_primary(p)]
    cross_groups: dict[str, list] = {}
    for p in cross:
        cross_groups.setdefault(p.get("PrimaryCat", "?"), []).append(p)
    cross_order = sorted(cross_groups, key=lambda c: (-len(cross_groups[c]), c))
    ordered_cross = [p for c in cross_order for p in cross_groups[c]]
    papers = primary + ordered_cross
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
        csum = _escape(p.get("CN_Summary", ""))
        crev = _escape(p.get("CN_Review", ""))
        auth = _escape(_format_authors(p))
        onel = _escape(p.get("CN_Oneliner") or _one_liner(p.get("CN_Summary", "")))
        is_x = p in cross
        xbadge = f" [交叉: {_escape(p.get('PrimaryCat',''))}]" if is_x else ""

        cards.append(f'<details class="paper-card" id="paper-{i}"><summary>'
            f'<div class="card-body"><div class="card-num">#{i}{xbadge}  ·  {_escape(pid)}</div>'
            f'<div class="card-title">{ttl}</div><div class="card-title-cn">{cnt}</div>'
            f'<div class="card-authors">{auth}</div><div class="card-oneline">{onel}</div></div></summary>'
            f'<div class="detail"><h4>速览</h4><p>{csum}</p><h4>评价</h4><p>{crev}</p>'
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
    parser.add_argument("--date", default=None, help="期望的批次日期 YYYY-MM-DD（仅校验用，实际抓取以官网 /new 当前批次为准）")
    parser.add_argument("--cats", nargs="+", default=["gr-qc", "hep-th", "astro-ph"])
    parser.add_argument("--out", default="./outputs-public", help="Output dir for HTML")
    parser.add_argument("--wait-minutes", type=int, default=45,
                        help="若当前批次尚未发布（标签早于预期），轮询等待的分钟数上限")
    args = parser.parse_args()

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("ERROR: DEEPSEEK_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.out, exist_ok=True)

    # ── STEP 0: 等待最新批次发布 ──
    # 批次在美东 Sun~Thu 的 20:00 发布（= 北京次日早上 8:00 EDT / 9:00 EST）。
    # 若运行过早（listing 仍是上一批），轮询等待最多 --wait-minutes，
    # 避免漏推/重复推。
    probe_cat = args.cats[0] if args.cats else "gr-qc"
    expected = _expected_label()
    print(f"Expected batch label: {expected}")

    # ── 抓取缓存通道（本地兜底）──
    # FETCH_CACHE_FILE 指向预先抓好的 {"label","data"} JSON，且其 label ==
    # 当天期望批次时，跳过全部 arXiv 网络请求。用于 arXiv 封锁 GitHub
    # Actions IP（406/429）的日子：本地跑 fetch_today.py 后提交缓存文件，
    # Actions 只做翻译/生成/部署/推送。label 不匹配时自动忽略缓存。
    all_data, summary, label = {}, {}, ""
    cache_file = os.environ.get("FETCH_CACHE_FILE", "")
    if cache_file and os.path.exists(cache_file):
        cached = None
        try:
            with open(cache_file, encoding="utf-8") as cf:
                cached = json.load(cf)
        except Exception as e:
            print(f"[WARN] fetch cache unreadable: {e}", file=sys.stderr)
        if cached and cached.get("label") == expected:
            all_data = {c: cached["data"].get(c, []) for c in args.cats}
            summary = {c: len(all_data[c]) for c in args.cats}
            label = cached["label"]
            print(f"Using committed fetch cache: batch {label} — "
                  + ", ".join(f"{c}={summary[c]}" for c in args.cats))
        elif cached:
            print(f"fetch cache is for batch {cached.get('label')} "
                  f"(expected {expected}) — fetching live")

    if not label:
        waited = 0
        while not args.date:
            label = scrape_listing(probe_cat)["label"]
            if label == expected:
                break
            if waited >= args.wait_minutes:
                print(f"[WARN] listing still shows {label} (expected {expected}) after {waited}min — proceeding anyway")
                break
            print(f"  Batch {expected} not published yet (listing shows {label}), waiting 10min ...")
            time.sleep(600)
            waited += 10

        # ── STEP 1: Fetch（listing 为权威来源，label 即批次日期）──
        label = ""
        for cat in args.cats:
            print(f"\n--- Fetching {cat} ---")
            try:
                papers, cat_label = fetch_category(cat)
                if cat_label and not label:
                    label = cat_label
                all_data[cat] = papers
                summary[cat] = len(papers)
                primary = [p for p in papers if p.get("Section") == "new"]
                print(f"  {cat}: {len(papers)} papers ({len(primary)} primary, {len(papers)-len(primary)} cross)")
            except Exception as e:
                print(f"  [ERROR] {cat}: {e}", file=sys.stderr)
                all_data[cat] = []
                summary[cat] = 0
            time.sleep(ARXIV_GAP)   # 分区之间留间隔，防 arXiv 429 限流

        # ── Second pass: retry any failed categories with extra patience ──
        failed = [c for c in args.cats if summary.get(c, 0) == 0]
        if failed:
            print(f"\n--- Second pass for failed: {failed} ---")
            time.sleep(30)
            for cat in failed:
                print(f"\n--- Retrying {cat} ---")
                try:
                    papers, cat_label = fetch_category(cat)
                    if papers:
                        if cat_label and not label:
                            label = cat_label
                        all_data[cat] = papers
                        summary[cat] = len(papers)
                        primary = [p for p in papers if p.get("Section") == "new"]
                        print(f"  {cat}: RECOVERED {len(papers)} papers ({len(primary)} primary, {len(papers)-len(primary)} cross)")
                    else:
                        print(f"  {cat}: still 0 papers")
                except Exception as e:
                    print(f"  [ERROR] retry {cat}: {e}", file=sys.stderr)
                time.sleep(10)

    if not label:
        print("ERROR: no batch label obtained — all categories failed", file=sys.stderr)
        sys.exit(1)

    # 与 --date 期望值核对（仅提示，不中断）
    if args.date and args.date != label:
        print(f"[WARN] --date {args.date} requested, but current listing batch is {label}")

    qd = label.replace("-", "")
    display = f"{label[0:4]}年{label[5:7]}月{label[8:10]}日"
    print(f"\nBatch: {label}  Display: {display}")

    # ── 重复推送防护：线上已推送同一批次则跳过 ──
    pushed_date = _live_summary_date()
    new_batch = label != pushed_date
    force = os.environ.get("FORCE_RERUN", "").lower() in ("1", "true", "yes")
    if force and not new_batch:
        print("FORCE_RERUN: 强制重跑当前已推批次（调试用）")
        new_batch = True
    print(f"Pushed batch on Pages: {pushed_date or '(unknown)'} → new_batch={new_batch}")
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as gf:
            gf.write(f"new_batch={'true' if new_batch else 'false'}\n")
    if not new_batch:
        print("This batch was already pushed — skipping translate/build/push.")
        return

    # ── 残缺保护：任何分区 0 篇则中止，绝不部署不完整报告 ──
    # （放在 new_batch 检查之后：已推过的批次即使本次抓取受限流影响也干净跳过）
    zero_cats = [c for c in args.cats if summary.get(c, 0) == 0]
    if zero_cats:
        print(f"ERROR: {zero_cats} fetched 0 papers — aborting WITHOUT deploy to avoid "
              f"overwriting Pages with an incomplete report", file=sys.stderr)
        sys.exit(1)

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

    # ── STEP 4: Save summary（含批次日期，供重复推送防护比对）──
    spath = os.path.join(args.out, "summary.json")
    with open(spath, "w", encoding="utf-8") as f:
        json.dump({"date": label, **summary}, f, ensure_ascii=False, indent=2)

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
