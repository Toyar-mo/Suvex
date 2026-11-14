---
name: apk-reverse
description: Android APK 静态逆向：jadx/apktool 解包、Manifest、smali、Java 逻辑与 native so 交接。
---

<!--
  Adapted from zhaoxuya520/reverse-skill (MIT License).
  Source: https://github.com/zhaoxuya520/reverse-skill/tree/main/skills/apk-reverse
  Changes: removed upstream routing/self-bootstrap and Frida/adb/rebuild
  scripts not installed in the standard Suvex image; kept static jadx/apktool
  workflow and native .so handoff.
-->

# APK 静态逆向

## 适用

- 分析 APK 的 Java/Kotlin 业务逻辑；
- 定位登录、签名、风控、证书校验、root 检测的静态实现；
- 查看/修改 `AndroidManifest.xml` 与 smali；
- APK 内含 `.so` 时交给 `ghidra-reverse` / `radare2`。

## 环境边界

镜像已装：

```text
jadx / apktool / dex2jar / java
```

**没有** Frida、adb、Android 设备或模拟器。不要写 Frida/adb/安装类命令；
需要动态 Hook 时明确说明当前环境不具备，改用静态分析或把样本交给有设备的环境。

## 工作流

### 1. 解包

```bash
mkdir -p /work/persist/apk/out
jadx -d /work/persist/apk/out/java /work/attachments/app.apk
apktool d -f -o /work/persist/apk/out/smali /work/attachments/app.apk
```

先看：

- `AndroidManifest.xml`：包名、权限、exported 组件、`usesCleartextTraffic`；
- `res/xml/network_security_config.xml`：证书 pinning；
- `assets/`、`lib/<abi>/`：配置、密钥、native 库；
- `META-INF/`：签名信息（只读核对，不要伪造签名）。

### 2. 定位逻辑

- 用 jadx 搜索 URL、`sign`、`encrypt`、`token`、`nonce`、`X509TrustManager`、
  `RootBeer`、`SafetyNet`、`Play Integrity`；
- 对混淆代码先看调用链和字符串，不要凭类名猜；
- 需要改 smali 时只改最小指令，记录原始偏移/方法签名。

### 3. native 交接

`lib/**.so` 复制到 `/work/persist/apk/native/`，用 `ghidra-decompile` 或
`rabin2 -zz` / `radare2` 分析；不要在 APK skill 里假装有 IDA/Ghidra MCP。

### 4. 产物

```text
/work/persist/apk/
  manifest-summary.md
  findings.md
  java/ smali/（按需）
  native/（so）
  evidence/（截图、命令输出、hash）
```

结论必须区分：静态看到 / 动态验证过 / 当前环境无法验证。

## 常见坑

- `apktool` 重建可能改变对齐/签名；没有设备时不要声称“已安装验证”；
- 加固/壳会把真实逻辑放进 native 或动态加载；先确认是否加壳；
- 只读分析不要修改原始 APK；需要修改时另存副本并记录。
