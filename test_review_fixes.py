"""Hồi quy cho loạt bug phát hiện khi rà soát toàn bộ code.

Mỗi test khoá lại ĐÚNG hành vi sai trước đây, để nó không quay lại.
"""
import json
import logging
import sys
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest

from models import AdminProfile, GroupPost, MonitoredGroup
from scraper.fb_time import parse_fb_time
from scraper.group_scraper import _to_number


# ------------------------------------------------ số kiểu "1.5 triệu"
@pytest.mark.parametrize("number,multiplier,expected", [
    ("1.5", "triệu", 1_500_000),     # trước đây xoá dấu chấm -> "15 triệu"
    ("1,5", "triệu", 1_500_000),
    ("1.5", "M", 1_500_000),
    ("12,500", "", 12_500),          # dấu phẩy nhóm nghìn
    ("1.500.000", "", 1_500_000),
    ("5.678", "", 5_678),            # nhóm đúng 3 chữ số -> nghìn
])
def test_decimal_numbers_are_not_magnified(number, multiplier, expected):
    assert _to_number(number, multiplier) == expected


# ------------------------------------------------ ngày không có năm ở tương lai
def test_date_without_year_in_future_rolls_back_one_year():
    """Hôm nay 3/3 đọc "12 tháng 5" -> phải là 12/5 NĂM TRƯỚC.

    Không lùi năm thì bài cũ bị coi là ở tương lai và lọt qua bộ lọc thời gian.
    """
    now = datetime(2026, 3, 3, 10, 0, 0)
    got = parse_fb_time("12 tháng 5 lúc 20:15", now)
    assert got is not None
    assert (got.year, got.month, got.day) == (2025, 5, 12), got


def test_date_without_year_in_past_keeps_current_year():
    now = datetime(2026, 10, 10, 12, 0, 0)
    got = parse_fb_time("12 tháng 5 lúc 20:15", now)
    assert got is not None and got.year == 2026, got


def test_explicit_future_year_is_kept():
    """"12 tháng 5, 2027" là ngày tương lai thật -> không được lùi năm."""
    now = datetime(2026, 3, 3, 10, 0, 0)
    got = parse_fb_time("12 tháng 5, 2027 lúc 20:15", now)
    assert got is not None and got.year == 2027, got


def test_english_date_without_year_rolls_back():
    now = datetime(2026, 3, 3, 10, 0, 0)
    got = parse_fb_time("May 12 at 20:15", now)
    assert got is not None and (got.year, got.month, got.day) == (2025, 5, 12), got


# ------------------------------------------------ members không bị nhét vào admins
def test_json_export_members_go_to_members_bucket(tmp_path, monkeypatch):
    from scraper.manual_importer import import_files

    monkeypatch.chdir(tmp_path)
    payload = {"group": {
        "id": "123456789",
        "name": "Nhom Thu Nghiem",
        "members": [
            {"label": "Nguyen A\nThành viên", "url": "https://www.facebook.com/a"},
            {"label": "Tran B", "url": "https://www.facebook.com/b"},
            {"label": "Nguyen A trung", "url": "https://www.facebook.com/a"},
        ],
    }}
    f = tmp_path / "g.json"
    f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    groups = import_files([f])
    assert len(groups) == 1
    g = groups[0]
    assert g.admins == [], "thành viên bị đếm nhầm thành quản trị viên"
    assert len(g.members) == 2, [m.profile_url for m in g.members]
    assert {m.role for m in g.members} == {"member"}


# ------------------------------------------------ file admin_*.html gắn vào nhóm
_GROUP_HTML = """<html><head>
<link rel="canonical" href="https://www.facebook.com/groups/123456789">
<meta property="og:title" content="Nhom Thu Nghiem">
<meta property="og:url" content="https://www.facebook.com/groups/123456789">
</head><body><h1>Nhom Thu Nghiem</h1><p>Nhom cong khai</p></body></html>"""

_ADMIN_HTML = """<html><head>
<link rel="canonical" href="https://www.facebook.com/profile.php?id=999888777666">
<meta property="og:title" content="Quan Tri Vien A">
<meta property="og:url" content="https://www.facebook.com/profile.php?id=999888777666">
</head><body><p>Song tai Ha Noi. 0987654321</p></body></html>"""


def test_admin_profile_file_is_attached_to_group(tmp_path, monkeypatch):
    """`admin_<gid>.html` phải thành admin của nhóm, không thành hồ sơ cá nhân.

    Trước đây PASS 1 xếp MỌI trang hồ sơ vào "person" nên quy ước tên file
    admin_/mod_/post_ không có tác dụng và không admin nào được gắn vào nhóm.
    """
    from scraper.manual_importer import import_files

    monkeypatch.chdir(tmp_path)
    (tmp_path / "group_123456789.html").write_text(_GROUP_HTML, encoding="utf-8")
    (tmp_path / "admin_123456789.html").write_text(_ADMIN_HTML, encoding="utf-8")

    groups = import_files(sorted(tmp_path.glob("*.html")))
    assert len(groups) == 1
    g = groups[0]
    assert [a.name for a in g.admins] == ["Quan Tri Vien A"], \
        [(a.name, a.role) for a in g.admins]
    assert g.admins[0].role == "admin"
    assert g.admins[0].fb_user_id == "999888777666"


