---
name: android-reverse
description: Android APK 逆向（本镜像有 jadx / apktool / dex2jar）。遇到 APK、DEX、资源或 manifest 题时用；纯 ELF/PE 不要用它，走 ghidra-headless 或 radare2。需要重打包签名时要额外确认工具，不要假设已经装好。
---

# Android / APK 逆向

本镜像：`jadx 1.5.6`、`apktool 2.7.0`、`d2j-dex2jar`、OpenJDK 25。

## 先分工：三个工具各管一段

| 工具 | 产出 | 什么时候用 |
| --- | --- | --- |
| `apktool d` | 资源、`AndroidManifest.xml`、`smali/` | 看权限、组件、字符串资源、so 清单 |
| `jadx` | Java 近似源码 | 读业务逻辑（首选） |
| `d2j-dex2jar` | `.jar` | 需要喂给其它 Java 工具时 |

## 起手

```bash
mkdir -p /work/apk
apktool d -f -o /work/apk/dec /work/chall.apk      # 资源 + smali
jadx -d /work/apk/java /work/chall.apk            # Java 源码
find /work/apk/dec -name '*.so' -o -name 'AndroidManifest.xml'
grep -RIn 'flag\|secret\|token\|http' /work/apk/java | head -30
```

`jadx` 输出可能上千个文件：**不要** `cat` 整棵目录，用 `grep -RIn` 和
"从入口找"的方式（`MainActivity`、`Application`、被 manifest 导出的组件）。

## 读什么

- `AndroidManifest.xml`：导出组件（`android:exported="true"`）、权限、
  deep link、`android:name` 指向的类 —— CTF 常从这里给入口。
- `res/values/strings.xml`：flag 常直接在这里或被简单编码。
- `smali`：Java 反编译失败/被混淆时看它；寄存器 `v0/p1`，比 Java 更接近字节码。
- `assets/` / `lib/`：真正的算法常在 native `.so`，转 `radare2` / `ghidra`。

## 坑

- **jadx 是近似源码**：混淆后类名是 `a/b/c`，控制流可能被优化。反编译失败
  时加 `--show-bad-code` 仍然能看个大概。
- **重打包要签名**：`apktool b` 只产出未签名 APK；本镜像不保证有
  `apksigner`/`keytool` 的完整链。先 `command -v apksigner keytool` 确认，
  没有就别承诺"改完能装"。
- **`apktool` 的 smali 不是 Java**：读逻辑先用 jadx；改逻辑才动 smali。
- 大 APK 的 `apktool d` 可能要几十秒；`run_shell` 的 timeout 给足。

## 与其他工具的衔接

- native `.so`：`ghidra-decompile` / `radare2` / `objdump`。
- 证书/签名：`apksigner verify --print-certs`（如已装）；`keytool -printcert`。
- 网络行为：`strings` 找 URL，再 `http_request` 验证；不要在沙箱里直接
  安装 APK 运行。
