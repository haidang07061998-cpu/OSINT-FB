"""Thu thập metadata nhóm Facebook công khai (name, privacy, members).

Lưu ý: Facebook thay đổi HTML liên tục và nhiều endpoint yêu cầu
đăng nhập. Module này chỉ PARSE những phần public render sẵn; nếu
gặp login-wall sẽ bỏ qua group đó (không cố bypass).
"""
from __future__ import annotations

import re
from typing import List, Optional

from bs4 import BeautifulSoup, Tag

from models import AdminProfile, GroupPrivacy, MonitoredGroup
from .utils import HttpClient, clean_text, logger


_PRIVACY_KEYWORDS = {
    "công khai": GroupPrivacy.PUBLIC,
    "cong khai": GroupPrivacy.PUBLIC,
    "public": GroupPrivacy.PUBLIC,
    "riêng tư": GroupPrivacy.PRIVATE,
    "rieng tu": GroupPrivacy.PRIVATE,
    "private": GroupPrivacy.PRIVATE,
    # Nhóm "kín" — chỉ khớp cụm có nghĩa kín, KHÔNG dùng chuỗi "kin" trần
    # vì trùng với "Kinh doanh", "Kinh tế", "kinh nghiệm"...
    "kín": GroupPrivacy.CLOSED,
    "closed": GroupPrivacy.CLOSED,
    "bí mật": GroupPrivacy.SECRET,
    "bi mat": GroupPrivacy.SECRET,
    "secret": GroupPrivacy.SECRET,
}

# Các cụm mô tả nhóm kín, kiểm tra trước để không bị "kinh doanh" nuốt mất.
_CLOSED_PHRASES = (
    "nhóm kín", "nhom kin", "group kin", "kín lặng", "kin lang",
    "closed group", "nhom closed", "kín (closed",
)

# Nhận cả biến thể có dấu và không dấu do môi trường render khác nhau
_MEMBERS_RE = re.compile(
    r"([\d.,]+)\s*((?:nghìn|nghin|ngàn|ngan|triệu|trieu|tr|K|k|M|m)?)"
    r"\s*(?:thành viên|thanh vien|members)",
    re.I,
)
_NAME_HINTS_RE = re.compile(r'"groupName"\s*:\s*"([^"]+)"')
_DESC_RE = re.compile(r'"groupDescription"\s*:\s*"([^"]+)"')
# Số thành viên nằm trong JSON nội bộ (fb hiện đã bỏ khỏi HTML tĩnh)
_SUBSCRIBER_JSON_RE = re.compile(
    r'"(?:subscriberCount|memberCount|membersCount)"\s*:\s*"?(\d[\d.,]*)', re.I
)
_MEMBER_JSON_RE = re.compile(
    r'"(?:total_member_count|subscribers)"\s*:\s*"?(\d[\d.,]*)', re.I
)

