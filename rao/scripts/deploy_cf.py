#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 site/ 目录部署到 Cloudflare Pages（Direct Upload 协议，纯 Python，无需 Node/wrangler）。

协议（与 wrangler 一致）：
  哈希：blake3(base64(文件字节) + 扩展名).hex()[:32]
  ① GET  /accounts/{aid}/pages/projects/{proj}/upload-token   （apiToken 鉴权，得短期 JWT）
  ② POST /pages/assets/check-missing                          （JWT 鉴权，只传缺失项）
  ③ POST /pages/assets/upload                                 （JWT 鉴权，{key,value,metadata,base64:true}）
  ④ POST /pages/assets/upsert-hashes                          （JWT 鉴权，声明本次部署全部哈希）
  ⑤ POST /accounts/{aid}/pages/projects/{proj}/deployments    （apiToken，multipart manifest）

用法：python scripts/deploy_cf.py
依赖：pip install blake3 requests
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import sys
import time
from pathlib import Path

import blake3
import requests

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
API = "https://api.cloudflare.com/client/v4"


def asset_hash(content: bytes, path: str) -> str:
    ext = path.rsplit(".", 1)[-1] if "." in path else ""
    payload = base64.b64encode(content).decode() + ext
    return blake3.blake3(payload.encode()).hexdigest()[:32]


def get_secret(name: str, default=None):
    """优先读环境变量，回落到本地 config.local.json（GitHub Actions 中用 Secrets 注入环境变量）。"""
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
    aid = get_secret("CLOUDFLARE_ACCOUNT_ID")
    token = get_secret("CLOUDFLARE_API_TOKEN")
    project = get_secret("CLOUDFLARE_PROJECT", "arxiv-rao")
    if not (aid and token):
        print(json.dumps({"ok": False, "error": "缺少 CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN"}, ensure_ascii=False))
        return 1
    h = {"Authorization": f"Bearer {token}"}

    files: dict[str, bytes] = {}
    for f in sorted(SITE.rglob("*")):
        if f.is_file():
            files["/" + f.relative_to(SITE).as_posix()] = f.read_bytes()
    if not files:
        print(json.dumps({"ok": False, "error": "site/ 目录为空"}))
        return 1
    manifest = {p: asset_hash(b, p) for p, b in files.items()}

    # ① 上传令牌
    tj = requests.get(f"{API}/accounts/{aid}/pages/projects/{project}/upload-token",
                      headers=h, timeout=30).json()
    jwt = (tj.get("result") or {}).get("jwt")
    if not tj.get("success") or not jwt:
        print(json.dumps({"ok": False, "step": "upload-token", "resp": tj}, ensure_ascii=False))
        return 1
    jh = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}

    # ② 只上传缺失的
    cm = requests.post(f"{API}/pages/assets/check-missing", headers=jh,
                       data=json.dumps({"hashes": list(manifest.values())}), timeout=60).json()
    missing = set((cm.get("result") or [])) if cm.get("success") else set(manifest.values())

    # ③ 上传文件内容
    bucket = []
    for path, content in files.items():
        if manifest[path] not in missing:
            continue
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        bucket.append({
            "key": manifest[path],
            "value": base64.b64encode(content).decode(),
            "metadata": {"contentType": ctype},
            "base64": True,
        })
    if bucket:
        ur = requests.post(f"{API}/pages/assets/upload", headers=jh,
                           data=json.dumps(bucket), timeout=180)
        try:
            uj = ur.json()
        except Exception:  # noqa: BLE001
            uj = {"success": False, "raw": ur.text[:300]}
        if not uj.get("success"):
            print(json.dumps({"ok": False, "step": "upload", "status": ur.status_code, "resp": uj}, ensure_ascii=False))
            return 1

    # ④ 声明本次部署的完整哈希列表
    uh = requests.post(f"{API}/pages/assets/upsert-hashes", headers=jh,
                       data=json.dumps({"hashes": list(manifest.values())}), timeout=60)
    try:
        uhj = uh.json()
    except Exception:  # noqa: BLE001
        uhj = {"success": False, "raw": uh.text[:300]}
    if not uhj.get("success"):
        print(json.dumps({"ok": False, "step": "upsert-hashes", "status": uh.status_code, "resp": uhj}, ensure_ascii=False))
        return 1

    # ⑤ 创建 deployment（multipart 清单，apiToken 鉴权）
    r = requests.post(
        f"{API}/accounts/{aid}/pages/projects/{project}/deployments",
        headers=h,
        files={"manifest": (None, json.dumps(manifest), "application/json")},
        timeout=60,
    )
    j = r.json()
    if not j.get("success"):
        print(json.dumps({"ok": False, "step": "create_deployment", "errors": j.get("errors")}, ensure_ascii=False))
        return 1
    dep_id = j["result"].get("id")

    url = f"https://{project}.pages.dev"
    for _ in range(40):
        time.sleep(3)
        sr = requests.get(f"{API}/accounts/{aid}/pages/projects/{project}/deployments/{dep_id}",
                          headers=h, timeout=30)
        sj = sr.json()
        if sj.get("success"):
            stage = sj["result"].get("latest_stage") or {}
            if stage.get("name") == "deploy" and stage.get("status") == "success":
                print(json.dumps({"ok": True, "url": url, "files": len(files), "uploaded": len(bucket)}, ensure_ascii=False))
                return 0
            if stage.get("status") == "failure":
                break
    print(json.dumps({"ok": False, "step": "poll", "note": "部署状态超时或失败", "url": url}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(main())
