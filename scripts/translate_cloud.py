#!/usr/bin/env python3
"""
Translate and evaluate arXiv papers using a cloud LLM API (DeepSeek).

Usage:
    python translate_cloud.py <json_path> [--category CAT]

Processes ALL papers in the JSON file one by one. Each paper is validated
and retried patiently until it passes (or gives up after 8 attempts).
Papers are saved back after each success to avoid data loss.

Requires: pip install openai
Env vars: DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL (optional), LLM_MODEL (optional)
"""

import json
import os
import re
import sys
import time
from argparse import ArgumentParser


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


def build_prompt(category: str) -> str:
    """Build the translation prompt template for a given category."""
    info = CATEGORY_PROMPTS.get(category, CATEGORY_PROMPTS["gr-qc"])
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

Paragraph 1 — 研究问题: What problem does this paper address? (1-2 sentences)
Paragraph 2 — 方法/框架: What methods or framework are used? (2-3 sentences)
Paragraph 3 — 主要发现: Key findings and results. (2-3 sentences)
Paragraph 4 — 评价与展望: Significance and outlook. (2-3 sentences)

CRITICAL: All LaTeX math MUST be wrapped in $...$. Output ONLY the JSON, nothing else.
The JSON object must contain three keys: cn_title, cn_abstract, cn_eval.
"""


RETRY_ABBR = "LIGO, GW, BH, GR, QPO, ISCO, FLRW, ADM, TOV, PBH, EHT, SKA, LISA, EGB, QFT, SUSY, CFT, EFT, RG, SUGRA, SNe, CMB, BAO, LSS, AGN, SMBH, GRB, FRB, DM, DE"

RETRY_PROMPT = f"""The previous translation attempt for this paper had quality issues. Please re-translate MORE CAREFULLY.

- cn_title MUST be fully in Chinese (no English words except approved abbreviations: {RETRY_ABBR})
- cn_abstract MUST be >70% Chinese characters
- cn_eval MUST contain all four markers: 研究问题, 方法/框架, 主要发现, 评价与展望

## Paper Information

- arXiv ID: {{paper_id}}
- Title: {{title}}
- Authors: {{authors}}
- Abstract (English): {{abstract}}

Output ONLY valid JSON. No excuses."""


def validate_translation(result: dict) -> list[str]:
    """Validate a translation result. Returns list of issues (empty = clean)."""
    issues = []

    cn_title = result.get("cn_title", "")
    cn_abstract = result.get("cn_abstract", "")
    cn_eval = result.get("cn_eval", "")

    # 1. Title must not be empty
    if not cn_title.strip():
        issues.append("cn_title empty")

    # 2. Abstract must have some Chinese content
    if not cn_abstract.strip():
        issues.append("cn_abstract empty")
    else:
        total_chars = len(cn_abstract)
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', cn_abstract))
        if total_chars > 20 and chinese_chars / max(total_chars, 1) < 0.15:
            issues.append(f"cn_abstract low CN ({chinese_chars}/{total_chars})")

    # 3. Evaluation must contain at least 2 of 4 required markers
    markers = ["研究问题", "方法", "主要发现", "评价"]
    found = sum(1 for m in markers if m in cn_eval)
    if found < 2:
        issues.append(f"eval missing markers ({found}/4)")

    return issues


def call_deepseek(client, model: str, prompt: str, temperature: float = 0.3) -> dict:
    """Call DeepSeek API and parse JSON response."""
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are a Chinese physicist. Always respond with valid JSON only."},
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
        max_tokens=4096,
    )
    content = response.choices[0].message.content.strip()

    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    return json.loads(content)


def translate_papers(json_path, api_key, base_url, model, category="gr-qc"):
    """
    Translate ALL papers in json_path one by one.
    Saves after each successful translation.
    """
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url)
    prompt_tmpl = build_prompt(category)

    with open(json_path, "r", encoding="utf-8") as f:
        papers = json.load(f)

    total = len(papers)
    if total == 0:
        print(f"[{category}] 0 papers, nothing to translate.")
        return

    flagged_count = 0
    t0 = time.time()

    for i, paper in enumerate(papers):
        paper_id = paper.get("ID", "unknown")
        title = paper.get("Title", "")
        authors = paper.get("Authors", "Unknown")
        abstract = paper.get("Summary", "")

        prompt = prompt_tmpl.format(
            paper_id=paper_id, title=title, authors=authors, abstract=abstract,
        )

        elapsed = time.time() - t0
        eta = (elapsed / max(i, 1)) * (total - i) if i > 0 else 0
        print(f"  [{i+1}/{total}] {paper_id}  (elapsed {elapsed:.0f}s, ETA {eta:.0f}s)")

        success = False
        for attempt in range(8):
            try:
                temp = 0.3 if attempt == 0 else 0.5
                result = call_deepseek(client, model, prompt, temperature=temp)
                issues = validate_translation(result)

                if not issues:
                    paper["CN_Title"] = result.get("cn_title", "")
                    paper["CN_Abstract"] = result.get("cn_abstract", "")
                    paper["CN_Eval"] = result.get("cn_eval", "")
                    print(f"    ✓ OK")
                    success = True
                    break
                else:
                    print(f"    ⚠ Issues ({', '.join(issues[:3])}) - attempt {attempt+1}/8")
                    if attempt < 7:
                        wait = 3 * (attempt + 1)
                        print(f"    ↻ Retrying in {wait}s...")
                        time.sleep(wait)
                        prompt = RETRY_PROMPT.format(
                            paper_id=paper_id, title=title, authors=authors, abstract=abstract,
                        )

            except json.JSONDecodeError:
                print(f"    ⚠ JSON parse error (attempt {attempt+1}/8)")
                if attempt < 7:
                    time.sleep(5)
                    prompt = RETRY_PROMPT.format(
                        paper_id=paper_id, title=title, authors=authors, abstract=abstract,
                    )

            except Exception as e:
                err_name = type(e).__name__
                err_msg = str(e)[:100]
                print(f"    ✗ {err_name}: {err_msg}")
                wait = 20 * (attempt + 1)
                print(f"    ↻ Waiting {wait}s...")
                time.sleep(wait)

        if not success:
            flagged_count += 1
            paper["CN_Title"] = "⚠️ " + title
            paper["CN_Abstract"] = abstract
            paper["CN_Eval"] = "⚠️ 翻译校验未通过，请查看原文摘要。"
            print(f"    ✗ Flagged after 8 attempts")

        # Save after each paper
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(papers, f, ensure_ascii=False, indent=2)

        # Gentle pace between papers
        time.sleep(10)

    elapsed = time.time() - t0
    flagged = f"{flagged_count} flagged" if flagged_count > 0 else "all clean"
    print(f"\n[{category}] Done! {total} papers in {elapsed:.0f}s ({flagged}).")


def main():
    parser = ArgumentParser(description="Translate arXiv papers using cloud LLM")
    parser.add_argument("json_path", help="Path to papers JSON file")
    parser.add_argument("--api-key", default=os.environ.get("DEEPSEEK_API_KEY", ""))
    parser.add_argument("--base-url", default=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", "deepseek-chat"))
    parser.add_argument("--category", default="gr-qc",
                        choices=["gr-qc", "hep-th", "astro-ph"],
                        help="arXiv category (default: gr-qc)")

    args = parser.parse_args()

    if not args.api_key:
        print("ERROR: DEEPSEEK_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    translate_papers(args.json_path, args.api_key, args.base_url, args.model, args.category)


if __name__ == "__main__":
    main()
