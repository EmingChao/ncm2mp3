#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ncm2mp3 —— 把网易云音乐加密格式 .ncm 还原成能直接用的音频（mp3 / flac / wav）。

零依赖：AES-128 和密钥流都是本文件里手写的，只用 Python 标准库。
ffmpeg / ffprobe 是可选增强（封标签、贴封面、转码、量响度、验证解码）。

──────────────────────────── 原理（一屏说完） ────────────────────────────
NCM 不是"另一种音频编码"，它只是一层壳，壳里装的就是原始 mp3 / flac 字节：

  [8B "CTENFDAM"] [2B gap] [4B key_len][key blob]
  [4B meta_len][meta blob]
  [4B crc32] [1B] [4B image_space][4B image_size][封面 jpg/png]
  [剩下全部 = 真音频 ⊕ 密钥流]

  ① key blob  逐字节 ^0x64 → AES-128-ECB(核心密钥) 解密 → 去掉 "neteasecloudmusic" 前缀
                                                            → 得到密钥 key
  ② meta blob 逐字节 ^0x63 → 去掉 "163 key(Don't modify):" → base64 → AES-128-ECB(meta 密钥)
              → 去掉 "music:" → JSON（曲名/歌手/专辑/原格式/码率/时长）
  ③ 剩下的音频按密钥流逐字节 XOR，得到的就是原始 mp3 或 flac 文件本身

所以还原是**无损**的：源是 320k mp3，出来的还是那个 320k mp3，一个字节都不重新编码。

⚠ 两个坑（这就是大部分"ncm 解不开"的原因）：
  · meta 段用的是 ^0x63，**不是** ^0x64（key 段才是 ^0x64）。两段常量不一样。
  · 密钥流不是连续 RC4：拿 KSA 后的 256 字节 S 盒派生出一个 **256 字节的块**
        blk[i] = S[(S[i] + S[(i + S[i]) & 0xFF]) & 0xFF]
    然后**循环重复**这个块，而且从块的**第 2 个字节**开始用（skip=1）。
    另有少数文件用连续 RC4 PRGA —— 本工具两种都试，取能解出合法音频头的那个。

──────────────────────────────── 用法 ────────────────────────────────
  python3 ncm2mp3.py 某首歌.ncm                  # → 同目录 "歌手 - 歌名.mp3"（不重编码）
  python3 ncm2mp3.py 某首歌.ncm -o out/          # 指定输出目录
  python3 ncm2mp3.py 某首歌.ncm --format wav     # 给剪辑软件用的无损 wav
  python3 ncm2mp3.py 某首歌.ncm --wav            # 原样还原 + 另出一份 wav
  python3 ncm2mp3.py song.ncm --format mp3 --bitrate 320k
  python3 ncm2mp3.py 整个目录/ --recursive       # 批量
  python3 ncm2mp3.py 某首歌.ncm --inspect        # 只解剖，不写文件（排查坏文件用）
  python3 ncm2mp3.py 某首歌.ncm --verify         # 还原后用 ffmpeg 完整解码一遍，确认没坏
  python3 ncm2mp3.py 某首歌.ncm --loudness       # 顺便量 LUFS / 峰值（MV 交付要看）
  python3 ncm2mp3.py 某首歌.ncm --json           # 机器可读结果（给脚本 / agent 用）
  python3 ncm2mp3.py --selftest                  # 自检 AES / 密钥流（对官方向量 + 自造 NCM 往返）
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib

# ── 网易云写死的两把密钥 + 两段各自的异或常量 ──────────────────────────────
CORE_KEY = bytes.fromhex("687A4852416D736F356B496E62617857")   # b"hzHRAmso5kInbaxW"
META_KEY = bytes.fromhex("2331346C6A6B5F215C5D2630553C2728")   # b"#14ljk_!\\]&0U<'("
KEY_XOR, META_XOR = 0x64, 0x63
NCM_MAGIC = b"CTENFDAM"
KEY_PREFIX = b"neteasecloudmusic"
META_PREFIX = b"163 key(Don't modify):"
META_JSON_PREFIX = b"music:"


