#!/usr/bin/env python3
"""本地抓取兜底通道：在本地网络抓取当天批次，写入 fetch-cache.json。

用途：arXiv 封锁 GitHub Actions IP（406/429）导致云端抓取失败时，
在本地（家庭/办公网络）跑这个脚本，然后 commit + push，再手动触发
Actions —— run.py 检测到 fetch-cache.json 的 label 与当天期望批次一致，
就会跳过全部 arXiv 请求，直接翻译/生成/部署/推送。

用法（在仓库根目录）：
    python scripts/fetch_today.py

注意：label 不是当天批次的缓存会被 run.py 自动忽略，不会误用旧数据。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run  # scripts/run.py

# 本地兜底时网络抖动常见，重试退避和请求间隔调小（云端限流场景才需要长退避）
run.ARXIV_DELAY = 5
run.ARXIV_GAP = 3

OUT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "fetch-cache.json")
CATS = ["gr-qc", "hep-th", "astro-ph"]


def main():
    expected = run._expected_label()
    print(f"Expected batch label (ET today): {expected}")

    data, label = {}, ""
    for cat in CATS:
        papers, cat_label = run.fetch_category(cat)
        data[cat] = papers
        label = label or cat_label
        print(f"  {cat}: {len(papers)} papers ({cat_label})")

    if label != expected:
        print(f"[WARN] listing batch {label} != expected {expected} — "
              f"缓存仍会写入，但 run.py 仅在 label==expected 时才使用它")

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump({"label": label, "data": data}, f, ensure_ascii=False)
    print(f"Wrote {OUT_FILE} — commit + push, 然后手动触发 Actions 即可。")


if __name__ == "__main__":
    main()
