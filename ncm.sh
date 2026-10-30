#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  ncm.sh —— .ncm（网易云加密壳）→ mp3 / flac / wav。你只需要记这几个动词。
#
#   ./ncm.sh env                 自检（python3 / ffmpeg / AES 与密钥流算法）
#   ./ncm.sh check <x.ncm>       只解剖结构，不写文件（坏文件诊断用）
#   ./ncm.sh <x.ncm|目录>...      转换：原样还原，不重新编码（目录=批量）
#   ./ncm.sh wav <x.ncm>         还原 + 另出一份 44.1k 无损 wav
#   ./ncm.sh mv  <x.ncm|目录>     出「MV 可用素材」：mp3(带标签封面) + wav + 下一步命令
#   ./ncm.sh batch <目录>        递归找 .ncm 批量转
#   ./ncm.sh selftest            算法自检（FIPS-197 AES + 自造 NCM 往返）
#
# 输出默认落在 .ncm 同目录，文件名改成「歌手 - 歌名」。
# 想指定目录：加 -o out/ ；想保原名：加 --keep-name ；其余参数原样透传给脚本。
# ═══════════════════════════════════════════════════════════════════════════
set -uo pipefail
SRC="${BASH_SOURCE[0]}"; case "$SRC" in /*) ;; *) SRC="$PWD/$SRC";; esac
SKILL="$(cd "$(dirname "$SRC")" && pwd)"
PY="$SKILL/scripts/ncm2mp3.py"
say()  { printf '\033[1m%s\033[0m\n' "$*"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
warn() { printf '\033[33m! %s\033[0m\n' "$*" >&2; }

command -v python3 >/dev/null || die "找不到 python3"
[ -f "$PY" ] || die "找不到脚本：$PY"

cmd="${1:-help}"; shift || true

case "$cmd" in
# ───────────────────────────────────────────────────────────────────────
env)
  say "── 环境自检 ──"
  printf 'python3   '; python3 -V
  printf 'ffmpeg    '; ffmpeg -version 2>/dev/null | head -1 || echo "缺（可选；装不上也能还原，只是不封标签/不转码）"
  printf 'ffprobe   '; ffprobe -version 2>/dev/null | head -1 || echo "缺（可选）"
  echo
  say "── 算法自检 ──"
  python3 "$PY" --selftest || die "算法自检没过，别用这个工具"
  ;;

check|inspect)
  [ $# -ge 1 ] || die "要给文件：./ncm.sh check 歌.ncm"
  python3 "$PY" "$@" --inspect
  ;;

selftest) python3 "$PY" --selftest ;;

wav)
  [ $# -ge 1 ] || die "要给文件：./ncm.sh wav 歌.ncm"
  python3 "$PY" "$@" --wav --verify
  ;;

mv)
  [ $# -ge 1 ] || die "要给文件或目录：./ncm.sh mv 歌.ncm"
  say "── 还原成 MV 可用素材（mp3 带标签封面 + 44.1k wav）──"
  python3 "$PY" "$@" --wav --verify --loudness
  echo
  say "—— 下一步（music-mv 流程）——"
  cat <<'EOS'
  ./mv.sh analyze <上一步产出的 mp3>     测 BPM / 拍位 / 情绪段落
  ./mv.sh lyric   <歌.lrc>               歌词 → lyrics.js
  剪映 / PR / DaVinci 里直接用那份 wav（它不重新编码，最保险）
EOS
  ;;

batch)
  [ $# -ge 1 ] || die "要给目录：./ncm.sh batch 某个目录/"
  d="$1"; shift || true
  python3 "$PY" "$d" --recursive "$@"
  ;;

help|-h|--help|"") awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}" ;;

*)  # 默认动作＝转换：把动词本身也当输入路径
  python3 "$PY" "$cmd" "$@"
  ;;
esac
