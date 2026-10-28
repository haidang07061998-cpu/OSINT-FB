"""Hồi quy cho các bug đã sửa.

Mỗi test ghi lại đúng lỗi gốc để chắc chắn nó không quay lại.

Lịch sử:
  1. `nghìn` có dấu không được nhân -> "5 nghìn thành viên" ra 5 thay vì 5000.
  2. `Kinh doanh` chứa chuỗi "kin" -> nhóm công khai bị gán nhầm là nhóm kín.
  3. `dedupe_people` xoá CẢ HAI bản ghi khi trùng tên -> mất dữ liệu.
  4. `parse_group` gọi `group(2)` trên regex chỉ có 1 nhóm bắt -> IndexError.
  5. `max_retries` trong config không được dùng (hardcode 3).
  6. Nội dung bài đăng ghi thô vào ô Excel -> formula injection.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest

from exporter import safe_cell
from models import AdminProfile, GroupPrivacy, MonitoredGroup, ScanProfile
from scraper.group_scraper import (
    _detect_privacy,
    _to_number,
    parse_group,
)
from scraper.manual_importer import dedupe_people
from scraper.utils import HttpClient, ScraperConfig


# ---------------------------------------------------------------- 1
@pytest.mark.parametrize("number,multiplier,expected", [
    ("5", "nghìn", 5_000),
    ("5", "nghin", 5_000),
    ("12,5", "nghìn", 12_500),
    ("3,4", "nghìn", 3_400),
    ("2", "triệu", 2_000_000),
    ("2", "trieu", 2_000_000),
    ("850", "", 850),
    ("850", "người", 850),
])
def test_multiplier_variants(number, multiplier, expected):
    assert _to_number(number, multiplier) == expected


# ---------------------------------------------------------------- 2
@pytest.mark.parametrize("text,expected", [
    ("Nhom Kinh doanh bat dong san", GroupPrivacy.UNKNOWN),
    ("Nhom kinh doanh", GroupPrivacy.UNKNOWN),
    ("Nhom Kinh tế", GroupPrivacy.UNKNOWN),
    ("Nhom cong khai", GroupPrivacy.PUBLIC),
    ("public group", GroupPrivacy.PUBLIC),
    ("Nhom rieng tu", GroupPrivacy.PRIVATE),
    ("private group", GroupPrivacy.PRIVATE),
    ("Nhom bi mat", GroupPrivacy.SECRET),
    ("secret group", GroupPrivacy.SECRET),
    ("Day la nhom kin", GroupPrivacy.CLOSED),
    ("Closed group", GroupPrivacy.CLOSED),
    ("Kín (Closed)", GroupPrivacy.CLOSED),
])
def test_detect_privacy_no_false_positive(text, expected):
    assert _detect_privacy(text) == expected


# ---------------------------------------------------------------- 3
def test_dedupe_people_keeps_one_record():
    """Trùng tên ở 2 bucket khác nhau: phải giữ lại ĐÚNG 1 bản ghi."""
    g = MonitoredGroup(group_id="1", url="u")
    g.admins = [AdminProfile(
        name="Nguyen Van A", profile_url="https://facebook.com/nguyen.a", role="admin",
    )]
    g.members = [AdminProfile(
        name="nguyen van a",
        profile_url="https://facebook.com/profile.php?id=100012345678",
        role="member",
    )]

    dedupe_people(g)

    survivors = list(g.admins) + list(g.moderators) + list(g.members)
    assert len(survivors) == 1, f"phải còn đúng 1 bản ghi, thực tế còn {len(survivors)}"
    # Bản được giữ phải là bản có UID số (định danh ổn định hơn)
    assert "id=100012345678" in (survivors[0].profile_url or "")


def test_dedupe_people_same_bucket():
    """Trùng tên trong cùng một bucket: cũng chỉ giữ 1."""
    g = MonitoredGroup(group_id="1", url="u")
    g.admins = [
        AdminProfile(name="Tran Thi B", profile_url="https://facebook.com/tranb"),
        AdminProfile(name="tran thi b", profile_url="https://facebook.com/profile.php?id=10009"),
    ]
    dedupe_people(g)
    assert len(g.admins) == 1, g.admins


def test_dedupe_people_no_dupe_keeps_all():
    """Không trùng tên thì phải giữ nguyên tất cả."""
    g = MonitoredGroup(group_id="1", url="u")
    g.admins = [
        AdminProfile(name="Nguyen A", profile_url="https://facebook.com/a"),
        AdminProfile(name="Tran B", profile_url="https://facebook.com/b"),
        AdminProfile(name="Le C", profile_url="https://facebook.com/c"),
    ]
    dedupe_people(g)
    assert len(g.admins) == 3


# ---------------------------------------------------------------- 4
def test_parse_group_member_count_from_json():
    """member_count chỉ nằm trong JSON -> trước đây IndexError."""
    html = (
        '<html><head><title>Nhom Test | Facebook</title></head><body>'
        '<div>Nhom cong khai</div>'
        '<script>{"subscriberCount": "12,500"}</script>'
        '</body></html>'
    )
    g = parse_group(html, "999", "https://facebook.com/groups/999")
    assert g is not None
    assert g.member_count == 12_500
    assert g.privacy == GroupPrivacy.PUBLIC


# ---------------------------------------------------------------- 5
def test_max_retries_is_used():
    """max_retries trong config phải thực sự điều khiển số lần thử."""
    c1 = HttpClient(ScraperConfig(max_retries=1))
    c2 = HttpClient(ScraperConfig(max_retries=7))
    # tenacity lưu stop strategy; so sánh số lần thử tối đa
    assert c1._retrying.stop.max_attempt_number == 1
    assert c2._retrying.stop.max_attempt_number == 7


def test_profile_rate_limit_wired():
    """build_client phải lấy rate_limit_seconds của profile."""
    from pipeline import build_client
    profile = ScanProfile(name="p", rate_limit_seconds=7.5)
    client = build_client({"scraper": {"request_delay": 3.0}}, profile)
    assert client.cfg.request_delay == 7.5


# ---------------------------------------------------------------- 6
@pytest.mark.parametrize("payload", [
    '=HYPERLINK("http://evil","x")',
    "=cmd|'/c calc'!A0",
    "+1+1",
    "-2+3",
    "@SUM(A1:A9)",
])
def test_formula_injection_neutralised(payload):
    assert safe_cell(payload).startswith("'")


@pytest.mark.parametrize("benign", [
    "Nhom bình thường",
    "0987654321",
    "https://facebook.com/abc",
    "Kín",
    "bình thường = đẹp",
])
def test_formula_injection_leaves_benign(benign):
    assert safe_cell(benign) == benign


def test_workbook_post_content_is_text():
    """Nội dung bài đăng trong file .xlsx phải là TEXT, không phải công thức."""
    import openpyxl

    from exporter import export_workbook

    g = MonitoredGroup(
        group_id="1", name="=HYPERLINK(\"http://evil\",\"G\")", url="u",
        privacy=GroupPrivacy.PUBLIC,
    )
    from models import GroupPost
    g.recent_posts = [GroupPost(
        post_id="p", author_name="A", content="=cmd|'/c calc'!A0",
    )]

    tmp = Path(__file__).parent / "_regression_out.xlsx"
    try:
        export_workbook([g], tmp)
        wb = openpyxl.load_workbook(tmp)
        assert str(wb["Tong quan nhom"]["B2"].value).startswith("'")
        assert str(wb["Bai dang public"]["E2"].value).startswith("'")
    finally:
        if tmp.exists():
            tmp.unlink()


# ---------------------------------------------------------------- 7
def test_max_accounts_mirrors_alias():
    """max_accounts và max_admins_per_group phải luôn khớp."""
    from models import ScanLimits
    lim = ScanLimits(max_accounts=3)
    assert lim.max_admins_per_group == 3
    # config cũ chỉ đặt max_admins_per_group -> nâng lên max_accounts
    old = ScanLimits(max_admins_per_group=25)
    assert old.max_accounts == 25


def test_utcnow_is_naive():
    """utcnow phải là naive để so sánh với datetime bóc từ FB không lỗi."""
    from models import utcnow
    assert utcnow().tzinfo is None


# ---------------------------------------------------------------- 10
def _rec(key, phone=None, email=None, gid="111", url=None):
    from models import IdentityRecord
    return IdentityRecord(
        key=key, name=f"n{key}", profile_url=url or f"facebook.com/{key}",
        phones=[phone] if phone else [], emails=[email] if email else [],
        group_ids=[gid],
    )


def test_cluster_id_is_stable_across_input_order():
    """Thứ tự bản ghi đổi thì mã cụm KHÔNG được đổi.

    Trước đây mã là E001/E002 theo vị trí: đảo thứ tự là "E002" của tháng trước
    biến thành cụm người khác mà báo cáo không hề báo lỗi.

    Cần HAI cụm và đảo thứ tự để thứ tự khám phá thực sự đảo — với một cụm
    duy nhất thì idx luôn bằng 1 nên test sẽ pass dù code còn lỗi.
    """
    from scraper.entity_linker import link_identities

    # Cụm 1: a+b (SĐT 111), Cụm 2: x+y (SĐT 777)
    base = [_rec("a", phone="+849111"), _rec("b", phone="+849111"),
            _rec("x", phone="+849777"), _rec("y", phone="+849777")]

    forward = link_identities(list(base))
    # Đảo thứ tự: cụm SĐT 777 được khám phá trước -> idx của nó phải là 1.
    backward = link_identities(list(reversed(base)))

    assert len(forward) == 2 and len(backward) == 2
    ids = {c.cluster_id for c in forward}
    assert ids == {c.cluster_id for c in backward}, \
        "mã cụm đổi theo thứ tự bản ghi"


def test_cluster_id_tracks_which_people():
    """Mã cụm phải bám vào NỘI DUNG: cùng vị trí nhưng khác người -> khác mã."""
    from scraper.entity_linker import link_identities

    first = link_identities([_rec("a", phone="+849111"), _rec("b", phone="+849111")])
    second = link_identities([_rec("z", phone="+849999"), _rec("w", phone="+849999")])
    assert first[0].cluster_id != second[0].cluster_id


def test_cluster_id_ignores_unrelated_new_clusters():
    """Thêm một cụm mới không được làm đổi mã của cụm cũ."""
    from scraper.entity_linker import link_identities

    before = link_identities([_rec("a", phone="+849111"), _rec("b", phone="+849111")])
    after = link_identities([
        _rec("a", phone="+849111"), _rec("b", phone="+849111"),
        _rec("x", phone="+847777"), _rec("y", phone="+847777"),
    ])
    assert len(after) == 2
    old = {c.cluster_id for c in before}
    assert old <= {c.cluster_id for c in after}, f"mã cũ biến mất: {old}"


def test_cluster_ids_unique():
    from scraper.entity_linker import link_identities

    recs = []
    for i in range(6):
        recs += [_rec(f"p{i}a", phone=f"+84900{i}"), _rec(f"p{i}b", phone=f"+84900{i}")]
    clusters = link_identities(recs)
    ids = [c.cluster_id for c in clusters]
    assert len(ids) == len(set(ids)), ids


def test_save_clusters_replaces_stale_rows(tmp_path):
    """Cụm đã tan ở lần trước không được còn nằm trong DB."""
    from models import EntityCluster, IdentityRecord
    from scraper.entity_linker import save_clusters

    db = tmp_path / "m.db"
    m1 = IdentityRecord(key="a", name="A", profile_url="facebook.com/a")
    m2 = IdentityRecord(key="b", name="B", profile_url="facebook.com/b")
    save_clusters(db, [EntityCluster(cluster_id="C-OLD", title="cũ",
                                     confidence="certain", members=[m1, m2])])
    save_clusters(db, [EntityCluster(cluster_id="C-NEW", title="mới",
                                     confidence="high", members=[m1, m2])])

    import sqlite3
    conn = sqlite3.connect(str(db))
    ids = [r[0] for r in conn.execute("SELECT cluster_id FROM entity_clusters")]
    conn.close()
    assert ids == ["C-NEW"], f"còn sót cụm cũ: {ids}"


# ---------------------------------------------------------------- 8
def test_person_uid_prefers_page_url_over_page_body():
    """Trang hồ sơ lưu bằng Ctrl+S chứa ID bạn bè -> không được gán nhầm.

    Trang thật của Facebook nhét JSON vào <head> TRƯỚC og:url, nên quét toàn
    văn bản HTML sẽ ra ID của người khác (bạn bè/gợi ý) chứ không phải chủ
    trang. Phải lấy UID từ og:url/canonical của chính trang.
    """
    from scraper.person_scraper import parse_person_from_html

    html = """<html><head>
