#!/usr/bin/env python3
"""
Translate and evaluate arXiv papers using a cloud LLM API (DeepSeek).

Usage:
    python translate_cloud.py <json_path> [--api-key KEY] [--model MODEL]
                              [--base-url URL] [--batch-size N]

Requires:
    pip install openai

Environment variables (alternative to CLI flags):
    DEEPSEEK_API_KEY     - API key
    DEEPSEEK_BASE_URL    - API base URL (default: https://api.deepseek.com)

Each paper is sent to the LLM with a structured prompt asking for:
1. Chinese title translation
2. Chinese abstract translation
3. ~300-character four-paragraph Chinese evaluation

Includes automatic quality validation: detects untranslated content,
missing evaluation sections, and malformed output. Failed papers
are auto-retried with adjusted parameters. Flagged papers get a ⚠️
marker in the output.

The enriched JSON is written back in place.
"""

import json
import os
import re
import sys
import time
from argparse import ArgumentParser


PROMPT_TEMPLATE = """You are a Chinese physicist specializing in general relativity and quantum cosmology. Review the following arXiv paper and provide your response in Chinese.

## Paper Information

- arXiv ID: {paper_id}
- Title: {title}
- Authors: {authors}
- Abstract (English): {abstract}

## Instructions

Provide the following three items in Chinese. Output ONLY valid JSON, no other text.

1. "cn_title": Translate the title into Chinese. Preserve all technical abbreviations in English (e.g., LIGO, GW, BH, GR, QPO, ISCO, FLRW).
2. "cn_abstract": Full Chinese translation of the abstract. Use $...$ for all LaTeX math symbols.
3. "cn_eval": A four-paragraph Chinese evaluation (~300 characters total):

Paragraph 1 — 研究问题: What problem does this paper address? (1-2 sentences)
Paragraph 2 — 方法/框架: What methods or framework are used? (2-3 sentences)
Paragraph 3 — 主要发现: Key findings and results. (2-3 sentences)
Paragraph 4 — 评价与展望: Significance and outlook. (2-3 sentences)

CRITICAL: All LaTeX math MUST be wrapped in $...$. Output ONLY the JSON, nothing else.

Example output format:
{{"cn_title": "中文标题", "cn_abstract": "中文摘要...", "cn_eval": "研究问题：...\\n\\n方法/框架：...\\n\\n主要发现：...\\n\\n评价与展望：..."}}
"""

RETRY_PROMPT = """The previous translation attempt for this paper had quality issues. Please re-translate MORE CAREFULLY.

- cn_title MUST be fully in Chinese (no English words except approved abbreviations: LIGO, GW, BH, GR, QPO, ISCO, FLRW, ADM, TOV, PBH, EHT, SKA, LISA, EGB)
- cn_abstract MUST be >70% Chinese characters
- cn_eval MUST contain all four markers: 研究问题, 方法/框架, 主要发现, 评价与展望

## Paper Information

- arXiv ID: {paper_id}
- Title: {title}
- Authors: {authors}
- Abstract (English): {abstract}

Output ONLY valid JSON. No excuses."""


def validate_translation(result: dict) -> list[str]:
    """Validate a translation result. Returns list of issues (empty = clean)."""
    issues = []

    cn_title = result.get("cn_title", "")
    cn_abstract = result.get("cn_abstract", "")
    cn_eval = result.get("cn_eval", "")

    # 1. Title must not be empty or purely English
    if not cn_title.strip():
        issues.append("cn_title is empty")
    else:
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', cn_title))
        english_words = len(re.findall(r'[a-zA-Z]{3,}', cn_title))
        if chinese_chars < 2 and english_words > 3:
            issues.append("cn_title appears to be mostly English")

    # 2. Abstract must have sufficient Chinese content
    if not cn_abstract.strip():
        issues.append("cn_abstract is empty")
    else:
        total_chars = len(cn_abstract)
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', cn_abstract))
        if total_chars > 20 and chinese_chars / max(total_chars, 1) < 0.3:
            issues.append(f"cn_abstract has low Chinese ratio ({chinese_chars}/{total_chars})")
        if total_chars < 30:
            issues.append("cn_abstract is too short")

    # 3. Evaluation must contain all four required markers
    for marker in ["研究问题", "方法", "主要发现", "评价"]:
        if marker not in cn_eval:
            issues.append(f"cn_eval missing marker: {marker}")

    # 4. Evaluation must be substantial
    if len(cn_eval) < 80:
        issues.append("cn_eval is too short")

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
        max_tokens=2048,
    )
    content = response.choices[0].message.content.strip()

    # Strip markdown code fences if present
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    return json.loads(content)


