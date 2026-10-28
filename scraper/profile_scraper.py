"""Thu thập thông tin tài khoản Quản trị viên / Kiểm duyệt viên (public).

Nguồn dữ liệu hợp lệ:
  - Trang "about" / bio công khai
  - Bài đăng public có ghim thông tin liên hệ
  - Text hiển thị trên trang thành viên public

Module tự động nhận diện VAI TRÒ (admin/mod) dựa trên dấu hiệu
label công khai, sau đó trích xuất SĐT & social links.
"""
from __future__ import annotations

import re
from typing import List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from models import AdminProfile
from .link_extractor import extract_contacts
from .utils import HttpClient, clean_text, logger

# Các cụm label cho thấy vai trò quản trị (Facebook hiển thị trên UI public)
_ROLE_PATTERNS = [
    (re.compile(r"(?i)\bquản trị viên\b|\badmin\b|\badministrator\b"), "admin"),
    (re.compile(r"(?i)\bkiểm duyệt viên\b|\bmoderator\b|\bmod\b"), "moderator"),
]

_NAME_META_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')
_FB_ID_RE = re.compile(r"(?i)(?:profile\.php\?id=|facebook\.com/)(\d{6,})")
# Tiêu đề trang KHÔNG phải tên người (trang đăng nhập, trang lỗi...)
_CHROME_TITLE_RE = re.compile(
    r"(?i)^(facebook|log ?in|log into facebook|đăng nhập|home|trang chủ"
    r"|content not found|không tìm thấy.*)$"
)


def _is_chrome_title(value: Optional[str]) -> bool:
    return bool(value) and bool(_CHROME_TITLE_RE.match(value.strip("| ")))


def _detect_role(text: str) -> Optional[str]:
    for pattern, role in _ROLE_PATTERNS:
        if pattern.search(text):
            return role
    return None


def _collect_text_bits(soup: BeautifulSoup) -> str:
    """Gộp toàn bộ text liên quan: meta description, about, bio, contact."""
    bits: List[str] = []
    og = soup.find("meta", property="og:description")
    if og and og.get("content"):
        bits.append(og["content"])
    # các thẻ <p>, <span> chứa bio
    for tag in soup.find_all(["p", "span", "div"], limit=200):
        txt = tag.get_text(" ", strip=True)
        if txt and 10 < len(txt) < 500:
            bits.append(txt)
    return "\n".join(bits)


def parse_admin_from_html(
    html: str,
    profile_url: str,
    role_hint: Optional[str] = None,
) -> Optional[AdminProfile]:
    soup = BeautifulSoup(html, "lxml")
    text = _collect_text_bits(soup)

    # ---- Tên ----
    # Ưu tiên og:title (metadata của CHÍNH trang). `"name"` đầu tiên trong HTML
    # thô có thể thuộc về người khác (bạn bè / gợi ý ở khung bên cạnh) -> gán
    # nhầm tên của người khác vào hồ sơ này.
    name = None
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        cand = clean_text(og["content"].replace(" | Facebook", "").strip("| "))
        if cand and not _is_chrome_title(cand):
            name = cand
    if not name:
        m = _NAME_META_RE.search(html)
        if m:
            name = clean_text(m.group(1))

    # ---- Vai trò ----
    role = role_hint or _detect_role(text) or "unknown"

    # ---- ID ----
    # Lấy UID từ URL của chính trang trước; chỉ khi URL không có ID mới quét
    # HTML, vì match đầu tiên trong toàn văn bản có thể là người khác.
    m = _FB_ID_RE.search(profile_url) or _FB_ID_RE.search(html)
    fb_uid = m.group(1) if m else None

    # ---- Liên hệ (feature cốt lõi) ----
    phones, links = extract_contacts([text])
    verified = "tích xanh" in text.lower() or "verified account" in text.lower()
    if not verified:
        v = soup.find(attrs={"aria-label": re.compile(r"(?i)verified")})
        verified = bool(v)

    profile = AdminProfile(
        fb_user_id=fb_uid,
        name=name,
        profile_url=profile_url,
        role=role,
        is_verified=verified,
        phones=phones,
        social_links=links,
    )
    logger.info(
        "Profile %s | role=%s | phones=%d | links=%d",
        name or fb_uid or profile_url, role, len(phones), len(links),
    )
    return profile


def scrape_admin_profile(
    client: HttpClient,
    profile_url: str,
    role_hint: Optional[str] = None,
) -> Optional[AdminProfile]:
    try:
        resp = client.get(profile_url)
        return parse_admin_from_html(resp.text, profile_url, role_hint)
    except Exception as exc:  # noqa: BLE001
        logger.error("Lỗi scrape profile %s: %s", profile_url, exc)
        return None


def discover_admin_urls(group_html: str, base_url: str) -> List[str]:
    """Bóc tách URL profile xuất hiện trong HTML nhóm + đoán vai trò.

    Dùng heuristic: ảnh đại diện admin, link /profile.php, /people/...
    """
    soup = BeautifulSoup(group_html, "lxml")
    urls: List[str] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        low = href.lower()
        if any(k in low for k in ("/user/", "/profile.php", "/people/")):
            full = urljoin(base_url, href)
            full = full.split("&__tn__")[0].split("?__tn__")[0]
            if full not in seen:
                seen.add(full)
                urls.append(full)
    return urls
