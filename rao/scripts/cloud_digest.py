#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek 精读生成器（云端"AI 评分 + 精读"环节，v2 双层结构）

输入：data/candidates_<date>.json、research_profile.md、config.json
输出：data/digest_<date>.json（papers=核心精读 2-8 篇，radar=领域动态 0-10 篇）
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

# 雷达层领域标签（与 research_profile.md §3.1 一致），命中 title+abstract 即标记
RADAR_RULES = [
    (r"teleparallel|f\(T\)|torsion scalar|Nieh[–-]Yan|spin connection|tetrad", "teleparallel/f(T)"),
    (r"f\(Q\)|symmetric teleparallel|nonmetricity|non-metricity|metric-affine", "f(Q)/STG"),
    (r"parity[- ]violation|Chern[–-]Simons|birefringence|chiral gravitational|gravitational wave polarization|cosmic birefringence|circular polarization", "宇称破缺引力"),
    (r"mimetic|DHOST|Horndeski|cuscuton|scalar[–-]tensor", "mimetic/标量-张量"),
    (r"ELKO|Elko|mass[ -]dimension[ -]one", "Elko/特殊旋量"),
    (r"baryogenesis|leptogenesis|baryon asymmetry|lepton asymmetry", "引力重子/轻子生成"),
    (r"Kaluza[–-]Klein|dimensional reduction|compactification", "高维统一"),
    (r"modified gravity|f\(R\)|dark energy|inflation|quintessence|Ostrogradsky|strong coupling|degrees of freedom|ghost|stability", "修正引力宇宙学一般项"),
]

REL_LABEL = {"same-model": "撞车预警", "same-family": "近邻", "field": "风向"}
REL_RANK = {"same-model": 0, "same-family": 1, "field": 2}


def radar_fields_of(paper: dict) -> list[str]:
    text = (paper["title"] + " " + paper["abstract"]).lower()
    return [tag for pattern, tag in RADAR_RULES if re.search(pattern, text, re.IGNORECASE)]


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
给下列每篇论文输出三个字段：
- score：0-10 的相关度分数（锚点见画像 §4 评分锚点；严格执行 §4 的降权规则——但注意修正引力宇宙学现象学只压到 4-5 分，不要压到 3 分以下，它们还要进雷达层）
- reason：一句中文理由（≤40 字）
- relation：该论文与画像中 Rao 已发表模型/在研课题（§1 课题卡、§2 主线）的关系：
  "same-model" = 同一模型或同一问题（可能撞车）；
  "same-family" = 同一理论族（如同为 teleparallel、f(Q)、宇称破缺引力、Elko 等）；
  "field" = 仅同属广义修正引力/宇宙学领域；
  "none" = 不属于上述任何

# 输出（严格 JSON，不要输出其他内容）
{{"scores": [{{"id": "arXiv号", "score": 7, "reason": "一句话理由", "relation": "same-family"}}]}}"""


DIGEST_SYS = """你是理论物理研究助手，为做修正引力与引力热力学研究的用户（Rao）精读 arXiv 论文并写每日推送的核心层内容。

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
{{"papers": [{{"id": "arXiv号", "cn_title": "...", "one_liner": "...", "did_and_eval": "...", "learn": "...", "topics": "..."}}]}}"""


RADAR_SYS = """你是理论物理研究助手，为做修正引力宇宙学的用户（Rao）写每日"领域动态"简评。

# 用户研究画像
{profile}

# 任务
对给出的每篇论文（均为 Rao 所做领域的新进展），按画像 §6 输出紧凑简评，字段：
- cn_title：中文译题
- one_liner：一句话简介（≤60 字）
- relation_note：与 Rao 的关系一句话，必须落到具体模型或 Rao 的某篇论文/课题卡，说不清写"同领域"，禁止编造
- brief：80-120 字简评，诚实说明新意与局限；现象学拟合类须标注"现象学类，方法新意有限"
- action：仅当该论文被标注为"撞车预警"时必填（如"核对该文与 P2 是否同条件，更新最近邻表"），其他填"暂无"

# 写作要求
- 全部中文；公式用 Unicode 纯文本；不吹捧

