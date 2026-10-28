"""Hệ thống Scan Profile: preset + wizard chọn lựa + chọn profile khi chạy.

Mỗi profile trả lời: quét BAO NHIÊU nhóm / tài khoản / bài đăng, trong
khoảng thời gian nào, và quét NHỮNG NỘI DUNG gì.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Dict, Optional

import yaml

from models import ContentSelection, ScanLimits, ScanProfile, TimeWindow
from .utils import get_logger

logger = get_logger("profile")


# ---------------- Preset mặc định ----------------
def default_profiles() -> Dict[str, ScanProfile]:
    return {
        "quick": ScanProfile(
            name="quick",
            description="Quét nhanh 5 nhóm: meta + số thành viên + 5 bài mới trong 3 ngày",
            content=ContentSelection(
                member_list=False, new_members=False, admin_contacts=False,
                admin_bio=False, post_media=False, comments=False, keywords=False,
            ),
            window=TimeWindow(days_back=3, max_posts=5),
            limits=ScanLimits(max_groups=5, max_accounts=5, max_posts_per_group=5),
            rate_limit_seconds=2.0,
        ),
        "standard": ScanProfile(
            name="standard",
            description="Quét tiêu chuẩn: đủ nhóm + admin/mod + liên hệ + 20 bài/7 ngày",
            content=ContentSelection(),
            window=TimeWindow(days_back=7, max_posts=20),
            limits=ScanLimits(max_groups=10, max_accounts=10, max_posts_per_group=20),
            rate_limit_seconds=3.0,
        ),
        "deep": ScanProfile(
            name="deep",
            description="Quét sâu 20 nhóm: có media + keywords + tối đa 50 bài/30 ngày",
            content=ContentSelection(post_media=True, keywords=True, member_list=False),
            window=TimeWindow(days_back=30, max_posts=50),
            limits=ScanLimits(
                max_groups=20, max_accounts=15,
                max_members_per_group=20, max_posts_per_group=50,
            ),
            rate_limit_seconds=3.5,
        ),
        "private_daily": ScanProfile(
            name="private_daily",
            description="Nhóm RIÊNG TƯ bạn tự xem: 1-2 nhóm/ngày, có bằng chứng",
            content=ContentSelection(
                post_media=True, screenshot_evidence=True, admin_contacts=True,
            ),
            window=TimeWindow(days_back=1, max_posts=30),
            limits=ScanLimits(
                max_groups=2, max_accounts=6,
                max_posts_per_group=30, max_members_per_group=0,
            ),
            rate_limit_seconds=5.0,
            include_private=True,
        ),
        "audit_evidence": ScanProfile(
            name="audit_evidence",
            description="Phục vụ báo cáo/đối chứng: ít dữ liệu nhất, đủ bằng chứng",
            content=ContentSelection(
                posts=True, post_reactions=True, comments=True,
                screenshot_evidence=True, keywords=True, link_verify=False,
            ),
            window=TimeWindow(days_back=90, max_posts=100),
            limits=ScanLimits(
                max_groups=3, max_accounts=10,
                max_posts_per_group=100, max_comments_per_post=20,
            ),
            rate_limit_seconds=5.0,
        ),
    }


# ---------------- Load / resolve ----------------
def list_profiles(cfg: dict) -> Dict[str, ScanProfile]:
    """Profile trong config nếu có, lấp đủ bằng preset cho thiếu."""
    merged = default_profiles()
    for name, data in (cfg.get("scan_profiles") or {}).items():
        try:
            merged[name] = ScanProfile(name=name, **(data or {}))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Profile %s lỗi cấu hình (%s), dùng preset", name, exc)
    return merged


def resolve_profile(cfg: dict, name: Optional[str] = None) -> ScanProfile:
    profiles = list_profiles(cfg)
    name = name or cfg.get("active_profile") or "standard"
    if name not in profiles:
        available = ", ".join(profiles)
        raise SystemExit(f"Không có profile '{name}'. Các profile: {available}")
    return profiles[name]


# ---------------- Lưu vào config ----------------
def save_profile(
    cfg: dict,
    profile: ScanProfile,
    config_path: str = "config.yaml",
    backup: bool = True,
) -> None:
    """Ghi/cập nhật profile trong config.yaml, giữ các section khác.

    `yaml.safe_dump` ghi lại toàn bộ file nên MẤT CÁC COMMENT đã viết tay.
    Vì vậy trước khi ghi luôn tạo bản sao lưu `config.yaml.bak`.
    """
    cfg.setdefault("scan_profiles", {})[profile.name] = yaml.safe_load(
        profile.model_dump_json(exclude={"name"})
    )
    path = Path(config_path)
    if backup and path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        try:
            shutil.copy2(str(path), str(bak))
        except OSError as exc:
            logger.warning("Không tạo được backup config (%s)", exc)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    logger.info("Đã lưu profile '%s' vào %s", profile.name, path)


# ---------------- Wizard tương tác ----------------
def _ask(prompt: str, default: str = "") -> str:
    raw = input(f"{prompt} [{default}]: ").strip()
    return raw or default


def _ask_int(prompt: str, default: int, lo: int = 0, hi: int = 1000) -> int:
    for _ in range(3):  # tối đa 3 lần hỏi, tránh treo nếu nhập sai liên tục
        raw = _ask(prompt, str(default))
        try:
            val = int(raw)
        except ValueError:
            print(f"  Nhập số (mặc định {default})")
            continue
        if not (lo <= val <= hi):
            print(f"  Phải trong [{lo}, {hi}]")
            continue
        return val
    return default


def _ask_float(prompt: str, default: float, lo: float = 0.2, hi: float = 30.0) -> float:
    for _ in range(3):
        raw = _ask(prompt, str(default))
        try:
            val = float(raw)
        except ValueError:
            print(f"  Nhập số thập phân (mặc định {default})")
            continue
        if not (lo <= val <= hi):
            print(f"  Phải trong [{lo}, {hi}]")
            continue
        return val
    return default


def _ask_bool(prompt: str, default: bool) -> bool:
    raw = _ask(f"{prompt} (y/n)", "y" if default else "n").lower()
    return raw.startswith(("y", "1"))


def wizard() -> ScanProfile:
    """Wizard tiếng Việt chọn toàn bộ thông số quét."""
    print("=" * 62)
    print(" CẤU HÌNH LƯỢT QUÉT MỚI")
    print("=" * 62)

    name = _ask("Tên profile (vd: quyet-ngay)", "tuy-chinh")
    desc = _ask("Mô tả ngắn", "Cấu hình tuỳ chỉnh")

    print("\n-- QUY MÔ --")
    max_groups = _ask_int("Số nhóm tối đa mỗi lượt quét", 5)
    max_accounts = _ask_int("Số tài khoản (admin/mod) tối đa soi mỗi nhóm", 10)
    max_members = _ask_int("Số thành viên tối đa lấy mỗi nhóm (0 = bỏ qua)", 10)
    max_posts = _ask_int("Số bài đăng tối đa mỗi nhóm", 20)
    max_comments = _ask_int("Số bình luận tối đa mỗi bài (0 = bỏ qua)", 0)

    print("\n-- KHOẢNG THỜI GIAN --")
    days_back = _ask_int(
        "Chỉ lấy bài đăng trong bao nhiêu ngày gần nhất (0 = không giới hạn)", 7
    )

    print("\n-- NỘI DUNG CẦN QUÉT (y = quét / n = bỏ) --")
    c = ContentSelection(
        group_meta=_ask_bool("1. Meta nhóm (tên, privacy, mô tả, ảnh)", True),
        member_count=_ask_bool("2. Số lượng thành viên + biến động", True),
        member_list=bool(max_members) and _ask_bool("3. Danh sách thành viên", False),
        new_members=_ask_bool("4. Thành viên mới tham gia", False),
        admins=_ask_bool("5. Danh sách admin / kiểm duyệt + vai trò", True),
        admin_contacts=_ask_bool("6. Thông tin LIÊN HỆ admin (SĐT/social/email)", True),
        admin_bio=_ask_bool("7. Bio admin, ngày tham gia, tích xanh", True),
        posts=_ask_bool("8. Bài đăng", True),
        post_reactions=_ask_bool("9. Lượt thích/bình luận/chia sẻ", True),
        post_media=_ask_bool("10. Link ảnh/video trong bài", False),
        comments=bool(max_comments) and _ask_bool("11. Nội dung bình luận", False),
        activity_stats=_ask_bool("12. Thống kê hoạt động (tần suất, giờ đăng)", True),
        keywords=_ask_bool("13. Từ khoá nổi bật", True),
        link_verify=_ask_bool("14. KIỂM TRA link còn sống (chủ động - mặc định off)", False),
        screenshot_evidence=_ask_bool("15. Chụp màn hình + hash làm bằng chứng", False),
    )

    print("\n-- TỐC ĐỘ --")
    rate = _ask_float("Giãn cách giữa 2 request (giây)", 3.0)
    include_private = _ask_bool(
        "Bao gồm cả nhóm RIÊNG TƯ (chỉ khi bạn tự có quyền xem)", False
    )

    return ScanProfile(
        name=name,
        description=desc,
        content=c,
        window=TimeWindow(days_back=days_back, max_posts=max_posts),
        limits=ScanLimits(
            max_groups=max_groups,
            max_accounts=max_accounts,
            max_members_per_group=max_members,
            max_posts_per_group=max_posts,
            max_comments_per_post=max_comments,
        ),
        rate_limit_seconds=rate,
        include_private=include_private,
    )
