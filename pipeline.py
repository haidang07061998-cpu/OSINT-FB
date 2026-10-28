"""Orchestrator: chạy toàn bộ pipeline cho danh sách ID nhóm."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import yaml

from exporter import export_contacts_csv, export_group_summary_csv, export_json, export_workbook
from models import GroupPrivacy, MonitoredGroup, ScanProfile
from scraper.group_scraper import scrape_group
from scraper.post_scraper import compute_activity, scrape_recent_posts
from scraper.profile_scraper import scrape_admin_profile
from scraper.scan_profile import resolve_profile
from scraper.utils import HttpClient, ScraperConfig, get_logger
from storage import diff_snapshots, save_groups, save_snapshot

logger = get_logger("pipeline")


def load_config(path: str = "config.yaml") -> Dict:
    """Đọc config.yaml. File rỗng trả về {} thay vì None để .get() không lỗi."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def report_dir(cfg: Dict) -> Path:
    """Thư mục xuất báo cáo, lấy từ `scraper.report_dir` trong config."""
    return Path(cfg.get("scraper", {}).get("report_dir", "reports"))


def build_client(cfg: Dict, profile: Optional[ScanProfile] = None) -> HttpClient:
    """Tạo HttpClient. Nếu có profile thì rate_limit_seconds của profile
    được ưu tiên (trước đây trường này bị khai báo nhưng không bao giờ dùng)."""
    s = cfg.get("scraper", {})
    delay = profile.rate_limit_seconds if profile else s.get("request_delay", 3.0)
    return HttpClient(ScraperConfig(
        request_delay=float(delay),
        max_retries=s.get("max_retries", 3),
        timeout=s.get("timeout", 15),
        proxies=s.get("proxies"),
        user_agent=cfg.get("facebook", {}).get(
            "user_agent", "Mozilla/5.0 (compatible; OSINTGroupMonitor/1.0)"
        ),
    ))


def process_targets(
    cfg: Dict,
    targets: Optional[List[Dict]] = None,
    profile=None,
    progress_cb=None,
    client: Optional[HttpClient] = None,
) -> List[MonitoredGroup]:
    client = client or build_client(cfg, profile)
    legacy_limits = cfg.get("limits", {})
    only_public = cfg.get("facebook", {}).get("only_public_groups", True)
    targets = targets or cfg.get("target_groups", [])
    if profile:
        targets = targets[: profile.limits.max_groups]
    results: List[MonitoredGroup] = []

    for i, t in enumerate(targets, 1):
        # Config do người dùng sửa tay: một mục thiếu id/url (hoặc không phải
        # dict) không được làm chết cả lượt quét.
        if not isinstance(t, dict):
            logger.warning("Bỏ qua mục tiêu không hợp lệ (không phải dict): %r", t)
            continue
        gid = str(t.get("id") or "").strip()
        url = str(t.get("url") or "").strip()
        if not gid or not url:
            logger.warning("Bỏ qua mục tiêu thiếu id/url: %r", t)
            if progress_cb:
                progress_cb(i, len(targets), f"bỏ qua mục tiêu lỗi: {t}")
            continue

        # Số bài lấy từ trang nhóm: profile quyết định, không profile thì 20
        max_posts = (
            profile.window.max_posts if profile
            else legacy_limits.get("max_posts_per_group", 20)
        )
        group = scrape_group(
            client, gid, url,
            max_members=_max_members(profile),
            max_posts=max_posts,
        )
        if not group:
            if progress_cb:
                progress_cb(i, len(targets), f"[{gid}] không quét được (có thể yêu cầu đăng nhập)")
            continue
        is_private = group.privacy == GroupPrivacy.PRIVATE
        block_private = only_public and is_private
        if block_private and not (profile and profile.include_private):
            logger.info("Bỏ qua nhóm riêng tư theo cấu hình: %s", gid)
            if progress_cb:
                progress_cb(i, len(targets), f"[{gid}] bỏ qua (riêng tư)")
            continue

        # Bài đăng: số lượng + khoảng thời gian theo profile
        c = profile.content if profile else None
        if c and not c.posts:
            logger.info("Profile không bật quét bài đăng: %s", gid)
        else:
            posts = scrape_recent_posts(
                client, url, max_posts,
                window=profile.window if profile else None,
                profile=profile,
            )
            group.recent_posts = posts
            group.activity = compute_activity(posts)

        results.append(group)
        if progress_cb:
            progress_cb(
                i, len(targets),
                f"[{gid}] {group.name} | {group.member_count or '?'} thành viên, "
                f"{len(group.admins)} admin/mod",
            )

    return results


def _max_members(profile) -> int:
    """Số thành viên tối đa lấy mỗi nhóm (0 = không lấy danh sách)."""
    if profile:
        return profile.limits.max_members_per_group
    return 0  # không có profile -> không tự ý kéo danh sách thành viên


