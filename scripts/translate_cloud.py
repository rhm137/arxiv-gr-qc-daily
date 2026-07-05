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

The enriched JSON is written back in place.
"""

import json
import os
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

    print(f"Processing papers {batch_start+1}-{batch_end} of {total}...")

    for i, paper in enumerate(batch):
        paper_id = paper.get("ID", "unknown")
        title = paper.get("Title", "")
        authors = paper.get("Authors", "Unknown")
        abstract = paper.get("Summary", "")

        prompt = PROMPT_TEMPLATE.format(
            paper_id=paper_id,
            title=title,
            authors=authors,
            abstract=abstract,
        )

        idx = batch_start + i
        print(f"  [{idx+1}/{total}] Translating {paper_id}: {title[:60]}...")

        for attempt in range(3):
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "You are a Chinese physicist. Always respond with valid JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.3,
                    max_tokens=2048,
                )
                content = response.choices[0].message.content.strip()

                # Strip markdown code fences if present
                if content.startswith("```"):
                    lines = content.split("\n")
                    content = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

                result = json.loads(content)
                paper["CN_Title"] = result.get("cn_title", "")
                paper["CN_Abstract"] = result.get("cn_abstract", "")
                paper["CN_Eval"] = result.get("cn_eval", "")
                print(f"    ✓ Done")
                break

            except json.JSONDecodeError as e:
                print(f"    ⚠ JSON parse error (attempt {attempt+1}): {e}")
                if attempt == 2:
                    paper["CN_Title"] = title
                    paper["CN_Abstract"] = abstract
                    paper["CN_Eval"] = "翻译失败，请稍后重试。"
                time.sleep(2)

            except Exception as e:
                print(f"    ✗ API error (attempt {attempt+1}): {e}")
                if attempt == 2:
                    paper["CN_Title"] = title
                    paper["CN_Abstract"] = abstract
                    paper["CN_Eval"] = "API 调用失败，请检查 API Key 和网络。"
                time.sleep(5)

        # Rate limiting between papers
        if i < len(batch) - 1:
            time.sleep(1)

    # Save enriched JSON
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(papers, f, ensure_ascii=False, indent=2)

    print(f"\nDone! {batch_end - batch_start} papers translated. Saved to {json_path}")


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
