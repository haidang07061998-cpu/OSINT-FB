"""Thiết lập console để in được tiếng Việt trên Windows.

Windows mặc định dùng codepage cp1252 (Western European) nên `print()` với
chuỗi có dấu (ầ, ệ, ạ, ử, ữ...) sẽ ném UnicodeEncodeError và làm sập lệnh.
Hàm này ép stdout/stderr về UTF-8, thay thế cho việc phải đặt tay
PYTHONIOENCODING=utf-8 trước mỗi lần chạy.
"""
from __future__ import annotations

import sys
from typing import Any

_ENCODINGS = {"utf-8", "utf8", "utf_8"}


def _reconfigure(stream: Any, name: str) -> bool:
    """Đặt encoding UTF-8 cho một stream nếu stream hỗ trợ.

    Trả về True nếu đã đổi. Không ném lỗi: stream đã bị thay (vd khi chạy trong
    notebook, hoặc stream đã đóng) thì bỏ qua.
    """
    if stream is None:
        return False
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return False
    current = (getattr(stream, "encoding", "") or "").lower()
    if current in _ENCODINGS:
        return False
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):  # pragma: no cover - phụ thuộc môi trường
        return False
    return True


def setup_console() -> bool:
    """Ép stdout + stderr sang UTF-8. Trả về True nếu có phải thay đổi.

    Gọi hàm này sớm nhất có thể (trước mọi lệnh `print`/`input`) khi chạy CLI.
    """
    changed = False
    for stream in (sys.stdout, sys.stderr):
        if _reconfigure(stream, "stream"):
            changed = True
    return changed


def describe() -> str:
    """Mô tả encoding hiện tại, tiện cho log chẩn đoán."""
    return (
        f"stdout={getattr(sys.stdout, 'encoding', '?')} "
        f"stderr={getattr(sys.stderr, 'encoding', '?')}"
    )