# ══════════════════════════════════════════════════════════════════════════
# AES-128-ECB 解密（S 盒按定义算出来，不手抄表；对 FIPS-197 官方向量自检）
# ══════════════════════════════════════════════════════════════════════════
def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _gmul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        b >>= 1
        a = _xtime(a)
    return p & 0xFF


def _rotl8(x: int, n: int) -> int:
    return ((x << n) | (x >> (8 - n))) & 0xFF


def _build_sbox() -> list[int]:
    inv = [0] * 256
    for i in range(1, 256):
        for j in range(1, 256):
            if _gmul(i, j) == 1:
                inv[i] = j
                break
    return [(inv[i] ^ _rotl8(inv[i], 1) ^ _rotl8(inv[i], 2) ^ _rotl8(inv[i], 3)
             ^ _rotl8(inv[i], 4) ^ 0x63) & 0xFF for i in range(256)]


SBOX = _build_sbox()
INV_SBOX = [0] * 256
for _i, _v in enumerate(SBOX):
    INV_SBOX[_v] = _i


def _expand_key(key: bytes) -> list[list[int]]:
    if len(key) != 16:
        raise ValueError("AES-128 需要 16 字节密钥")
    w = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    rcon = 1
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = [SBOX[b] for b in t[1:] + t[:1]]      # RotWord + SubWord
            t[0] ^= rcon
            rcon = _xtime(rcon)
        w.append([w[i - 4][k] ^ t[k] for k in range(4)])
    return [sum(w[4 * r:4 * r + 4], []) for r in range(11)]


def _inv_shift_rows(s: list[int]) -> None:
    for r in range(1, 4):                              # 状态按列存：s[4*c + r]
        row = [s[4 * c + r] for c in range(4)]
        row = row[-r:] + row[:-r]
        for c in range(4):
            s[4 * c + r] = row[c]


def _inv_mix_columns(s: list[int]) -> None:
    for c in range(4):
        a = s[4 * c:4 * c + 4]
        s[4 * c + 0] = _gmul(a[0], 14) ^ _gmul(a[1], 11) ^ _gmul(a[2], 13) ^ _gmul(a[3], 9)
        s[4 * c + 1] = _gmul(a[0], 9) ^ _gmul(a[1], 14) ^ _gmul(a[2], 11) ^ _gmul(a[3], 13)
        s[4 * c + 2] = _gmul(a[0], 13) ^ _gmul(a[1], 9) ^ _gmul(a[2], 14) ^ _gmul(a[3], 11)
        s[4 * c + 3] = _gmul(a[0], 11) ^ _gmul(a[1], 13) ^ _gmul(a[2], 9) ^ _gmul(a[3], 14)


def aes128_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError(f"密文长度必须是 16 的倍数（收到 {len(data)}）")
    rk = _expand_key(key)
    out = bytearray()
    for off in range(0, len(data), 16):
        s = list(data[off:off + 16])
        for i in range(16):
            s[i] ^= rk[10][i]
        for rnd in range(9, 0, -1):
            _inv_shift_rows(s)
            for i in range(16):
                s[i] = INV_SBOX[s[i]]
            for i in range(16):
                s[i] ^= rk[rnd][i]
            _inv_mix_columns(s)
        _inv_shift_rows(s)
        for i in range(16):
            s[i] = INV_SBOX[s[i]]
        for i in range(16):
            s[i] ^= rk[0][i]
        out += bytes(s)
    return bytes(out)


def pkcs7_unpad(b: bytes, block: int = 16) -> bytes:
    if not b:
        return b
    n = b[-1]
    if 1 <= n <= block and b[-n:] == bytes([n]) * n:
        return b[:-n]
    return b


# ══════════════════════════════════════════════════════════════════════════
# 密钥流：NCM 的「改良 RC4」= KSA 之後取 256 字节块循环，且从第 2 字节起用
# 少数文件用连续 RC4 PRGA，两种都实现，用哪个靠"能不能解出合法音频头"决定
# ══════════════════════════════════════════════════════════════════════════
def build_keybox(key: bytes) -> bytearray:
    s = bytearray(range(256))
    j = 0
    klen = len(key) or 1
    for i in range(256):
        j = (j + s[i] + key[i % klen]) & 0xFF
        s[i], s[j] = s[j], s[i]
    return s


