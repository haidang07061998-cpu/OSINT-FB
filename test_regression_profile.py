"""Test hồi quy: nhận diện trang hồ sơ/trang nhóm bằng NỘI DUNG + bóc SĐT/email."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest

from scraper.manual_importer import _detect_profile_url, _fold_vi, _page_kind
from scraper.person_scraper import parse_person_from_html

GROUP_HTML = """
<html><head><title>Mekong News | Facebook</title></head><body>
<div>Bai viet moi</div><div>Quan tri vien</div>
<a href="https://www.facebook.com/groups/798124254690405/user/1">u</a>
<a href="https://www.facebook.com/groups/798124254690405/members">m</a>
</body></html>"""

# Trang hồ sơ lưu bằng Ctrl+S: tên file tuỳ ý, không có tiền tố profile_
PROFILE_HTML = """<html><head>
<meta property="og:type" content="profile">
<meta property="og:url" content="https://www.facebook.com/profile.php?id=100012345678">
<link rel="canonical" href="https://www.facebook.com/profile.php?id=100012345678&__cft__[0]=AZA1-x">
<meta property="og:title" content="Tran Huu Le">
<title>Tran Huu Le | Facebook</title></head><body>
<div>Song tai Can Tho</div><div>Lam viec tai Cong ty TNHH ABC</div>
<div>Hoc tai Dai hoc Can Tho</div><div>0987654321</div><div>le@abc.com</div>
</body></html>"""

ABOUT_PAGE = """<html><head><title>Mekong News | Facebook</title></head><body>
<div>Mekong News</div><div>Nhom cong khai - 3,4K thanh vien</div>
<div>Tham gia nhom</div><div>Gioi thieu</div><div>Thao luan</div><div>Moi nguoi</div>
<div>Quan tri vien va nguoi kiem duyet - 10</div>
<div>Viet Tam</div><div>Quan tri vien</div><div>Nguoi co tieng noi</div>
<div>Hang Xao Lai Buon</div><div>Quan tri vien</div><div>Lam viec tai Lam tu do</div>
<div>Nam Long</div><div>Nguoi kiem duyet</div><div>Nha bao</div>
</body></html>"""

# Lỗi font/encoding ("Song tai" thay vì "Sống tại") phải vẫn nhận ra
PROFILE_NO_DIACRITIC = """<html><head><title>Le | Facebook</title></head><body>
<div>Song tai Can Tho</div><div>Den tu Hau Giang</div>
<div>Lam viec tai Cong ty ABC</div><div>0987654321</div></body></html>"""


def test_page_kind_group():
    kind, gid = _page_kind(GROUP_HTML)
    assert kind in ("group", "members") and gid == "798124254690405", (kind, gid)


def test_page_kind_person():
    kind, gid = _page_kind(PROFILE_HTML)
    assert kind == "person" and not gid, (kind, gid)


def test_page_kind_about():
    """Trang "Giới thiệu" phải nhận là nhóm, không phải hồ sơ."""
    assert _page_kind(ABOUT_PAGE)[0] == "group"


def test_page_kind_profile_no_diacritic():
    assert _page_kind(PROFILE_NO_DIACRITIC)[0] == "person"


def test_detect_profile_url_keeps_query():
    """URL phải giữ lại ?id= (trước đây bị cắt tại dấu "?")."""
    url = _detect_profile_url(PROFILE_HTML)
    assert url == "https://www.facebook.com/profile.php?id=100012345678", url


@pytest.fixture
def person():
    return parse_person_from_html(PROFILE_HTML, "Tran Huu Le Facebook.html")


def test_person_url(person):
    assert person.profile_url == "https://www.facebook.com/profile.php?id=100012345678"


def test_person_fields(person):
    assert person.location and "Can Tho" in person.location, person.location
    assert person.school and "Can Tho" in person.school, person.school
    assert person.job and "ABC" in person.job, person.job


def test_person_contacts(person):
    assert [x.normalized for x in person.phones] == ["+84987654321"]
    assert "le@abc.com" in person.emails


def test_person_keywords_exclude_labels(person):
    assert person.keywords, person.keywords
    assert not any(re.match(r"^(sdt|sđt|email)", k, re.I) for k in person.keywords), \
        person.keywords


def test_fold_vi():
    assert _fold_vi("Sống tại Cần Thơ") == "song tai can tho"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