<script>{"userID":"123456789012","ft_ent_identifier":"123456789012"}</script>
<meta property="og:url" content="https://www.facebook.com/profile.php?id=999888777666">
<meta property="og:title" content="Nguyen Van A">
<title>Nguyen Van A | Facebook</title></head><body>
<a href="https://www.facebook.com/111222333444">mot nguoi khac</a>
</body></html>"""
    p = parse_person_from_html(html, "profile_999888777666.html")
    assert p.fb_user_id == "999888777666", p.fb_user_id


# ---------------------------------------------------------------- 9
def test_entities_respects_report_dir(tmp_path, monkeypatch):
    """run_entities phải ghi vào report_dir trong config, không hardcode."""
    from models import PersonProfile, PhoneCandidate
    from scraper.entity_linker import run_entities
    from storage import save_persons

    monkeypatch.chdir(tmp_path)

    db = tmp_path / "monitor.db"
    phone = PhoneCandidate(raw="0987654321", normalized="+84987654321")
    # Hai tài khoản khác profile_url nhưng chung SĐT -> phải gộp được 1 cụm.
    save_persons(db, [
        PersonProfile(name="Nguyen A", profile_url="facebook.com/a", phones=[phone]),
        PersonProfile(name="Nguyen A", profile_url="facebook.com/b", phones=[phone]),
    ])

    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "scraper:\n  data_dir: .\n  report_dir: bao_cao_rieng\n", encoding="utf-8"
    )
    run_entities(str(cfg))

    assert (tmp_path / "bao_cao_rieng" / "cum_danh_tinh.csv").exists(), \
        f"chưa ghi đúng report_dir; thấy: {sorted(p.name for p in tmp_path.iterdir())}"
    assert not (tmp_path / "reports").exists(), "không được tạo thư mục 'reports' mặc định"
