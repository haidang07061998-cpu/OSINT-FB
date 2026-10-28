"""Parse thời gian Facebook (tiếng Việt + EN) -> datetime để lọc theo khoảng.

Facebook hiển thị tương đối/không đồng nhất:
  "vừa xong", "5 phút trước", "2 giờ trước", "Hôm qua lúc 08:30",
  "3 ngày trước", "12 tháng 5 lúc 20:15", "17 tháng 4, 2024 lúc 09:00",
  "May 12 at 20:15", "3h", "2d"...
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

_NUM_UNIT = re.compile(
    r"(?i)\b(\d+)\s*(phút|phut|p|m|giờ|gio|g|h|ngày|ngay|d|tuần|tuan|w|tháng|thang|thg|m)\b"
)
_HOUR_SEP = re.compile(r"(?i)\b(\d{1,2})\s*[:h]\s*(\d{2})\b")
_DATE_FULL = re.compile(
    r"(?i)\b(?:ngày\s*)?(\d{1,2})\s*(?:tháng|thang|thg)\s*(\d{1,2})"
    r"(?:\s*,?\s*(?:năm\s*)?(\d{4}))?\b"
)
_EN_DATE = re.compile(
    r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})"
    r"(?:\s*,?\s*(\d{4}))?\b"
)
_MONTH_EN = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_UNIT_DELTA = {
    "phút": ("minutes", 1), "phut": ("minutes", 1), "p": ("minutes", 1), "m": ("minutes", 1),
    "giờ": ("hours", 1), "gio": ("hours", 1), "g": ("hours", 1), "h": ("hours", 1),
    "ngày": ("days", 1), "ngay": ("days", 1), "d": ("days", 1),
    "tuần": ("days", 7), "tuan": ("days", 7), "w": ("days", 7),
    "tháng": ("days", 30), "thang": ("days", 30), "thg": ("days", 30),
}


def _build(now: datetime, year: int, month: int, day: int, text: str,
           explicit_year: bool) -> Optional[datetime]:
    """Ghép ngày + giờ (nếu có) thành datetime.

    Facebook bỏ năm khi bài còn mới, nên một ngày KHÔNG kèm năm mà rơi vào
    tương lai chỉ có một nghĩa: bài đăng năm trước (hôm nay 3/3 đọc thấy
    "12 tháng 5" thì đó là 12/5 năm trước). Không lùi năm thì bài cũ bị coi
    là ở tương lai và lọt qua mọi bộ lọc theo khoảng thời gian.
    """
    hm = _HOUR_SEP.search(text)
    try:
        if hm:
            base = now.replace(year=year, month=month, day=day,
                               hour=int(hm.group(1)), minute=int(hm.group(2)),
                               second=0, microsecond=0)
        else:
            base = now.replace(year=year, month=month, day=day,
                               second=0, microsecond=0)
    except ValueError:
        return None
    if not explicit_year and base > now:
        try:
            base = base.replace(year=year - 1)
        except ValueError:  # 29/02 khi năm trước không phải năm nhuận
            return None
    return base


def parse_fb_time(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Trả về datetime ước lượng; None nếu không nhận diện được."""
    if not text:
        return None
    now = now or datetime.now()
    low = text.lower()

    if "vừa xong" in low or "just now" in low:
        return now

    # "Hôm qua lúc 08:30" / "Yesterday at 8:30"
    m = re.search(r"(?i)(?:hôm qua|yesterday)\s*(?:lúc\s*)?(\d{1,2})[:h](\d{2})", low)
    if m:
        d = (now - timedelta(days=1)).replace(
            hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0
        )
        return d
    if "hôm qua" in low or "yesterday" in low:
        return (now - timedelta(days=1)).replace(second=0, microsecond=0)

    # "N phút/giờ/ngày/tuần/tháng trước"
    m = _NUM_UNIT.search(low)
    if m and "trước" in low:
        val = int(m.group(1))
        unit = _UNIT_DELTA.get(m.group(2))
        if unit:
            field, mult = unit
            return now - timedelta(**{field: val * mult})

    # "12 tháng 5 lúc 20:15 [2024]"
    m = _DATE_FULL.search(low)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        explicit_year = bool(m.group(3))
        year = int(m.group(3)) if explicit_year else now.year
        return _build(now, year, month, day, low, explicit_year)

    # "May 12 at 20:15, 2024"
    m = _EN_DATE.search(low)
    if m:
        month = _MONTH_EN.get(m.group(1)[:3], 0)
        day = int(m.group(2))
        explicit_year = bool(m.group(3))
        year = int(m.group(3)) if explicit_year else now.year
        if month:
            return _build(now, year, month, day, low, explicit_year)

    return None


def within_window(text: str, days_back: int, now: Optional[datetime] = None) -> bool:
    """True nếu thời gian bài đăng nằm trong khoảng days_back ngày. days_back<=0 -> luôn True."""
    if days_back <= 0:
        return True
    dt = parse_fb_time(text, now)
    if dt is None:
        return True  # không rõ thời gian thì giữ lại (an toàn)
    return dt >= (now or datetime.now()) - timedelta(days=days_back)
