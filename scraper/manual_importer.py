"""Import dữ liệu từ HTML người dùng TỰ LƯU (Ctrl+S).

Quy trình an toàn cho NHÓM RIÊNG TƯ:
    1. Bạn tự đăng nhập account dự phòng, mở trang nhóm / profile admin
    2. Ctrl+S lưu trang vào thư mục `html_dump/` (đặt tên theo quy ước)
    3. Chạy: python main.py import html_dump
       -> tool tự parse + bốc tách SĐT/social links + xuất Excel/CSV

Tool KHÔNG đăng nhập, KHÔNG tự động click/scroll -> không tạo tín hiệu
automation. Tái sử dụng toàn bộ parser của pipeline public.

Quy ước đặt tên file:
    group_<id>.html            Trang nhóm (tên, thành viên, privacy...)
    group_<id>_posts.html      Tab "Bài viết" của nhóm (tuỳ chọn)
    profile_<id>_<tên>.html    Trang cá nhân admin/mod (bắt buộc tiền tố profile_)
Vai trò được suy ra từ tên file: admin_* -> admin, mod_* -> moderator.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from bs4 import BeautifulSoup

from models import (
    AdminProfile,
    GroupPrivacy,
    GroupPost,
    MonitoredGroup,
    PersonProfile,
)
from .fb_time import parse_fb_time
from .group_scraper import (
    _ADMINS_HEAD_RE,
    _MEMBER_ZONE_RE,
    parse_group,
)
from .person_scraper import parse_person_from_html
from .post_scraper import compute_activity, parse_posts
from .profile_scraper import parse_admin_from_html
from .utils import clean_fb_url, clean_text, get_logger

logger = get_logger("manual")

_FILENAME_RE = re.compile(
    r"^(?P<kind>group|profile|post|admin|mod|person|canhan|user)"
    r"(?:[_-](?P<rest>.+))?$",
    re.I,
)


def _load_json_export(path: Path) -> Optional[MonitoredGroup]:
    """Đọc file .json do browser/export_group.js tạo ra."""
    try:
        # utf-8-sig: chịu được file lưu bằng Notepad/PowerShell có BOM
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        logger.error("Không đọc được JSON %s: %s", path, exc)
        return None

    g = data.get("group") or {}
    gid = str(g.get("id") or "")
    if not gid:
        return None

    try:
        privacy = GroupPrivacy(g.get("privacy") or "unknown")
    except ValueError:
        privacy = GroupPrivacy.UNKNOWN

    group = MonitoredGroup(
        group_id=gid,
        name=g.get("name") or f"Group {gid}",
        url=data.get("source_url") or f"https://www.facebook.com/groups/{gid}",
        privacy=privacy,
        member_count=g.get("member_count"),
    )

    def _first_line(value: Optional[str]) -> Optional[str]:
        """'Mekong News\\nQuản trị viên' -> 'Mekong News'"""
        if not value:
            return None
        return clean_text(value.split("\n")[0]) or None

    for bucket, role in ((g.get("admins") or [], "admin"),
                         (g.get("moderators") or [], "moderator")):
        for p in bucket:
            url = p.get("url") or ""
            label = _first_line(p.get("label"))
            prof = AdminProfile(
                name=label,
                profile_url=url,
                role=role,
                source=g.get("name"),
            )
            prof.social_links, prof.phones = _contacts_from_bio(
                p.get("label") or ""
            )
            if role == "moderator":
                group.moderators.append(prof)
            else:
                group.admins.append(prof)

    members_seen = set()
    for p in (g.get("members") or []):
        url = p.get("url") or ""
        if not url or url in members_seen:
            continue
        members_seen.add(url)
        group.admins.append(AdminProfile(
            name=_first_line(p.get("label")), profile_url=url,
            role="member", source="danh sách thành viên",
        ))

    posts = g.get("posts") or []
    if posts:
        group.recent_posts = [
            GroupPost(
                content=(p.get("text") or "")[:500],
                time_raw=p.get("time") or None,
                created_at=parse_fb_time(p.get("time") or "") or None,
            )
            for p in posts
        ]
        group.activity = compute_activity(group.recent_posts)

    logger.info(
        "Import từ JSON: %s | %s | %d thành viên | %d admin/mod | %d bài",
        gid, group.name, group.member_count or 0,
        len(group.admins) + len(group.moderators), len(group.recent_posts),
    )
    return group


def _contacts_from_bio(text: str):
    from .link_extractor import extract_contacts

    try:
        phones, links = extract_contacts([text])
    except Exception:  # noqa: BLE001
        return [], []
    return links, phones


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        logger.error("Không đọc được %s: %s", path, exc)
        return None


def _classify(path: Path) -> Optional[Tuple[str, str, str]]:
    """Trả về (kind, group_id, filename_hint) hoặc None nếu bỏ qua."""
    if path.suffix.lower() not in {".html", ".htm"}:
        return None
    stem = path.stem
    m = _FILENAME_RE.match(stem)
    if not m:
        return None
    kind = m.group("kind").lower()
    rest = (m.group("rest") or "").strip()

    # group_123 | group_123_posts | 123_group
    gid = ""
    if kind == "group":
        nums = re.findall(r"\d{5,}", rest)
        gid = nums[0] if nums else (rest or stem)
    return kind, gid, path.name


def _group_url_from_html(html: str) -> str:
    m = re.search(r'https?://(?:www\.|m\.|web\.)?facebook\.com/groups/(\d+)', html)
    return m.group(0) if m else ""


def _profile_url_from_html(html: str) -> str:
    """Lấy canonical URL của profile đang được lưu.

    Chỉ bỏ tham số theo dõi của Facebook (`?__cft__[0]=...`), GIỮ `?id=`
    vì đó là định danh của tài khoản.
    """
    m = re.search(r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"', html)
    if m and "facebook.com" in m.group(1):
        return _clean_fb_url(m.group(1))
    m = re.search(r'"userID"\s*:\s*"(\d+)"', html)
    if m:
        return f"https://www.facebook.com/profile.php?id={m.group(1)}"
    m = re.search(r'<meta property="og:url" content="([^"]+)"', html)
    if m and ("facebook.com/" in m.group(1)):
        return _clean_fb_url(m.group(1))
    return ""


def _clean_fb_url(url: str) -> str:
    """Bỏ tham số theo dõi, giữ `?id=` (dùng chung với person_scraper)."""
    return clean_fb_url(url) or ""


def _uid_of(url: str) -> str:
    """Lấy UserID số từ URL profile (dạng ổn định, không đổi theo username)."""
    m = re.search(r"profile\.php\?id=(\d+)", url or "")
    if m:
        return m.group(1)
    m = re.search(r"/groups/\d+/user/(\d+)", url or "")
    return m.group(1) if m else ""


def _name_key(name: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def dedupe_people(group: MonitoredGroup) -> int:
    """Gộp các bản ghi cùng một người (Facebook dùng cả ID số lẫn username).

    Trả về số bản ghi đã bị loại bởi trùng tên.
    """
    all_buckets = (group.admins, group.moderators, group.members)
    seen: Dict[str, AdminProfile] = {}
    # Bản ghi bị loại, ghi nhận theo id() vì AdminProfile không hash được.
    # KHÔNG xoá trực tiếp trong vòng lặp: xoá giữa lúc duyệt làm lệch chỉ số,
    # bản ghi bị xoá lại quay về khi `bucket[:] = keep` chạy sau đó.
    drop: Set[int] = set()

    for bucket in all_buckets:
        keep: List[AdminProfile] = []
        for p in bucket:
            key = _name_key(p.name)
            # chỉ khử trùng khi có tên đủ dài và khác tên nhóm
            if len(key) < 3 or key == _name_key(group.name):
                keep.append(p)
                continue
            prev = seen.get(key)
            if prev is None:
                seen[key] = p
                keep.append(p)
                continue
            # cùng tên: giữ bản có ID số (ổn định hơn) và bù thông tin thiếu
            prefer, other = (p, prev) if _uid_of(p.profile_url or "") else (prev, p)
            if not prefer.name and other.name:
                prefer.name = other.name
            if other.phones and not prefer.phones:
                prefer.phones = other.phones
            if other.social_links and not prefer.social_links:
                prefer.social_links = other.social_links
            if prefer.role == "unknown" and other.role != "unknown":
                prefer.role = other.role
            if not prefer.source and other.source:
                prefer.source = other.source
            if not prefer.fb_user_id and other.fb_user_id:
                prefer.fb_user_id = other.fb_user_id
            # Bản ưu tiên luôn phải có mặt; bản kia bị loại ở vòng dọn sau.
            # Trước đây khi `prefer is prev` (bản mới không có UID) thì nhánh
            # xoá bị bỏ qua và bản cũ vẫn sót lại -> khử trùng trong cùng
            # bucket không bao giờ có tác dụng.
            drop.add(id(other))
            seen[key] = prefer
            if not any(x is prefer for x in keep):
                keep.append(prefer)
        bucket[:] = keep

    if drop:
        for b in all_buckets:
            b[:] = [x for x in b if id(x) not in drop]
    return len(drop)


def _merge_group(dst: MonitoredGroup, src: MonitoredGroup) -> None:
    """Gộp kết quả từ nhiều file HTML của cùng 1 nhóm (lưu nhiều lần)."""
    # giữ giá trị "đầy đủ hơn"
    if not dst.name or dst.name.startswith("Group "):
        dst.name = src.name
    if not dst.description:
        dst.description = src.description
    if not dst.member_count and src.member_count:
        dst.member_count = src.member_count
    if dst.privacy == GroupPrivacy.UNKNOWN and src.privacy != GroupPrivacy.UNKNOWN:
        dst.privacy = src.privacy

    role_rank = {"member": 0, "unknown": 0, "moderator": 1, "admin": 2}

    def _ev_rank(profile) -> int:
        """Mức độ tin cậy của vai trò: badge > suy theo vị trí > mặc định."""
        src = (profile.source or "").strip().lower()
        if src.startswith("badge"):
            return 2
        if src.startswith("pos"):
            return 1
        return 0

    def _absorb(a, b) -> None:
        """Bù thông tin từ b cho a; nâng vai trò nếu bằng chứng mới mạnh hơn.

        Must stay in sync with _promote: badge > vị trí, admin > kd > thành viên.
        """
        if not a.name and b.name:
            a.name = b.name
        if b.phones and not a.phones:
            a.phones = b.phones
        if b.social_links and not a.social_links:
            a.social_links = b.social_links
        if b.profile_url and not a.profile_url:
            a.profile_url = b.profile_url
        new_rank = _ev_rank(b) * 10 + role_rank.get(b.role, 0)
        old_rank = _ev_rank(a) * 10 + role_rank.get(a.role, 0)
        if new_rank > old_rank:
            a.role = b.role
            a.source = b.source

    # Gộp mọi người THEO PROFILE_URL, rồi phân lại đúng bucket.
    # Cách cũ chỉ "nâng vai trò" trong bucket sẵn có -> người vẫn nằm lại
    # bucket cũ (admin bị rơi thành thành viên).
    pool: "Dict[str, AdminProfile]" = {}
    order: List[str] = []

    def _put(profile) -> None:
        # không có URL thì dùng tên làm khoá (Facebook đôi khi chỉ ghi tên)
        key = (profile.profile_url or "").strip() or "n:" + _name_key(profile.name)
        if key == "n:":
            return
        if key in pool:
            # ưu tiên bản có ID số proxy: id cố định
            have = pool[key]
            if not have.name and profile.name:
                have.name = profile.name
            _absorb(have, profile)
            return
        copy = profile.model_copy(deep=True)
        pool[key] = copy
        order.append(key)

    for profile in list(dst.admins) + list(dst.moderators) + list(dst.members):
        _put(profile)
    for profile in list(src.admins) + list(src.moderators) + list(src.members):
        _put(profile)

    dst.admins = [pool[k] for k in order if pool[k].role == "admin"]
    dst.moderators = [pool[k] for k in order if pool[k].role == "moderator"]
    dst.members = [pool[k] for k in order if pool[k].role not in ("admin", "moderator")]

    if src.recent_posts:
        dst.recent_posts = src.recent_posts
        dst.activity = src.activity

    n = dedupe_people(dst)
    if n:
        logger.info("Bỏ %d bản ghi trùng người (cùng tên, khác URL)", n)


def _diag_post_markers(html: str) -> dict:
    """Đếm các dấu hiệu cấu trúc để chẩn đoán vì sao không bóc được bài đăng."""
    from bs4 import BeautifulSoup as _BS

    out = {}
    try:
        soup = _BS(html, "lxml")
        text = soup.get_text(" ", strip=True)
        out["do_dai_text"] = len(text)
        out["so_o_binh_luan"] = len(re.findall(r"Bình luận dưới tên", text))
        out["so_bai_viet_moi"] = len(re.findall(r"Bài viết mới", text))
        out["so_role_article"] = len(soup.find_all(attrs={"role": "article"}))
        out["so_link_posts"] = len(re.findall(r"/posts/\d+|/permalink/", html))
        out["so_thoi_gian_ngan"] = len(
            re.findall(r"\d+\s*(?:phút|giờ|ngày|tuần|tháng)(?:\s*trước)?", text)
        )
        out["so_anh"] = len(soup.find_all("img"))
        # trích đoạn quanh từ khóa để nhìn cấu trúc
        for kw in ("Bài viết mới", "Bình luận dưới tên", "Xem thêm"):
            i = text.find(kw)
            if i >= 0:
                out["ngoi_quanh_" + kw.replace(" ", "_")] = text[max(0, i - 120): i + 160]
    except Exception as exc:  # noqa: BLE001
        out["loi"] = str(exc)[:120]
    return out


def _describe_file(html: str) -> str:
    """Đoán loại trang để giúp người dùng hiểu file đó đọc được gì.

    Không dựa vào nhãn menu (mọi trang nhóm đều có "Giới thiệu"/"Thảo luận"),
    chỉ dựa vào dấu hiệu nội dung thật sự xuất hiện.
    """
    from bs4 import BeautifulSoup as _BS

    try:
        text = _BS(html, "lxml").get_text(" ", strip=True)
    except Exception:  # noqa: BLE001
        text = html[:200000]

    hints = []
    # trang Thảo luận: có tiêu đề "Bài viết mới"
    if re.search(r"Bài viết mới", text):
        hints.append("TRANG THẢO LUẬN (có bài viết)")
    if re.search(r"/posts/\d+|/permalink/", html):
        hints.append("có link bài đăng")
    if _ADMINS_HEAD_RE.search(text):
        hints.append("có khu Quản trị/Kiểm duyệt")
    if _MEMBER_ZONE_RE.search(text):
        hints.append("có khu Thành viên")
    # chỉ coi là tab Giới thiệu khi URL thật sự là /about
    if re.search(r"facebook\.com/groups/\d+/about", html, re.I) or \
            re.search(r"facebook\.com/groups/\d+\?(?:sk=about|section=about)", html, re.I):
        hints.append("tab Giới thiệu")
    if re.search(r"Sự kiện|events", text):
        hints.append("mục Sự kiện")
    if re.search(r"Ảnh và video|photos?\/album", text, re.I):
        hints.append("mục Ảnh")

    if not hints:
        return "không nhận diện được nội dung"
    return ", ".join(hints)


def _fold_vi(text: Optional[str]) -> str:
    """Bỏ dấu tiếng Việt + hạ chữ thường để khớp nhãn.

    Trang Ctrl+S đôi khi lỗi font/encoding ("Song tai" thay vì
    "Sống tại") -> nếu khớp cả dấu sẽ không nhận ra trang.
    """
    if not text:
        return ""
    # NFD rồi bỏ dấu kết hợp: "Sống" -> "Song"
    no_marks = "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if not unicodedata.combining(ch)
    )
    # chữ đ/Đ không có dấu kết hợp -> xử lý riêng
    return no_marks.replace("đ", "d").replace("Đ", "d").lower()


def _extract_group_id(html: str) -> str:
    """Rút ID nhóm từ mọi nguồn trong HTML (canonical, JSON, link tương đối)."""
    for pat in (
        r"facebook\.com/groups/(\d{6,})",
        r'"groupID"\s*:\s*"(\d{6,})"',
        r'"group_id"\s*:\s*"?(\d{6,})"?',
        r"/groups/(\d{6,})",
    ):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return ""


def _detect_group_id(html: str, hint: str = "") -> Optional[str]:
    """Tự nhận diện ID nhóm từ nội dung HTML, không phụ thuộc tên file."""
    # canonical / og:url
    for pat in (
        r'<link[^>]+rel="canonical"[^>]+href="[^"]*facebook\.com/groups/(\d{6,})',
        r'<meta[^>]+property="og:url"[^>]+content="[^"]*facebook\.com/groups/(\d{6,})',
    ):
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1)
    # bất kỳ link /groups/<id> nào trong trang
    m = re.search(r"facebook\.com/groups/(\d{6,})", html, re.I)
    if m:
        return m.group(1)
    m = re.search(r'"groupID"\s*:\s*"(\d{6,})"', html)
    if m:
        return m.group(1)
    # cuối cùng: tên file
    m = re.search(r"(\d{9,})", hint or "")
    return m.group(1) if m else None


def _detect_profile_url(html: str) -> Optional[str]:
    """Trang cá nhân: lấy URL chuẩn từ canonical/og:url/user id.

    Lưu ý: bắt buộc xác nhận đó là trang CÁ NHÂN (canonical trỏ tới
    `profile.php?id=` hoặc `facebook.com/<username>` KHÔNG nằm trong
    `/groups/`), nếu nuri trang nhóm có `/groups/<id>/user/<uid>` cũng sẽ
    bị hiểu nhầm là cá nhân.
    """
    for pat in (
        r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"',
        r'<meta[^>]+property="og:url"[^>]+content="([^"]+)"',
    ):
        m = re.search(pat, html, re.I)
        if not m:
            continue
        url = _clean_fb_url(m.group(1))
        if "/groups/" in url:
            return None
        if re.search(
            r"facebook\.com/(?:profile\.php\?id=\d+|[A-Za-z0-9.\-_]{2,})(?:\?|$|#)",
            url,
        ):
            return url
    # KHÔNG dùng "userID" từ JS: đó là ID người đang xem (viewer),
    # xuất hiện trên MỌI trang nhóm/bài viết -> sẽ nhận nhầm thành cá nhân.
    return None


def _page_kind(html: str) -> Tuple[str, str]:
    """Nhận loại trang bằng NỘI DUNG (không nhìn tên file).

    Trả về (kind, gid):
      "person"  : trang cá nhân  (canonical/og:url -> profile.php?id= | /<username>)
      "group"   : trang nhóm     (canonical/og:url -> /groups/<id>|...)
      "members" : trang thành viên nhóm
      "unknown" : không nhận ra

    Thứ tự kiểm tra đặt trước group vì trang nhóm đã lưu cũng chứa
    `"userID"` (của người đăng nhập) và link `/groups/<id>/user/<uid>`.
    """
    try:
        soup = BeautifulSoup(html[:800000], "lxml")
    except Exception:  # noqa: BLE001
        soup = None

    canonical = og_url = og_type = ""
    if soup is not None:
        link = soup.find("link", rel="canonical")
        if link and link.get("href"):
            canonical = link["href"]
        for meta in soup.find_all("meta"):
            prop = (meta.get("property") or meta.get("name") or "").lower()
            content = meta.get("content") or ""
            if prop == "og:url" and content:
                og_url = content
            elif prop == "og:type" and content:
                og_type = content
    base = _clean_fb_url(canonical or og_url)

    # ---- 1. Trang nhóm: URL chuẩn trỏ vào /groups/ ----
    m = re.search(r"facebook\.com/groups/(\d{6,})", base, re.I)
    if m:
        gid = m.group(1)
        if re.search(r"/groups/\d+/(?:members|people)", base, re.I):
            return "members", gid
        return "group", gid

    # ---- 2. Trang cá nhân: URL chuẩn là profile.php?id= hoặc /<username> ----
    if _detect_profile_url(html):
        return "person", ""

    # ---- 3. Dấu hiệu nhóm trong cấu trúc trang ----
    for pat in (
        r'"groupID"\s*:\s*"(\d{6,})"',
        r"facebook\.com/groups/(\d{6,})",
    ):
        m = re.search(pat, html)
        if m:
            kind = "members" if re.search(
                r"/groups/\d{6,}/(?:members|people)", html, re.I
            ) else "group"
            return kind, m.group(1)
    if re.search(r"/groups/\d{6,}/(?:members|people|posts|permalink)", html):
        m = re.search(r"/groups/(\d{6,})/(?:members|people|posts|permalink)", html)
        if m:
            return "group", m.group(1)

    # ---- 4. Dấu hiệu văn bản (bỏ dấu tiếng Việt cho chắc) ----
    text_all = soup.get_text(" ", strip=True) if soup is not None else html[:200000]
    folded = _fold_vi(text_all)
    # Dấu hiệu ĐẶC TRƯNG trang nhóm: ổ viết bình luận / điều hướng nhóm
    has_group_feed = bool(re.search(
        r"(bai viet moi|tham gia nhom|binh luan duoi ten|so thanh vien"
        r"|nhom cong khai|nhom kin)", folded
    ))
    # Dấu hiệu ĐẶC TRƯNG trang cá nhân: các mục trong Giới thiệu
    has_person_about = bool(re.search(
        r"(song tai|den tu|lam viec tai|hoc tai|them cho o|nghe nghiep|"
        r"them vao muc chinh|chu so huu cua trang)", folded
    ))

    if has_group_feed:
        # ưu tiên ID nhóm nếu có, không cũng vẫn là trang nhóm
        return "group", _extract_group_id(html)
    if has_person_about or og_type == "profile":
        return "person", ""
    if _extract_group_id(html):
        return "group", _extract_group_id(html)

    return "unknown", ""


def import_from_folder(folder: str | Path) -> List[MonitoredGroup]:
    """Parse mọi file trong một thư mục."""
    folder = Path(folder)
    if not folder.is_dir():
        raise NotADirectoryError(f"Không thấy thư mục: {folder}")
    return import_files(sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in (".html", ".htm", ".json")
    ))


def import_files(paths) -> List[MonitoredGroup]:
    """Parse danh sách file bất kỳ (tên tùy ý), tự nhận diện & gộp dồn.

    Nhận diện nhóm dựa trên NỘI DUNG trang (canonical / og:url / /groups/<id>),
    không phụ thuộc tên file.
    """
    return _import_path_list([Path(p) for p in paths])


def import_uploads(uploaded) -> List[MonitoredGroup]:
    """Parse các file người dùng chọn từ giao diện web (đối tượng có .name/.read()).

    Dùng thư mục tạm tự xoá sau khi xử lý — không giữ lại file trên đĩa.
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="osint_up_") as tmp:
        paths: List[Path] = []
        for f in uploaded:
            p = Path(tmp) / Path(f.name).name
            p.write_bytes(f.read())
            paths.append(p)
        return _import_path_list(paths)


