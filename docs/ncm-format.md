# `.ncm` 格式逆向笔记

这份文档记录的是**实测**出来的布局，用一份 320 kbps 的真实文件核对过（8,604,276 字节，音频段起于
第 495,111 字节）。所有偏移都是从 0 开始、小端（`<I`）计数。

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 0   8B   "CTENFDAM"                       魔数                          │
│ 8   2B   gap                              实测 0x01 0x70，两端都不读       │
│ 10  4B   key_len                          u32 LE                       │
│ 14  N    key blob                         N = key_len（16 的倍数）       │
├──────────────────────────────────────────────────────────────────────────┤
│ ?   4B   meta_len                         u32 LE                       │
│ ?   M    meta blob                        M = meta_len（可为 0）          │
├──────────────────────────────────────────────────────────────────────────┤
│ ?   4B   crc32                            u32 LE，对封面算的，不用管       │
│ ?   1B   flag                             0x01                          │
│ ?   4B   image_space                      u32 LE（封面总占位）            │
│ ?   4B   image_size                       u32 LE（真实封面字节数）        │
│ ?   K    封面                              jpg / png，K = image_size     │
│ ?        padding                          image_space - image_size 字节   │
├──────────────────────────────────────────────────────────────────────────┤
│ ?   ...  音频                              原始 mp3/flac ⊕ 密钥流         │
└──────────────────────────────────────────────────────────────────────────┘
```

`audio_offset = 头 + key_len + 4 + meta_len + 4 + 1 + 8 + image_space`

## 1. key 段

```python
CORE_KEY = bytes.fromhex("687A4852416D736F356B496E62617857")   # ASCII "hzHRAmso5kInbaxW"

plain = AES128_ECB_decrypt(CORE_KEY, xor(blob, 0x64))   # ← 0x64
plain = pkcs7_unpad(plain)
assert plain.startswith(b"neteasecloudmusic")
rc4_key = plain[len(b"neteasecloudmusic"):]              # 17 字节之后
```

⚠ `key_len` 常常是 **144** 而不是 128：因为 `b"neteasecloudmusic" + key` 之后做了 PKCS7 填充。
不要硬编码 128，先 `unpad` 再切前缀。

## 2. meta 段

```python
META_KEY = bytes.fromhex("2331346C6A6B5F215C5D2630553C2728")   # ASCII "#14ljk_!\]&0U<'("

raw = xor(blob, 0x63)                                    # ← 0x63，和 key 段不一样！
assert raw.startswith(b"163 key(Don't modify):")          # 22 字节
b64 = raw[22:]
b64 = b64.rstrip(b"=")                                   # ⚠ 先去掉尾部 '='
b64 += b"=" * (-len(b64) % 4)                            #   再补到 4 的倍数
plain = AES128_ECB_decrypt(META_KEY, b64decode(b64))
plain = pkcs7_unpad(plain)
assert plain.startswith(b"music:")
meta = json.loads(plain[6:])
```

三个坑，每一个都足以让人拿到一堆废字节：

1. meta 的异或常量是 `0x63`，**key 段才是 `0x64`**。
2. base64 必须**先去 `=` 再重新补**。直接往原串尾部加 `'==='` 会得到错误长度（填充非法）。
3. 少数变体没有 `163 key(Don't modify):` 前缀，整段就是 base64。

`meta` 长这样：

```json
{"musicId":"...","musicName":"...","artist":[["Rapeter","12563131"]],
 "albumId":"...","album":"...","bitrate":320000,"duration":202666,"format":"mp3"}
```

`format` 和 `duration` 非常好用：`duration` 是毫秒，可以直接用来**交叉验证还原是否完整**
（`ffprobe` 出来的秒数应当一致）；`format` 告诉你壳里到底是 mp3 还是 flac。

## 3. 音频密钥流（本格式唯一真正反直觉的地方）

不是连续 RC4 PRGA。而是：先做标准 RC4 的 KSA，然后从 S 盒**派生出一个 256 字节的块**，
**循环重复**这个块，并且**从块的第 2 个字节开始**用。

```python
S = list(range(256))
j = 0
for i in range(256):                       # 标准 RC4 KSA
    j = (j + S[i] + key[i % len(key)]) & 0xFF
    S[i], S[j] = S[j], S[i]

blk = bytes(S[(S[i] + S[(i + S[i]) & 0xFF]) & 0xFF] for i in range(256))   # 256 字节块

# 解密：注意 (i + 1)，也就是 skip=1
plain = bytes(c ^ blk[(i + 1) & 0xFF] for i, c in enumerate(cipher))
```

等价写法：`keystream = (blk * (n // 256 + 1))[1:1 + n]`。

已知的三种变体（按此顺序探测，命中即停）：

| 变体 | 密钥流 | 出现频率 |
|---|---|---|
| `block256(skip=1)` | `blk[(i + 1) % 256]` | **主流**，本文档实测文件就是这种 |
| `block256(skip=0)` | `blk[i % 256]` | 少量 |
| `rc4-continuous` | 标准 PRGA 连续输出 | 少量早期文件 |

## 4. 怎么确认自己解对了

不要靠"听起来对不对"，靠**文件头**。解密前 4 KiB，看开头：

| 编码 | 魔数 |
|---|---|
| mp3（带 ID3） | `49 44 33` = `ID3` |
| mp3（裸 MPEG 帧） | `FF Ex` 或 `FF Fx`（`b[1] & 0xE0 == 0xE0`） |
| flac | `66 4C 61 43` = `fLaC` |
| ogg | `4F 67 67 53` = `OggS` |
| wav | `52 49 46 46` = `RIFF` |

实测文件在 `audio_offset + 1` 处解出 `ID3 03 04 00 ... TSSE` —— 这同时确定了 skip=1。

第二重验证：还原后用 `ffmpeg -i out.mp3 -f null -` 整条解码一遍，零报错即通过；
再用 `ffprobe` 比时长与 meta 里的 `duration`。

## 5. 走过的死路（省你几天）

下面这些都试过、都失败，别重复：

- 连续 RC4 PRGA 解全曲后搜 `ID3` / `fLaC` / `LAME` / `Xing` / `Info` → 全无命中。**因为算法本身不对，不是偏移错了。**
- 各种 key 截断：144 / 127 / 112 / 111 字节。
- `md5(key)` / `sha256(key)` 派生密钥。
- 按 0x8000 / 0x4000 / 65536 字节分块重置 PRGA。
- 把密钥流索引当作**绝对文件偏移**。
- 周期性检测：按 16 / 32 / 48 / 64 / 1044 / 2088 字节找重复 —— 只有 **256** 的位移在局部出现 0.16 的相关性（那就是答案，只是当时没顺着往下走）。
- 假设音频段是 AES-ECB / CBC（CORE_KEY、META_KEY、`key[:16]`、md5、sha256 全试）。

## 6. 参考实现

`ncmdump` 0.1.1（PyPI, 2018, @Nzix）里正是上述变体的原始实现，但它依赖 `pycryptodome` + `mutagen`。
本项目用标准库重写了 AES-128-ECB、PKCS7 与两种密钥流，并对三处差异做了自动探测。