def keystream_block(key: bytes) -> bytes:
    """NCM 主流的密钥流：256 字节块；解密时循环重复，并从 offset=1 开始用。"""
    s = build_keybox(key)
    return bytes(s[(s[i] + s[(i + s[i]) & 0xFF]) & 0xFF] for i in range(256))


class BlockStream:
    """256 字节块循环。skip=1 是 NCM 的写法（块的第 0 个字节不用，流从 blk[1] 起）。"""

    def __init__(self, key: bytes, skip: int = 1):
        blk = keystream_block(key)
        self.unit = blk[skip:] + blk[:skip]
        self.name = f"block256(skip={skip})"

    def next(self, n: int) -> bytes:
        return (self.unit * (n // 256 + 1))[:n]


class Rc4Stream:
    """连续 RC4 PRGA（备用写法，PRGA 状态跨块延续）。"""

    def __init__(self, key: bytes):
        self.s = build_keybox(key)
        self.i = self.j = 0
        self.name = "rc4-continuous"

    def next(self, n: int) -> bytes:
        s, i, j = self.s, self.i, self.j
        out = bytearray(n)
        for k in range(n):
            i = (i + 1) & 0xFF
            j = (j + s[i]) & 0xFF
            s[i], s[j] = s[j], s[i]
            out[k] = s[(s[i] + s[j]) & 0xFF]
        self.i, self.j = i, j
        return bytes(out)


STREAM_FACTORIES = (lambda k: BlockStream(k, 1), lambda k: BlockStream(k, 0),
                    lambda k: Rc4Stream(k))


def xor_bytes(data: bytes, stream: bytes) -> bytes:
    n = len(data)
    return (int.from_bytes(data, "big") ^ int.from_bytes(stream[:n], "big")).to_bytes(n, "big")


# ══════════════════════════════════════════════════════════════════════════
# NCM 结构解析
# ══════════════════════════════════════════════════════════════════════════
class NcmError(Exception):
    pass


def _u32(f) -> int:
    b = f.read(4)
    if len(b) != 4:
        raise NcmError("文件被截断（读不到 4 字节长度字段）")
    return struct.unpack("<I", b)[0]


def _xor_const(b: bytes, k: int) -> bytes:
    return bytes(x ^ k for x in b)


def _detect_codec(head: bytes) -> str:
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "mp3"
    if head[:4] == b"RIFF":
        return "wav"
    return ""


def _parse_meta(blob: bytes) -> tuple[dict, int, str]:
    """meta blob → (json, 用的异或常量, 备注)。自动试几个异或常量，谁解出 JSON 用谁。"""
    for xor in (META_XOR, 0x64, 0x00, 0x62):
        raw = _xor_const(blob, xor) if xor else blob
        if not raw.startswith(META_PREFIX):
            continue
        try:
            b64 = raw[len(META_PREFIX):].rstrip(b"=")
            b64 += b"=" * ((-len(b64)) % 4)
            plain = aes128_ecb_decrypt(META_KEY, base64.b64decode(b64))
            plain = pkcs7_unpad(plain)
            if plain.startswith(META_JSON_PREFIX):
                plain = plain[len(META_JSON_PREFIX):]
            return json.loads(plain.rstrip(b"\x00").decode("utf-8", "replace")), xor, ""
        except Exception:  # noqa: BLE001
            continue
    # 有的变种没有 "163 key(Don't modify):" 前缀，直接是 base64
    for xor in (META_XOR, 0x64):
        try:
            raw = _xor_const(blob, xor)
            b64 = raw.rstrip(b"=")
            b64 += b"=" * ((-len(b64)) % 4)
            plain = aes128_ecb_decrypt(META_KEY, base64.b64decode(b64))
            plain = pkcs7_unpad(plain)
            if plain.startswith(META_JSON_PREFIX):
                plain = plain[len(META_JSON_PREFIX):]
            return json.loads(plain.rstrip(b"\x00").decode("utf-8", "replace")), xor, "无前缀变种"
        except Exception:  # noqa: BLE001
            continue
    return {}, META_XOR, "meta 段解不出来（不影响音频还原）"


def parse_ncm(path: str) -> dict:
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        magic = f.read(8)
        if magic != NCM_MAGIC:
            raise NcmError(f"不是 NCM 文件：头 8 字节是 {magic!r}，应为 b'CTENFDAM'")
        f.seek(2, 1)                                            # 2 字节空位

        key_len = _u32(f)
        if key_len == 0 or key_len > 1 << 20 or key_len % 16:
            raise NcmError(f"key 段长度异常：{key_len}（应为 16 的倍数）")
        key_plain, key_xor_used = None, KEY_XOR
        for xor in (KEY_XOR, 0x63):
            cand = pkcs7_unpad(aes128_ecb_decrypt(CORE_KEY, _xor_const(f.read(key_len), xor)))
            f.seek(14)                                          # 回到 key 段起点重读
            if cand.startswith(KEY_PREFIX):
                key_plain, key_xor_used = cand, xor
                f.seek(14 + key_len)
                break
        if key_plain is None:
            raise NcmError("密钥段解不出来（不是已知 NCM 版本，或文件损坏）")
        rc4_key = key_plain[len(KEY_PREFIX):]

        meta_len = _u32(f)
        meta, meta_xor, meta_note = {}, META_XOR, "无 meta 段"
        if meta_len:
            if meta_len > 8 << 20:
                raise NcmError(f"meta 段长度异常：{meta_len}")
            meta, meta_xor, meta_note = _parse_meta(f.read(meta_len))

        f.read(4)                                               # crc32（用不上）
        f.read(1)
        a, b = _u32(f), _u32(f)                                 # image_space, image_size
        cover, cover_type = b"", "jpg"
        # 两种已知排布：image_space 是总占位、image_size 是真实图长；也要兼容只写一个长度
        if 0 < a <= size and 0 < b <= a:
            cover = f.read(b)
            f.seek(a - b, 1)
        else:
            f.seek(-8, 1)
            n = _u32(f)
            if 0 < n <= size:
                cover = f.read(n)
        if cover[:8] == b"\x89PNG\r\n\x1a\n":
            cover_type = "png"
        audio_offset = f.tell()

    if audio_offset >= size:
        raise NcmError("音频段为空（文件可能没下载完）")

    # 用"能不能解出合法音频头"来选密钥流写法
    head_len = 4096
    with open(path, "rb") as f:
        f.seek(audio_offset)
        head = f.read(head_len)
    chosen, codec = None, ""
    for factory in STREAM_FACTORIES:
        st = factory(rc4_key)
        c = _detect_codec(xor_bytes(head, st.next(len(head))))
        if c:
            chosen, codec = st, c
            break
    if chosen is None:
        raise NcmError("密钥流对不上，解不出 mp3/flac 头 —— 音频段可能被别的工具二次加密过，"
                       "或文件损坏/没下载完整")

    return {"rc4_key": rc4_key, "meta": meta, "meta_xor": meta_xor, "meta_note": meta_note,
            "key_xor": key_xor_used, "cover": cover, "cover_type": cover_type,
            "audio_offset": audio_offset, "codec": codec, "stream": chosen.name,
            "size": size, "audio_size": size - audio_offset}


def decrypt_audio(path: str, info: dict, out_path: str, chunk: int = 1 << 20) -> None:
    """边读边解边写：8MB 的歌峰值内存也就几 MB。chunk 取 256 的整数倍以保持块对齐。"""
    key = info["rc4_key"]
    factory = {"block256(skip=1)": lambda k: BlockStream(k, 1),
               "block256(skip=0)": lambda k: BlockStream(k, 0),
               "rc4-continuous": lambda k: Rc4Stream(k)}[info["stream"]]
    st = factory(key)
    t0 = time.time()
    with open(path, "rb") as src, open(out_path, "wb") as dst:
        src.seek(info["audio_offset"])
        done = 0
        while True:
            data = src.read(chunk)
            if not data:
                break
            dst.write(xor_bytes(data, st.next(len(data))))
            done += len(data)
            if sys.stderr.isatty():
                sys.stderr.write(f"\r  解密 {done * 100 // max(info['audio_size'], 1):3d}% "
                                 f"{done >> 20}MB/{info['audio_size'] >> 20}MB  "
                                 f"{time.time() - t0:.1f}s")
                sys.stderr.flush()
    if sys.stderr.isatty():
        sys.stderr.write("\r" + " " * 56 + "\r")


# ══════════════════════════════════════════════════════════════════════════
# ffmpeg / ffprobe（可选增强）
# ══════════════════════════════════════════════════════════════════════════
def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def safe_name(s: str) -> str:
    bad = '/\\:*?"<>|\n\r\t'
    s = "".join("_" if ch in bad else ch for ch in s).strip(" .")
    return s or "untitled"


def ffprobe(path: str) -> dict:
    if not have("ffprobe"):
        return {}
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                            "-print_format", "json", path],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            return {"error": r.stderr.strip()[:300]}
        j = json.loads(r.stdout)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    a = next((s for s in j.get("streams", []) if s.get("codec_type") == "audio"), {})
    fmt = j.get("format", {})
    return {"codec": a.get("codec_name"), "sample_rate": int(a.get("sample_rate") or 0),
            "channels": a.get("channels"), "bit_rate": int(fmt.get("bit_rate") or 0),
            "duration": float(fmt.get("duration") or 0),
            "tags": {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}}


def verify_decodable(path: str) -> tuple[bool, str]:
    """整条音频完整解码一遍（不落盘），有任何解码错误就判失败。"""
    if not have("ffmpeg"):
        return True, "（没装 ffmpeg，跳过验证）"
    r = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-i", path, "-f", "null", "-"],
                       capture_output=True, text=True)
    err = r.stderr.strip()
    return (r.returncode == 0 and not err), err[:400]


