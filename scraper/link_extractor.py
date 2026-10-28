"""Module trích xuất SĐT & liên kết mạng xã hội từ văn bản công khai.

Chỉ xử lý văn bản được tìm thấy trên nguồn công khai (bio, about,
description, bài đăng public). Không tìm cách lấy dữ liệu bị ẩn.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from models import PhoneCandidate, SocialLink

# Bản đồ domain -> tên nền tảng
PLATFORM_MAP = {
    "facebook.com": "Facebook",
    "fb.com": "Facebook",
    "m.facebook.com": "Facebook",
    "web.facebook.com": "Facebook",
    "instagram.com": "Instagram",
    "tiktok.com": "TikTok",
    "twitter.com": "Twitter/X",
    "x.com": "Twitter/X",
    "linkedin.com": "LinkedIn",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "telegram.me": "Telegram",
    "t.me": "Telegram",
    "zalo.me": "Zalo",
    "vk.com": "VK",
    "reddit.com": "Reddit",
}

# Regex URL có scheme
_URL_RE = re.compile(
    r"(?i)\b(?:https?://|www\.)[^\s<>\"'()]+",
)
# Regex URL mạng xã hội viết tay (bare domain: facebook.com/xyz, t.me/abc)
# Dựng động từ keyword nền tảng để khớp chính xác t.me/x.com/fb.com
_SOCIAL_DOMAIN_RE = re.compile(
    r"(?i)\b(?:"
    + "|".join(sorted((re.escape(d) for d in PLATFORM_MAP), key=len, reverse=True))
    + r")/[^\s<>\"'()]+"
)

# Cho phép dấu phân cách giữa các nhóm số (vd "0912 345 678", "+84-912-345-678")
_SEP = r"[\s.\-]"

_PHONE_PATTERNS = [
    # +84xxxxxxxxx hoặc 0084xxx
    re.compile(rf"(?<!\d)(?:\+84|0084)(?:{_SEP}?\d){{9,10}}(?!\d)"),
    # 0xxxxxxxxx (10 số VN), có thể có separator
    re.compile(rf"(?<!\d)0\d(?:{_SEP}?\d){{8,9}}(?!\d)"),
    # Số quốc tế +XX xxx xxx xxx
    re.compile(rf"(?<!\d)\+\d{{1,3}}(?:{_SEP}?\d){{2,4}}(?:(?:{_SEP}?\d){{3,4}}){{1,3}}(?!\d)"),
]


def _domain(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    try:
        return (urlparse(url).netloc or "").lower().replace("www.", "")
    except Exception:
        return ""


def _platform_for(url: str) -> Optional[str]:
    d = _domain(url)
    if not d:
        return None
    for dom, name in PLATFORM_MAP.items():
        if d == dom or d.endswith("." + dom):
            return name
    return None


def _extract_handle(url: str) -> Optional[str]:
    """Lấy handle từ path cuối cùng (vd facebook.com/abc.def)."""
    try:
        path = urlparse(url if "://" in url else "http://" + url).path.strip("/")
        parts = [p for p in path.split("/") if p]
        if not parts:
            return None
        # Bỏ page/profile/premium_story ... lấy token cuối
        skip = {"profile.php", "pages", "pg", "groups", "story.php", "watch"}
        for token in reversed(parts):
            if token.lower() not in skip and not token.startswith("p"):
                return token
        return parts[-1]
    except Exception:
        return None


# ------------------- Social links --------------------------
def extract_social_links(text: str) -> List[SocialLink]:
    found: List[SocialLink] = []
    seen = set()
    for match in list(_URL_RE.findall(text or "")) + list(_SOCIAL_DOMAIN_RE.findall(text or "")):
        url = match.rstrip(".,;:!?)")
        platform = _platform_for(url)
        if not platform:
            continue
        key = (platform, _domain(url), _extract_handle(url))
        if key in seen:
            continue
        seen.add(key)
        found.append(
            SocialLink(platform=platform, url=url, handle=_extract_handle(url))
        )
    return found


# ------------------- Phone numbers -------------------------
def _normalize_phone(raw: str) -> Optional[str]:
    """Chuẩn hoá về dạng chỉ chứa chữ số và dấu + đầu."""
    cleaned = re.sub(r"[^\d+]", "", raw)
    cleaned = cleaned.lstrip("+") if cleaned.count("+") > 1 else cleaned
    if cleaned.startswith("0084"):
        cleaned = "+84" + cleaned[4:]
    elif cleaned.startswith("84") and len(cleaned) >= 11:
        cleaned = "+" + cleaned
    elif cleaned.startswith("0"):
        cleaned = "+84" + cleaned[1:]
    if re.fullmatch(r"\+?\d{9,15}", cleaned):
        return cleaned
    return None


def extract_phones(text: str) -> List[PhoneCandidate]:
    candidates: List[PhoneCandidate] = []
    seen = set()
    for pattern in _PHONE_PATTERNS:
        for m in pattern.findall(text or ""):
            # m có thể là group tuple nếu regex có group không cần thiết
            raw = m if isinstance(m, str) else (m[0] if m else "")
            if not raw:
                continue
            # Giữ nguyên cụm từ gốc với dấu phân cách để làm context
            norm = _normalize_phone(raw)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            candidates.append(PhoneCandidate(raw=raw, normalized=norm))
    return candidates


# ------------------- Email addresses -----------------------
_EMAIL_RE = re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
_EMAIL_NOISE_RE = re.compile(
    r"(?i)\b[a-z0-9._%+-]+@(?:facebook\.com|instagram\.com|fb\.com|meta\.com)\b"
)


def extract_emails(text: str) -> List[str]:
    """Lấy email, bỏ qua email sở hữu domain mạng xã hội (không phải liên hệ thật)."""
    found: List[str] = []
    for mail in _EMAIL_RE.findall(text or ""):
        if _EMAIL_NOISE_RE.fullmatch(mail):
            continue
        low = mail.lower()
        if low not in found:
            found.append(low)
    return found


# ------------------- Orchestrator --------------------------
def extract_contacts(texts: Iterable[str]) -> Tuple[List[PhoneCandidate], List[SocialLink]]:
    """Chạy trên nhiều đoạn text, gộp & khử trùng kết quả."""
    all_text = "\n".join(t for t in texts if t)
    phones = extract_phones(all_text)
    links = extract_social_links(all_text)
    return phones, links
