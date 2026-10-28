"""Test fb_time + window filter."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest

from scraper.fb_time import parse_fb_time, within_window
from scraper.post_scraper import parse_posts
from models import TimeWindow

NOW = datetime(2026, 10, 10, 12, 0, 0)


@pytest.mark.parametrize("text,delta", [
    ("vừa xong", timedelta(minutes=0)),
    ("5 phút trước", timedelta(minutes=5)),
    ("2 giờ trước", timedelta(hours=2)),
    ("3 ngày trước", timedelta(days=3)),
    ("2 tuần trước", timedelta(weeks=2)),
    ("3 tháng trước", timedelta(days=90)),
    ("Hôm qua lúc 08:30", timedelta(days=1) + timedelta(hours=3, minutes=30)),
])
def test_relative_vn_time(text, delta):
    got = parse_fb_time(text, NOW)
    want = NOW - delta
    assert got is not None, f"không parse: {text}"
    assert abs((got - want).total_seconds()) < 90, f"{text}: {got} vs {want}"


def test_absolute_vn_date():
    m = parse_fb_time("12 tháng 5 lúc 20:15", NOW)
    assert m and (m.month, m.day, m.hour) == (5, 12, 20), m


def test_absolute_vn_date_with_year():
    m = parse_fb_time("17 tháng 4, 2024 lúc 09:00", NOW)
    assert m and m.year == 2024 and (m.month, m.day, m.hour) == (4, 17, 9), m


@pytest.mark.parametrize("text,days,want", [
    ("2 giờ trước", 7, True),
    ("3 ngày trước", 7, True),
    ("3 tháng trước", 7, False),
    ("30 ngày trước", 7, False),
    ("3 ngày trước", 30, True),
])
def test_window_filter(text, days, want):
    assert within_window(text, days, NOW) is want


@pytest.fixture
def posts_html():
    now_ts = int(datetime(2026, 10, 10, 10, 0, 0).timestamp())
    return f"""
<html><body>
<div role="article"><span data-utime="{now_ts}">cũ</span><h2>A</h2><p>Bài 1 trong nhóm, có nội dung đủ dài để parse và kiểm tra</p><a href="/nguoidung1">link</a><span>5 bình luận</span><span>12 lượt thích</span></div>
<div role="article"><span aria-label="2 giờ trước">gần</span><h2>B</h2><p>Bài mới trong ngày với nội dung đủ dài để parse cho chắc chắn</p></div>
<div role="article"><div>3 tháng trước, nội dung bài cũ</div><h2>C</h2><p>Bài quá cũ, phải bị lọc khỏi khoảng 7 ngày</p></div>
</body></html>
"""


def test_parse_posts_with_window(posts_html):
    posts = parse_posts(posts_html, limit=10, window=TimeWindow(days_back=7))
    assert len(posts) == 2, [p.content[:40] for p in posts]
    assert any(p.likes for p in posts), "phải parse được likes"
    assert any(p.comments_count for p in posts), "phải parse được comment count"


def test_parse_posts_no_window(posts_html):
    assert len(parse_posts(posts_html, limit=10, window=None)) == 3


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