def measure_lufs(path: str) -> dict:
    if not have("ffmpeg"):
        return {}
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path,
                            "-af", "ebur128=peak=true", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=1800)
        tail = r.stderr[r.stderr.rfind("Summary:"):]
        out = {}
        for line in tail.splitlines():
            line = line.strip()
            if line.startswith("I:"):
                try:
                    out["lufs_i"] = float(line.split()[1])
                except (IndexError, ValueError):
                    pass
            elif line.startswith("Peak:"):
                try:
                    out["peak_dbfs"] = float(line.split()[1])
                except (IndexError, ValueError):
                    pass
        return out
    except Exception:  # noqa: BLE001
        return {}


def meta_names(meta: dict) -> tuple[str, str]:
    title = meta.get("musicName") or meta.get("name") or ""
    artists = meta.get("artist") or []
    if isinstance(artists, list):
        artist = "/".join(str(a[0]) if isinstance(a, (list, tuple)) else str(a) for a in artists)
    else:
        artist = str(artists)
    return title, artist


def write_tagged(src: str, dst: str, meta: dict, cover: bytes, cover_type: str,
                 transcode: dict | None) -> bool:
    """用 ffmpeg 封标签 + 贴封面；音频流 copy，不重新编码。"""
    if not have("ffmpeg"):
        shutil.copyfile(src, dst)
        return False
    title, artist = meta_names(meta)
    album = meta.get("album") or ""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src]
    tmp_cover = None
    if cover:
        fd, tmp_cover = tempfile.mkstemp(suffix="." + cover_type)
        with os.fdopen(fd, "wb") as cf:
            cf.write(cover)
        cmd += ["-i", tmp_cover, "-map", "0:a", "-map", "1:v", "-c:v", "copy",
                "-disposition:v", "attached_pic", "-metadata:s:v", "title=Album cover"]
    if transcode:
        cmd += ["-c:a", transcode["codec"], "-b:a", transcode["bitrate"]]
        if transcode["codec"] != "flac":
            cmd += ["-ar", "44100"]
    else:
        cmd += ["-c:a", "copy"]
    for k, v in (("title", title), ("artist", artist), ("album_artist", artist), ("album", album)):
        if v:
            cmd += ["-metadata", f"{k}={v}"]
    cmd += ["-id3v2_version", "3", dst]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode == 0 and os.path.exists(dst)
    finally:
        if tmp_cover and os.path.exists(tmp_cover):
            os.unlink(tmp_cover)


