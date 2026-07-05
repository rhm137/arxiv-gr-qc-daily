#!/usr/bin/env python3
"""
Fetch arXiv gr-qc papers for a given date and parse to JSON.

Usage:
    python fetch_papers.py YYYYMMDD [output_dir]

    output_dir defaults to ~/arxiv/

The script:
1. Queries arXiv API for gr-qc papers submitted on the given date.
2. Parses the Atom XML response.
3. Saves parsed papers as all-papers.json.
4. Prints summary statistics.
"""

import json
import os
import sys
import xml.etree.ElementTree as ET
from urllib.request import urlopen, Request
from urllib.error import URLError


ARXIV_API = "http://export.arxiv.org/api/query"
MAX_RESULTS = 100
USER_AGENT = "WorkBuddy-arxiv-gr-qc-skill/1.0"


def fetch_papers(date_str: str) -> str:
    """Fetch raw XML from arXiv API for gr-qc on the given date.

    Args:
        date_str: Date in YYYYMMDD format.

    Returns:
        Raw XML string from arXiv API.
    """
    query = (
        f"cat:gr-qc+AND+submittedDate:[{date_str}0000+TO+{date_str}2359]"
    )
    url = (
        f"{ARXIV_API}?search_query={query}"
        f"&sortBy=submittedDate&sortOrder=ascending"
        f"&start=0&max_results={MAX_RESULTS}"
    )

    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except URLError as e:
        print(f"[ERROR] Failed to fetch from arXiv API: {e}", file=sys.stderr)
        sys.exit(1)


def parse_xml(xml_text: str) -> list[dict]:
    """Parse arXiv Atom XML into a list of paper dicts.

    Args:
        xml_text: Raw XML string from arXiv API.

    Returns:
        List of paper dictionaries.
    """
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
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} YYYYMMDD [output_dir]")
        sys.exit(1)

    date_str = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser("~/arxiv/")

    if not date_str.isdigit() or len(date_str) != 8:
        print(f"[ERROR] Invalid date format: {date_str}. Expected YYYYMMDD.",
              file=sys.stderr)
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)

    print(f"Fetching arXiv gr-qc papers for date: {date_str}...")
    xml_text = fetch_papers(date_str)

    # Save raw XML
    xml_path = os.path.join(output_dir, "raw-data.xml")
    with open(xml_path, "w", encoding="utf-8") as f:
        f.write(xml_text)
    print(f"  Raw XML saved to: {xml_path}")

    # Parse
    papers = parse_xml(xml_text)
    print(f"  Parsed {len(papers)} papers from XML.")

    if not papers:
        print("[WARN] No papers found for this date. The arXiv listing may not "
              "be available yet (usually updates around 00:00 UTC).")
        sys.exit(0)

    # Statistics
    primary = [p for p in papers if p["PrimaryCat"] == "gr-qc"]
    cross = [p for p in papers if p["PrimaryCat"] != "gr-qc"]

    cross_cats = {}
    for p in cross:
        c = p["PrimaryCat"]
        cross_cats[c] = cross_cats.get(c, 0) + 1

    print(f"\n  Summary:")
    print(f"    Total papers: {len(papers)}")
    print(f"    Primary (gr-qc): {len(primary)}")
    print(f"    Cross-listed: {len(cross)}")
    if cross_cats:
        for cat, count in sorted(cross_cats.items(), key=lambda x: -x[1]):
            print(f"      - {cat}: {count}")

    # Save JSON
    json_path = os.path.join(output_dir, "all-papers.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(papers, f, ensure_ascii=False, indent=2)
    print(f"\n  JSON saved to: {json_path}")


if __name__ == "__main__":
    main()
