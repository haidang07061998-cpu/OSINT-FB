"""Nạp dữ liệu mà không giấu lỗi.

Vấn đề: `try: load() except: return []` biến mọi lỗi thành "chưa có dữ liệu".
Người dùng thấy bảng trống và tưởng đã quét, trong khi thật ra là DB khoá,
schema cũ, hay file hỏng.

Ở đây lỗi vẫn được nuốt (dashboard không được sập), nhưng được ghi lại để
hiện ra cho người dùng.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, List, Tuple

logger = logging.getLogger("osint.safe_load")

# Lỗi đã báo để không spam lặp lại cùng một thông báo mỗi lần rerun.
_seen: List[str] = []


def reset_seen() -> None:
    """Xoá danh sách lỗi đã báo (gọi khi người dùng bấm nạp lại dữ liệu)."""
    _seen.clear()


def safe_load(loader: Callable[[], Any], empty: Any,
              on_error: Callable[[str], None] = None) -> Any:
    """Gọi `loader()`; lỗi thì báo qua `on_error` và trả `empty`.

    `on_error` nhận chuỗi thông báo đã định dạng. Không truyền thì chỉ ghi log.
    """
    try:
        return loader()
    except Exception as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
        if msg not in _seen:
            _seen.append(msg)
            logger.error("Khong doc duoc du lieu - %s", msg)
            if on_error is not None:
                on_error(msg)
        return empty


def report_collector(sink: List[str]) -> Callable[[str], None]:
    """Tạo callback gom thông báo lỗi vào `sink` (tiện cho test)."""
    return lambda msg: sink.append(msg)
