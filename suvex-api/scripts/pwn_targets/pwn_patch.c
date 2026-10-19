/* 真的 pwn 靶机 #3 —— 必须先用 patchelf 才能跑起来的那道。
 *
 * ★ 这道题存在的理由
 *
 * `pwn_baby` 与 `pwn_rop` 都是"下载下来就能跑"，patchelf 全程用不上。
 * 于是"补装 patchelf 到底有没有用"这件事，在那两道题上永远测不出来 ——
 * 不是补强没用，是题目设计里根本没有需要它的那一环。
 *
 * 这道题把那一环补上：编译时用
 *     -Wl,--dynamic-linker=/pwn64/ld-linux-x86-64.so.2
 * 把 ELF 的 PT_INTERP 指到一个**不存在的路径**。于是直接执行会得到
 *     bash: ./pwn_patch: No such file or directory
 * 一个明明 `ls` 看得见、`file` 也认得、却"不存在"的文件 —— 这是 ELF
 * 层面最经典的假象，`file` 输出里那行 interpreter 就是唯一线索。
 *
 * 破解只有一个办法：
 *     patchelf --set-interpreter /lib64/ld-linux-x86-64.so.2 ./pwn_patch
 * 改不了就一步都走不下去。这就把"用不用得上 patchelf"从运气变成了
 * 二元可控的条件。
 *
 * ★ 为什么 flag 在二进制里（和前两道相反）
 *
 * `pwn_baby`/`pwn_rop` 是远程靶机，flag 只在运行时从 `PWN_FLAG_FILE` 读，
 * 二进制里没有 —— 那样才逼得出 checksec/cyclic/gdb 这一串。
 * 这道题**没有远程靶机**（真 CTF 里 patchelf 类题目通常也是纯本地附件），
 * 那 flag 就只能在二进制里。但直接写明文等于送分（`strings` 一把捞走），
 * 所以按字节 XOR 掩码存着，只有 `win()` 运行时才还原。
 * 于是"不解题就拿不到 flag"这条性质还在。
 *
 * 编译（Kali，gcc 14.2.0 实测）：
 *   gcc -O0 -fno-stack-protector -no-pie \
 *       -Wl,--dynamic-linker=/pwn64/ld-linux-x86-64.so.2 \
 *       -o pwn_patch pwn_patch.c
 *
 * ★ 实测偏移 = 88（0x58）。与 `pwn_baby` 同一个栈帧布局：
 *     sub $0x50,%rsp  ← 80 字节
 *     返回地址在 rbp+8 ⇒ 0x50 + 8 = 88
 *
 * checksec 期望：Partial RELRO / No canary found / NX enabled / No PIE
 * 额外期望：`readelf -l` 的 INTERP 段 = /pwn64/ld-linux-x86-64.so.2
 */

#include <stdio.h>
#include <unistd.h>
#include <sys/auxv.h>

/* flag 的 XOR 掩码。0x5a 只是为了让它不是明文，
 * 不承担任何密码学责任 —— 这道题考的是 patchelf，不是逆向。 */
#define MASK 0x5a

/* "flag{patchelf_before_you_run}" 逐字节 ^ 0x5a */
static const unsigned char ENC[] = {
    0x3c, 0x36, 0x3b, 0x3d, 0x21, 0x2a, 0x3b, 0x2e,
    0x39, 0x32, 0x3f, 0x36, 0x3c, 0x05, 0x38, 0x3f,
    0x3c, 0x35, 0x28, 0x3f, 0x05, 0x23, 0x35, 0x2f,
    0x05, 0x28, 0x2f, 0x34, 0x27
};

__attribute__((used)) static void win(void) {
    char out[sizeof ENC + 1];
    for (unsigned i = 0; i < sizeof ENC; i++) {
        out[i] = (char)(ENC[i] ^ MASK);
    }
    out[sizeof ENC] = '\0';
    puts(out);
}

int main(void) {
    /* 无缓冲：走管道时 stdout 全缓冲会把 banner 和 flag 都卡住，
     * 模型看到"连上了但没回显"，会误判成靶机不通。 */
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stdin, NULL, _IONBF, 0);

    /* ★ 为什么要有这一段：PATHELF 不是唯一路径的话，这道题就白造了。
     *
     * 实测发现一条绕过：`/lib64/ld-linux-x86-64.so.2 ./pwn_patch` ——
     * 解释器由命令行指定，`PT_INTERP` 里那个坏路径根本没人看，程序照样跑。
     * 这跟"用 gdb 硬改入口"是同一类：都不需要 patchelf。（探针见
     * `_tmp/probe_ldso.c`，两种加载方式唯一的干净差别就是 AT_BASE。）
     *
     * 内核按 `PT_INTERP` 加载时，AT_BASE 是那个解释器被映射到的地址（非 0）；
     * 反过来由 ld.so 当解释器去带一个可执行文件时，AT_BASE 是 0
     * —— 因为"解释器"就是它自己，没有第二个映像要报地址。
     * 于是只要 AT_BASE == 0，就说明这不是内核按 ELF 头正常加载起来的。
     */
    if (getauxval(AT_BASE) == 0) {
        puts("这个程序不是被正常加载起来的。");
        puts("直接执行它会报 No such file or directory —— 先去把那个路径修好。");
        return 2;
    }

    puts("=== pwn_patch ===");
    puts("提示：这个程序直接跑会报 No such file or directory，那不是文件没了。");
    puts("input: ");

    char buf[64];
    ssize_t n = read(0, buf, 512);
    printf("你发了 %zd 字节\n", n);
    return 0;
}
