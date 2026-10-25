# GitHub Actions 夸克分享文件中继方案

## 解决什么问题

本机网络（陕西电信）被夸克分享 CDN 以 **HTTP 412** 全面封锁：分享文件的直链
主机 `dl-guest-*.drive.quark.cn` / `dl-pc-sz.drive.quark.cn` 从本机请求一律 412，
已排除 Cookie、请求头、客户端、fr 参数等因素，判定为 IP/ASN 级封锁。

自己网盘的 CDN（`pds.quark.cn`）虽可达，但账号免费配额仅 10GB（已用 996GB），
17.3GB+ 的文件根本存不进来，转存路线不可行。

**本方案**：让 GitHub Actions runner（微软云服务器，IP 干净）代替本机下载，
按 1.8GB 分块上传到 GitHub Release，本机再经 ghfast / gh-proxy 镜像拉回拼接。

## 文件说明

| 文件 | 作用 |
|---|---|
| `.github/workflows/quark-relay.yml` | Actions 工作流（探测 → 分块下载 → 上传 Release） |
| `scripts/quark_fetch.py` | 下载器：遍历分享目录 → fr=mac 申请直链 → Range 分块并行下载 → 边下边传 |
| `fetch_parts.sh` | 本机脚本：经镜像拉回全部分块并拼接还原（断点续传） |

## 使用步骤

### 1. 准备令牌（用户侧，约 2 分钟）

浏览器登录 https://github.com → Settings → Developer settings → Personal access tokens →
**Tokens (classic)** → Generate new token (classic)：
- Note 随便填（如 `quark-relay`）
- Expiration 选 **7 days**
- 勾选 **`repo`** 这一个权限
- 生成后复制（`ghp_` 开头）

> 用完请回到该页面删除令牌（或等它 7 天自动过期）。

### 2. 一键搭建并触发（本机执行）

```bash
cd E:/ai/github-relay
GH_TOKEN=ghp_你的令牌 python setup_via_api.py --monitor
```

脚本自动完成：建 public 仓库 `fengzikun5210/quark-relay` → 推送 4 个文件 →
dispatch 触发工作流 → 持续监控（每 60 秒报告运行状态与 Release 资产增长）。

### 3. 运行过程

工作流第一步自动探测：先下载分享内的 4MB winrar 测试文件验证 runner IP 未被
412 封锁（约 1 分钟）。若日志出现 `❌ runner IP 同样被 412 封锁`，说明 GitHub 的
IP 段也被夸克封锁，此路线不可行，需换其他方案（见下方备选）。
探测通过后自动进入 45GB 主文件分块下载，中断后重新触发即断点续传
（按 Release 已有资产跳过已完成分块）。

### 4. 本机拉回

Release 页面出现分块资产后：

```bash
bash fetch_parts.sh fengzikun5210/quark-relay v1 LTX-2.3-OmniNFT.rar
# 镜像慢/挂时可换：bash fetch_parts.sh fengzikun5210/quark-relay v1 LTX-2.3-OmniNFT.rar https://gh-proxy.com
```

脚本会自动拉完全部 `LTX-2.3-OmniNFT.rar.part000` ~ `part026` 并拼接为
`LTX-2.3-OmniNFT.rar`（45.07GB ≈ 48,399,010,683 字节）。

## 关键参数与边界

- **分块大小 1.8GB**：GitHub Release 单资产上限 2GB
- **并行 3**：runner 磁盘约 14GB，峰值占用约 5.4GB
- **单次运行上限 6 小时**：45GB 若在 6 小时内没下完会中断，重新 Run workflow 自动续传
- **直链 6 小时有效**：下载器在分块重试时会自动重新申请直链
- **免费额度**：私有仓库 2000 分钟/月；public 仓库 Actions 完全免费且无时长限制
- **无 Range 降级**：若 CDN 不支持 Range，自动切流式模式（无断点续传，一次跑完）

## 备选方案（探测失败时）

1. **手机流量下载**：夸克 APP 打开分享链接，4G/5WiFi 下载后 USB 传电脑（最稳）
2. **Cloudflare Worker 中继**：`E:/ai/quark-relay-worker.js`（免费部署，见文件内注释）
3. **SVIP**：解决配额与限速，但官方通道能否绕开 412 未验证
4. **换网络**：任何未被封锁的宽带/热点，直链流程照用

## 风险提示

- GitHub Actions 用量政策不建议将其作为通用文件分发服务；个人自用中转
  45GB 级别通常不会触发限制，但请勿高频跑
- 公开仓库 = 公开再分发该整合包；分享链接本身无密码永久有效，公开范围未变，
  但请自行评估是否接受
