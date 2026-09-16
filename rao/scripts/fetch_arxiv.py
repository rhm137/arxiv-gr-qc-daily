#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
arXiv 每日新稿抓取（gr-qc / hep-th / astro-ph.CO）

- 数据源：arXiv 官方 RSS 每日 listing（rss.arxiv.org）
- 按 arXiv ID（去版本号）跨类去重；只保留新上线论文
- 过滤已推送 ID（data/seen_ids.json）
- 关键词加权初筛（规则内嵌，来自 research_profile.md）
输出：
  data/listing_<date>.json     全部新稿（去重后）
  data/candidates_<date>.json  初筛候选（供 LLM 评分）
退出码：0 正常（含"无更新"情形，此时 candidates 为空且 no_update=true）；1 抓取失败
"""
from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

BJ = timezone(timedelta(hours=8))

FEEDS = {
    "gr-qc": "https://rss.arxiv.org/rss/gr-qc",
    "hep-th": "https://rss.arxiv.org/rss/hep-th",
    "astro-ph.CO": "https://rss.arxiv.org/rss/astro-ph.CO",
}

# (正则, 权重, 标签) —— 命中 title+abstract 即加分
KEYWORD_RULES = [
    (r"teleparallel|f\(T\)|f\(Q\)|nonmetricity|non-metricity|symmetric teleparallel|metric-affine|Nieh[–-]Yan|spin connection|tetrad|torsion", 6, "非黎曼几何/挠率"),
    (r"ELKO|mass[ -]dimension[ -]one|Elko", 6, "Elko"),
    (r"horizon thermodynamic|apparent horizon|Misner[–-]Sharp|Kodama|unified first law|first law of (black hole|horizon)|generalized second law|Wald entropy|Noether charge|Iyer[–-]Wald|covariant phase space|black hole chemistry|P[–-]V critical|Jacobson|entanglement entropy|Page curve|island|generalized entropy|Rényi|Renyi|Hawking temperature|Unruh|thermal (state|propert)|surface gravity", 5, "引力热力学"),
    (r"parity[- ]violation|birefringence|Chern[–-]Simons|circular polarization|helicity|chiral gravitational|gravitational wave polarization|cosmic birefringence", 5, "宇称破缺/双折射"),
    (r"Hamiltonian|constraint (algebra|analysis)|Ostrogradsky|strong coupling|strong-coupling|ghost|degrees of freedom|stability|second order action|kinetic matrix|degeneracy", 3, "自由度/稳定性"),
    (r"inflation|primordial|cosmological perturbation|mimetic|baryogenesis|leptogenesis|dark energy|de Sitter thermodynamics|curvaton|reheating", 3, "宇宙学"),
    (r"modified gravity|modified theories of gravity|higher[ -]derivative gravity|Gauss[–-]Bonnet|f\(R\)|scalar[–-]tensor|Horndeski|DHOST", 3, "修正引力"),
    (r"black hole|gravitational wave|CMB|Bogoliubov|quantum field theory in curved|curved spacetime|Kaluza[–-]Klein|holograph|AdS/CFT|Gaussian state|entanglement", 2, "背景相关"),
]

MIN_SCORE = 5
MAX_CANDIDATES = 40


def listing_date(now: datetime | None = None) -> str:
    """arXiv 日期：每天 08:00（北京时间）切换。定时任务在 07:5x 运行 => 取到的是昨天的 listing。"""
    now = now or datetime.now(BJ)
    return (now - timedelta(hours=8)).date().isoformat()


def fetch_feed(url: str) -> bytes:
    r = requests.get(url, timeout=40, headers={"User-Agent": "arxiv-daily-digest/1.0 (research use)"})
    r.raise_for_status()
    return r.content


def parse_items(content: bytes, source_cat: str) -> list[dict]:
    root = ET.fromstring(content)
    items = []
    for item in root.iter():
        if item.tag.split("}")[-1] != "item":
            continue
        rec: dict = {"source": source_cat, "categories": []}
        for ch in item:
            name = ch.tag.split("}")[-1]
            text = (ch.text or "").strip()
            if name == "title":
                text = re.sub(r"\s*\(arXiv:[^)]+\)\s*$", "", text)
                rec["title"] = re.sub(r"\s+", " ", text)
            elif name == "link":
                rec["link"] = text
            elif name == "description":
                mtype = re.search(r"Announce Type:\s*([\w-]+)", text)
                rec["announce_type"] = mtype.group(1) if mtype else "new"
                rec["abstract"] = re.sub(r"^Abstract:\s*", "", re.sub(r"\s+", " ", text))
            elif name == "category":
                if text:
                    rec["categories"].append(text)
            elif name in ("creator", "author"):
                rec["authors"] = text
        m = re.search(r"arxiv\.org/abs/([^/?#\s]+)", rec.get("link", ""))
        if not m:
            continue
        full = m.group(1)
        rec["id"] = re.sub(r"v\d+$", "", full)
        rec["version"] = full
        rec.setdefault("authors", "")
        rec.setdefault("abstract", "")
        items.append(rec)
    return items


def keyword_score(paper: dict) -> tuple[int, list[str]]:
    text = (paper["title"] + " " + paper["abstract"]).lower()
    total, tags = 0, []
    for pattern, weight, tag in KEYWORD_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            total += weight
            tags.append(tag)
    return total, sorted(set(tags))


def main() -> int:
    date = listing_date()
    seen_path = DATA / "seen_ids.json"
    seen = set(json.loads(seen_path.read_text(encoding="utf-8"))) if seen_path.exists() else set()

    all_papers: dict[str, dict] = {}
    per_feed: dict[str, int] = {}
    errors: list[str] = []
    for cat, url in FEEDS.items():
        try:
            items = parse_items(fetch_feed(url), cat)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{cat}: {e}")
            continue
        per_feed[cat] = len(items)
        for it in items:
            if it.get("announce_type", "new") != "new":
                continue  # 只收新上线论文，跳过 v2+ 替换与跨类转发
            if it["id"] not in all_papers:
                all_papers[it["id"]] = it
            else:
                all_papers[it["id"]]["source"] += "+" + cat

    if errors and not all_papers:
        print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False))
        return 1

    new_papers = [p for pid, p in all_papers.items() if pid not in seen]
    no_update = len(all_papers) == 0

    listing = {
        "listing_date": date,
        "fetched_at": datetime.now(BJ).isoformat(timespec="seconds"),
        "per_feed": per_feed,
        "total_deduped": len(all_papers),
        "already_seen": len(all_papers) - len(new_papers),
        "no_update": no_update,
        "papers": sorted(all_papers.values(), key=lambda p: p["id"]),
    }
    (DATA / f"listing_{date}.json").write_text(json.dumps(listing, ensure_ascii=False, indent=1), encoding="utf-8")

    scored = []
    for p in new_papers:
        s, tags = keyword_score(p)
        if s >= MIN_SCORE:
            scored.append({**p, "kscore": s, "ktags": tags})
    scored.sort(key=lambda p: -p["kscore"])
    candidates = scored[:MAX_CANDIDATES]

    out = {
        "listing_date": date,
        "no_update": no_update,
        "total_new_unseen": len(new_papers),
        "passed_prefilter": len(scored),
        "candidates": candidates,
        "errors": errors,
    }
    (DATA / f"candidates_{date}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "candidates"}, ensure_ascii=False))
    print(f"candidates: {len(candidates)} -> data/candidates_{date}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
