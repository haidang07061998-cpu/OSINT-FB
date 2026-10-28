#!/usr/bin/env python3
"""CLI chính cho công cụ giám sát hội nhóm Facebook (OSINT).

Lệnh:
    python main.py configure                        # Wizard chọn cách quét
    python main.py profiles                         # Xem các profile quét
    python main.py entities                         # Ghép tài khoản cùng chủ thể
    python main.py discover                         # Tìm seed nhóm theo khu vực + chủ đề
    python main.py run --profile standard --seed seed_groups.yaml
    python main.py diff                             # Báo cáo thay đổi so với lần trước
    python main.py import <FOLDER>                  # Parse HTML tự lưu (nhóm riêng tư)
    python main.py list                             # Xem DB hiện có
    python main.py person [tên]                     # Tra cứu hồ sơ cá nhân
    python main.py group <GROUP_ID>                 # Xem chi tiết 1 nhóm
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# PHẢI là import đầu tiên và phải chạy trước mọi thứ khác: console Windows
# mặc định dùng cp1252, không encode được tiếng Việt có dấu. Gọi muộn thì
# các dòng `print` trước đó đã chết với UnicodeEncodeError.
from console import setup_console

setup_console()

from pipeline import load_config, run  # noqa: E402
from scraper.entity_linker import run_entities  # noqa: E402
from scraper.manual_importer import run_import  # noqa: E402
from scraper.region_discovery import run_discovery  # noqa: E402
from scraper.scan_profile import list_profiles, save_profile, wizard  # noqa: E402
from storage import diff_snapshots, load_groups, query_admins, query_persons  # noqa: E402


def _load_cfg(path: str) -> dict:
    return load_config(path)


def _db_path(cfg: dict) -> Path:
    return Path(cfg.get("scraper", {}).get("data_dir", "data")) / "monitor.db"


def cmd_configure(args) -> int:
    cfg = _load_cfg(args.config)
    profile = wizard()
    save_profile(cfg, profile, args.config)
    print(f"\nĐã lưu. Chạy: python main.py run --profile {profile.name}")
    return 0


def cmd_profiles(args) -> int:
    cfg = _load_cfg(args.config)
    active = cfg.get("active_profile", "standard")
    for name, p in list_profiles(cfg).items():
        mark = "*" if name == active else " "
        print(f"{mark} {name:<16} {p.description}")
    return 0


def cmd_entities(args) -> int:
    run_entities(args.config)
    return 0


def cmd_discover(args) -> int:
    run_discovery(args.config)
    return 0


def cmd_run(args) -> int:
    run(args.config, seed=args.seed or None, profile_name=args.profile or None)
    return 0


def cmd_diff(args) -> int:
    changes = diff_snapshots(_db_path(_load_cfg(args.config)))
    if not changes:
        print("Không có thay đổi so với lần quét trước.")
        return 0
    for ch in changes:
        print(f"[{ch['group_id']}]")
        for note in ch["note"]:
            print("   -", note)
    return 0


def cmd_import(args) -> int:
    if not args.target:
        print("Cần đường dẫn thư mục chứa HTML, vd: python main.py import html_dump",
              file=sys.stderr)
        return 2
    run_import(args.target, args.config)
    return 0


def cmd_list(args) -> int:
    for g in load_groups(_db_path(_load_cfg(args.config))):
        print(f"{g.group_id:<16} | {g.privacy.value:<8} | "
              f"members={g.member_count} | admins={len(g.admins)} | {g.name}")
    return 0


def cmd_person(args) -> int:
    rows = query_persons(_db_path(_load_cfg(args.config)), args.target)
    if not rows:
        print("Không có hồ sơ cá nhân nào.")
        return 0
    for r in rows:
        print(f"- {r['name']} ({r['profile_url']}) "
              f"phones={r['phones']} emails={r['emails']}")
    return 0


def cmd_group(args) -> int:
    if not args.target:
        print("Cần GROUP_ID", file=sys.stderr)
        return 2
    rows = query_admins(_db_path(_load_cfg(args.config)), args.target)
    if not rows:
        print(f"Không có admin/mod nào cho nhóm {args.target}")
        return 0
    for r in rows:
        print(f"- [{r['role']}] {r['name']} ({r['profile_url']}) "
              f"phones={r['phones']} links={r['social_links']}")
    return 0


COMMANDS = {
    "configure": cmd_configure,
    "profiles": cmd_profiles,
    "entities": cmd_entities,
    "discover": cmd_discover,
    "run": cmd_run,
    "diff": cmd_diff,
    "import": cmd_import,
    "list": cmd_list,
    "person": cmd_person,
    "group": cmd_group,
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="OSINT Group Monitor - thu thập thông tin CÔNG KHAI từ nhóm FB"
    )
    parser.add_argument(
        "command",
        choices=sorted(COMMANDS) + ["help"],
        nargs="?",
        default="help",
    )
    parser.add_argument("target", nargs="?", default="")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--seed", default="", help="File seed target_groups.yaml")
    parser.add_argument(
        "--profile",
        default="",
        help="Tên profile quét (quick/standard/deep/private_daily/audit_evidence/tùy chỉnh)",
    )
    args = parser.parse_args()

    handler = COMMANDS.get(args.command)
    if handler is None:
        # "help" là lệnh hợp lệ, không phải lỗi -> exit 0. Trước đây trả 1 nên
        # script chạy `python main.py help` bị coi là thất bại.
        parser.print_help()
        return 0
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
