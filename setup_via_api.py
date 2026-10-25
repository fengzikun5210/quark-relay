#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup_via_api.py — 用 GitHub REST API 一键搭建中继仓库并触发工作流

用法（Git Bash）：
  GH_TOKEN=ghp_你的令牌 python setup_via_api.py            # 建仓库+推文件+触发
  GH_TOKEN=ghp_你的令牌 python setup_via_api.py --monitor  # 同上，并持续监控运行/Release 资产

说明：文件经 Git Data API（blob→tree→commit→ref）一次性原子提交，
      contents API 无法在尚不存在的目录（如 .github/workflows/）中直接建文件。
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
OWNER = "fengzikun5210"
REPO = "quark-relay"
DIR = os.path.dirname(os.path.abspath(__file__))
FILES = ["scripts/quark_fetch.py", "fetch_parts.sh", "README.md",
         "setup_via_api.py"]   # .github/workflows/ 需用户在网页端创建（API 禁建）


def gh(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw.decode(errors="replace")[:300]}


def push_tree(token):
    """经 Git Data API 一次性提交全部文件，返回是否成功"""
    st, d = gh("GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/main", token=token)
    if st != 200:
        print(f"❌ 获取 main 引用失败 HTTP {st}: {d.get('message')}")
        return False
    commit_sha = d["object"]["sha"]
    st, d = gh("GET", f"/repos/{OWNER}/{REPO}/git/commits/{commit_sha}", token=token)
    base_tree = d["tree"]["sha"]

    tree = []
    for rel in FILES:
        local = os.path.join(DIR, rel.replace("/", os.sep))
        if not os.path.exists(local):
            print(f"❌ 本地文件缺失: {local}")
            return False
        content = base64.b64encode(open(local, "rb").read()).decode()
        st, d = gh("POST", f"/repos/{OWNER}/{REPO}/git/blobs",
                   {"content": content, "encoding": "base64"}, token=token)
        if st != 201:
            print(f"❌ 建 blob 失败 {rel} HTTP {st}: {d.get('message')}")
            return False
        tree.append({"path": rel, "mode": "100644", "type": "blob", "sha": d["sha"]})
        print(f"  blob: {rel}")

    st, d = gh("POST", f"/repos/{OWNER}/{REPO}/git/trees",
               {"base_tree": base_tree, "tree": tree}, token=token)
    if st != 201:
        print(f"❌ 建 tree 失败 HTTP {st}: {d.get('message')}")
        return False
    new_tree = d["sha"]

    st, d = gh("POST", f"/repos/{OWNER}/{REPO}/git/commits",
               {"message": "add quark-relay pipeline (workflow + fetcher + setup)",
                "tree": new_tree, "parents": [commit_sha]}, token=token)
    if st != 201:
        print(f"❌ 建 commit 失败 HTTP {st}: {d.get('message')}")
        return False
    new_commit = d["sha"]

    st, d = gh("POST", f"/repos/{OWNER}/{REPO}/git/refs/heads/main",
               {"sha": new_commit}, token=token)
    if st != 200:
        print(f"❌ 更新 ref 失败 HTTP {st}: {d.get('message')}")
        return False
    print("✅ 全部文件已一次性提交到 main")
    return True


def main():
    token = os.environ.get("GH_TOKEN")
    if not token:
        sys.exit("请先设置 GH_TOKEN，例如：GH_TOKEN=ghp_xxx python setup_via_api.py")

    # 1. 建仓库（已存在则继续）
    st, d = gh("POST", "/user/repos",
               {"name": REPO, "private": False, "auto_init": True,
                "description": "夸克分享文件中继下载（GitHub Actions relay）"}, token)
    if st == 201:
        print(f"✅ 仓库已创建: {d['full_name']}")
        time.sleep(8)  # 等待 auto_init 提交落地
    else:
        print(f"⚠️ 建仓库返回 HTTP {st}: {d.get('message', d)}（已存在则继续）")

    # 2. 一次性推送全部文件
    if not push_tree(token):
        sys.exit(1)

    # 3. 触发工作流
    st, d = gh("POST", f"/repos/{OWNER}/{REPO}/actions/workflows/quark-relay.yml/dispatches",
               {"ref": "main"}, token)
    if st == 204:
        print("✅ 工作流已触发（第一步是 4MB 探测，约 1 分钟出结果）")
    else:
        print(f"❌ 触发失败 HTTP {st}: {d}")
        sys.exit(1)

    # 4. 初始状态
    time.sleep(6)
    st, d = gh("GET", f"/repos/{OWNER}/{REPO}/actions/runs?per_page=1", token=token)
    runs = d.get("workflow_runs", [])
    if runs:
        r = runs[0]
        print(f"运行地址: {r['html_url']}")
        print(f"当前状态: {r['status']} / {r.get('conclusion') or '进行中'}")

    if "--monitor" in sys.argv:
        print("\n=== 监控中（每 60 秒刷新）===", flush=True)
        while True:
            time.sleep(60)
            st, d = gh("GET", f"/repos/{OWNER}/{REPO}/actions/runs?per_page=1", token=token)
            runs = d.get("workflow_runs", [])
            st2, rels = gh("GET", f"/repos/{OWNER}/{REPO}/releases", token=token)
            assets = []
            for rl in (rels if isinstance(rels, list) else []):
                assets += [a["name"] for a in rl.get("assets", [])]
            if runs:
                r = runs[0]
                print(f"[{time.strftime('%H:%M:%S')}] run={r['status']}/{r.get('conclusion') or '...'} "
                      f"| Release 资产 {len(assets)} 个 {assets[:4]}{'...' if len(assets) > 4 else ''}",
                      flush=True)
                if r["status"] == "completed":
                    print(f"\n🏁 运行结束: {r.get('conclusion')} | {r['html_url']}")
                    break


if __name__ == "__main__":
    main()
