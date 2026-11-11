"""自定义工具配方的版本探测（v1.91）。

只测**纯函数**：输出拼装与解析。真正的容器探测属于集成路径
（`service.probe_recipe_tools`），需要镜像与 Docker；在无 Docker 的
环境里跑它只会得到一条"连不上"的假失败，掩盖解析层的真实回归。
"""

from __future__ import annotations

import pytest

from app.kernel.recipes import (
    VERSION_MAX_CHARS,
    parse_version_probe,
    version_probe_command,
)


def test_command_batches_path_and_version() -> None:
    script = version_probe_command(['nmap', 'ffuf'])
    assert 'command -v nmap' in script
    assert 'nmap --version' in script
    assert 'command -v ffuf' in script
    # 一条脚本而不是两次往返。
    assert script.count('__SUPEX_VERSION__') >= 8


def test_command_rejects_injection_shaped_names() -> None:
    with pytest.raises(ValueError):
        version_probe_command(['nmap; rm -rf /'])
    with pytest.raises(ValueError):
        version_probe_command(['$(id)'])


def test_parse_distinguishes_missing_from_unasked() -> None:
    out = (
        '__SUPEX_VERSION__Snmap=/usr/bin/nmap__SUPEX_VERSION__\n'
        '__SUPEX_VERSION__Vnmap=7.94-1__SUPEX_VERSION__\n'
        '__SUPEX_VERSION__Sghost=__SUPEX_VERSION__\n'
    )
    parsed = parse_version_probe(out)
    assert parsed['nmap'] == {'path': '/usr/bin/nmap', 'version': '7.94-1'}
    # 问过、没有 → 出现且为空
    assert parsed['ghost'] == {'path': '', 'version': ''}
    # 没问过 → 不出现
    assert 'sqlmap' not in parsed


def test_parse_keeps_version_containing_equals() -> None:
    out = '__SUPEX_VERSION__Sx=/bin/x__SUPEX_VERSION__\n__SUPEX_VERSION__Vx=tool 1.0 (build=abc)__SUPEX_VERSION__\n'
    parsed = parse_version_probe(out)
    assert parsed['x']['version'] == 'tool 1.0 (build=abc)'


def test_parse_truncates_long_versions() -> None:
    long = 'v' * (VERSION_MAX_CHARS + 50)
    out = f'__SUPEX_VERSION__St=/bin/t__SUPEX_VERSION__\n__SUPEX_VERSION__Vt={long}__SUPEX_VERSION__\n'
    parsed = parse_version_probe(out)
    assert len(parsed['t']['version']) == VERSION_MAX_CHARS


def test_unrelated_output_is_ignored() -> None:
    out = 'some banner\nnmap version 7.94\n/bin/sh: 1: x: not found\n'
    assert parse_version_probe(out) == {}
