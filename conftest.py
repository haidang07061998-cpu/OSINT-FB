"""Thiết lập chung cho pytest.

- Ép stdout/stderr về UTF-8 để test in tiếng Việt không crash trên Windows
  (console mặc định cp1252 không encode được dấu).
- Cho phép `import` trực tiếp các module ở thư mục gốc mà không cần
  `sys.path.insert` trong từng file test.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from console import setup_console  # noqa: E402

setup_console()