# 输出（严格 JSON）
{{"radar": [{{"id": "arXiv号", "cn_title": "...", "one_liner": "...", "relation_note": "...", "brief": "...", "action": "暂无"}}]}}"""


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
    core_min = int(cfg.get("core_min", 2))
    core_max = int(cfg.get("core_max", 8))
    core_min_score = float(cfg.get("core_min_score", 7))
    radar_max = int(cfg.get("radar_max", 10))
    radar_min_score = float(cfg.get("radar_min_score", 4))
    model_score = cfg.get("ds_model_score", "deepseek-chat")
    model_digest = cfg.get("ds_model_digest", "deepseek-chat")
    profile = (ROOT / "research_profile.md").read_text(encoding="utf-8")

    # ---------- ① 评分（含 relation） ----------
    lines = []
    for c in candidates:
        abstract = c["abstract"][:700]
        lines.append(f"### {c['id']}\n标题: {c['title']}\n作者: {c.get('authors','')}\n分类: {'/'.join(c['categories'])}\n摘要: {abstract}\n")
    raw = ds_chat(SCORING_SYS.format(profile=profile), "请为以下论文打分：\n\n" + "\n".join(lines), model_score, max_tokens=4096)
    smap = {s["id"]: s for s in parse_json_loose(raw)["scores"]}

    for c in candidates:
        s = smap.get(c["id"], {})
        c["llm_score"] = float(s.get("score", 0))
        c["llm_reason"] = s.get("reason", "")
        c["relation"] = s.get("relation", "none")
        c["radar_fields"] = radar_fields_of(c)
        # 核心方向补偿：关键词层已确认属于 Rao 核心几何方向的论文 +1（封顶 10）
        if any(t in ("非黎曼几何/挠率", "Elko") for t in c.get("ktags", [])):
            c["llm_score"] = min(10.0, c["llm_score"] + 1.0)
    candidates.sort(key=lambda c: (-c["llm_score"], -c["kscore"]))

    # ---------- ② 双轨选择 ----------
    core = [c for c in candidates if c["llm_score"] >= core_min_score][:core_max]
    if len(core) < core_min:
        core = candidates[: min(core_min, len(candidates))]
    core_ids = {c["id"] for c in core}

    radar_pool = [
        c for c in candidates
        if c["id"] not in core_ids and c["radar_fields"] and c["llm_score"] >= radar_min_score
    ]
    radar_pool.sort(key=lambda c: (REL_RANK.get(c["relation"], 3), -c["llm_score"], -c["kscore"]))
    radar = radar_pool[:radar_max]

    radar_note = None
    if not radar:
        n_eligible = sum(1 for c in candidates if c["id"] not in core_ids and c["radar_fields"])
        if n_eligible:
            radar_note = f"今日修正引力宇宙学类候选 {n_eligible} 篇均未入选雷达层（评分均低于 {radar_min_score:g}，多为现象学拟合类）。"

    # ---------- ③ 核心层精读 ----------
    papers = []
    if core:
        papers_md = []
        for c in core:
            papers_md.append(f"### {c['id']}（相关度 {c['llm_score']}）\n标题: {c['title']}\n作者: {c.get('authors','')}\n摘要: {c['abstract']}\n")
        raw2 = ds_chat(DIGEST_SYS.format(profile=profile), "请精读以下论文并按要求输出：\n\n" + "\n".join(papers_md),
                       model_digest, max_tokens=8000)
        dmap = {p["id"]: p for p in parse_json_loose(raw2).get("papers", [])}
        n_must = 0
        for c in core:
            d = dmap.get(c["id"], {})
            must = c["llm_score"] >= 8 and n_must < 2
            if must:
                n_must += 1
            papers.append({
                "id": c["id"], "title": c["title"], "cn_title": d.get("cn_title", ""),
                "authors": c.get("authors", ""), "link": c["link"], "source": c["source"],
                "categories": c["categories"], "score": c["llm_score"], "must_read": must,
                "one_liner": d.get("one_liner", c.get("llm_reason", "")),
                "did_and_eval": d.get("did_and_eval", "暂无"),
                "learn": d.get("learn", "暂无"),
                "topics": d.get("topics", "暂无"),
            })

    # ---------- ④ 雷达层简评 ----------
    radar_out = []
    if radar:
        radar_md = []
        for c in radar:
            label = REL_LABEL.get(c["relation"], "风向")
            radar_md.append(
                f"### {c['id']}（{label}，相关度 {c['llm_score']}，领域：{'/'.join(c['radar_fields'])}）\n"
                f"标题: {c['title']}\n作者: {c.get('authors','')}\n摘要: {c['abstract'][:900]}\n"
            )
        raw3 = ds_chat(RADAR_SYS.format(profile=profile), "请为以下论文写领域动态简评：\n\n" + "\n".join(radar_md),
                       model_digest, max_tokens=6000)
        rmap = {r["id"]: r for r in parse_json_loose(raw3).get("radar", [])}
        for c in radar:
            r = rmap.get(c["id"], {})
            label = REL_LABEL.get(c["relation"], "风向")
            radar_out.append({
                "id": c["id"], "title": c["title"], "cn_title": r.get("cn_title", ""),
                "authors": c.get("authors", ""), "link": c["link"], "source": c["source"],
                "categories": c["categories"], "score": c["llm_score"],
                "relation": c["relation"], "relation_label": label,
                "radar_fields": c["radar_fields"],
                "one_liner": r.get("one_liner", c.get("llm_reason", "")),
                "relation_note": r.get("relation_note", "同领域"),
                "brief": r.get("brief", "暂无"),
                "action": r.get("action", "暂无"),
            })

    digest = {
        "listing_date": date,
        "generated_at": datetime.now(BJ).isoformat(timespec="seconds"),
        "generator": f"{model_score}/{model_digest}",
        "stats": {
            "total_new": cand.get("total_new_unseen", 0),
            "candidates": cand.get("passed_prefilter", 0),
            "core": len(papers),
            "radar": len(radar_out),
        },
        "papers": papers,
        "radar": radar_out,
        "radar_note": radar_note,
    }
    (DATA / f"digest_{date}.json").write_text(json.dumps(digest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"ok": True, "core": len(papers), "radar": len(radar_out), "date": date}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