def _import_path_list(paths: List[Path]) -> List[MonitoredGroup]:
    groups: Dict[str, MonitoredGroup] = {}
    standalone: Dict[str, MonitoredGroup] = {}

    files: List[Tuple[str, str, str, Path]] = []  # (kind, gid, hint, path)
    all_html: List[Path] = []
    file_reports: List[dict] = []
    for path in paths:
        path = Path(path)
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix == ".json":
            # File do browser/export_group.js tạo -> parse riêng
            grp = _load_json_export(path)
            if grp:
                if grp.group_id in groups:
                    _merge_group(groups[grp.group_id], grp)
                else:
                    groups[grp.group_id] = grp
            continue
        if suffix in (".html", ".htm"):
            all_html.append(path)

    # ---- PASS 1: nhóm (nhận MỌI file html, tự nhận diện ID từ nội dung) ----
    skipped: List[str] = []
    consumed: set = set()

    for path in all_html:
        html = _read(path)
        if not html:
            continue

        # PASS 1: nhận loại trang BẰNG NỘI DUNG (không theo tên file)
        kind, gid = _page_kind(html)
        if kind == "person":
            files.append(("person", "", path.name, path))
            consumed.add(path)
            continue
        if not gid:
            # không có ID nhóm: có thể là trang cá nhân lạ
            if _detect_profile_url(html):
                files.append(("person", "", path.name, path))
                consumed.add(path)
                continue
            # hoặc là trang nhóm nhưng FB không ghi ID (vd tab Giới thiệu)
            # -> gắn vào nhóm duy nhất đang có, đừng bỏ mất dữ liệu
            if len(groups) == 1:
                gid = next(iter(groups))
                logger.info(
                    "Trang '%s' không ghi ID nhom -> gắn vào nhóm %s",
                    path.name, gid,
                )
            else:
                skipped.append(path.name)
                continue

        url = (
            f"https://www.facebook.com/groups/{gid}"
            + ("/members" if re.search(r"/members|/people", html[:20000], re.I) else "")
        )
        group = parse_group(html, gid, url)
        if not group:
            skipped.append(path.name)
            continue
        consumed.add(path)
        stats = {
            "file": path.name,
            "size_kb": round(path.stat().st_size / 1024, 1),
            "kind": _describe_file(html),
            "admins": len(group.admins),
            "moderators": len(group.moderators),
            "members": len(group.members),
            "posts": len(group.recent_posts),
            # Mẫu văn bản rút gọn: giúp xác định cấu trúc trang khi không có file gốc
            "text_sample": BeautifulSoup(html, "lxml")
            .get_text(" ", strip=True)[:1500],
            "post_links": len(re.findall(r"/groups/\d+/(?:posts|permalink)/", html)),
            "user_links": len(re.findall(r"/groups/\d+/user/", html)),
            "role_mentions": {
                "Quản trị viên": len(re.findall(r"Quản trị viên", html)),
                "Kiểm duyệt viên": len(re.findall(r"Kiểm duyệt viên", html)),
            },
            "diag": _diag_post_markers(html),
        }
        if gid in groups:
            before = len(groups[gid].admins) + len(groups[gid].moderators) \
                + len(groups[gid].members) + len(groups[gid].recent_posts)
            _merge_group(groups[gid], group)
            after = len(groups[gid].admins) + len(groups[gid].moderators) \
                + len(groups[gid].members) + len(groups[gid].recent_posts)
            stats["new_items"] = max(0, after - before)
            stats["merge"] = True
            logger.info(
                "Gop them vao nhom %s (tu %s): +%d muc moi",
                gid, path.name, stats["new_items"],
            )
        else:
            groups[gid] = group
            stats["new_items"] = stats["admins"] + stats["moderators"] \
                + stats["members"] + stats["posts"]
            stats["merge"] = False
            logger.info("Da import nhom %s (%s) tu %s", gid, group.name, path.name)
        file_reports.append(stats)

    if skipped:
        for name in skipped:
            file_reports.append({
                "file": name, "size_kb": 0, "kind": "BỎ QUA - không đọc được",
                "admins": 0, "moderators": 0, "members": 0, "posts": 0,
                "new_items": 0, "merge": False,
            })
        logger.warning(
            "Bo qua %d file khong nhan dien duoc: %s",
            len(skipped), ", ".join(skipped[:5]),
        )

    # ---- PASS 2: profile (admin/mod) ----
    profiles: List[Tuple[AdminProfile, str, Path]] = []
    for kind, gid, hint, path in files:
        if kind not in ("profile", "admin", "mod"):
            continue
        html = _read(path)
        if not html:
            continue
        role = "admin" if kind == "admin" else ("moderator" if kind == "mod" else None)
        # Nếu chỉ prefix "profile_", suy vai trò từ phần còn lại của tên file
        if role is None:
            low = path.stem.lower()
            if "admin" in low or "quan_tri" in low or "quantri" in low:
                role = "admin"
            elif "mod" in low or "kiem_duyet" in low or "kiemduyet" in low:
                role = "moderator"
        purl = _profile_url_from_html(html)
        profile = parse_admin_from_html(html, purl or f"file:{path.name}", role)
        if profile and profile.role == "unknown" and role:
            profile.role = role
        if profile:
            profiles.append((profile, gid, path))

    # Ghép profile vào nhóm. Ưu tiên: gid tường minh trong tên file
    # (profile_<groupid>_x.html). Nếu không có, rơi về nhóm duy nhất.
    for profile, gid, path in profiles:
        target_gid = gid if gid in groups else (next(iter(groups)) if len(groups) == 1 else "")
        if not target_gid:
            logger.info(
                "Profile doc lap: %s (%s) | phones=%d links=%d",
                profile.name, profile.role,
                len(profile.phones), len(profile.social_links),
            )
            standalone.setdefault("x", MonitoredGroup(
                group_id="standalone", name="Profiles doc lap", url="about:blank"
            )).admins.append(profile)
            continue
        target = groups[target_gid]
        bucket = target.moderators if profile.role == "moderator" else target.admins
        bucket.append(profile)
        logger.info(
            "Gan %s (%s) vao nhom %s | phones=%d links=%d",
            profile.name, profile.role, target_gid,
            len(profile.phones), len(profile.social_links),
        )

    # ---- PASS 3: bài đăng ----
    for kind, gid, hint, path in files:
        if kind != "post":
            continue
        html = _read(path)
        if not html:
            continue
        target_gid = gid if gid in groups else (next(iter(groups)) if len(groups) == 1 else "")
        if not target_gid:
            continue
        posts = parse_posts(html, limit=100)
        if posts:
            groups[target_gid].recent_posts = posts
            groups[target_gid].activity = compute_activity(posts)
            logger.info("Gan %d bai dang vao nhom %s", len(posts), target_gid)

    # ---- PASS 4: profile CÁ NHÂN (chế độ chuyên biệt) ----
    persons: List[PersonProfile] = []
    for kind, gid, hint, path in files:
        if kind not in ("person", "canhan", "user"):
            continue
        html = _read(path)
        if not html:
            continue
        person = parse_person_from_html(html, source_file=path.name)
        if person:
            persons.append(person)

    # Nếu có profile độc lập, gom thành nhóm tổng hợp để vẫn xuất báo cáo được
    for synth in standalone.values():
        groups[synth.group_id] = synth

    for g in groups.values():
        n = dedupe_people(g)
        if n:
            logger.info(
                "Nhóm %s: bỏ %d bản ghi trùng người (cùng tên, khác dạng link)",
                g.group_id, n,
            )
    importer_state["file_reports"] = file_reports
    # Lưu lại để xem lại được sau (phục vụ chẩn đoán)
    try:
        out = Path("reports") / "last_import.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(file_reports, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass
    logger.info(
        "=== IMPORT XONG: %d nhom, %d profile ca nhan ===",
        len(groups), len(persons),
    )
    results: List[MonitoredGroup] = list(groups.values())
    importer_state["persons"] = persons
    return results


# Lưu kết quả person để run_import xuất báo cáo (đơn giản, single-thread)
importer_state: Dict[str, list] = {}


def run_import(folder: str, cfg_path: str = "config.yaml") -> List[MonitoredGroup]:
    """CLI entry: import folder -> lưu DB + snapshot + xuất báo cáo."""
    from exporter import export_persons_csv
    from pipeline import finalize, load_config

    cfg = load_config(cfg_path)
    groups = import_from_folder(folder)
    persons = importer_state.get("persons", [])
    if not groups and not persons:
        logger.warning(
            "Không đọc được dữ liệu nào. Kiểm tra các file .html trong %s", folder
        )
        return []

    finalize(cfg, groups, persons)

    if persons:
        export_persons_csv(persons, Path("reports") / "ho_so_ca_nhan.csv")
    logger.info(
        "Xong | nhom=%d nguoi_ho_so=%d | bao cao: reports/bao_cao.xlsx",
        len(groups), len(persons),
    )
    return groups
