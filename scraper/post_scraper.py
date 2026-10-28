"""Thu thập bài đăng từ HTML trang nhóm Facebook (đã lưu bằng Ctrl+S).

Cấu trúc HTML đã lưu KHÔNG dùng `role="article"` như bản render trực tiếp,
nên bộ nhận diện dựa trên các điểm ổn định hơn:
  1. Thẻ <a> trỏ tới /posts/<id> hoặc /permalink/  -> mốc bài đăng
  2. Hoặc khối chứa cụm chữ đặc trưng: thời gian + lượt thích/bình luận
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup, Tag

from models import GroupActivity, GroupPost, ScanProfile, TimeWindow
from .fb_time import parse_fb_time
from .utils import HttpClient, clean_text, logger

_POST_RE = re.compile(r"(?i)(\d[\d.,]*)\s*(?:bình luận|comments?)")
_REACT_RE = re.compile(r"(?i)(\d[\d.,]*)\s*(?:lượt thích|likes?|reactions?|thích)")
_SHARE_RE = re.compile(r"(?i)(\d[\d.,]*)\s*(?:lượt chia sẻ|shares?)")
_TIME_HINT_RE = re.compile(
    r"(?i)(?:\d+\s*(?:phút|giờ|ngày|tuần|tháng)(?:\s*trước)?"
    r"|hôm qua(?:\s*lúc\s*\d{1,2}[:h]\d{2})?"
    r"|vừa xong|\d{1,2}\s*tháng\s*\d{1,2})"
)
_MEDIA_RE = re.compile(r"(?i)\.(?:jpg|jpeg|png|gif|webp|mp4)(?:\?|$)")
_POST_LINK_RE = re.compile(
    r"/(?:groups/\d+/posts/\d+|permalink/\d+|posts/\d+|videos/\d+|photos/\d+)", re.I
)
# Trang "Thảo luận": mỗi bài kết thúc bằng ô soạn bình luận -> dùng làm mốc tách
_COMPOSER_RE = re.compile(r"Bình luận dưới tên|Write a comment|comment phía dưới", re.I)
_AUTHOR_BLOCK_RE = re.compile(
    r"(Người kiểm duyệt|Kiểm duyệt viên|Quản trị viên|Moderator|Admin)", re.I
)
_USER_LINK_RE = re.compile(r"/groups/\d+/user/([\w.\-]+)|profile\.php\?id=(\d+)", re.I)

# Thời gian dạng rút gọn: "2 giờ", "3 ngày", "5 phút" (không có chữ "trước")
_SHORT_TIME_RE = re.compile(
    r"^\s*(\d+)\s*(phút|giờ|ngày|tuần|tháng)\s*$", re.I
)

# Giới hạn kích thước khối bài đăng hợp lý
_MAX_BLOCK_CHARS = 6000
_MIN_BLOCK_CHARS = 60


def _num(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    digits = re.sub(r"\D", "", value.replace(",", "").replace(".", ""))
    return int(digits) if digits else None


_BADGE_ROLE_RE = re.compile(
    r"(Quản trị viên|Người quản lý|Quản trị|Administrator|Admin)"
    r"|(Kiểm duyệt viên|Người kiểm duyệt|Kiểm duyệt|Moderator)",
    re.I,
)


def _author_role_of(block: Tag) -> Optional[str]:
    """Vai trò người đăng lấy từ huy hiệu (Quản trị viên / Người kiểm duyệt).

    Trang đã lưu đặt huy hiệu trong aria-label: "Quản trị viên, xem chi tiết về huy hiệu".
    """
    for node in block.find_all(attrs={"aria-label": True}):
        label = node.get("aria-label") or ""
        if "huy hiệu" not in label and "huyệu" not in label:
            # vẫn thử nếu nhãn ghi thẳng vai trò dạng "Quản trị viên"
            if not re.search(r"(Quản trị viên|Người kiểm duyệt|Kiểm duyệt viên)", label, re.I):
                continue
        m = _BADGE_ROLE_RE.search(label)
        if not m:
            continue
        role_txt = m.group(0)
        if re.search(r"kiểm duyệt|moderator", role_txt, re.I):
            return "moderator"
        return "admin"
    return None


def _extract_media(block: Tag) -> List[str]:
    urls: List[str] = []
    for tag in block.find_all(["img", "a", "video"], limit=60):
        for attr in ("src", "href"):
            v = tag.get(attr)
            if v and _MEDIA_RE.search(v) and "emoji" not in v and "static" not in v:
                urls.append(v)
                break
    return list(dict.fromkeys(urls))[:10]


def _pick_block(anchor: Tag) -> Optional[Tag]:
    """Tìm khối chứa nội dung bài đăng gắn với anchor.

    Dừng ngay khi khối đã đủ nội dung; không leo qua khối chứa nhiều bài
    (đó là khối feed chung, sẽ gộp nhầm các bài vào một).
    """
    node = anchor
    best = None
    for _ in range(8):
        node = node.parent
        if node is None or not isinstance(node, Tag):
            break
        # khối chứa từ 2 bài trở lên -> là feed chung, dừng
        if len(_POST_LINK_RE.findall(str(node))) > 1:
            break
        text = node.get_text(" ", strip=True)
        if not text:
            continue
        if len(text) > _MAX_BLOCK_CHARS:
            break
        if len(text) >= _MIN_BLOCK_CHARS and (
            _TIME_HINT_RE.search(text)
            or _REACT_RE.search(text)
            or _POST_RE.search(text)
        ):
            if best is None:
                best = node
            # đã có nội dung đủ dài -> chấp nhận, không leo tiếp
            if len(text) >= 200:
                return node
    return best


def _author_of(block: Tag) -> Tuple[Optional[str], Optional[str]]:
    for a in block.find_all("a", href=True):
        m = _USER_LINK_RE.search(a["href"])
        if not m:
            continue
        label = clean_text(a.get_text(" ")) or ""
        img = a.find("img")
        if not label and img is not None:
            label = clean_text(img.get("alt") or "")
        if label and 1 < len(label) <= 70:
            handle = m.group(1) or m.group(2)
            url = (
                f"https://www.facebook.com/{handle}"
                if m.group(1)
                else f"https://www.facebook.com/profile.php?id={handle}"
            )
            return label, url
    return None, None


def _short_time(text: str):
    """'2 giờ' -> now - 2h (Facebook hiển thị rút gọn, không có chữ 'trước')."""
    m = _SHORT_TIME_RE.match(text or "")
    if not m:
        return None
    val = int(m.group(1))
    unit = m.group(2).lower()
    delta = {
        "phút": val, "giờ": val * 60, "ngày": val * 1440,
        "tuần": val * 10080, "tháng": val * 43200,
    }.get(unit, 0)
    if not delta:
        return None
    from datetime import datetime as _dt, timedelta as _td
    return _dt.now() - _td(minutes=delta)


def _post_time(block: Tag):
    time_raw = None
    created = None
    for tag in block.find_all(["abbr", "time", "span"], limit=120):
        for attr in ("data-utime", "aria-label", "title"):
            v = tag.get(attr)
            if not v:
                continue
            if attr == "data-utime":
                try:
                    from datetime import datetime as _dt
                    created = _dt.fromtimestamp(int(v))
                    time_raw = str(v)
                    return created, time_raw
                except (ValueError, OSError):
                    continue
            m = _TIME_HINT_RE.search(v)
            if m:
                dt = parse_fb_time(v)
                if dt:
                    return dt, v[:60]
            # dạng rút gọn: "2 giờ"
            dt = _short_time(v.strip())
            if dt:
                return dt, v.strip()[:20]

    # dạng rút gọn nằm rải rác trong text -> dò từng từ
    for token in (t.strip() for t in block.get_text(" ", strip=True).split("·")):
        dt = _short_time(token)
        if dt:
            return dt, token[:20]

    text = block.get_text(" ", strip=True)
    m = _TIME_HINT_RE.search(text)
    if m:
        raw = m.group(0)
        dt = parse_fb_time(raw) or _short_time(raw)
        return dt, raw[:60]
    # cuối cùng: dò thời gian rút gọn ("2 giờ") trong text
    m2 = _SHORT_TIME_RE.search(text)
    if m2:
        return _short_time(m2.group(0)), m2.group(0)[:20]
    return None, None


def _post_id_of(block: Tag) -> Optional[str]:
    for a in block.find_all("a", href=True):
        m = re.search(r"/posts/(\d+)|/permalink/(\d+)|/videos/(\d+)|/photos/(\d+)", a["href"])
        if m:
            return next(g for g in m.groups() if g)
    return None


_UI_NOISE_RE = re.compile(
    r"(Chỉ báo trạng thái online|Tài khoản đã xác minh|Đã chia sẻ với"
    r"|Sản phẩm/Dịch vụ|Gửi tin nhắn|Xem thêm|Theo dõi|Thích|Bình luận"
    r"|Chia sẻ|·|·|\|)",
    re.I,
)


_PREFIX_JUNK_RE = re.compile(
    r"^\s*(?:"
    r"Tham gia\s+[\wÀ-ỹ .|-]{2,40}\s*|"        # "Tham gia <tên>"
    r"Nhóm công khai|Công khai|Bạn bè|"
    r"Chỉ báo trạng thái online|Tài khoản đã xác minh|"
    r"Đã chia sẻ với[^A-Za-zÀ-ỹ]{0,30}|"
    r"Ảnh từ bài viết của[\wÀ-ỹ .|-]{2,60}|"
    r"\d{1,4}\s*(?:phút|giờ|ngày|tuần|tháng)(?:\s*trước)?|"
    r"\d{1,2}[:h]\d{2}"
    r")[\s·|:-]*",
    re.I,
)
_BAD_AUTHOR_RE = re.compile(
    r"(Chỉ báo trạng thái|Xem thêm|Gửi tin nhắn|Sản phẩm|Dịch vụ"
    r"|Tài khoản đã xác minh|Thích|Bình luận|Chia sẻ)",
    re.I,
)


def _strip_prefix(text: str, author: Optional[str]) -> str:
    """Bóc dần các mảnh thừa ở đầu nội dung bài."""
    out = _strip_chrome(text)
    if author:
        out = re.sub(r"^\s*" + re.escape(author) + r"[\s·|:-]*", "", out, count=1)
    out = _AUTHOR_BLOCK_RE.sub(" ", out, count=1)
    # lặp tối đa 5 lần để gỡ chuỗi rác nối tiếp.
    # DỪNG nếu phần còn lại bắt đầu bằng chữ thường -> đó là nội dung thật,
    # không phải nhãn giao diện (tránh nuốt "9 tháng đầu năm" thành "đầu năm").
    for _ in range(5):
        new = _PREFIX_JUNK_RE.sub("", out, count=1).strip()
        if not new or new == out:
            break
        if new[:1].islower():
            break
        out = new
    return _strip_chrome(out)


def _clean_noise(text: str) -> str:
    """Loại nhiễu giao diện: alt-text lặp lại, nhãn nút, dấu phân tách."""
    text = _strip_chrome(text)
    # gộp các từ ngắn lặp liên tiếp >=3 lần (alt-text icon)
    text = re.sub(r"\b(\w{2,12})(?:\s+\1){2,}\b", " ", text)
    text = _UI_NOISE_RE.sub(" ", text)
    # loại chuỗi "Facebook" còn sót
    text = re.sub(r"\bFacebook\b", " ", text)
    return _strip_chrome(text)


def _strip_chrome(text: str) -> str:
    """Bỏ phần đầu/cuối nhiễu (breadcrumb, nút thao tác), giữ nội dung bài."""
    return re.sub(r"\s+", " ", text or "").strip()


def _blocks_by_composer(soup) -> list:
    """Chiến lược 3: mỗi ô 'Bình luận dưới tên' đánh dấu cuối 1 bài.

    Dùng cho trang "Thảo luận" (feed) không có link /posts/.
    """
    blocks = []
    for node in soup.find_all(string=re.compile(_COMPOSER_RE)):
        # leo lên khối chứa nội dung bài (không chứa nhiều bài khác)
        cur = node.parent
        best = None
        for _ in range(10):
            if cur is None or not isinstance(cur, Tag):
                break
            if len(_COMPOSER_RE.findall(cur.get_text(" ", strip=True))) > 1:
                break                       # đã chạm khối chứa nhiều bài
            txt = cur.get_text(" ", strip=True)
            if len(txt) > _MAX_BLOCK_CHARS:
                break
            if best is None and len(txt) >= _MIN_BLOCK_CHARS:
                best = cur
            if len(txt) >= 400 and best is not None:
                break
            cur = cur.parent
        if best is not None:
            blocks.append(best)
    return blocks


def _score_block(block: Tag) -> int:
    """Chấm điểm khả năng là bài đăng thật (cao = giống bài viết)."""
    text = block.get_text(" ", strip=True)
    score = 0
    if block.get("role") == "article":
        score += 3                       # khung bài viết chuẩn của Facebook
    if block.find("a", href=_USER_LINK_RE):
        score += 3                       # có tác giả
    if _SHORT_TIME_RE.search(text) or _TIME_HINT_RE.search(text):
        score += 2                       # có mốc thời gian
    if _COMPOSER_RE.search(text):
        score += 2                       # có ô soạn bình luận (dấu hiệu đặc trưng)
    if block.find(["img", "video"]):
        score += 1                       # có ảnh/video
    # trừ điểm cho thẻ nhóm/trang (hay bị nhầm)
    if re.search(r"người theo dõi|Trang web tin tức|theo dõi", text, re.I):
        score -= 3
    if re.match(r"^\s*(Nhóm|Trang|Group|Page)\b", text):
        score -= 3
    if len(text) < _MIN_BLOCK_CHARS:
        score -= 2
    return score


def _blocks_by_time(soup) -> list:
    """Chiến lược 4: bám theo mốc thời gian của bài.

    Trang đã lưu có dạng: 'Tên tác giả · 3 ngày trước · Đã chia sẻ với ...'
    Mỗi bài có đúng 1 mốc thời gian -> leo lên tới khối chứa duy nhất mốc đó.
    """
    pats = re.compile(
        r"^\s*\d{1,4}\s*(?:phút|giờ|ngày|tuần|tháng)(?:\s*trước)?\s*$", re.I
    )
    blocks: list = []
    seen = set()
    for node in soup.find_all(string=pats):
        if not str(node).strip():
            continue
        cur = node.parent
        best = None
        for _ in range(14):
            if cur is None or not isinstance(cur, Tag):
                break
            times = [t for t in cur.find_all(string=pats) if t.strip()]
            if len(times) > 1:
                break                      # đã bao gồm bài khác
            txt = cur.get_text(" ", strip=True)
            if len(txt) > _MAX_BLOCK_CHARS:
                break
            # giữ khối LỚN NHẤT còn chứa duy nhất mốc thời gian này
            if len(txt) >= _MIN_BLOCK_CHARS:
                best = cur
            cur = cur.parent
        if best is not None and id(best) not in seen:
            seen.add(id(best))
            blocks.append(best)
    return blocks


def _collect_blocks(soup) -> list:
    """Chạy MỌI chiến lược rồi chọn các khối giống bài đăng nhất.

    Không dừng ở chiến lược đầu có kết quả: trang đã lưu dùng role=article
    cho thẻ nhóm/trang, còn bài viết mới đánh dấu bằng ô soạn bình luận.
    """
    candidates: list = []

    # 1) role=article
    candidates.extend(soup.find_all(attrs={"role": "article"}))

    # 2) neo theo link bài đăng
    for a in soup.find_all("a", href=True):
        if _POST_LINK_RE.search(a["href"]):
            b = _pick_block(a)
            if b is not None:
                candidates.append(b)

    # 3) neo theo ô "Bình luận dưới tên"
    candidates.extend(_blocks_by_composer(soup))

    # 4) neo theo mốc thời gian (quan trọng nhất với trang đã lưu)
    candidates.extend(_blocks_by_time(soup))

    # bỏ khối lồng nhau: giữ khối nhỏ nhất trong các khối trùng chứa nhau
    uniq: list = []
    seen_ids = set()
    for b in candidates:
        if id(b) in seen_ids:
            continue
        seen_ids.add(id(b))
        uniq.append(b)
    uniq.sort(key=lambda x: len(x.get_text(" ", strip=True)))

    kept: list = []
    kept_ids = set()
    for b in uniq:
        if id(b) in kept_ids:
            continue
        # bỏ nếu đã nằm trong khối đã giữ
        if any(id(d) in kept_ids for d in b.descendants):
            continue
        kept.append(b)
        kept_ids.add(id(b))

    scored = [
        (_score_block(b), len(b.get_text(" ", strip=True)), i, b)
        for i, b in enumerate(kept)
    ]
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [b for sc, _n, _i, b in scored if sc >= 3]


def parse_posts(
    html: str,
    limit: int = 20,
    window: Optional[TimeWindow] = None,
    profile: Optional[ScanProfile] = None,
) -> List[GroupPost]:
    """Bóc bài đăng từ HTML đã lưu.

    `profile` (tuỳ chọn) điều khiển việc có lấy media và bình luận hay không;
    không truyền profile thì hành vi như cũ (lấy media, không lấy bình luận).
    """
    soup = BeautifulSoup(html, "lxml")
    posts: List[GroupPost] = []
    seen_ids = set()

    blocks = _collect_blocks(soup)
    logger.info("Nhom: %d khoi ung vien, chon %d khoi co diem cao",
                len(soup.find_all(attrs={"role": "article"})), len(blocks))

    for block in blocks[: limit * 3]:
        if len(posts) >= limit:
            break
        text = _strip_chrome(block.get_text(" ", strip=True))
        if len(text) < _MIN_BLOCK_CHARS:
            continue

        pid = _post_id_of(block)
        if pid and pid in seen_ids:
            continue
        if pid:
            seen_ids.add(pid)

        created, time_raw = _post_time(block)
        # Lọc theo khoảng ngày dựa trên datetime đã tính (chính xác hơn text)
        if window and window.days_back > 0 and created is not None:
            from datetime import datetime as _dt, timedelta as _td
            if created < _dt.now() - _td(days=window.days_back):
                continue

        author_name, author_url = _author_of(block)
        if author_name and _BAD_AUTHOR_RE.search(author_name):
            author_name = None
            author_url = None

        # Bỏ nhiễu (alt-text "Facebook" lặp) TRƯỚC, rồi mới cắt tiền tố
        content = _clean_noise(text)
        content = _strip_prefix(content, author_name)
        content = _COMPOSER_RE.split(content)[0]
        content = _clean_noise(content)
        if len(content) < 20:
            content = _strip_prefix(_clean_noise(text), author_name)
        content = content[:1000]
        if len(content) < 20:
            continue          # không đủ nội dung để là bài đăng

        # Khử trùng: cùng tác giả + cùng thời gian + mở đầu giống nhau
        dedup_key = (
            (author_name or "").strip().lower(),
            time_raw or "",
            content[:50].lower(),
        )
        if dedup_key in seen_ids:
            continue
        seen_ids.add(dedup_key)

        likes = _num((_REACT_RE.search(text) or [None, None])[1]) if _REACT_RE.search(text) else None
        comments = _num((_POST_RE.search(text) or [None, None])[1]) if _POST_RE.search(text) else None
        shares = _num((_SHARE_RE.search(text) or [None, None])[1]) if _SHARE_RE.search(text) else None
        # Trang đã lưu thường KHÔNG chứa số tương tác -> đánh dấu để không
        # bị hiểu nhầm "0 lượt thích" là thật.
        metrics_available = any(v is not None for v in (likes, comments, shares))

        want_media = not profile or profile.content.post_media
        want_comments = bool(profile and profile.content.comments)
        max_comments = profile.limits.max_comments_per_post if profile else 0

        media_urls = _extract_media(block) if want_media else []

        comment_texts: List[str] = []
        if want_comments and max_comments > 0:
            for cnode in block.find_all(attrs={"role": "comment"}):
                ctext = _clean_noise(cnode.get_text(" ", strip=True))
                if ctext:
                    comment_texts.append(ctext[:300])
                if len(comment_texts) >= max_comments:
                    break

        posts.append(GroupPost(
            post_id=pid,
            author_name=author_name,
            author_url=author_url,
            content=content,
            created_at=created,
            time_raw=time_raw,
            likes=likes,
            comments_count=comments,
            shares=shares,
            metrics_available=metrics_available,
            author_role=_author_role_of(block),
            media_urls=media_urls,
            comments=comment_texts,
        ))
    return posts


def compute_activity(posts: List[GroupPost]) -> GroupActivity:
    if not posts:
        return GroupActivity()
    newest = max((p.created_at for p in posts if p.created_at), default=None)
    oldest = min((p.created_at for p in posts if p.created_at), default=None)
    span = None
    if newest and oldest and newest > oldest:
        span = (newest - oldest).days
    return GroupActivity(
        posts_per_day=float(len(posts)),
        last_seen_post_at=newest,
        growth_note=(f"{len(posts)} bài; trải rộng {span} ngày"
                     if span else f"{len(posts)} bài"),
    )


def scrape_recent_posts(
    client: HttpClient,
    group_url: str,
    limit: int = 20,
    window: Optional[TimeWindow] = None,
    profile: Optional[ScanProfile] = None,
) -> List[GroupPost]:
    try:
        resp = client.get(group_url)
        fetch_n = limit * 3 if window and window.days_back > 0 else limit
        posts = parse_posts(resp.text, fetch_n, window, profile)[:limit]
        logger.info("Bài đăng thu thập được từ %s: %d", group_url, len(posts))
        return posts
    except Exception as exc:  # noqa: BLE001
        logger.error("Lỗi scrape bài đăng %s: %s", group_url, exc)
        return []