# ---- Regex cho trang "Mọi người"/"Thành viên" đã lưu (Ctrl+S) ----
_MEMBERS_TAB_RE = re.compile(
    r"([\d.,]+)\s*(nghìn|nghin|ngàn|ngan|triệu|trieu|tr|K|k|M|m)?\s*(thành viên|Thành viên|members)",
)
_ADMINS_HEAD_RE = re.compile(
    r"(Quản trị viên và người kiểm duyệt|Quản trị viên và kiểm duyệt"
    r"|Moderators? (?:and|&) Admins?|Quản trị viên)\D{0,12}(\d{1,4})"
)
_MEMBER_ZONE_RE = re.compile(r"(Thành viên điểm chung|Thành viên \d|Common members|^\s*Members\b)")
_PROFILE_USER_RE = re.compile(
    r'href="(?:https?://[^"]*?)?(?:www\.|m\.|web\.)?facebook\.com/'
    r'(?:groups/\d+/(?:user|people)/([\w.\-]+)'      # /groups/<id>/user/<handle>
    r'|profile\.php\?id=(\d+)'                        # /profile.php?id=
    r'|(?!groups/|events/|marketplace/|pages/|pg/|photo|video|watch|help|'
    r'login|home|search|notifications|messages|settings|friends|privacy|'
    r'terms|policies|share|sharer|permalink|dialog)([\w.\-]{2,}))'
    r'(?:[/?#"][^"]*)?"'
)
USER_LINK_RE = re.compile(
    r"/groups/\d+/(?:user|people)/|profile\.php\?id=", re.I
)
_ROLE_BADGE_RE = re.compile(r"(Quản trị viên|Quan tri viên|Admin|Moderator|Kiểm duyệt viên)", re.I)
# Badge hiển thị trên thẻ người dùng -> vai trò (đáng tin hơn thứ tự vùng)
_BADGE_ADMIN_RE = re.compile(
    r"(Quản trị viên|Quan tri viên|Quan tri vien|Người quản lý"
    r"|Nguoi quan ly|Administrator|\bAdmin\b)", re.I
)
_BADGE_MOD_RE = re.compile(
    r"(Người kiểm duyệt|Nguoi duyet|Kiểm duyệt viên|Kiem duyet vien|Moderator)", re.I
)
# Các từ mô tả phụ sau tên -> cắt tên tại đây
_NAME_STOP_RE = re.compile(
    r"(Quản trị viên|Quan tri viên|Kiểm duyệt viên|Moderator|Administrator"
    r"|Người có tiếng nói|Người sáng tạo|Người nổi tiếng|Thành viên"
    r"|Trang web|trang cá nhân|người theo dõi|Theo dõi|Thích|Bạn chung"
    r"|tương tác|Đã thích|Xem thể loại|Đã tham gia|Quản lý|Điểm chung"
    r"|hoặc gần đây|Bạn chung với|Quan bộ|Tương tác chung)",
    re.I,
)
# Nhãn giao diện hay bị đọc nhầm làm tên nhóm (modal tìm kiếm, menu...)
_CHROME_NAME_RE = re.compile(
    r"^(Kết quả tìm kiếm|Search results|Trang chủ|Home|Tin nhắn|Messages"
    r"|Thông báo|Notifications|Bài viết mới|New posts|Sự kiện|Events"
    r"|Ảnh|Photos|Videos|Bạn bè|Friends|Cài đặt|Settings|Giới thiệu"
    r"|Giúp và phản hồi|Help và phản hồi|Trang bản tin|Feed)$",
    re.I,
)
# Từ dành của Facebook, không phải tên người dùng
_FB_RESERVED_WORDS = {
    "groups", "events", "marketplace", "pages", "pg", "profile", "stories",
    "story", "watch", "photo", "videos", "help", "login", "home", "search",
    "notifications", "messages", "settings", "friends", "about", "privacy",
    "terms", "policies", "share", "join", "see", "more", "people", "user",
    "sharer", "permalink", "hashtag", "share.php", "sharer.php", "dialog",
}


def _multiplier(text: str) -> int:
    low = text.strip().lower()
    if low in ("nghin", "nghìn", "ngàn", "ngan", "k"):
        return 1_000
    if low in ("trieu", "triệu", "tr", "m"):
        return 1_000_000
    return 1


def _grouped_thousands(s: str, sep: str) -> Optional[str]:
    """'5.678' -> '5678' nếu MỌI nhóm sau dấu đều đúng 3 chữ số, else None.

    Phân biệt dấu phân cách hàng nghìn với dấu thập phân: "501.234" là 501234
    còn "1.5" là 1,5. Trước đây code xoá thẳng dấu chấm nên "1.5 triệu" bị đọc
    thành "15 triệu" — sai gấp 10 lần.
    """
    parts = s.split(sep)
    if len(parts) < 2:
        return None
    if not parts[0].isdigit():
        return None
    if not all(p.isdigit() and len(p) == 3 for p in parts[1:]):
        return None
    return "".join(parts)


