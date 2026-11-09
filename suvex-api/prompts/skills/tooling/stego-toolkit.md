---
name: stego-toolkit
description: 图片/音频隐写与条码（本镜像有 stegoveritas、zbarimg、steghide、binwalk、pngcheck、exiftool）。遇到"图片里好像有东西"、二维码、LSB、附加数据时用；纯密码学或流量题不要用它。
---

# 隐写与条码工具箱

本镜像：`stegoveritas`（含 `-bruteLSB`）、`zbarimg`、`steghide`、`binwalk`、
`pngcheck`、`exiftool`、`foremost`。**没有 zsteg**（用 stegoveritas 覆盖）。

## 固定顺序（先便宜后昂贵）

```bash
file /work/chall.png
exiftool /work/chall.png | head -40          # 元数据/注释/版权
strings -n 6 /work/chall.png | head           # 明文线索
zbarimg -q /work/chall.png                    # 二维码/条码
pngcheck -v /work/chall.png                   # PNG 块结构（是否被改坏）
binwalk /work/chall.png                       # 附加/嵌套文件
```

前四步几十毫秒；`-bruteLSB` 可能要几分钟，放最后。

## stegoveritas（一站式）

```bash
mkdir -p /work/stego
stegoveritas -out /work/stego /work/chall.png
stegoveritas -bruteLSB -out /work/stego /work/chall.png   # LSB 穷举（慢）
stegoveritas -meta -trailing -out /work/stego /work/chall.png
stegoveritas -steghide -password '题目给的口令' /work/chall.jpg
```

`-out` 必须指向**可写目录**（放 `/work`）；默认 `./results` 在只读根上会失败。
输出是一堆文件：先 `find /work/stego -type f -size +0` 看非空结果，再逐个
`file` / `strings`。

## 其它工具的分工

- `steghide`：只支持 **JPEG/BMP/WAV/AU**，**不认 PNG**。先
  `steghide info file.jpg` 确认有嵌入数据，再 `extract -sf`；口令错与"没有
  隐写"的报错是同一句，别把后者当结论。
- `zbarimg`：只认标准条码/二维码；模糊图先放大/锐化（imagemagick 已装）。
- `binwalk -e`：按签名提取；提取失败时会安静地留空目录，**不要**把空目录
  当"没有隐藏文件"。
- `foremost`：按文件头尾雕刻；碎片文件可能损坏，恢复后要逐个验证。

## 坑

- **PNG 的 LSB 不用 steghide**：那是 stegoveritas `-bruteLSB` / `-extractLSB`
  的活；zsteg 未装。
- **调色板/位深**：改过位深的图可能打不开；先 `pngcheck` 确认结构。
- **音频**：`ffmpeg` 提波形/频谱（已装）；镜像没有 `sox`，别写它。
- **字典**：镜像**没有预装 rockyou**；口令要么题目给，要么用少量候选试，
  不要凭空白跑大字典。
- 输出目录放 `/work`；`/tmp` 不保证可写（根只读时）。

## 与其他工具的衔接

- 提取出的压缩包：`7z` / `unzip` / `bkcrack`（已知明文攻击）。
- 需要看图：`view_image`（本平台工具）—— 截图/图片先真正"看"一眼，再猜。
- 内存/磁盘取证：`volatility3` / `sleuthkit`。
