"""`ctf2` 子包的测试。

有 `__init__.py` 是刻意的：`tests/` 是一个包（见 `tests/__init__.py`），
而"包下面的目录也是包"让 `from tests.kernel.conftest import ...` 这种
导入稳定可用，不依赖 pytest 的 rootdir 推断。

⚠️ 它同时也是父 conftest 生效的前提之一：`tests/conftest.py` 里
那两个 autouse 的夹具（建库 + 迁移、每用例清表）对子目录同样生效，
所以这里**不重复**任何基础设施 —— 重复的代价是两份隔离口径漂移。
"""

from __future__ import annotations
