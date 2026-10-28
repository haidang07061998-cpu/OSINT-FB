"""Xuất báo cáo: CSV tổng hợp + JSON chi tiết."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable, List

import pandas as pd

from models import MonitoredGroup, utcnow

# Ký tự mà Excel/CSheets sẽ diễn giải thành công thức nếu đặt ở đầu ô.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value):
    """Vô hiệu hoá công thức độc hại trong ô Excel/CSV.

    Nội dung bài đăng và tên do người dùng Facebook kiểm soát được ghi thẳng
    vào báo cáo. Một chuỗi bắt đầu bằng `=`, `+`, `-` hoặc `@` sẽ được
    Excel/Sheets hiểu là CÔNG THỨC và thực thi khi mở file (CSV injection).
    Với dữ liệu kiểu này, thêm dấu nháy đơn ở đầu là cách chuẩn để ép kiểu
    văn bản mà không làm mất nội dung hiển thị.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def safe_rows(rows: List[dict]) -> List[dict]:
    """Áp safe_cell cho mọi giá trị chuỗi trong các dòng bảng."""
    return [{k: safe_cell(v) for k, v in row.items()} for row in rows]


def export_json(groups: Iterable[MonitoredGroup], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = [g.model_dump(mode="json") for g in groups]
    out_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out_path


def export_group_summary_csv(groups: Iterable[MonitoredGroup], out_path: Path) -> Path:
    rows = []
    for g in groups:
        rows.append({
            "group_id": g.group_id,
            "name": g.name,
            "url": g.url,
            "privacy": g.privacy.value,
            "member_count": g.member_count,
            "admins": len(g.admins),
            "moderators": len(g.moderators),
            "posts": len(g.recent_posts),
            "last_scanned": g.last_scanned.isoformat(),
        })
    df = pd.DataFrame(safe_rows(rows))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    return out_path


def export_contacts_csv(groups: Iterable[MonitoredGroup], out_path: Path) -> Path:
    """CSV chuyên bóc tách liên hệ quản trị/kiểm duyệt/thành viên."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows: List[dict] = []
    for g in groups:
        people = (
            list(g.admins) + list(g.moderators) + list(g.members)
        )
        for p in people:
            phones = "; ".join(x.normalized for x in p.phones)
            links = "; ".join(f"{s.platform}: {s.url}" for s in p.social_links)
            rows.append({
                "group_id": g.group_id,
                "group_name": g.name,
                "role": p.role,
                "name": p.name,
                "fb_user_id": p.fb_user_id,
                "profile_url": p.profile_url,
                "verified": p.is_verified,
                "phones": phones,
                "social_links": links,
                "extracted_at": p.extracted_at.isoformat(),
            })
    rows = safe_rows(rows)
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [
            "group_id","group_name","role","name","fb_user_id","profile_url","verified","phones","social_links","extracted_at"
        ])
        writer.writeheader()
        writer.writerows(rows)
    return out_path


def _contacts_rows(groups: Iterable[MonitoredGroup]) -> List[dict]:
    rows: List[dict] = []
    for g in groups:
        for p in list(g.admins) + list(g.moderators) + list(g.members):
            rows.append({
                "Mã nhóm": g.group_id,
                "Tên nhóm": g.name,
                "Vai trò": p.role,
                "Tên tài khoản": p.name,
                "FB ID": p.fb_user_id,
                "Profile URL": p.profile_url,
                "Tích xanh": "Có" if p.is_verified else "Không",
                "Số điện thoại": "; ".join(x.normalized for x in p.phones),
                "Mạng xã hội": "; ".join(f"{s.platform}: {s.url}" for s in p.social_links),
                "Trích xuất lúc": p.extracted_at.isoformat(),
            })
    return safe_rows(rows)


_ROLE_VN = {
    "admin": "Quản trị viên",
    "moderator": "Kiểm duyệt viên",
    "member": "Thành viên",
}


def _posts_rows(groups: Iterable[MonitoredGroup]) -> List[dict]:
    rows: List[dict] = []
    for g in groups:
        for post in g.recent_posts:
            rows.append({
                "Mã nhóm": g.group_id,
                "Tên nhóm": g.name,
                "Tác giả": post.author_name,
                "Vai trò người đăng": _ROLE_VN.get(post.author_role or "", ""),
                "Nội dung": (post.content or "")[:500],
                # Trang đã lưu có thể không chứa số tương tác -> ghi rõ
                # thay vì để trống (dễ bị hiểu là 0 lượt thích).
                "Lượt thích": post.likes if post.metrics_available else None,
                "Bình luận": post.comments_count if post.metrics_available else None,
                "Chia sẻ": post.shares if post.metrics_available else None,
                "Có số tương tác": "Có" if post.metrics_available else "Không (trang đã lưu không chứa)",
                "Số ảnh/video": len(post.media_urls),
                "Thời gian": post.created_at.isoformat() if post.created_at else "",
                "Thời gian gốc": post.time_raw or "",
            })
    return safe_rows(rows)


def _persons_rows(groups: Iterable[MonitoredGroup] = (), persons: Iterable = ()) -> List[dict]:
    rows: List[dict] = []
    for p in persons:
        rows.append({
            "Tên": p.name,
            "Username": p.username,
            "FB ID": p.fb_user_id,
            "Profile URL": p.profile_url,
            "Số điện thoại": "; ".join(x.normalized for x in p.phones),
            "Email": "; ".join(p.emails),
            "Mạng xã hội": "; ".join(f"{s.platform}: {s.url}" for s in p.social_links),
            "Giới tính": p.gender,
            "Sinh nhật": p.birthday,
            "Sống tại": p.location,
            "Đến từ": p.hometown,
            "Công việc": p.job,
            "Nơi làm việc": p.company,
            "Học vấn": p.school,
            "Tình trạng quan hệ": p.relationship,
            "Tích xanh": "Có" if p.is_verified else "Không",
            "Từ khóa": "; ".join(p.keywords),
            "Website": p.website,
            "Nguồn": p.source_file,
            "Trích xuất lúc": p.extracted_at.isoformat(),
        })
    return safe_rows(rows)


def export_persons_csv(persons: Iterable, out_path: Path) -> Path:
    if isinstance(out_path, Path):
        out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = _persons_rows(persons=persons)
    fieldnames = list(rows[0].keys()) if rows else [
        "Tên", "Username", "FB ID", "Profile URL", "Số điện thoại", "Email",
        "Mạng xã hội", "Giới tính", "Sinh nhật", "Sống tại", "Đến từ",
        "Công việc", "Nơi làm việc", "Học vấn", "Tình trạng quan hệ",
        "Tích xanh", "Từ khóa", "Website", "Nguồn", "Trích xuất lúc",
    ]
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return out_path


def export_workbook(
    groups: Iterable[MonitoredGroup],
    out_path: Path,
    persons: Iterable = (),
) -> Path:
    """Xuất 1 file Excel .xlsx gồm 3 sheet: Nhóm, Liên hệ, Bài đăng (+ Profiles)."""
    groups = list(groups)
    persons = list(persons)
    if isinstance(out_path, Path):
        out_path.parent.mkdir(parents=True, exist_ok=True)

    summary = pd.DataFrame(safe_rows([{
        "Mã nhóm": g.group_id,
        "Tên nhóm": g.name,
        "URL": g.url,
        "Loại nhóm": g.privacy.value,
        "Thành viên": g.member_count,
        "Admin/Mod": len(g.admins) + len(g.moderators),
        "Thành viên lấy được": len(g.members),
        "Bài public": len(g.recent_posts),
        "Mô tả": g.description,
        "Quét lúc": g.last_scanned.isoformat(),
    } for g in groups]))

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Tong quan nhom", index=False)
        pd.DataFrame(_contacts_rows(groups)).to_excel(
            writer, sheet_name="Lien he QT-KD", index=False
        )
        pd.DataFrame(_posts_rows(groups)).to_excel(
            writer, sheet_name="Bai dang public", index=False
        )
        if persons:
            pd.DataFrame(_persons_rows(persons=persons)).to_excel(
                writer, sheet_name="Ho so ca nhan", index=False
            )
        # Auto-fit cột cho đọc dễ
        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            for col in ws.columns:
                width = max((len(str(c.value)) for c in col if c.value), default=10)
                ws.column_dimensions[col[0].column_letter].width = min(width + 2, 60)
    return out_path


def make_report(prefix: str = "report") -> Path:
    # `datetime.utcnow()` đã bị deprecated (và trả datetime "naive" mang giờ
    # UTC); dùng utcnow() của models để cùng một quy ước thời gian.
    ts = utcnow().strftime("%Y%m%d_%H%M%S")
    return Path("reports") / f"{prefix}_{ts}"
