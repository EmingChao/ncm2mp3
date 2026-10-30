---
name: ncm2mp3
description: 把网易云音乐的 .ncm 加密壳还原成能直接用的音频（mp3 / flac / wav），给剪辑和 MV 用。零依赖、不重新编码、音质不掉，顺手封好标题/歌手/专辑和封面。触发词：ncm、ncm转mp3、ncm解密、网易云音乐格式、网易云下载的歌打不开、解锁ncm、ncm2mp3、音乐格式转换、加密音乐转换、把这首歌转成mp3、"这个格式剪映打不开"、"网易云下载的歌怎么用"。也可以反过来查"这首歌是什么格式/码率/时长"，或诊断解不开的坏 ncm 文件。
---

# ncm2mp3

**一句话**：`.ncm` 不是一种音频编码，它是一只壳——壳里装的就是原始 mp3/flac 的字节。把壳撬开、按密钥流异或回来，得到的就是那个 mp3 本身。**不转码、不重新压缩，音质一点不掉。**

**装在哪**：实体安装在 `~/.agents/skills/ncm2mp3`（**复制文件，不是软链**）。源目录是 `~/develop/private/mv/ncm2mp3`；改完源目录后跑一次 `./install.sh` 同步过去（它会顺带跑自检）。

## 0. 先跑这一句

```bash
~/.agents/skills/ncm2mp3/ncm.sh env      # 自检：python3 / ffmpeg / 算法（不到 1 秒）
~/.agents/skills/ncm2mp3/ncm.sh 歌.ncm    # 转（输出在同目录，名字改成「歌手 - 歌名.mp3」）
```

在源目录里也可以写 `./ncm.sh ...`。脚本本体是 `scripts/ncm2mp3.py`（纯标准库，能单独拷到任何机器上跑）。

## 1. 动词表

| 命令 | 干什么 | 什么时候用 |
|---|---|---|
| `ncm.sh env` | 查 python3/ffmpeg + 跑算法自检 | 第一次用、换机器、出怪事 |
| `ncm.sh 歌.ncm` | **原样还原**（mp3→mp3 / flac→flac） | 90% 的情况，这个就够 |
| `ncm.sh wav 歌.ncm` | 还原 + 另出 44.1k 无损 wav | 剪映/PR/DaVinci 要 wav 时 |
| `ncm.sh mv 歌.ncm` | mp3(带标签封面) + wav + 量 LUFS + 打印下一步 | **要给 music-mv 做 MV 时用这个** |
| `ncm.sh batch 目录/` | 递归批量 | 一次几十首 |
| `ncm.sh check 歌.ncm` | 只解剖结构，不写文件 | 解不开、怀疑文件坏 |
| `ncm.sh selftest` | AES + 密钥流算法自检 | 想确认工具本身没坏 |

想要更多控制，直接调脚本（`ncm.sh` 会把参数原样透传）：

```bash
python3 scripts/ncm2mp3.py 歌.ncm -o out/            # 指定输出目录
python3 scripts/ncm2mp3.py 歌.ncm --format mp3 --bitrate 320k
python3 scripts/ncm2mp3.py 歌.ncm --keep-name        # 保原文件名
python3 scripts/ncm2mp3.py 歌.ncm --json             # 机器可读（给脚本/agent）
python3 scripts/ncm2mp3.py 歌.ncm --verify           # 整条解码一遍确认没坏
python3 scripts/ncm2mp3.py 歌.ncm --loudness         # 量 LUFS / 峰值
```

## 2. 选哪种格式（MV / 剪辑场景）

| 用途 | 选 | 理由 |
|---|---|---|
| 丢进剪映、PR、DaVinci、AE 当音轨 | **wav**（`ncm.sh wav`） | 剪辑软件对 wav 最无痛，也不会二次压缩 |
| music-mv 的 `analyze`（librosa 测拍） | **mp3** | librosa 直接读，体积小、算得快 |
| 存着听 / 发人 / 塞手机 | **mp3**（默认输出） | 原样就是 320k，别转码 |
| 要再压一版小的 | `--format mp3 --bitrate 192k` | ⚠ 会真的重编码，一般不需要 |

**默认永远别加 `--format`**：源是 mp3 就还你 mp3，源是 flac 就还你 flac，一个字节都不重新编。

## 3. 它到底做了什么（真出问题时才需要看）

```
[8B "CTENFDAM"] [2B] [key段] [meta段] [crc32] [1B] [封面长度×2] [封面 jpg] [音频 ⊕ 密钥流]
     ①                ②         ③                                    ④
```
① key 段：逐字节 `^0x64` → AES-128-ECB(核心密钥) → 去掉 `neteasecloudmusic` 前缀 = 密钥。
② meta 段：逐字节 **`^0x63`**（⚠ 和①的 `0x64` 不一样）→ 去 `163 key(Don't modify):` → base64 → AES-128-ECB(meta 密钥) → 去 `music:` → JSON（曲名/歌手/专辑/原格式/码率/时长）。
③ 封面长度是**两个 u32**（`image_space` 和 `image_size`），不是标准 ncmdump 里的一个。
④ 音频：密钥流 = KSA 后从 S 盒派生的 **256 字节块**，**循环重复**且**从第 2 个字节开始用**（skip=1）。不是连续 RC4。

工具对这四处都做了**自动探测**（异或常量试 0x63/0x64、密钥流试 block-skip1 / block-skip0 / 连续RC4、封面长度试两种排布），用"能不能解出合法 mp3/flac 头"来判定，所以不同版本的 ncm 一般都能吃。

## 4. 排错

| 现象 | 原因 | 怎么办 |
|---|---|---|
| `不是 NCM 文件：头 8 字节是 b'...'` | 根本不是 ncm，或下了一半 | `ncm.sh check` 看头；重下 |
| `密钥段解不出来` | 不是已知 NCM 版本 / 文件损坏 | `ncm.sh check`；换个来源重下 |
| `密钥流对不上，解不出 mp3/flac 头` | 音频段被别的工具二次加密过，或文件截断 | 文件不完整：看大小对比「码率×时长」；重下 |
| 解出来能播但时长短一截 | 没下载完整 | 重下 |
| 没封面 / 没标签 | 没装 ffmpeg | `brew install ffmpeg`（只有封标签和转码需要它，还原不需要） |
| 输出名字乱码或带 `/` | 歌曲名里有非法字符 | 工具会自动替换成 `_`；或用 `--keep-name` |

## 5. 边界（明确不做的事）

- **不做别的壳**：`.qmc*`(QQ音乐)、`.kgm/.kgma`(酷狗)、`.mflac/.mgg`(QQ)、`.xm`(虾米) 是另一套算法，本工具不吃 —— 直接告诉用户"那是 QQ/酷狗 的壳，不是 ncm"。
- **不碰 DRM 之外的东西**：只还原"用户本地已有的文件"的封装层，不下载、不联网、不带任何账号逻辑。
- **不重新编码**（除非你显式 `--format`），所以音质就是源文件音质，别期待"变好"。
- 输出的 mp3 **保留原 ID3 标签**并额外封入 曲名/歌手/专辑 + 封面（1280×1280 常见）。

## 6. 验收（做完自己检查一遍）

```bash
ncm.sh check 歌.ncm             # ① 结构对：codec=mp3、audio_offset 合理、meta 有歌名
ncm.sh 歌.ncm --verify          # ② 整条解码无报错（脚本会打印「解码验证：整条解完无报错 ✓」）
ffprobe 歌.mp3                   # ③ 时长/码率/采样率和 ncm meta 里的 duration/bitrate 对得上
```

三者都对，才算这活干完了。
