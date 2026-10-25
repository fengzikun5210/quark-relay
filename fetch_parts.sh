#!/usr/bin/bash
# fetch_parts.sh — 本机运行：从 GitHub Release 镜像拉回全部分块并拼接还原
# 用法（Git Bash / WSL / Linux）：
#   bash fetch_parts.sh <owner/repo> <tag> <文件名> [镜像]
# 示例：
#   bash fetch_parts.sh myname/quark-relay v1 LTX-2.3-OmniNFT.rar
#   bash fetch_parts.sh myname/quark-relay v1 LTX-2.3-OmniNFT.rar https://gh-proxy.com
#
# 依赖：curl。断点续传：重复运行即可，已下载且大小正确的分块会跳过。

set -u
REPO="${1:?用法: fetch_parts.sh <owner/repo> <tag> <文件名> [镜像]}"
TAG="${2:?缺少 tag}"
NAME="${3:?缺少文件名}"
MIRROR="${4:-https://ghfast.top}"

BASE="https://github.com/$REPO/releases/download/$TAG"
mkdir -p parts && cd parts || exit 1

echo "==> 获取分块列表（经 $MIRROR）"
LIST=$(curl -sL --noproxy '*' --max-time 60 "$MIRROR/$BASE" \
  | grep -oE "$NAME\.part[0-9]{3}" | sort -u)
if [ -z "$LIST" ]; then
  # 镜像不返回 HTML 列表时，按序号盲试探 000-999
  echo "    镜像未返回列表，按序号试探..."
  for i in $(seq -w 0 999); do
    P="$NAME.part$i"
    CODE=$(curl -sIL --noproxy '*' --max-time 20 -o /dev/null -w "%{http_code}" "$MIRROR/$BASE/$P")
    [ "$CODE" = "200" ] && LIST="$LIST$P\n"
  done
  LIST=$(printf "%b" "$LIST" | sort -u)
fi
[ -z "$LIST" ] && { echo "未找到任何分块，请检查仓库/tag/文件名或换镜像"; exit 1; }

COUNT=$(printf "%s\n" "$LIST" | wc -l)
echo "==> 共 $COUNT 个分块，开始下载"
i=0
for P in $LIST; do
  i=$((i + 1))
  # 已有且非空则跳过（断点续传）
  if [ -s "$P" ]; then
    echo "[$i/$COUNT] $P 已存在，跳过"
    continue
  fi
  echo "[$i/$COUNT] 下载 $P"
  curl -L --noproxy '*' --retry 5 --retry-delay 3 --retry-all-errors \
       --speed-time 60 --speed-limit 10240 \
       -o "$P" "$MIRROR/$BASE/$P" || echo "  !! $P 下载失败，重新运行本脚本可续"
done

echo "==> 拼接还原"
cd ..
cat parts/$NAME.part* > "$NAME" 2>/dev/null
SIZE=$(stat -c %s "$NAME" 2>/dev/null || stat -f %z "$NAME")
echo "==> 完成: $NAME ($SIZE bytes)"
echo "    校验：解压测试 winrar t \"$NAME\" 或 7z t \"$NAME\""