def _to_number(num_text: str, mult_text: str = "") -> Optional[int]:
    """'12,5' + 'nghin' -> 12500 | '12,500' + '' -> 12500 | '5.678' -> 5678"""
    s = (num_text or "").strip().replace(" ", "")
    if not s:
        return None
    if "." in s and "," in s:
        # Có cả hai dấu: dấu xuất hiện SAU CÙNG là dấu thập phân
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        s = s.replace("," if dec == "." else ".", "")
        head, _, frac = s.partition(dec)
        if not (head.isdigit() and frac.isdigit()):
            return None
        value = float(f"{head}.{frac}")
    elif "." in s or "," in s:
        sep = "." if "." in s else ","
        grouped = _grouped_thousands(s, sep)
        if grouped is not None:
            value = float(grouped)
        else:
            head, _, frac = s.partition(sep)
            if not (head.isdigit() and frac.isdigit()):
                return None
            value = float(f"{head}.{frac}")
    elif s.isdigit():
        value = float(s)
    else:
        return None
    result = int(round(value * _multiplier(mult_text)))
    return result if result > 0 else None


def _detect_privacy(text: str) -> GroupPrivacy:
    """Đoán quyền riêng tư của nhóm.

    Dùng ranh giới từ (\\b) để tránh trùng chuỗi: trước đây "kin" khớp với
    "Kinh doanh" khiến nhóm công khai bị gán nhầm thành nhóm kín.
    """
    low = text.lower()
    # Ưu tiên các cụm dài, dễ nhận diện trước (có ranh giới từ để
    # "nhom kin" không khớp với "nhom kinh doanh")
    for phrase in _CLOSED_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", low):
            return GroupPrivacy.CLOSED
    for kw, val in _PRIVACY_KEYWORDS.items():
        if re.search(rf"\b{re.escape(kw)}\b", low):
            return val
    return GroupPrivacy.UNKNOWN


def _strip_fb_suffix(text: Optional[str]) -> Optional[str]:
    """'Mekong News | Facebook' -> 'Mekong News'"""
    if not text:
        return None
    cleaned = re.split(r"\s*\|\s*Facebook\s*$", text, flags=re.I)[0]
    cleaned = re.sub(r"\s*[-–]\s*Facebook\s*$", "", cleaned, flags=re.I)
    return clean_text(cleaned)


def _cut_name(raw: str) -> Optional[str]:
    """Cắt phần mô tả thừa sau tên (Người có tiếng nói, người theo dõi...)."""
    raw = re.sub(r"\s+", " ", raw or "").strip()
    if not raw:
        return None
    stop = _NAME_STOP_RE.search(raw)
    if stop and stop.start() > 0:
        raw = raw[: stop.start()]
    raw = raw.strip(" ·,-–—")
    if len(raw) < 2 or len(raw) > 70:
        return None
    return raw or None


def _card_of(anchor: Tag) -> Tag:
    """Khối đại diện của 1 người: leo lên từ <a> cho tới khi chứa >1 link profile.

    Khối này chứa cả badge "Quản trị viên"/"Kiểm duyệt viên" (thường là khối
    cha ngay bên ngoài khối ảnh đại diện). Dừng trước khi gặp tiêu đề khu vực
    để không nuốt luôn cả khối chứa danh sách.
    """
    node = anchor
    last = anchor
    for _ in range(6):
        node = node.parent
        if node is None or not isinstance(node, Tag):
            break
        links = node.find_all("a", href=USER_LINK_RE)
        if len(links) > 1:
            break          # đã chạm khối chứa nhiều người (feed chung)
        txt = node.get_text(" ", strip=True)
        if len(txt) > 600:
            break
        last = node
        if not links:
            break
    return last


def norm_profile_url(url: Optional[str]) -> Optional[str]:
    """Chuẩn hoá mọi dạng link profile Facebook về 1 dạng duy nhất.

    https://www.facebook.com/groups/<gid>/user/100070999000111  -> profile.php?id=...
    https://www.facebook.com/profile.php?id=...                  -> giữ nguyên
    https://www.facebook.com/ten-hien-thanh                      -> giữ nguyên
    """
    if not url:
        return None
    m = re.search(r"/groups/\d+/(?:user|people)/([\w.\-]+)", url)
    if m:
        return f"https://www.facebook.com/profile.php?id={m.group(1)}"
    m = re.search(r"profile\.php\?id=(\d+)", url)
    if m:
        return f"https://www.facebook.com/profile.php?id={m.group(1)}"
    m = re.search(r"facebook\.com/([\w.\-]+)", url)
    if m and m.group(1).lower() not in _FB_RESERVED_WORDS:
        return f"https://www.facebook.com/{m.group(1)}"
    return None


