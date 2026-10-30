# ncm2mp3

**把网易云音乐的 `.ncm` 还原成能直接用的 `mp3` / `flac` / `wav` —— 逐字节还原，零依赖。**

Bit-exact `.ncm` unwrapper: zero dependencies, no transcoding, tags and cover art preserved.
[English](#english) · [格式逆向笔记](docs/ncm-format.md) · [装成 agent 技能](#装成-agent-技能)

`.ncm` 不是一种音频编码，它是一只壳：壳里装的就是**原始** mp3 或 flac 的字节流（用密钥流异或过，前面套着
AES-128 加密的头）。把壳撬开，得到的就是网易云当初编码的那个文件本身 —— **不转码、不重新压缩、音质一点不掉**。

单文件实现，**零第三方依赖**：AES-128 是手写的，裸 `python3` 就能跑（不需要 `pycryptodome` / `mutagen` /
`numpy`）。`ffmpeg` 是可选的，只用于封标签、贴封面、转码、量响度和验证解码。

```
$ ./ncm.sh 歌.ncm
[1/1] 歌.ncm
  → 歌手 - 歌名.mp3
    mp3 → mp3（原样拷贝，零损失） · 3:22 · 339 kbps · 48000 Hz · 2 ch · 封面 482 KB
    解码验证：整条解完无报错 ✓
```

## 为什么还需要另一个 ncm 解码器

公开发布的大部分 ncmdump 在新版客户端产出的文件上会失败，因为格式在三处变了。本工具对三处**全部自动探测**：

| # | 变了什么 | 本工具怎么办 |
|---|---|---|
| 1 | **meta 段**的异或常量是 `0x63`，而 **key 段**是 `0x64` —— 两段不一样 | 依次试 `0x63`、`0x64` 和无前缀变体，取能解出合法 JSON 的那个 |
| 2 | 音频密钥流**不是连续 RC4**：它是从 KSA 状态派生的 **256 字节块**，**循环重复**使用，并从**第 2 个字节**开始（`skip=1`） | 依次试 block/skip=1、block/skip=0、连续 RC4，取能解出合法 mp3/flac 头的那个 |
| 3 | 封面长度前缀是**两个** `u32`（`image_space`、`image_size`），不是一个 | 两种排布都解析，正确跳过填充 |

如果只从本仓库带走一句话：**密钥流是循环重复的 256 字节块，不是流式的。**
完整二进制布局与逐行伪代码见 [docs/ncm-format.md](docs/ncm-format.md)。

## 快速开始

```bash
git clone https://github.com/EmingChao/ncm2mp3.git
cd ncm2mp3

./ncm.sh env             # 自检：python3 / ffmpeg / 算法（不到 1 秒）
./ncm.sh 歌.ncm           # 还原 → 「歌手 - 歌名.mp3」（名字取自文件内嵌元数据）
./ncm.sh wav 歌.ncm       # 另出一份 44.1 kHz 无损 wav（剪映 / PR / DaVinci 用这个最稳）
./ncm.sh mv  歌.ncm       # MV 素材包：mp3 + wav + 量 LUFS 峰值 + 打印下一步
./ncm.sh batch ~/Music/  # 递归批量，把目录里所有 .ncm 都转掉
./ncm.sh check 歌.ncm     # 只解剖结构、不写文件（坏文件诊断）
./ncm.sh selftest        # 算法自检（FIPS-197 AES 向量 + 自造 ncm 往返）
```

需要 Python 3.8+，别的都不用装。`ffmpeg` 在 `PATH` 里更好（可选）。

## 命令行参数

`ncm.sh` 只是一层薄封装，本体是 `scripts/ncm2mp3.py`（这一个文件可以单独拷到任何机器上跑）。

```bash
python3 scripts/ncm2mp3.py 歌.ncm                  # 原地还原，无损拷贝
python3 scripts/ncm2mp3.py 歌.ncm -o out/          # 指定输出目录
python3 scripts/ncm2mp3.py 歌.ncm --format wav     # 给剪辑软件用的 wav（这才会真的转码）
python3 scripts/ncm2mp3.py 歌.ncm --format mp3 --bitrate 320k
python3 scripts/ncm2mp3.py 歌.ncm --keep-name      # 保留原文件名
python3 scripts/ncm2mp3.py 歌.ncm --verify         # 用 ffmpeg 整条解码一遍，有问题就报
python3 scripts/ncm2mp3.py 歌.ncm --loudness       # EBU R128 LUFS + 真峰值
python3 scripts/ncm2mp3.py 歌.ncm --json           # 机器可读，给脚本 / agent 用
python3 scripts/ncm2mp3.py --selftest
```

| 参数 | 含义 |
|---|---|
| `-o/--out DIR` | 输出目录（默认和 `.ncm` 同目录） |
| `--format auto\|mp3\|flac\|wav\|m4a` | `auto`（默认）= 复现源编码，逐字节一致 |
| `--bitrate 320k` | 只在转 mp3/m4a 时生效 |
| `--wav` | 额外再写一份 44.1 kHz 16 bit wav |
| `--keep-name` | 不改名成「歌手 - 歌名」 |
| `--recursive` | 递归目录 |
| `--inspect` | 只打印结构后退出，不写文件 |
| `--verify` | 整条 ffmpeg 解码验证，报解码错误 |
| `--loudness` | 量 LUFS / 真峰值 |
| `--json` | 输出 JSON 结果 |
| `--selftest` | 算法自检 |

## 装成 agent 技能

`SKILL.md` 按标准 agent-skill frontmatter 编写，整个目录就是一个可加载的技能。

```bash
./install.sh                            # 复制（不是软链）到 ~/.agents/skills/ncm2mp3
./install.sh ~/.claude/skills/ncm2mp3   # 或者装到别处
```

装好之后，一句「把这个 ncm 转成 mp3」就会路由到这个技能和它的动词表
（`env` / `check` / 转换 / `wav` / `mv` / `batch` / `selftest`）。

## 验证

这里什么都不盲信 —— 仓库自带三道自检：

```bash
./ncm.sh selftest                 # AES 对 FIPS-197 官方向量 + 自造 ncm 打包往返
./ncm.sh check  歌.ncm            # 结构对不对：codec / 音频偏移 / 元数据
./ncm.sh 歌.ncm --verify          # 整条解码，零报错
ffprobe 歌.mp3                    # 时长、码率要和对得上 ncm 里记的元数据
```

一个真实 320 kbps 测试文件：8,604,276 字节，音频段从第 495,111 字节开始（8,109,165 字节 ≈ 320 kbps ×
202.7 s）—— 解出来的 mp3 能干净解码，且时长与文件内记录的 duration **精确到毫秒**吻合，
这正是"密钥流对了"的证明。

## 排错

| 现象 | 原因 | 怎么办 |
|---|---|---|
| `不是 NCM 文件：头 8 字节是 b'...'` | 根本不是 ncm，或者下载到一半 | `./ncm.sh check` 看头；重新下载 |
| `密钥段解不出来` | 未知的 ncm 版本，或文件损坏 | 重新下载；开 issue 带上头部字节 |
| `密钥流对不上，解不出 mp3/flac 头` | 音频段被别的工具二次加密过，或文件被截断 | 用「码率 × 时长」估一下大小对比；重新下载 |
| 能播但比预期短 | 没下载完整 | 重新下载 |
| 没封面 / 没标签 | 没装 `ffmpeg` | `brew install ffmpeg`（只有封标签和转码需要它） |
| 文件名缺字 / 有怪字符 | 标题里有非法字符 | 已自动替换为 `_`；或加 `--keep-name` |

## 边界

- **只处理 `.ncm`。** QQ 音乐的 `.qmc*` / `.mflac` / `.mgg`、酷狗的 `.kgm` / `.kgma`、虾米的 `.xm`
  是另外几套算法，本工具**不吃**。
- 只处理本地文件：不下载、无账号逻辑、**全程零网络请求**。
- 默认逐字节还原 —— 输出永远**不会比源文件更好**，只会和它完全一样。

## 免责

请只用于你自己磁盘上合法拥有的文件，用于个人用途（视频剪辑、归档），并遵守文件来源平台的
服务条款。本仓库不包含任何内容、不促进任何分发，它只负责去掉一层本地容器封装。

## License

MIT —— 见 [LICENSE](LICENSE)。

---

## English

**`.ncm` is not an audio codec — it is a wrapper.** Inside sits the *original* mp3/flac byte stream,
XORed with a keystream, behind an AES-128-encrypted header. Unwrap it and you get the exact file
NetEase encoded: no transcoding, no re-compression, no quality loss.

Single file, **zero dependencies** — AES is implemented by hand, so a bare `python3` runs it
(no `pycryptodome`, no `mutagen`, no `numpy`). `ffmpeg` is optional: tags, cover art, transcoding,
loudness and verification only.

### Why another ncm decoder?

Most public implementations fail on files from newer clients. Three things drifted, all auto-detected here:

| # | What changed | How this tool handles it |
|---|---|---|
| 1 | the **metadata** blob is XORed with `0x63`, the **key** blob with `0x64` | tries `0x63`, `0x64` and a no-prefix variant; keeps the one that yields valid JSON |
| 2 | the audio keystream is **not** continuous RC4: it is a 256-byte block derived from the KSA state, **repeated**, consumed from **byte 1** (`skip=1`) | tries block/skip=1, block/skip=0 and continuous RC4; keeps the one that yields a valid mp3/flac header |
| 3 | the cover-art prefix is **two** `u32`s (`image_space`, `image_size`), not one | parses both layouts and skips padding correctly |

Take one line away: **the keystream is a repeated 256-byte block, not a stream.**
Full layout and pseudocode: [docs/ncm-format.md](docs/ncm-format.md).

### Quick start

```bash
git clone https://github.com/EmingChao/ncm2mp3.git && cd ncm2mp3
./ncm.sh env                 # sanity check: python3 / ffmpeg / algorithm self-test (<1s)
./ncm.sh song.ncm            # unwrap → "Artist - Title.mp3"
./ncm.sh wav song.ncm        # also emit a 44.1 kHz wav for NLEs
./ncm.sh mv  song.ncm        # mp3 + wav + LUFS/peak + next-step hints
./ncm.sh batch ~/Music/      # recursive
./ncm.sh check song.ncm      # dump structure only, write nothing
./ncm.sh selftest            # AES (FIPS-197 vector) + packed-ncm round trip
```

Requires Python 3.8+; nothing else. CLI flags: `-o DIR`, `--format auto|mp3|flac|wav|m4a`,
`--bitrate`, `--wav`, `--keep-name`, `--recursive`, `--inspect`, `--verify`, `--loudness`, `--json`,
`--selftest`. The program itself is `scripts/ncm2mp3.py` — copy that one file anywhere.

Install as an agent skill with `./install.sh` (copies, does not symlink).

### Scope and disclaimer

Only `.ncm`. `.qmc*` / `.mflac` / `.mgg` (QQ Music), `.kgm` (Kugou) and `.xm` (Xiami) use different
schemes and are not supported. Local files only — no downloads, no accounts, **no network calls**.
Byte-exact by default: the output is never better than the source, only identical to it.

Use it on files you legitimately have on your own disk, for personal use such as video editing or
archiving, and respect the terms of service of the platform the file came from.

MIT licensed.