def enrich_admins(
    cfg: Dict,
    groups: List[MonitoredGroup],
    profile=None,
    progress_cb=None,
    client: Optional[HttpClient] = None,
) -> List[MonitoredGroup]:
    """Bổ sung profile admin/mod + bốc tách SĐT/social links.

    Quét CẢ admin lẫn kiểm duyệt viên (trước đây chỉ `group.admins` được soi,
    còn moderator thì hiện ra trong dashboard nhưng không có liên hệ).
    """
    client = client or build_client(cfg, profile)
    c = profile.content if profile else None
    if c and not (c.admins and c.admin_contacts):
        return groups

    max_accounts = profile.limits.max_accounts if profile else 10
    total = sum(
        min(len(g.admins) + len(g.moderators), max_accounts) for g in groups
    ) or 1
    done = 0
    for g in groups:
        # Ưu tiên admin, rồi kiểm duyệt viên, tới hạn mức max_accounts
        picked = (list(g.admins) + list(g.moderators))[:max_accounts]
        for p in picked:
            # Hồ sơ chỉ có tên, không có link -> không có gì để tải.
            # Trước đây vẫn gọi client.get(None) và ghi log lỗi mỗi lần.
            if not p.profile_url:
                continue
            full = scrape_admin_profile(client, p.profile_url, role_hint=p.role or None)
            if full:
                p.phones = full.phones
                p.social_links = full.social_links
                if not c or c.admin_bio:
                    p.is_verified = full.is_verified or p.is_verified
                if not p.fb_user_id:
                    p.fb_user_id = full.fb_user_id
            done += 1
            if progress_cb:
                progress_cb(done, total, f"Soi tài khoản {p.name or p.profile_url}")
    return groups


def finalize(
    cfg: Dict,
    groups: List[MonitoredGroup],
    persons: Optional[List] = None,
    db: Optional[Path] = None,
) -> Path:
    """Lưu DB (nhóm + hồ sơ + cụm danh tính), xuất báo cáo, tính delta."""
    persons = persons or []
    db = db or Path(cfg.get("scraper", {}).get("data_dir", "data")) / "monitor.db"
    rdir = report_dir(cfg)
    rdir.mkdir(parents=True, exist_ok=True)

    if groups:
        save_groups(db, groups)
        for g in groups:
            save_snapshot(db, g)
    if persons:
        from storage import save_persons

        save_persons(db, persons)

    if groups:
        export_group_summary_csv(groups, rdir / "summary.csv")
        export_contacts_csv(groups, rdir / "contacts.csv")
        export_json(groups, rdir / "full.json")
    if persons:
        from exporter import export_persons_csv

        export_persons_csv(persons, rdir / "ho_so_ca_nhan.csv")
    if groups or persons:
        export_workbook(groups, rdir / "bao_cao.xlsx", persons=persons)

    # Delta so với lần quét trước
    changes = diff_snapshots(db)
    if changes:
        logger.info("=== THAY ĐỔI SO VỚI LẦN TRƯỚC: %d nhóm ===", len(changes))
        for ch in changes:
            for note in ch["note"]:
                logger.info("  [%s] %s", ch["group_id"], note)

    # Ghép danh tính offline (cụm cùng chủ thể) - không tốn thêm request
    try:
        from scraper.entity_linker import (
            export_entities_csv,
            export_entities_workbook,
            link_identities,
            load_records_from_db,
            save_clusters,
        )

        clusters = link_identities(load_records_from_db(db))
        if clusters:
            save_clusters(db, clusters)
            export_entities_csv(clusters, rdir / "cum_danh_tinh.csv")
            export_entities_workbook(clusters, rdir / "cum_danh_tinh.xlsx")
            logger.info(
                "=== CỤM DANH TÍNH: %d cụm (%s) ===",
                len(clusters), rdir / "cum_danh_tinh.xlsx",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Entity linking lỗi (bỏ qua): %s", exc)
    return db


def _load_seed(seed_path: str) -> List[Dict]:
    """Đọc seed_groups.yaml do lệnh `discover` sinh ra."""
    p = Path(seed_path)
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("target_groups", [])


def run(
    cfg_path: str = "config.yaml",
    seed: Optional[str] = None,
    profile_name: Optional[str] = None,
) -> List[MonitoredGroup]:
    cfg = load_config(cfg_path)
    profile = resolve_profile(cfg, profile_name)
    logger.info(
        "PROFILE '%s': %s | %d nhóm/lượt, ≤%d admin, ≤%d bài/nhóm trong %d ngày",
        profile.name, profile.description,
        profile.limits.max_groups, profile.limits.max_admins_per_group,
        profile.window.max_posts, profile.window.days_back,
    )

    # Gộp target từ config + seed file (nếu có), ưu tiên config trước
    targets: List[Dict] = list(cfg.get("target_groups", []))
    if seed:
        seed_targets = _load_seed(seed)
        if seed_targets:
            logger.info("Nạp %d nhóm từ seed %s", len(seed_targets), seed)
            have = {str(t.get("id")) for t in targets}
            targets += [t for t in seed_targets if str(t.get("id")) not in have]
    if not targets:
        raise SystemExit(
            "Không có mục tiêu. Hãy thêm target_groups trong config, "
            "chạy `python main.py discover`, hoặc `python main.py configure`."
        )

    logger.info("=== BẮT ĐẦU QUÉT %d NHÓM (profile=%s) ===",
                min(len(targets), profile.limits.max_groups), profile.name)
    # Một client duy nhất cho cả lượt quét: đảm bảo giãn cách giữa các
    # request được tính trên toàn bộ quá trình, không phải từng pha riêng.
    client = build_client(cfg, profile)
    groups = process_targets(cfg, targets, profile, client=client)
    groups = enrich_admins(cfg, groups, profile, client=client)

    finalize(cfg, groups)
    logger.info("=== XONG. Nhóm: %d | Excel: %s ===",
                len(groups), report_dir(cfg) / "bao_cao.xlsx")
    return groups


if __name__ == "__main__":
    run()
