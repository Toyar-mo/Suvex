#!/usr/bin/env python3
"""Userspace ARM64 函数模拟模板（unicorn）—— 无 KVM 环境下跑 .so 里的单个函数。

用法：
    python3 unicorn-arm64-probe.py <path-to-arm64.so> <function_vaddr_hex>

把 .so 的全部 PT_LOAD 段映射进 unicorn，从给定函数地址开始执行，逐条反汇编并
记录寄存器；函数 `ret`（LR=0 返回）或超出步数即停。可扩展为「喂密文→读明文」：
在 TARGET 函数里设为 arg0 指向的 buffer 写上密文，执行到返回后读 arg0 指向的内存
即明文。

依赖：unicorn、capstone（chat 沙箱镜像 /opt/venv 已自带 unicorn 2.1.2 + capstone 6.0.0）。
定位函数地址：`strings -n 6 x.so | grep -iE 'Decrypt|Submit'` 找到字符串，再在反汇编里
找引用它的函数边界（capstone / objdump / radare2）。本模板只演示「能跑起来」。
"""
import struct
import sys

from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn.arm64_const import (
    UC_ARM64_REG_SP,
    UC_ARM64_REG_LR,
    UC_ARM64_REG_X0,
)
from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM


def load_elf64_segments(path):
    data = open(path, "rb").read()
    if data[:4] != b"\x7fELF" or data[4] != 2:  # 64-bit
        raise SystemExit("不是 64 位 ELF")
    e_phoff = struct.unpack_from("<Q", data, 0x20)[0]
    e_phentsize = struct.unpack_from("<H", data, 0x36)[0]
    e_phnum = struct.unpack_from("<H", data, 0x38)[0]
    segs = []
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        if struct.unpack_from("<I", data, off)[0] != 1:  # PT_LOAD
            continue
        flags = struct.unpack_from("<I", data, off + 4)[0]
        p_off = struct.unpack_from("<Q", data, off + 8)[0]
        p_vaddr = struct.unpack_from("<Q", data, off + 0x10)[0]
        p_filesz = struct.unpack_from("<Q", data, off + 0x20)[0]
        p_memsz = struct.unpack_from("<Q", data, off + 0x28)[0]
        segs.append((flags, p_off, p_vaddr, p_filesz, p_memsz))
    return data, segs


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    so_path = sys.argv[1]
    target = int(sys.argv[2], 16)
    max_steps = int(sys.argv[3], 16) if len(sys.argv) > 3 else 200

    data, segs = load_elf64_segments(so_path)
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for flags, p_off, p_vaddr, filesz, memsz in segs:
        base = p_vaddr & ~0xFFF
        end = (p_vaddr + memsz + 0xFFF) & ~0xFFF
        # 有的段之间不连续，逐个 map（若重叠则跳过已 map 部分）
        try:
            uc.mem_map(base, end - base)
        except Exception:
            pass
        uc.mem_write(p_vaddr, data[p_off:p_off + filesz])
    stack = 0x40000000
    uc.mem_map(stack, 0x100000)
    uc.reg_write(UC_ARM64_REG_SP, stack + 0x80000)
    uc.reg_write(UC_ARM64_REG_LR, 0)  # 返回地址 0 → ret 即结束

    md = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
    steps = [0]

    def on_code(uc, addr, size, user):
        steps[0] += 1
        if steps[0] <= max_steps:
            for insn in md.disasm(bytes(uc.mem_read(addr, 4)), addr):
                print(f"  {addr:#x}: {insn.mnemonic} {insn.op_str}")

    uc.hook_add(UC_HOOK_CODE, on_code)
    try:
        uc.emu_start(target, 0, count=max_steps)
    except Exception as e:  # UcError：跳到未 map 的内存/非法指令等
        print(f"  [stop] {type(e).__name__}: {e}")
    print(f"executed {steps[0]} instr; X0={uc.reg_read(UC_ARM64_REG_X0):#x}")


if __name__ == "__main__":
    main()
