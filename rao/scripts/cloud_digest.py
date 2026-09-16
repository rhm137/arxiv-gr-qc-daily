#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek 精读生成器（云端"AI 评分 + 精读"环节）

输入：data/candidates_<date>.json、research_profile.md、config.json
输出：data/digest_<date>.json（结构与本地 Kimi 版一致，供 deliver.py 使用）
昨日无更新 / 无候选时不写 digest，输出 {"digested": 0} 并以 0 退出。

环境：DEEPSEEK_API_KEY（或本地 config.local.json）
用法：python scripts/cloud_digest.py [--date 2026-09-16]
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BJ = timezone(timedelta(hours=8))
DS_URL = "https://api.deepseek.com/chat/completions"


def get_secret(name: str, default=None):
    v = os.environ.get(name)
    if v:
        return v
    p = ROOT / "config.local.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8")).get(name, default)
    return default


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def ds_chat(system: str, user: str, model: str, max_tokens: int, retries: int = 2) -> str:
    key = get_secret("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(DS_URL, headers=headers, json=body, timeout=600)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(6 * (attempt + 1))
    raise last


def parse_json_loose(text: str):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return json.loads(t)


SCORING_SYS = """你是理论物理研究助手，为一位做修正引力与引力热力学研究的用户（Rao）给 arXiv 论文打相关度分。

# 用户研究画像
{profile}

# 任务
给下列每篇论文打 0-10 的相关度分数（评分锚点见画像 §4），并给一句中文理由（≤40 字，说明为什么相关或不相关）。
严格执行画像 §3 的降权规则：纯观测/加参数重画相图式现象学/纯数值工作要压到 4 分以下。

# 输出（严格 JSON，不要输出其他内容）
{{"scores": [{{"id": "arXiv号", "score": 7, "reason": "一句话理由"}}]}}"""


DIGEST_SYS = """你是理论物理研究助手，为做修正引力与引力热力学研究的用户（Rao）精读 arXiv 论文并写每日推送内容。

# 用户研究画像
{profile}

# 任务
对给出的每篇论文，输出中文精读内容，字段如下：
- cn_title：中文译题（简洁准确，英文技术缩写保留）
- one_liner：一句话简介（≤60 字，说清做了什么和为什么值得注意）
- did_and_eval：「做了什么 + 简短评价」，2-4 句概括问题、方法、结论，然后另起一段以"评价："开头点出论证强度与潜在缺陷（如只查了某个扇区/依赖特殊背景/结论只是必要条件等）
- learn：「Rao 可以学到/了解到什么」，结合画像中的知识状态与当前学习模块，指出可借用的工具、恒等式、反例或需警惕的约定差异
- topics：「推荐课题」，1-2 个具体切入点，说明与画像 §1 课题卡（P1-P5/Q1）的关系与第一步计算；写不出具体的就填"暂无"，禁止编造

# 写作要求
- 全部中文；公式用 Unicode 纯文本（如 f_Q + 2Q·f_QQ ≥ 0），不要用 LaTeX
- 评价要诚实有棱角，不吹捧；不确定的地方明说"需回正文核对"
- 每个字段 80-200 字

# 输出（严格 JSON）
{{"papers": [{{"id": "arXiv号", "cn_title": "...", "one_liner": "...", "did_and_eval": "...", "learn": "...", "topics": "..."}}],
 "runner_ups": [{{"id": "arXiv号", "cn_title": "...", "one_liner": "一句话点评"}}]}}"""


def main() -> int:
    date = None
    if "--date" in sys.argv:
        date = sys.argv[sys.argv.index("--date") + 1]
    date = date or (datetime.now(BJ) - timedelta(hours=8)).date().isoformat()

    cand_path = DATA / f"candidates_{date}.json"
    if not cand_path.exists():
        print(json.dumps({"ok": True, "digested": 0, "note": "无 candidates 文件"}, ensure_ascii=False))
        return 0
    cand = load_json(cand_path)
    candidates = cand.get("candidates", [])
    if cand.get("no_update") or not candidates:
        print(json.dumps({"ok": True, "digested": 0, "note": "昨日无更新或无候选"}, ensure_ascii=False))
        return 0

    cfg = load_json(ROOT / "config.json")
    top_n = int(cfg.get("top_n_digest", 6))
    model_score = cfg.get("ds_model_score", "deepseek-chat")
    model_digest = cfg.get("ds_model_digest", "deepseek-chat")
    profile = (ROOT / "research_profile.md").read_text(encoding="utf-8")

    # ---------- ① 评分 ----------
    lines = []
    for c in candidates:
        abstract = c["abstract"][:700]
        lines.append(f"### {c['id']}\n标题: {c['title']}\n作者: {c.get('authors','')}\n分类: {'/'.join(c['categories'])}\n摘要: {abstract}\n")
    scoring_user = "请为以下论文打分：\n\n" + "\n".join(lines)
    raw = ds_chat(SCORING_SYS.format(profile=profile), scoring_user, model_score, max_tokens=4096)
    scored = parse_json_loose(raw)["scores"]
    smap = {s["id"]: s for s in scored}

    for c in candidates:
        s = smap.get(c["id"], {})
        c["llm_score"] = float(s.get("score", 0))
        c["llm_reason"] = s.get("reason", "")
        # 核心方向补偿：关键词层已确认属于 Rao 的核心几何方向（teleparallel/挠率/非度规/Elko）
        # 的论文给 +1 加权（封顶 10），防止通用模型系统性低估这些"小众但对口"的方向
        if any(t in ("非黎曼几何/挠率", "Elko") for t in c.get("ktags", [])):
            c["llm_score"] = min(10.0, c["llm_score"] + 1.0)
    candidates.sort(key=lambda c: (-c["llm_score"], -c["kscore"]))

    selected = candidates[:top_n]
    runner_pool = candidates[top_n:top_n + 3]

    # ---------- ② 精读 ----------
    papers_md = []
    for c in selected:
        papers_md.append(f"### {c['id']}（相关度评分 {c['llm_score']}）\n标题: {c['title']}\n作者: {c.get('authors','')}\n摘要: {c['abstract']}\n")
    runner_md = "\n".join(f"- {c['id']}：{c['title']}（评分 {c['llm_score']}，{c['llm_reason']}）" for c in runner_pool)
    digest_user = (
        "请精读以下论文并按要求输出：\n\n" + "\n".join(papers_md)
        + "\n\n另外为以下几篇 runner-up 各写一句点评：\n" + runner_md
    )
    raw2 = ds_chat(DIGEST_SYS.format(profile=profile), digest_user, model_digest, max_tokens=8000)
    dres = parse_json_loose(raw2)
    dmap = {p["id"]: p for p in dres.get("papers", [])}

    # ---------- ③ 组装 ----------
    n_must = 0
    papers = []
    for c in selected:
        d = dmap.get(c["id"], {})
        must = c["llm_score"] >= 8 and n_must < 2
        if must:
            n_must += 1
        papers.append({
            "id": c["id"],
            "title": c["title"],
            "cn_title": d.get("cn_title", ""),
            "authors": c.get("authors", ""),
            "link": c["link"],
            "source": c["source"],
            "categories": c["categories"],
            "score": c["llm_score"],
            "must_read": must,
            "one_liner": d.get("one_liner", c.get("llm_reason", "")),
            "did_and_eval": d.get("did_and_eval", "暂无"),
            "learn": d.get("learn", "暂无"),
            "topics": d.get("topics", "暂无"),
        })
    if n_must == 0 and papers and papers[0]["score"] >= 7:
        papers[0]["must_read"] = True

    runner_ups = []
    rmap = {r["id"]: r for r in dres.get("runner_ups", [])}
    for c in runner_pool:
        r = rmap.get(c["id"], {})
        runner_ups.append({
            "id": c["id"],
            "title": c["title"],
            "cn_title": r.get("cn_title", ""),
            "one_liner": r.get("one_liner", c.get("llm_reason", "")),
        })

    digest = {
        "listing_date": date,
        "generated_at": datetime.now(BJ).isoformat(timespec="seconds"),
        "generator": f"{model_score}/{model_digest}",
        "stats": {
            "total_new": cand.get("total_new_unseen", 0),
            "candidates": cand.get("passed_prefilter", 0),
            "selected": len(papers),
        },
        "papers": papers,
        "runner_ups": runner_ups,
    }
    (DATA / f"digest_{date}.json").write_text(json.dumps(digest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"ok": True, "digested": len(papers), "date": date}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
