#!/usr/bin/env python3
"""
Fetch arXiv papers for multiple categories on a given date and parse to JSON.

Usage:
    python fetch_papers.py YYYYMMDD [output_dir] [--cats cat1 cat2 ...]

    output_dir defaults to ./outputs/
    --cats defaults to gr-qc hep-th astro-ph

The script:
1. Queries arXiv API for each category's papers submitted on the given date.
2. Parses the Atom XML response.
3. Saves parsed papers per category as {cat}.json.
4. Saves a summary.json with per-category paper counts.
5. Prints summary statistics.
"""

import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from argparse import ArgumentParser
from urllib.request import urlopen, Request
from urllib.error import URLError


ARXIV_API = "http://export.arxiv.org/api/query"
MAX_RESULTS = 200
USER_AGENT = "WorkBuddy-arxiv-multi-skill/1.0"

# For astro-ph we query two most relevant sub-categories to keep volume manageable
ASTRO_SUBCATS = ["astro-ph.CO", "astro-ph.HE"]

CATEGORY_INFO = {
    "gr-qc":    {"name": "引力与量子宇宙学", "cn_name": "广义相对论与量子宇宙学"},
    "hep-th":   {"name": "高能理论物理",     "cn_name": "高能物理-理论"},
    "astro-ph": {"name": "天体物理",         "cn_name": "天体物理学"},
}


def fetch_raw_xml(cat_query: str, date_str: str, max_results: int = MAX_RESULTS) -> str:
    """Fetch raw XML from arXiv API for a query on the given date."""
    query = (
        f"{cat_query}+AND+submittedDate:[{date_str}0000+TO+{date_str}2359]"
    )
    url = (
        f"{ARXIV_API}?search_query={query}"
        f"&sortBy=submittedDate&sortOrder=ascending"
        f"&start=0&max_results={max_results}"
    )

    req = Request(url, headers={"User-Agent": USER_AGENT})
    print(f"  GET {url[:200]}...")
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except URLError as e:
        print(f"[ERROR] Failed to fetch {cat_query} from arXiv API: {e}", file=sys.stderr)
        return ""


def parse_xml(xml_text: str) -> list[dict]:
    """Parse arXiv Atom XML into a list of paper dicts."""
    ns = {
        "a": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }

    root = ET.fromstring(xml_text)
    papers = []

    for entry in root.findall(".//a:entry", ns):
        def _text(tag):
            el = entry.find(tag, ns)
            return el.text.strip() if el is not None and el.text else ""

        paper_id = _text("a:id").replace("http://arxiv.org/abs/", "")

        authors = [
            a.find("a:name", ns).text.strip()
            for a in entry.findall("a:author", ns)
            if a.find("a:name", ns) is not None
        ]

        primary_cat_el = entry.find("arxiv:primary_category", ns)
        primary_cat = (
            primary_cat_el.attrib["term"]
            if primary_cat_el is not None
            else "unknown"
        )

        all_cats = [
            c.attrib["term"]
            for c in entry.findall("a:category", ns)
        ]

        comment_el = entry.find("arxiv:comment", ns)
        comment = comment_el.text.strip() if (
            comment_el is not None and comment_el.text
        ) else ""

        paper = {
            "ID": paper_id,
            "Title": _text("a:title"),
            "Authors": "; ".join(authors),
            "Summary": _text("a:summary"),
            "PrimaryCat": primary_cat,
            "AllCats": ", ".join(all_cats),
            "Comment": comment,
        }
        papers.append(paper)

    return papers


def main():
    parser = ArgumentParser(description="Fetch arXiv papers for multiple categories")
    parser.add_argument("date", help="Date in YYYYMMDD format")
    parser.add_argument("output_dir", nargs="?", default="./outputs/",
                        help="Output directory (default: ./outputs/)")
    parser.add_argument("--cats", nargs="+",
                        default=["gr-qc", "hep-th", "astro-ph"],
                        help="Categories to fetch (default: gr-qc hep-th astro-ph)")

    args = parser.parse_args()
    date_str = args.date

    if not date_str.isdigit() or len(date_str) != 8:
        print(f"[ERROR] Invalid date format: {date_str}. Expected YYYYMMDD.",
              file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.output_dir, exist_ok=True)
    summary = {}

    for cat in args.cats:
        info = CATEGORY_INFO.get(cat, {"name": cat, "cn_name": cat})
        print(f"\n{'='*60}")
        print(f"Fetching arXiv {cat} ({info['cn_name']}) papers for {date_str}...")

        if cat == "astro-ph":
            # astro-ph: query two sub-categories, merge, dedup
            all_papers = []
            seen_ids = set()
            for sub in ASTRO_SUBCATS:
                print(f"  Querying {sub}...")
                xml_text = fetch_raw_xml(f"cat:{sub}", date_str, max_results=200)
                if not xml_text:
                    print(f"    [SKIP] No response for {sub}")
                    continue
                sub_papers = parse_xml(xml_text)
                # Dedup by paper ID
                new = 0
                for p in sub_papers:
                    if p["ID"] not in seen_ids:
                        seen_ids.add(p["ID"])
                        all_papers.append(p)
                        new += 1
                print(f"    {len(sub_papers)} from API, {new} new after dedup")
                time.sleep(3)  # be nice to arXiv API between sub-queries
            papers = all_papers
        else:
            xml_text = fetch_raw_xml(f"cat:{cat}", date_str)
            if not xml_text:
                print(f"  [SKIP] No response for {cat}")
                summary[cat] = 0
                continue
            papers = parse_xml(xml_text)
            # Save raw XML
            xml_path = os.path.join(args.output_dir, f"{cat}-raw.xml")
            with open(xml_path, "w", encoding="utf-8") as f:
                f.write(xml_text)

        print(f"  Parsed {len(papers)} papers.")

        # Statistics
        if cat == "astro-ph":
            primary = [p for p in papers if p["PrimaryCat"].startswith("astro-ph")]
            cross = [p for p in papers if not p["PrimaryCat"].startswith("astro-ph")]
        else:
            primary = [p for p in papers if p["PrimaryCat"] == cat]
            cross = [p for p in papers if p["PrimaryCat"] != cat]

        cross_cats = {}
        for p in cross:
            c = p["PrimaryCat"]
            cross_cats[c] = cross_cats.get(c, 0) + 1

        print(f"  Primary ({cat}): {len(primary)}, Cross-listed: {len(cross)}")
        if cross_cats:
            for c, count in sorted(cross_cats.items(), key=lambda x: -x[1]):
                print(f"    - {c}: {count}")

        # Save per-category JSON
        json_path = os.path.join(args.output_dir, f"{cat}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(papers, f, ensure_ascii=False, indent=2)
        print(f"  JSON saved to: {json_path}")

        summary[cat] = len(papers)

        # Be nice to arXiv API
        if cat != args.cats[-1]:
            time.sleep(5)

    # Save summary
    summary_path = os.path.join(args.output_dir, "summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"Summary ({date_str}):")
    total = sum(summary.values())
    for cat in args.cats:
        cat_name = CATEGORY_INFO.get(cat, {}).get("cn_name", cat)
        print(f"  {cat} ({cat_name}): {summary[cat]} 篇")
    print(f"  总计: {total} 篇")
    print(f"  Saved to: {summary_path}")


if __name__ == "__main__":
    main()
