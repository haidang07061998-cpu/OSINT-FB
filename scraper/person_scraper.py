"""Parse profile CÁ NHÂN Facebook từ HTML người dùng tự lưu (Ctrl+S).

QUAN TRỌNG — CẢNH BÁO PHÁP LÝ
------------------------------
Module này bóc tách THÔNG TIN CÁ NHÂN (SĐT, email, ngày sinh, địa chỉ...).
Theo Nghị định 13/2023/NĐ-CP (bảo vệ dữ liệu cá nhân), việc thu thập xử
lý dữ liệu cá nhân bắt buộc phải được SỰ ĐỒNG Ý của chủ thể hoặc có căn cứ
khác theo luật. Chỉ sử dụng module này với mục đích hợp pháp (xác minh chủ
tài khoản quản trị/kiểm duyết trong khuôn khổ được uỷ quyền, an ninh mạng,
báo chí/học thuật có giấy phép...). Tool ghi `source_file` để truy vết nguồn
gốc từng bản ghi.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

from models import PersonProfile
from .link_extractor import extract_emails, extract_phones, extract_social_links
from .utils import clean_fb_url, clean_text, logger

# Nhãn field trên trang "Giới thiệu" — hỗ trợ có dấu/không dấu
_ABOUT_LABELS: Dict[str, tuple] = {
    "birthday": ("Sinh nhật", "Sinh nhat", "Ngày sinh", "Ngay sinh", "Sinh"),
    "gender": ("Giới tính", "Gioi tinh"),
    "location": ("Sống tại", "Song tai", "Đang sống", "Dang song", "Địa điểm", "Dia diem"),
    "hometown": ("Đến từ", "Den tu", "Quê quán", "Que quan"),
    "job": ("Làm việc tại", "Làm việc", "Lam viec tai", "Lam viec",
            "Công việc", "Cong viec", "Nghề nghiệp", "Nghe nghiep"),
    "company": ("Nơi làm việc", "Noi lam viec", "Công ty", "Cong ty"),
    "school": ("Trường", "Học tại", "Hoc tai", "Đã học tại", "Da hoc tai"),
    "website": ("Trang web", "Website", "Liên kết", "Lien ket"),
}

_RELATIONSHIP_RE = re.compile(
    r"(?i)(độc thân|doc than|đã kết hôn|da ket hon|đang hẹn hò|dang hen ho"
    r"|đã đính hôn|da dinh hon|ly thân|ly hon|góa|goa|quan hệ phức tạp"
    r"|quan he phuc tap|mẹ đơn thân|bo don than)",
)
_NAME_META_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')
# CHỈ nhận UID từ dạng URL (profile.php?id= hoặc facebook.com/<số>).
# KHÔNG dùng `"userID":"..."` trong JS: đó là ID của NGƯỜI ĐANG XEM trang,
# có mặt trên mọi trang đã lưu -> gán nhầm danh tính cho hồ sơ.
_FB_ID_RE = re.compile(r"(?i)(?:profile\.php\?id=|facebook\.com/)(\d{6,})")
# Tiêu đề trang KHÔNG phải tên người (trang đăng nhập, trang lỗi...)
_CHROME_TITLE_RE = re.compile(
    r"(?i)^(facebook|log ?in|log into facebook|đăng nhập|home|trang chủ"
    r"|content not found|không tìm thấy.*)$"
)
_DATE_RE = re.compile(
    r"\b(?:([12]\d|3[01]|0?[1-9])[/\-](1[0-2]|0?[1-9])[/\-](19\d{2}|20[0-2]\d)"
    r"|(19\d{2}|20[0-2]\d))\b"
)


def _segments(soup: BeautifulSoup) -> List[str]:
    """Chia text thành các đoạn để nhận diện cặp 'nhãn: giá trị'."""
    raw = soup.get_text("|")
    return [s.strip() for s in raw.split("|") if s and s.strip()]


def _match_label(seg: str, labels: tuple) -> Optional[str]:
    """Tìm giá trị nếu segment chứa nhãn. Bỏ qua segment quá dài (thường là bio)."""
    # Nhãn dài trước ("Làm việc tại" trước "Làm việc") nếu không sẽ cắt
    # dính chữ "tại" vào giá trị.
    for lb in sorted(labels, key=len, reverse=True):
        # "Sống tại" -> segment đúng bằng nhãn -> giá trị ở segment kế tiếp
        if seg.strip().rstrip(":").strip().lower() == lb.lower().rstrip(":"):
            return ""
        # "Sống tại: Cần Thơ" / "Sống tại - Cần Thơ" / "Sống tại Cần Thơ"
        # (Facebook thường viết liền chỉ cách trắng -> phải nhận cả 3 dạng)
        m = re.search(
            r"^\s*" + re.escape(lb) + r"\s*(?:[:\-\u2013\u2014]\s*|\s+)"
            r"(\S.{1,120}?)\s*$",
            seg, re.I,
        )
        if m:
            return m.group(1).strip()
    return None


_BARE_DOMAIN_RE = re.compile(
    r"\b(?:https?://)?(?:www\.)?[\w-]{2,40}"
    r"\.(?:com|net|org|vn|info|io|me|tv|shop|store|biz|co)(?:/\S*)?\b",
    re.I,
)
# Không coi domain của chính Facebook là website của người dùng
_FB_DOMAIN_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:facebook\.com|fb\.com|fb\.me"
    r"|messenger\.com|m\.facebook\.com)(?:/|$)",
    re.I,
)


def _extract_about(soup: BeautifulSoup) -> Dict[str, str]:
    segs = _segments(soup)
    out: Dict[str, str] = {}
    for i, seg in enumerate(segs):
        if len(seg) > 200:
            continue
        for field, labels in _ABOUT_LABELS.items():
            if field in out:
                continue
            val = _match_label(seg, labels)
            if val is None:
                continue
            if not val:
                # Giá trị là segment kế tiếp (nếu hợp lệ)
                if i + 1 < len(segs):
                    nxt = segs[i + 1].strip()
                    if 0 < len(nxt) < 120:
                        out[field] = clean_text(nxt) or ""
            else:
                out[field] = clean_text(val) or ""

    # Website trên trang cá nhân thường chỉ ghi domain trần, không có nhãn
    # ("abc.vn" thay vì "Trang web: abc.vn") -> quét riêng để đỡ mất.
    if not out.get("website"):
        for seg in segs:
            if len(seg) > 200:
                continue
            m = _BARE_DOMAIN_RE.search(seg)
            if not m:
                continue
            dom = m.group(0).rstrip(".,")
            if _FB_DOMAIN_RE.search(dom):
                continue          # đó là link Facebook, không phải website
            out["website"] = dom
            break
    return {k: v for k, v in out.items() if v}


_KEYWORD_STOP = {
    "và", "của", "là", "tại", "ở", "với", "cho", "các", "một", "những",
    "người", "này", "được", "có", "theo", "về", "từ", "trên", "dưới",
    "facebook", "profile", "ngày", "tháng", "năm", "số", "http", "https",
}


def _extract_keywords(bio, about: Dict[str, str], links) -> List[str]:
    """Rút từ khóa nổi bật từ bio / nghề / trường / website của hồ sơ.

    - Cụm viết HOA  -> giữ nguyên cụm ("Chuyên Gia Tài Chính")
    - Giá trị nhãn  -> giữ nguyên ("Công ty TNHH ABC", "Đại học Cần Thơ")
    - Từ đơn lẻ    -> chỉ lấy từ dài >= 5 ký tự để tránh nhiễu
    """
    out: List[str] = []
    seen = set()

    def _push(value: str) -> None:
        v = clean_text(value) or ""
        low = v.lower()
        if not v or len(v) < 3 or low in seen:
            return
        # không coi số điện thoại / email là "từ khóa"
        if re.match(r"^(?:sdt|sđt|email|e-mail|tel|phone)\b", v, re.I) or \
                re.match(r"^[\d\s.()+-]+$", v):
            return
        seen.add(low)
        out.append(v)

    about = about or {}
    bio = clean_text(bio) or ""
    label_vals = [str(about.get(k)) for k in
                  ("job", "company", "school", "website", "location", "hometown")
                  if about.get(k)]

    # 1) Cụm viết HOA giữ nguyên ("CHUYÊN GIA TÀI CHÍNH"), bỏ nếu đã
    #    nằm sẵn trong một giá trị nhãn (tránh "TNHH ABC" lặp lại).
    haystack = " · ".join([bio] + label_vals)
    for m in re.finditer(r"\b([A-ZÀ-ỸĐ][A-ZÀ-ỸĐ]+(?:\s+[A-ZÀ-ỸĐ]{2,})*)\b", haystack):
        phrase = m.group(1).strip()
        words = [w for w in phrase.split()
                 if len(w) > 1 and w.lower() not in _KEYWORD_STOP]
        if len(words) < 2 and len(phrase) < 5:
            continue
        if any(phrase.lower() in v.lower() for v in label_vals):
            continue
        _push(phrase)

    # 2) Giữ nguyên giá trị nhãn (nghề, công ty, trường, web)
    for v in label_vals:
        _push(v)

    # 3) Cụm Việt trong bio: chunk bắt đầu bằng chữ hoa, 2-5 từ
    #    ("Nông dân", "Cần Thơ", "Mê Kông")
    if bio:
        for chunk in re.split(r"[·;,.!?()\[\]|\n\r]+", bio):
            chunk = chunk.strip()
            if not chunk or not chunk[0].isupper():
                continue
            words = chunk.split()
            if not (2 <= len(words) <= 5):
                continue
            words = [w for w in words if w.lower() not in _KEYWORD_STOP]
            if len(words) >= 2:
                _push(" ".join(words))
            if len(out) >= 30:
                break
    return out[:30]


def parse_person_from_html(html: str, source_file: str = "") -> Optional[PersonProfile]:
    soup = BeautifulSoup(html, "lxml")
    all_text = soup.get_text(" ", strip=True)

    # Thông tin SĐT/email thường nằm trong thẻ <meta> chứ không hiện trong
    # DOM -> ghép thêm vào vùng quét, nếu không sẽ bỏ sót liên hệ.
    meta_bits = []
    for meta in soup.find_all("meta"):
        val = meta.get("content") or ""
        if val:
            meta_bits.append(val)
    if meta_bits:
        all_text = f"{all_text} " + " ".join(meta_bits)

    # ---- Tên & URL ----
    # Ưu tiên og:title (metadata của CHÍNH trang hồ sơ). `"name"` đầu tiên
    # trong HTML thô có thể là của người khác -> gán nhầm tên.
    name = None
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        cand = clean_text(og["content"].split("|")[0].strip())
        if cand and not _CHROME_TITLE_RE.match(cand.strip("| ")):
            name = cand
    if not name:
        m = _NAME_META_RE.search(html)
        if m:
            name = clean_text(m.group(1))

    profile_url = ""
    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        profile_url = canonical["href"]
    if not profile_url:
        og = soup.find("meta", property="og:url")
        profile_url = (og.get("content") if og else "") or ""
    # URL lưu trên trang thường kèm tracking (`&__cft__[0]=...`) -> làm sạch,
    # giữ lại `?id=` để gộp đúng cùng tài khoản khi nạp nhiều file.
    profile_url = clean_fb_url(profile_url) or ""

    fb_uid = None
    # Ưu tiên UID từ URL của CHÍNH trang (og:url/canonical đã làm sạch ở trên).
    # Quét toàn bộ HTML chỉ là fallback: một trang hồ sơ lưu bằng Ctrl+S chứa
    # rất nhiều ID của bạn bè/gợi ý, và match đầu tiên trong toàn văn bản có
    # thể thuộc về người KHÁC -> gán nhầm thông tin cá nhân.
    m = _FB_ID_RE.search(profile_url) or _FB_ID_RE.search(html)
    if m:
        fb_uid = m.group(1)

    username = None
    if profile_url:
        path = re.sub(r"https?://[^/]+", "", profile_url).strip("/")
        parts = [p for p in path.split("/") if p and not p.startswith("profile.php")]
        if parts:
            username = parts[0]

    # ---- Liên hệ (feature chính) ----
    phones = extract_phones(all_text)
    emails = extract_emails(all_text)
    links = extract_social_links(all_text)

    # ---- About ----
    about = _extract_about(soup)

    # ---- Tình trạng quan hệ ----
    relationship = None
    rm = _RELATIONSHIP_RE.search(all_text)
    if rm:
        relationship = rm.group(1)

    # ---- Sinh nhật ----
    birthday = about.get("birthday")
    if not birthday:
        dm = _DATE_RE.search(all_text)
        if dm and (dm.group(1) or dm.group(4)):
            birthday = "/".join(g for g in dm.groups() if g)

    bio = None
    og = soup.find("meta", property="og:description")
    if og and og.get("content"):
        bio = clean_text(og["content"])

    verified = bool(re.search(r"(?i)(đã xác minh|verified account)", all_text)) or bool(
        soup.find(attrs={"aria-label": re.compile(r"(?i)verified")})
    )

    if not name and not fb_uid:
        logger.warning("Không parse được profile từ %s", source_file)
        return None

    person = PersonProfile(
        fb_user_id=fb_uid,
        name=name,
        username=username,
        profile_url=profile_url or None,
        bio=bio,
        phones=phones,
        emails=emails,
        social_links=links,
        gender=about.get("gender"),
        birthday=birthday,
        location=about.get("location"),
        hometown=about.get("hometown"),
        job=about.get("job"),
        company=about.get("company"),
        school=about.get("school"),
        relationship=relationship,
        website=about.get("website"),
        keywords=_extract_keywords(bio, about, links),
        is_verified=verified,
        source_file=source_file or None,
    )
    logger.info(
        "Person %s | phones=%d emails=%d links=%d keywords=%d",
        name or fb_uid, len(phones), len(emails), len(links),
        len(person.keywords),
    )
    return person