# Tiêu đề khu vực chứa từ "Quản trị viên" nhưng KHÔNG phải badge của người
_ZONE_HEADER_RE = re.compile(
    r"(?:Quản trị viên và người kiểm duyệt|Quản trị viên và kiểm duyệt"
    r"|Moderators? (?:and|&) Admins?)\s*[·:]?\s*\d{0,4}",
    re.I,
)


def _detect_role_by_badge(text: str) -> str:
    """Đọc badge vai trò, bỏ qua tiêu đề khu vực.

    Thứ tự: kiểm "Kiểm duyệt viên" trước vì cụm này hiếm, gần như chỉ xuất
    hiện ở badge; còn "quản trị viên" hay xuất hiện trong mô tả tự do.
    """
    cleaned = _ZONE_HEADER_RE.sub(" ", text or "")
    if _BADGE_MOD_RE.search(cleaned):
        return "moderator"
    if _BADGE_ADMIN_RE.search(cleaned):
        return "admin"
    return "member"


def _name_from_node(anchor, block_name: str = "") -> Optional[str]:
    """Lấy tên người từ thẻ <a>: text -> alt ảnh -> alt/aria-label khối cha ngay cạnh.

    KHÔNG leo nhiều cấp, để tránh lấy nhầm tên nhóm (<h1>) hay tiêu đề trang.
    """
    def _ok(v, strict=True):
        got = _cut_name(v)
        if not got:
            return None
        # tên lấy trực tiếp từ thẻ <a> thì được phép trùng tên nhóm
        # (ví dụ trang chính của nhóm do chính nhóm quản lý)
        if strict and block_name and got.lower() == block_name.lower():
            return None
        return got if _valid_person_name(got, block_name, strict) else None

    label = clean_text(anchor.get_text(" ")) or ""
    if label and len(label) <= 80:
        got = _ok(label, strict=False)
        if got:
            return got
    for img in anchor.find_all("img", limit=3):
        if img.get("alt"):
            got = _ok(img["alt"], strict=False)
            if got:
                return got
    parent = anchor.parent
    if parent is not None and getattr(parent, "name", None) in ("div", "span", "li"):
        al = parent.get("aria-label")
        if al:
            got = _ok(al, strict=True)
            if got:
                return got
        for img in parent.find_all("img", limit=3):
            if img.get("alt"):
                got = _ok(img["alt"], strict=True)
                if got:
                    return got
    return None


def _valid_person_name(name: str, block_name: str = "", strict: bool = True) -> bool:
    """Loại tên không hợp lệ: rỗng, trùng tên nhóm, hoặc là nhãn vai trò."""
    if not name or len(name) < 2:
        return False
    low = name.strip().lower()
    if strict and block_name and low == block_name.strip().lower():
        return False
    if _BADGE_ADMIN_RE.fullmatch(low) or _BADGE_MOD_RE.fullmatch(low):
        return False
    if low in {"thành viên", "xem tất cả", "theo dõi", "thích", "xem thêm"}:
        return False
    if len(name) > 60:
        return False
    return True