# ------------------------------------------------ UID của admin lấy từ URL trang
def test_admin_uid_comes_from_page_url_not_viewer_js():
    """Trang lưu có `"userID"` của NGƯỜI ĐANG XEM -> không được dùng làm UID."""
    from scraper.profile_scraper import parse_admin_from_html

    html = """<html><head>
<script>{"userID":"123456789012"}</script>
<meta property="og:url" content="https://www.facebook.com/profile.php?id=999888777666">
<meta property="og:title" content="Admin That"></head>
<body><a href="https://www.facebook.com/111222333444">ban be</a></body></html>"""
    p = parse_admin_from_html(html, "https://www.facebook.com/profile.php?id=999888777666")
    assert p is not None
    assert p.fb_user_id == "999888777666", p.fb_user_id
    assert p.name == "Admin That", p.name


# ------------------------------------------------ ghi DB idempotent khi thiếu URL
def test_profile_without_url_is_not_duplicated(tmp_path):
    """UNIQUE(group_id, profile_url) không chặn NULL -> mỗi lần quét thêm 1 dòng."""
    from storage import query_admins, save_group

    db = tmp_path / "monitor.db"
    g = MonitoredGroup(
        group_id="123456789", name="G", url="https://www.facebook.com/groups/123456789",
        admins=[AdminProfile(name="Khong Co Link")],
    )
    save_group(db, g)
    save_group(db, g)
    save_group(db, g)

    rows = query_admins(db, "123456789")
    assert len(rows) == 1, f"bảng admins phình theo mỗi lần quét: {len(rows)} dòng"


# ------------------------------------------------ posts_json luôn là JSON hợp lệ
def test_snapshot_posts_json_is_valid_when_long(tmp_path):
    from storage import save_snapshot

    db = tmp_path / "monitor.db"
    posts = [GroupPost(content="noi dung bai " + "x" * 300) for _ in range(150)]
    g = MonitoredGroup(group_id="1", name="G", url="u", recent_posts=posts)
    save_snapshot(db, g)

    import sqlite3
    conn = sqlite3.connect(str(db))
    raw = conn.execute("SELECT posts_json FROM snapshots").fetchone()[0]
    conn.close()
    parsed = json.loads(raw)  # trước đây cắt chuỗi ở 20000 ký tự -> JSON hỏng
    assert isinstance(parsed, list)
    assert len(parsed) == 100


# ------------------------------------------------ khoá bản ghi khi thiếu URL
def test_entity_record_keys_unique_without_profile_url(tmp_path):
    """Hai hồ sơ không có link không được trùng `key` (DSU coi là một người)."""
    from scraper.entity_linker import load_records_from_db
    from storage import save_group

    db = tmp_path / "monitor.db"
    save_group(db, MonitoredGroup(
        group_id="123456789", name="G",
        url="https://www.facebook.com/groups/123456789",
        admins=[AdminProfile(name="Nguoi A"), AdminProfile(name="Nguoi A")],
    ))
    records = load_records_from_db(db)
    keys = [r.key for r in records]
    assert len(keys) == len(set(keys)), f"trùng khoá bản ghi: {keys}"


# ------------------------------------------------ factory log dùng chung theo tiến trình
def test_overlapping_sessions_do_not_uninstall_stamping_factory():
    """Phiên B đóng KHÔNG được gỡ RecordFactory khi phiên A còn chạy.

    Trước đây `_installed_factory` là cờ toàn cục: B (mở trước) đóng sẽ gỡ
    factory dù A đang quét -> log của A mất run_id và handler của A bỏ hết
    dòng log từ đó trở đi.
    """
    from scraper.log_capture import ScanSession

    log = logging.getLogger("osint")
    old_handlers, old_propagate = log.handlers, log.propagate
    log.handlers = [logging.NullHandler()]
    log.propagate = False

    a_entered = threading.Event()
    b_done = threading.Event()
    b_entered = threading.Event()

    def session_b():
        with ScanSession():
            b_entered.set()
            a_entered.wait(5)
        b_done.set()

    t = threading.Thread(target=session_b)
    t.start()
    try:
        assert b_entered.wait(5), "thread B không mở được phiên"
        with ScanSession() as a:
            a_entered.set()
            assert b_done.wait(5), "thread B không đóng được phiên"
            t.join(5)
            logging.getLogger("osint.pipeline").info("dong cua A sau khi B dong")
        assert "dong cua A sau khi B dong" in a.render(), a.render()
    finally:
        a_entered.set()
        t.join(5)
        log.handlers, log.propagate = old_handlers, old_propagate


# ------------------------------------------------ `help` không phải lỗi
def test_help_command_exits_zero(monkeypatch):
    import main as cli

    monkeypatch.setattr(sys, "argv", ["main.py", "help"])
    assert cli.main() == 0
