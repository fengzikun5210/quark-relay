#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quark_fetch.py — 夸克分享文件中继下载器（在 GitHub Actions runner 上运行）

背景：本机网络被夸克分享 CDN（dl-guest-*.drive.quark.cn / dl-pc-sz.drive.quark.cn）
以 HTTP 412 全面封锁；GitHub Actions runner 位于微软云，IP 干净，可正常取回文件。
下载后按 1.8GB 分块上传到 GitHub Release，本机再经 ghfast / gh-proxy 镜像拉回拼接。

模式：
  --probe     下载分享内的 4MB 测试文件（winrar-x64-700scp.exe）并上传 Release，
              用于在启动 45GB 大下载前验证 runner IP 未被 412 封锁（约 1 分钟）
  --url-only  只遍历分享目录并申请直链后打印（本地调试用，不下载）
  默认        分块并行下载目标文件，边下边传 Release，支持跨次断点续传

依赖：python3 标准库 + curl + gh CLI（GitHub Actions ubuntu-latest 均内置，
      gh 通过环境变量 GH_TOKEN 自动认证）
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")
API = "https://drive-pc.quark.cn"
PROBE_NAME = "winrar-x64-700scp.exe"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def api_get(path):
    req = urllib.request.Request(API + path, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def api_post(path, body):
    req = urllib.request.Request(
        API + path, data=json.dumps(body).encode("utf-8"),
        headers={"User-Agent": UA, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def get_stoken(pwd_id):
    d = api_post("/1/clouddrive/share/sharepage/token", {"pwd_id": pwd_id})
    if d.get("status") != 200:
        raise SystemExit(f"stoken 申请失败: {json.dumps(d, ensure_ascii=False)[:200]}")
    return d["data"]["stoken"]


def walk_share(pwd_id, stoken, pdir_fid, want, base=""):
    """递归遍历分享目录查找文件（按文件名匹配，返回文件信息 dict）"""
    q = urllib.parse.quote(stoken, safe="")
    d = api_get(f"/1/clouddrive/share/sharepage/detail?pr=ucpro&fr=pc&pwd_id={pwd_id}"
                f"&stoken={q}&pdir_fid={pdir_fid}&_page=1&_size=100"
                f"&_sort=file_type:asc,file_name:asc")
    for f in d.get("data", {}).get("list", []):
        name = f["file_name"]
        full = f"{base}/{name}"
        if f.get("file"):
            if name == want or full.endswith("/" + want) or full == want:
                return {"fid": f["fid"], "token": f.get("share_fid_token"),
                        "pdir_fid": pdir_fid, "size": int(f.get("size") or 0),
                        "path": full}
        else:
            r = walk_share(pwd_id, stoken, f["fid"], want, full)
            if r:
                return r
    return None


class UrlBox:
    """直链缓存（直链约 6 小时有效，失败时可刷新）"""

    def __init__(self, pwd_id):
        self.pwd_id = pwd_id
        self.stoken = get_stoken(pwd_id)
        self.item = None
        self.url = None
        self.lock = threading.Lock()

    def acquire(self, item):
        self.item = item
        with self.lock:
            return self._refresh()

    def refresh(self):
        with self.lock:
            return self._refresh()

    def _refresh(self):
        d = api_post("/1/clouddrive/file/download?pr=ucpro&fr=mac", {
            "fids": [self.item["fid"]], "pwd_id": self.pwd_id,
            "stoken": self.stoken, "pdir_fid": self.item["pdir_fid"],
            "share_fid_tokens": {self.item["fid"]: self.item["token"]}})
        if d.get("status") != 200:
            raise SystemExit("直链申请失败: " + json.dumps(d, ensure_ascii=False)[:300])
        self.url = d["data"][0]["download_url"]
        return self.url


def curl_dl(url, out, rng=None, max_bytes=None, max_time=1800):
    """下载到文件，返回 HTTP 状态码（412=CDN 封锁）"""
    cmd = ["curl", "-sSL", "--retry", "5", "--retry-delay", "3", "--retry-all-errors",
           "--speed-time", "60", "--speed-limit", "20480",
           "-A", UA, "-e", "https://pan.quark.cn/"]
    if rng:
        cmd += ["-r", rng]
    if max_bytes:
        cmd += ["--max-filesize", str(max_bytes)]
    if max_time:
        cmd += ["--max-time", str(max_time)]
    cmd += ["-w", "%{http_code}", "-o", out, url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=max_time + 120)
    code = (r.stdout or "").strip()[-3:] if r.stdout else "000"
    return code, r.stderr


def gh(args, check=True):
    return subprocess.run(["gh"] + args, capture_output=True, text=True, check=check)


def ensure_release(tag, notes):
    """返回已存在的资产名集合；release 不存在则创建"""
    r = gh(["release", "view", tag, "--json", "assets"], check=False)
    if r.returncode != 0:
        gh(["release", "create", tag, "--title", f"quark-relay {tag}", "-n", notes])
        log(f"Release [{tag}] 已创建")
        return set()
    return {a["name"] for a in json.loads(r.stdout).get("assets", [])}


def upload_asset(tag, path):
    r = gh(["release", "upload", tag, path, "--clobber"])
    if r.returncode != 0:
        raise RuntimeError(f"资产上传失败 {path}: {r.stderr[:200]}")


def probe_run(box, tag):
    """小文件端到端验证：下载 4MB winrar 并上传 Release"""
    url = box.acquire(box.probe_item)
    code, err = curl_dl(url, "probe.bin", max_time=300)
    if code == "412":
        raise SystemExit(
            "❌ runner IP 同样被夸克 CDN 412 封锁，GitHub Actions 路线不可行。\n"
            "   请改用：手机流量下载 / 换网络 / SVIP / Cloudflare Worker 中继。")
    if code != "200":
        raise SystemExit(f"探测下载失败 HTTP {code}: {err[:200]}")
    size = os.path.getsize("probe.bin")
    with open("probe.bin", "rb") as f:
        head = f.read(2)
    if head != b"MZ":
        raise SystemExit(f"探测内容异常（前2字节 {head!r}，大小 {size}），疑似中间页，终止。")
    log(f"✅ 探测成功：winrar-x64-700scp.exe {size:,} bytes，CDN 未封锁 runner IP")
    os.replace("probe.bin", "probe_winrar.exe")
    ensure_release(tag, "quark-relay 中继下载（先看 probe_winrar.exe 验证链路）")
    upload_asset(tag, "probe_winrar.exe")
    log("✅ probe_winrar.exe 已上传 Release，可经镜像下载验证整条链路")


def head_probe(url):
    """探测 Range 支持与文件总大小，返回 (total, range_supported, http_code)"""
    r = subprocess.run(
        ["curl", "-sS", "-r", "bytes=0-1048575", "--max-filesize", "2097152",
         "-D", "-", "-o", "/dev/null", "-A", UA, "-e", "https://pan.quark.cn/", url],
        capture_output=True, text=True, timeout=180)
    text = r.stdout or ""
    code = re.findall(r"HTTP/[\d.]+ (\d+)", text)
    code = code[-1] if code else "000"
    m = re.search(r"Content-Range:\s*bytes\s+\d+-\d+/(\d+)", text, re.I)
    total = int(m.group(1)) if m else None
    if total is None:
        m2 = re.search(r"Content-Length:\s*(\d+)", text, re.I)
        if m2:
            total = int(m2.group(1))
    return total, code == "206", code


def download_chunked(box, tag, total, chunk, parallel):
    """Range 分块并行下载 + 边下边传 + 断点续传"""
    n = (total + chunk - 1) // chunk
    prefix = os.path.basename(box.item["path"])
    existing = ensure_release(tag, "quark-relay 中继下载")
    todo = [i for i in range(n) if f"{prefix}.part{i:03d}" not in existing]
    log(f"总分块 {n}（每块 {chunk//1024//1024}MB），已完成 {n - len(todo)}，本次待下 {len(todo)}")

    def work(i):
        part = f"{prefix}.part{i:03d}"
        tmp = f"/tmp/{part}"
        start, end = i * chunk, min((i + 1) * chunk, total) - 1
        want = end - start + 1
        for attempt in range(8):
            try:
                url = box.url
                code, err = curl_dl(url, tmp, rng=f"bytes={start}-{end}",
                                    max_bytes=want + 1048576)
                if code == "200" and os.path.exists(tmp) and os.path.getsize(tmp) > want + 1048576:
                    os.remove(tmp)
                    raise RuntimeError("服务端忽略 Range 返回全文")
                if code in ("200", "206") and os.path.exists(tmp) and os.path.getsize(tmp) == want:
                    upload_asset(tag, tmp)
                    os.remove(tmp)
                    log(f"块 {i:03d}/{n - 1} 完成并上传 ({want:,} bytes)")
                    return
                log(f"块 {i:03d} 第 {attempt + 1} 次重试（HTTP {code}）")
                box.refresh()
            except Exception as e:
                log(f"块 {i:03d} 第 {attempt + 1} 次异常: {str(e)[:120]}")
                try:
                    box.refresh()
                except Exception:
                    pass
                time.sleep(3)
        raise RuntimeError(f"块 {i:03d} 多次重试仍失败")

    if todo:
        with cf.ThreadPoolExecutor(max_workers=parallel) as ex:
            list(ex.map(work, todo))
    log("全部分块下载并上传完成")


def download_stream(box, tag, chunk):
    """无 Range 时的降级流式下载：边收边传（无断点续传）"""
    prefix = os.path.basename(box.item["path"])
    os.makedirs("/tmp/parts", exist_ok=True)
    ensure_release(tag, "quark-relay 中继下载（流式模式）")
    cmd = ["curl", "-sSL", "--retry", "10", "--retry-delay", "5", "--retry-all-errors",
           "-A", UA, "-e", "https://pan.quark.cn/", box.url]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    i, got = 0, 0

    def read_exact(n):
        buf = b""
        while len(buf) < n:
            c = proc.stdout.read(n - len(buf))
            if not c:
                break
            buf += c
        return buf

    while True:
        buf = read_exact(chunk)
        if not buf:
            break
        name = f"{prefix}.part{i:03d}"
        path = f"/tmp/parts/{name}"
        with open(path, "wb") as f:
            f.write(buf)
        upload_asset(tag, path)
        os.remove(path)
        got += len(buf)
        log(f"块 {name} 上传（累计 {got:,} bytes）")
        i += 1
    proc.wait()
    log(f"流式下载完成，共 {got:,} bytes")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--share-url", default="https://pan.quark.cn/s/8aa3cf7b5dd4")
    ap.add_argument("--target", default="LTX-2.3-OmniNFT.rar")
    ap.add_argument("--tag", default="v1")
    ap.add_argument("--probe", action="store_true", help="小文件端到端验证")
    ap.add_argument("--url-only", action="store_true", help="只申请直链并打印")
    ap.add_argument("--chunk-mb", type=int, default=1800)
    ap.add_argument("--parallel", type=int, default=3)
    a = ap.parse_args()

    m = re.search(r"/s/([0-9A-Za-z]+)", a.share_url)
    if not m:
        raise SystemExit(f"无法从链接解析 pwd_id: {a.share_url}")
    pwd = m.group(1)

    box = UrlBox(pwd)
    if a.probe:
        box.probe_item = walk_share(pwd, box.stoken, "0", PROBE_NAME)
        if not box.probe_item:
            raise SystemExit(f"分享内找不到探测文件 {PROBE_NAME}")
        probe_run(box, a.tag)
        return

    item = walk_share(pwd, box.stoken, "0", a.target)
    if not item:
        raise SystemExit(f"分享内找不到文件: {a.target}")
    log(f"目标文件: {item['path']}  ({item['size']:,} bytes)")
    url = box.acquire(item)
    if a.url_only:
        print(url)
        return

    total, range_ok, code = head_probe(url)
    log(f"CDN 探测: HTTP {code} | Range 支持: {range_ok} | 总大小: {total}")
    if code == "412":
        raise SystemExit("❌ runner IP 被 412 封锁，路线不可行（见 --probe 说明）")
    if total is None:
        raise SystemExit("无法获取文件总大小")

    chunk = a.chunk_mb * 1024 * 1024
    if range_ok:
        download_chunked(box, a.tag, total, chunk, a.parallel)
    else:
        log("服务端不支持 Range，降级为流式下载（无断点续传）")
        download_stream(box, a.tag, chunk)

    log("✅ 全部完成。Release 资产即为全部分块，按名称序拼接即可还原。")


if __name__ == "__main__":
    main()