def parse_people_from_members_page(html: str) -> dict:
    """Bóc danh sách quản trị / kiểm duyệt / thành viên từ trang '/members' đã lưu.

    Cấu trúc thực tế (đã kiểm chứng trên trang Facebook lưu bằng Ctrl+S):
        <div>  (thẻ 1 người)
          <a href=".../groups/<gid>/user/<uid>">Tên Người</a>
          ... "Quản trị viên"  (badge nếu là admin)
          ... "Kiểm duyệt viên" (badge nếu là moderator)
        </div>
    Vai trò được xác định bằng BADGE, không phụ thuộc thứ tự vùng.
    """
    out = {
        "admins": [], "moderators": [], "members": [],
        "admin_count": None, "member_count": None,
    }
    try:
        soup = BeautifulSoup(html, "lxml")
        text_all = soup.get_text("\n", strip=True)
    except Exception:  # noqa: BLE001
        return out

    # --- số quản trị / kiểm duyệt (từ tiêu đề khu vực) ---
    m = _ADMINS_HEAD_RE.search(text_all)
    if m:
        try:
            out["admin_count"] = int(m.group(2))
        except ValueError:
            pass

    # --- số thành viên ---
    m = _MEMBERS_TAB_RE.search(text_all)
    if m:
        out["member_count"] = _to_number(m.group(1), m.group(2) or "")

    def _handle_of(href: str) -> Optional[str]:
        um = _PROFILE_USER_RE.search('href="' + href + '"')
        if not um:
            return None
        h = um.group(1) or um.group(2) or um.group(3)
        if not h or h.lower() in _FB_RESERVED_WORDS:
            return None
        if um.group(2):
            return f"profile.php?id={h}"
        return h

    def _url_of(href: str) -> str:
        if href.startswith("profile.php"):
            return f"https://www.facebook.com/{href}"
        # ID dạng số -> dùng dạng chuẩn để link chắc chắn mở được
        if href.isdigit():
            return f"https://www.facebook.com/profile.php?id={href}"
        return f"https://www.facebook.com/{href}"

    # Tên nhóm (để loại trường hợp lấy nhầm <h1> làm tên người)
    block_name = ""
    try:
        h1 = soup.find("h1")
        if h1:
            block_name = _cut_name(h1.get_text(" ", strip=True)) or ""
        if not block_name:
            tt = soup.find("title")
            if tt:
                block_name = _cut_name(_strip_fb_suffix(tt.get_text(" "))) or ""
    except Exception:  # noqa: BLE001
        block_name = ""

    # Mốc vị trí trong trang: khu "Quản trị viên" và khu "Thành viên điểm chung"
    text_nodes = soup.find_all(string=True)
    # thứ tự xuất hiện của từng text node trong tài liệu
    node_pos: dict = {id(n): i for i, n in enumerate(text_nodes)}

    def _pos_of(node) -> Optional[int]:
        if node is None:
            return None
        if id(node) in node_pos:
            return node_pos[id(node)]
        parent = getattr(node, "parent", None)
        if parent is not None and id(parent) in node_pos:
            return node_pos[id(parent)]
        # <a> thường rỗng -> lấy vị trí text node đầu tiên bên trong
        for inner in parent.find_all(string=True) if parent is not None else []:
            if id(inner) in node_pos:
                return node_pos[id(inner)]
        return None

    idx_admin_zone = _pos_of(next(
        (n for n in text_nodes if _ADMINS_HEAD_RE.search(str(n))), None
    ))
    idx_member_zone = _pos_of(next(
        (n for n in text_nodes if _MEMBER_ZONE_RE.search(str(n))), None
    ))
    if idx_member_zone is not None and idx_admin_zone is not None \
            and idx_member_zone < idx_admin_zone:
        idx_admin_zone, idx_member_zone = idx_member_zone, idx_admin_zone

    # 2) Mỗi thẻ <a> profile = 1 người.
    #    Vai trò xác định từ badge trong khối bọc quanh thẻ <a> đó.
    best: dict = {}
    for a in soup.find_all("a", href=True):
        h = _handle_of(a["href"])
        if not h:
            continue
        name = _name_from_node(a, block_name)
        if not name:
            continue
        # Khối đại diện: leo lên để gom badge vai trò (nằm ngoài khối ảnh đại diện)
        card = _card_of(a)
        txt = card.get_text(" ", strip=True) if card is not None else ""
        role = _detect_role_by_badge(txt)
        evidence = "badge" if role != "member" else "none"
        # Trang "Xem tất cả quản trị viên" thường KHÔNG có badge ->
        # suy vai trò theo vị trí: nằm sau tiêu đề Quản trị, trước khu Thành viên
        if role == "member" and idx_admin_zone is not None:
            pos = _pos_of(a.find(string=True))
            if pos is not None and pos > idx_admin_zone and (
                idx_member_zone is None or pos < idx_member_zone
            ):
                role = "admin"
                evidence = "pos"
        url = _url_of(h)
        prev = best.get(h)
        if prev is None:
            best[h] = {"name": name, "url": url, "role": role,
                       "ev": evidence, "rank": 0}
            continue
        # ưu tiên bằng chứng badge hơn suy đoán theo vị trí
        new_rank = (1 if evidence == "badge" else 0)
        if new_rank > prev["rank"]:
            best[h] = {"name": name, "url": url, "role": role,
                       "ev": evidence, "rank": new_rank}
        elif new_rank == prev["rank"] and len(name) < len(prev["name"]):
            best[h]["name"] = name

    for _, rec in best.items():
        if not rec["name"]:
            continue          # bỏ entry không lấy được tên
        item = {"name": rec["name"], "url": rec["url"], "ev": rec["ev"]}
        if rec["role"] == "admin":
            out["admins"].append(item)
        elif rec["role"] == "moderator":
            out["moderators"].append(item)
        else:
            out["members"].append(item)

    out["skipped_no_name"] = sum(1 for r in best.values() if not r["name"])

    # Cảnh báo nếu tiêu đề ghi N quản trị nhưng mới bóc được M
    total_found = len(out["admins"]) + len(out["moderators"])
    if out["admin_count"] and total_found < out["admin_count"]:
        logger.info(
            "Tiêu đề ghi %d quản trị/kiểm duyệt nhưng mới bóc được %d. "
            "Hãy cuộn hết danh sách trên trang rồi Ctrl+S lại để lấy đủ.",
            out["admin_count"], total_found,
        )
    return out