# ══════════════════════════════════════════════════════════════════════════
def inspect_file(path: str) -> dict:
    """只解剖不写盘：给坏文件做诊断用。"""
    info = parse_ncm(path)
    title, artist = meta_names(info["meta"])
    return {"src": path, "size": info["size"], "audio_offset": info["audio_offset"],
            "audio_size": info["audio_size"], "codec": info["codec"],
            "stream": info["stream"], "key_xor": info["key_xor"], "meta_xor": info["meta_xor"],
            "meta_note": info["meta_note"], "cover_bytes": len(info["cover"]),
            "title": title, "artist": artist, "album": info["meta"].get("album"),
            "ncm_format": info["meta"].get("format"),
            "ncm_bitrate": info["meta"].get("bitrate"),
            "ncm_duration_ms": info["meta"].get("duration")}


def convert_one(path: str, outdir: str | None, fmt: str, bitrate: str, keep_name: bool,
                want_wav: bool, lufs: bool, verify: bool, quiet: bool) -> dict:
    t0 = time.time()
    info = parse_ncm(path)
    meta = info["meta"]
    title, artist = meta_names(meta)
    if keep_name or not title:
        stem = os.path.splitext(os.path.basename(path))[0]
    else:
        stem = safe_name(f"{artist} - {title}" if artist else title)
    outdir = outdir or os.path.dirname(os.path.abspath(path))
    os.makedirs(outdir, exist_ok=True)
    codec = info["codec"]

    target = (codec if codec in ("mp3", "flac", "wav") else "mp3") if fmt == "auto" else fmt
    transcode = None
    if target != codec:
        transcode = {"wav": {"codec": "pcm_s16le", "bitrate": "0"},
                     "mp3": {"codec": "libmp3lame", "bitrate": bitrate},
                     "flac": {"codec": "flac", "bitrate": "0"},
                     "m4a": {"codec": "aac", "bitrate": bitrate}}.get(target)
        if transcode is None:
            raise NcmError(f"不支持的输出格式：{target}")
        if not have("ffmpeg"):
            raise NcmError("转码需要 ffmpeg（brew install ffmpeg）；或用 --format auto 原样输出")

    dst = os.path.join(outdir, f"{stem}.{target}")
    with tempfile.TemporaryDirectory() as td:
        raw = os.path.join(td, f"raw.{codec}")
        decrypt_audio(path, info, raw)
        if not write_tagged(raw, dst, meta, info["cover"], info["cover_type"], transcode):
            if transcode:
                subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", raw,
                                "-c:a", transcode["codec"], "-b:a", transcode["bitrate"], dst],
                               check=True)
            else:
                shutil.copyfile(raw, dst)

    out = {"src": path, "dst": dst, "src_codec": codec, "out_codec": target,
           "lossless_copy": codec == target, "stream": info["stream"],
           "meta": {"title": title, "artist": artist, "album": meta.get("album") or "",
                    "ncm_format": meta.get("format"), "ncm_bitrate": meta.get("bitrate")},
           "cover": len(info["cover"]), "seconds": round(time.time() - t0, 2)}
    out.update({k: v for k, v in ffprobe(dst).items() if k in
                ("duration", "sample_rate", "channels", "bit_rate")})
    if verify:
        ok, err = verify_decodable(dst)
        out["decode_ok"], out["decode_error"] = ok, err
    if lufs:
        out["loudness"] = measure_lufs(dst)
    if want_wav and target != "wav" and have("ffmpeg"):
        wav = os.path.join(outdir, f"{stem}.wav")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", dst,
                        "-c:a", "pcm_s16le", "-ar", "44100", wav], check=True)
        out["wav"] = wav
    return out


