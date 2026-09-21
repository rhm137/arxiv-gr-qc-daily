#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 site/ 同步到 rhm137/arxiv-rao-daily 的 gh-pages 分支（GitHub Pages 托管）。
github.io 域名在微信内可直接打开（pages.dev 会被微信软封）。

环境：GH_PAGES_PAT（GitHub 个人访问令牌；本地回落 config.local.json，CI 由 Secrets 注入）
用法：python scripts/deploy_ghpages.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
API = "https://api.github.com"
REPO = "rhm137/arxiv-rao-daily"
BRANCH = "gh-pages"


def get_secret(name: str, default=None):
    v = os.environ.get(name)
    if v:
        return v
    p = ROOT / "config.local.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")).get(name, default)
        except Exception:  # noqa: BLE001
            return default
    return default


def main() -> int:
    pat = get_secret("GH_PAGES_PAT")
    if not pat:
        print(json.dumps({"ok": False, "error": "缺少 GH_PAGES_PAT"}, ensure_ascii=False))
        return 1
    h = {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"}

    def req(method: str, url: str, **kw):
        for i in range(5):
            try:
                return getattr(requests, method)(url, headers=h, timeout=45, **kw)
            except requests.RequestException:
                time.sleep(5 * (i + 1))
        raise RuntimeError("GitHub 网络不可达: " + url)

    files: dict[str, bytes] = {}
    for f in sorted(SITE.rglob("*")):
        if f.is_file():
            files[f.relative_to(SITE).as_posix()] = f.read_bytes()
    if not files:
        print(json.dumps({"ok": False, "error": "site/ 目录为空"}))
        return 1

    # gh-pages 分支当前 head（404/409=仓库为空，首次部署需先用 Contents API 引导）
    ref = req("get", f"{API}/repos/{REPO}/git/refs/heads/{BRANCH}")
    if ref.status_code in (404, 409):
        first_path = "index.html"
        first_content = files.get(first_path) or next(iter(files.values()))
        init = req("put", f"{API}/repos/{REPO}/contents/{first_path}",
                   json={"message": "init gh-pages", "branch": BRANCH,
                         "content": base64.b64encode(first_content).decode()})
        base_sha = init.json()["commit"]["sha"]
    else:
        ref.raise_for_status()
        base_sha = ref.json()["object"]["sha"]

    entries = []
    for path, content in files.items():
        blob = req("post", f"{API}/repos/{REPO}/git/blobs",
                   json={"content": base64.b64encode(content).decode(), "encoding": "base64"}).json()
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})

    tree_payload: dict = {"tree": entries}
    if base_sha:
        base_tree = req("get", f"{API}/repos/{REPO}/git/commits/{base_sha}").json()["tree"]["sha"]
        tree_payload["base_tree"] = base_tree
    tree = req("post", f"{API}/repos/{REPO}/git/trees", json=tree_payload).json()

    msg = "deploy " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    commit = req("post", f"{API}/repos/{REPO}/git/commits",
                 json={"message": msg, "tree": tree["sha"], "parents": [base_sha] if base_sha else []}).json()

    if base_sha:
        r = req("patch", f"{API}/repos/{REPO}/git/refs/heads/{BRANCH}", json={"sha": commit["sha"]})
    else:
        r = req("post", f"{API}/repos/{REPO}/git/refs",
                json={"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]})
    ok = r.status_code in (200, 201)
    print(json.dumps({"ok": ok, "url": "https://rhm137.github.io/arxiv-rao-daily/", "files": len(files)}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