def parse_group(
    html: str,
    group_id: str,
    url: str,
    max_members: Optional[int] = None,
    max_posts: int = 20,
) -> Optional[MonitoredGroup]:
    """Bóc thông tin nhóm từ HTML. max_members=0 -> không giữ danh sách thành viên."""
    soup = BeautifulSoup(html, "lxml")
    text_all = soup.get_text(" ", strip=True)

    # ---- Tên nhóm: bỏ qua nhãn giao diện (h1 có thể là modal tìm kiếm) ----
    name = None
    candidates = []
    h1 = soup.find("h1")
    if h1:
        candidates.append(h1.get_text(" ", strip=True))
    title_tag = soup.find("title")
    if title_tag:
        candidates.append(title_tag.get_text(" ", strip=True))
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        candidates.append(og["content"])
    m_hint = _NAME_HINTS_RE.search(html)
    if m_hint:
        candidates.append(m_hint.group(1))

    for raw_name in candidates:
        cand = _strip_fb_suffix(raw_name)
        if not cand or len(cand) > 120:
            continue
        if _CHROME_NAME_RE.match(cand):
            continue                    # "Kết quả tìm kiếm", "Trang chủ"... -> bỏ
        name = cand
        break

    # ---- Mô tả ----
    description = None
    m = _DESC_RE.search(html)
    if m:
        description = _strip_fb_suffix(m.group(1))
    if not description:
        og = soup.find("meta", property="og:description")
        if og and og.get("content"):
            description = clean_text(og["content"])
    if not description:
        m = re.search(
            r'"(?:groupDescription|description)"\s*:\s*"([^"]{10,400})"', html
        )
        # Trước đây có điều kiện `"Mekong" not in ...` — một mẹo gắn với dữ liệu
        # thử nghiệm bị lọt vào code thật, khiến nhóm nào có chữ "Mekong" trong
        # mô tả thì mô tả bị bỏ qua vô cớ.
        if m:
            description = clean_text(m.group(1))

    # ---- Privacy ----
    privacy = _detect_privacy(text_all)

    # ---- Thành viên ----
    member_count = None
    for m in _MEMBERS_RE.finditer(text_all):
        num = _to_number(m.group(1), m.group(2) or "")
        if num and (member_count is None or num > member_count):
            member_count = num
    if member_count is None:
        for pattern in (_SUBSCRIBER_JSON_RE, _MEMBER_JSON_RE):
            mm = pattern.search(html)
            if mm:
                # Các regex JSON chỉ có MỘT nhóm bắt (số) — không có
                # nhóm thứ 2 cho đơn vị, nên chỉ truyền group(1).
                member_count = _to_number(mm.group(1), "")
                if member_count:
                    break

    if not name and not description and member_count is None:
        # Facebook trả shell rỗng -> cần đăng nhập
        logger.warning(
            "Không đọc được nội dung nhóm %s "
            "(có thể nhóm kín/riêng tư, cần đăng nhập)", group_id
        )
        return None

    if member_count is None:
        logger.info(
            "Nhóm %s: chỉ đọc được metadata công khai (tên/mô tả). "
            "Số thành viên & nội dung cần đăng nhập.", group_id
        )

    # Khám phá URL profile admin/mod từ HTML công khai
    from .profile_scraper import discover_admin_urls  # tránh circular import
    admin_urls = discover_admin_urls(html, url)

    # Nếu là trang "Mọi người" đã lưu -> bóc danh sách admin/kd/thành viên
    member_profiles: List[AdminProfile] = []
    admin_named: List[dict] = []
    mod_named: List[dict] = []
    people_parsed = False
    if "/members" in url or _ADMINS_HEAD_RE.search(text_all):
        people = parse_people_from_members_page(html)
        people_parsed = True
        if people["member_count"] and not member_count:
            member_count = people["member_count"]
        admin_named = [
            {"name": p["name"], "url": p["url"], "ev": p.get("ev", "badge")}
            for p in people["admins"]
        ]
        mod_named = [
            {"name": p["name"], "url": p["url"], "ev": p.get("ev", "badge")}
            for p in people["moderators"]
        ]
        member_profiles = [
            AdminProfile(name=p["name"], profile_url=p["url"], role="member")
            for p in people["members"]
        ]
        if people.get("skipped_no_name"):
            logger.info(
                "Bỏ qua %d tài khoản không lấy được tên "
                "(trang chưa tải xong hoặc tên nằm trong ảnh). "
                "Hãy đợi trang tải hết rồi lưu lại.",
                people["skipped_no_name"],
            )

    group = MonitoredGroup(
        group_id=group_id,
        name=name or f"Group {group_id}",
        url=url,
        privacy=privacy,
        member_count=member_count,
        description=description,
    )
    # Ưu tiên danh sách đã phân loại từ trang "Mọi người"
    if admin_named or mod_named or member_profiles:
        for p in admin_named:
            group.admins.append(AdminProfile(
                name=p["name"], profile_url=p["url"], role="admin",
                source=p.get("ev", "badge"),
            ))
        for p in mod_named:
            group.moderators.append(AdminProfile(
                name=p["name"], profile_url=p["url"], role="moderator",
                source=p.get("ev", "badge"),
            ))
        group.members = list(member_profiles)
        if max_members is not None:
            group.members = group.members[:max_members]
        # KHÔNG return sớm: trang còn có thể chứa bài đăng cần bóc ở dưới
    elif people_parsed:
        # Đã đọc danh sách người -> không thêm entry suông từ link trang
        pass

    # ---- Bài đăng (nếu trang có feed) ----
    try:
        from .post_scraper import compute_activity, parse_posts

        posts = parse_posts(html, limit=max_posts)
        if posts:
            group.recent_posts = posts
            group.activity = compute_activity(posts)
            logger.info("Bóc được %d bài đăng từ %s", len(posts), group_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Không đọc được bài đăng của %s: %s", group_id, exc)

    if people_parsed:
        return group

    # Gắn sơ bộ AdminProfile — chuẩn hoá URL, bỏ entry không lấy được tên
    seen_urls = {p.profile_url for p in group.admins} | \
                {p.profile_url for p in group.moderators} | \
                {p.profile_url for p in group.members}
    for aurl in admin_urls:
        norm = norm_profile_url(aurl)
        if not norm or norm in seen_urls:
            continue
        seen_urls.add(norm)
        group.admins.append(AdminProfile(profile_url=norm, role="unknown"))
    return group


def scrape_group(
    client: HttpClient,
    group_id: str,
    url: str,
    max_members: Optional[int] = None,
    max_posts: int = 20,
) -> Optional[MonitoredGroup]:
    from .utils import LoginRequired

    try:
        resp = client.get(url)
        group = parse_group(resp.text, group_id, url, max_members, max_posts)
        if group:
            logger.info(
                "OK | %s | %s | members=%s | privacy=%s | admin_urls=%d",
                group_id, group.name, group.member_count,
                group.privacy.value, len(group.admins),
            )
        return group
    except LoginRequired as exc:
        logger.error("CẦN ĐĂNG NHẬP | %s | %s", group_id, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error("Lỗi scrape nhóm %s: %s", group_id, exc)
        return None