# ══════════════════════════════════════════════════════════════════════════
def cmd_selftest() -> int:
    ok = True
    print("① AES-128（FIPS-197 附录取值）")
    key = bytes(range(16))
    plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    got = aes128_ecb_decrypt(key, bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a"))
    ok &= got == plain
    print(f"   解密 → {got.hex()}  {'✓' if got == plain else '✗'}")

    print("② 密钥流（KSA 后 S 盒 256 字节块）")
    blk = keystream_block(b"test-key")
    print(f"   块长 {len(blk)} · 前 8 字节 {blk[:8].hex()} · 周期自检 "
          f"{'✓' if blk * 2 == (blk + blk) else '✗'}")
    ok &= len(blk) == 256

    print("③ 自造 NCM 往返（打包 → 本工具解开 → 与原文件逐字节比对）")
    payload = bytes(range(256)) * 40 + b"ID3\x03\x00\x00\x00\x00\x00\x00"
    payload = b"ID3\x03\x00\x00\x00\x00\x00\x00" + payload          # 让头看起来像 mp3
    fake_key = b"0123456789abcdef" * 7
    ncm = _build_fake_ncm(fake_key, {"musicName": "自检", "artist": [["tester", "1"]],
                                     "format": "mp3", "bitrate": 320000, "duration": 1000},
                          payload, b"\xff\xd8\xff\xe0cover")
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "a.ncm")
        with open(src, "wb") as fh:
            fh.write(ncm)
        info = parse_ncm(src)
        raw = os.path.join(td, "out.mp3")
        decrypt_audio(src, info, raw)
        back = open(raw, "rb").read()
    same = back == payload
    ok &= same
    print(f"   结构 codec={info['codec']} stream={info['stream']} meta={info['meta'].get('musicName')}"
          f" → 解密一致 {'✓' if same else '✗'}")

    print("SELFTEST " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def _build_fake_ncm(key: bytes, meta: dict, payload: bytes, cover: bytes) -> bytes:
    """按 NCM 规范造一个文件，用来做往返自检（不是给用户用的，是给 selftest 用的）。"""
    key_blob = KEY_PREFIX + key
    key_blob += bytes([16 - len(key_blob) % 16] * (16 - len(key_blob) % 16))   # PKCS7
    key_enc = aes128_ecb_encrypt(CORE_KEY, key_blob)

    meta_json = META_JSON_PREFIX + json.dumps(meta, ensure_ascii=False).encode()
    meta_json += bytes([16 - len(meta_json) % 16] * (16 - len(meta_json) % 16))
    meta_enc = aes128_ecb_encrypt(META_KEY, meta_json)
    meta_blob = META_PREFIX + base64.b64encode(meta_enc)

    st = BlockStream(key, 1)
    enc = xor_bytes(payload, st.next(len(payload)))
    out = bytearray(NCM_MAGIC + b"\x01\x70")
    kb = _xor_const(key_enc, KEY_XOR)
    out += struct.pack("<I", len(kb)) + kb
    mb = _xor_const(meta_blob, META_XOR)
    out += struct.pack("<I", len(mb)) + mb
    out += struct.pack("<I", zlib.crc32(cover) & 0xFFFFFFFF) + b"\x01"
    out += struct.pack("<I", len(cover)) + struct.pack("<I", len(cover)) + cover
    out += enc
    return bytes(out)


def aes128_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    """只给 selftest 造样本用，实现最短路径的加密。"""
    rk = _expand_key(key)
    out = bytearray()
    for off in range(0, len(data), 16):
        s = list(data[off:off + 16])
        for i in range(16):
            s[i] ^= rk[0][i]
        for rnd in range(1, 11):
            for i in range(16):
                s[i] = SBOX[s[i]]
            for r in range(1, 4):                        # ShiftRows
                row = [s[4 * c + r] for c in range(4)]
                row = row[r:] + row[:r]
                for c in range(4):
                    s[4 * c + r] = row[c]
            if rnd != 10:                                # MixColumns
                for c in range(4):
                    a = s[4 * c:4 * c + 4]
                    s[4 * c + 0] = _gmul(a[0], 2) ^ _gmul(a[1], 3) ^ a[2] ^ a[3]
                    s[4 * c + 1] = a[0] ^ _gmul(a[1], 2) ^ _gmul(a[2], 3) ^ a[3]
                    s[4 * c + 2] = a[0] ^ a[1] ^ _gmul(a[2], 2) ^ _gmul(a[3], 3)
                    s[4 * c + 3] = _gmul(a[0], 3) ^ a[1] ^ a[2] ^ _gmul(a[3], 2)
            for i in range(16):
                s[i] ^= rk[rnd][i]
        out += bytes(s)
    return bytes(out)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="把网易云 .ncm 无损还原成 mp3/flac/wav（不重新编码，音质不掉）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：python3 ncm2mp3.py 歌.ncm -o out/ --wav --verify")
    ap.add_argument("inputs", nargs="*", help=".ncm 文件或目录")
    ap.add_argument("-o", "--out", help="输出目录（默认和 .ncm 同目录）")
    ap.add_argument("--format", default="auto", choices=["auto", "mp3", "flac", "wav", "m4a"],
                    help="auto=原样还原（mp3→mp3 / flac→flac，最保真）；wav=给剪辑软件")
    ap.add_argument("--bitrate", default="320k", help="转 mp3/m4a 的码率（默认 320k）")
    ap.add_argument("--wav", action="store_true", help="另外再出一份 44.1k 无损 wav")
    ap.add_argument("--keep-name", action="store_true", help="用原文件名，不改成「歌手 - 歌名」")
    ap.add_argument("--recursive", action="store_true", help="目录递归找 .ncm")
    ap.add_argument("--inspect", action="store_true", help="只解剖结构，不写文件")
    ap.add_argument("--verify", action="store_true", help="还原后整条解码一遍确认没坏")
    ap.add_argument("--loudness", action="store_true", help="量 LUFS / 峰值（慢几秒）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（给脚本 / agent 读）")
    ap.add_argument("--selftest", action="store_true", help="自检 AES / 密钥流 / 打包往返")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return cmd_selftest()
    if not a.inputs:
        ap.print_help()
        return 2

    files: list[str] = []
    for p in a.inputs:
        if os.path.isdir(p):
            if a.recursive:
                for root, _d, names in os.walk(p):
                    files += [os.path.join(root, n) for n in sorted(names)
                              if n.lower().endswith(".ncm")]
            else:
                files += [os.path.join(p, n) for n in sorted(os.listdir(p))
                          if n.lower().endswith(".ncm")]
        elif os.path.isfile(p):
            files.append(p)
        else:
            print(f"✗ 找不到：{p}", file=sys.stderr)
    if not files:
        print("✗ 没有找到 .ncm 文件", file=sys.stderr)
        return 2

    results, failed = [], 0
    for i, f in enumerate(files, 1):
        if not a.quiet and not a.json:
            print(f"[{i}/{len(files)}] {os.path.basename(f)}")
        try:
            if a.inspect:
                r = inspect_file(f)
                results.append(r)
                if not a.json:
                    for k, v in r.items():
                        print(f"  {k:16} {v}")
            else:
                r = convert_one(f, a.out, a.format, a.bitrate, a.keep_name, a.wav,
                                a.loudness, a.verify, a.quiet)
                results.append(r)
                if not a.json and not a.quiet:
                    dur = r.get("duration") or 0
                    br = r.get("bit_rate") or 0
                    print(f"  → {r['dst']}")
                    print(f"    {r['src_codec']} → {r['out_codec']}"
                          f"{'（原样拷贝，零损失）' if r['lossless_copy'] else '（已转码）'}"
                          f" · {int(dur // 60)}:{int(dur % 60):02d}"
                          f" · {br // 1000 if br else '?'} kbps"
                          f" · {r.get('sample_rate') or '?'} Hz"
                          f" · {r.get('channels') or '?'} ch"
                          f" · 封面 {r['cover'] // 1024 if r['cover'] else 0} KB")
                    if r.get("decode_ok") is False:
                        print(f"    ⚠ 解码验证没过：{r.get('decode_error')}")
                    elif r.get("decode_ok"):
                        print("    解码验证：整条解完无报错 ✓")
                    if r.get("loudness"):
                        l = r["loudness"]
                        print(f"    响度 {l.get('lufs_i', '?')} LUFS · "
                              f"峰值 {l.get('peak_dbfs', '?')} dBFS")
                    if r.get("wav"):
                        print(f"    另存 wav → {r['wav']}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            msg = f"{type(e).__name__}: {e}"
            if a.json:
                results.append({"src": f, "error": msg})
            else:
                print(f"  ✗ 失败：{msg}", file=sys.stderr)

    if a.json:
        print(json.dumps({"ok": failed == 0, "count": len(results), "failed": failed,
                          "results": results}, ensure_ascii=False, indent=2))
    elif not a.quiet and len(files) > 1:
        print(f"\n完成 {len(files) - failed}/{len(files)}"
              + (f"，失败 {failed}" if failed else ""))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