def translate_papers(json_path, api_key, base_url, model, batch_start=0, batch_size=None):
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url)

    with open(json_path, "r", encoding="utf-8") as f:
        papers = json.load(f)

    total = len(papers)
    if batch_size is None:
        batch_size = total

    batch_end = min(batch_start + batch_size, total)
    batch = papers[batch_start:batch_end]

    flagged_count = 0

    print(f"Processing papers {batch_start+1}-{batch_end} of {total}...")

    for i, paper in enumerate(batch):
        paper_id = paper.get("ID", "unknown")
        title = paper.get("Title", "")
        authors = paper.get("Authors", "Unknown")
        abstract = paper.get("Summary", "")

        prompt = PROMPT_TEMPLATE.format(
            paper_id=paper_id, title=title, authors=authors, abstract=abstract,
        )

        idx = batch_start + i
        print(f"  [{idx+1}/{total}] {paper_id}: {title[:60]}...")

        success = False
        for attempt in range(3):
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
                    print(f"    ⚠ Quality issues ({', '.join(issues[:2])})")
                    if attempt < 2:
                        print(f"    ↻ Retrying with stricter prompt...")
                        prompt = RETRY_PROMPT.format(
                            paper_id=paper_id, title=title, authors=authors, abstract=abstract,
                        )

            except json.JSONDecodeError as e:
                print(f"    ⚠ JSON parse error (attempt {attempt+1})")
                if attempt < 2:
                    prompt = RETRY_PROMPT.format(
                        paper_id=paper_id, title=title, authors=authors, abstract=abstract,
                    )
                time.sleep(2)

            except Exception as e:
                print(f"    ✗ API error (attempt {attempt+1}): {type(e).__name__}")
                time.sleep(5)

        if not success:
            # Fallback: use raw English
            flagged_count += 1
            paper["CN_Title"] = "⚠️ " + title
            paper["CN_Abstract"] = abstract
            paper["CN_Eval"] = "⚠️ 翻译校验未通过，请查看原文摘要。"
            print(f"    ✗ Marked as flagged")

        # Rate limiting
        if i < len(batch) - 1:
            time.sleep(1)

    # Save enriched JSON
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(papers, f, ensure_ascii=False, indent=2)

    status = "OK" if flagged_count == 0 else f"{flagged_count} flagged"
    print(f"\nDone! {batch_end - batch_start} papers ({status}). Saved to {json_path}")


def main():
    parser = ArgumentParser(description="Translate arXiv papers using cloud LLM")
    parser.add_argument("json_path", help="Path to all-papers.json")
    parser.add_argument("--api-key", default=os.environ.get("DEEPSEEK_API_KEY", ""))
    parser.add_argument("--base-url", default=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", "deepseek-chat"))
    parser.add_argument("--batch-start", type=int, default=0, help="Start index (0-based)")
    parser.add_argument("--batch-size", type=int, default=None, help="Number of papers per run")

    args = parser.parse_args()

    if not args.api_key:
        print("ERROR: DEEPSEEK_API_KEY not set. Use --api-key or set the environment variable.",
              file=sys.stderr)
        sys.exit(1)

    translate_papers(args.json_path, args.api_key, args.base_url, args.model,
                    args.batch_start, args.batch_size)


if __name__ == "__main__":
    main()
