#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  install.sh —— 把本技能"真装"一份到技能目录（复制文件，不是软链）。
#
#   ./install.sh                      装到 ~/.agents/skills/ncm2mp3
#   ./install.sh ~/.claude/skills/ncm2mp3   顺便也装到别处
#
#  改完源文件再跑一次就同步了。装完那个目录是独立的一份，删掉源目录也不影响它。
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:-$HOME/.agents/skills/ncm2mp3}"

mkdir -p "$DEST"
if command -v rsync >/dev/null; then
  rsync -a --delete --exclude install.sh --exclude __pycache__ --exclude '*.pyc' "$SRC/" "$DEST/"
else
  # 没有 rsync 的机器：手动复制
  find "$DEST" -mindepth 1 -maxdepth 1 ! -name install.sh -exec rm -rf {} +
  cp -R "$SRC/SKILL.md" "$SRC/ncm.sh" "$SRC/scripts" "$DEST/"
  rm -rf "$DEST/scripts/__pycache__"
fi
chmod 644 "$DEST/SKILL.md"
chmod 755 "$DEST/ncm.sh" "$DEST/scripts/ncm2mp3.py"

echo "✓ 已安装 → $DEST"
printf '  文件：'; (cd "$DEST" && ls -1 SKILL.md ncm.sh scripts/ncm2mp3.py | tr '\n' ' '); echo
printf '  自检：'; "$DEST/scripts/ncm2mp3.py" --selftest | tail -1
