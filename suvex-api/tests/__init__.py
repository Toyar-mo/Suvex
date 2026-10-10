"""测试包。

有 `__init__.py` 是刻意的：它让 `from tests.conftest import GOOD_PASSWORD`
这种导入可用（跨测试文件共用常量），而不是靠 pytest 的 rootdir 魔法。
"""

from __future__ import annotations
