"""Test tích hợp scan profile + snapshot delta."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest
import yaml

from models import AdminProfile, GroupPost, GroupPrivacy, MonitoredGroup
from storage import diff_snapshots, save_groups, save_snapshot
from scraper.scan_profile import (
    default_profiles, resolve_profile, save_profile,
)


@pytest.fixture
def presets():
    return default_profiles()


def test_presets_exist(presets):
    assert {"quick", "standard", "deep", "private_daily", "audit_evidence"} <= set(presets)


def test_preset_quick(presets):
    p = presets["quick"]
    assert p.limits.max_groups == 5 and p.window.days_back == 3
    assert p.content.member_list is False and p.content.posts is True


def test_preset_private_and_audit(presets):
    assert presets["private_daily"].include_private is True
    assert presets["audit_evidence"].content.screenshot_evidence is True


def test_resolve_custom_profile():
    cfg = {"scan_profiles": {"tuychinh": {"description": "test", "limits": {"max_groups": 3}}}}
    prof = resolve_profile(cfg, "tuychinh")
    assert prof.limits.max_groups == 3 and prof.name == "tuychinh"


def test_resolve_builtin_profile():
    assert resolve_profile({}, "deep").name == "deep"


def test_save_profile_keeps_other_sections(tmp_path):
    """Ghi profile KHÔNG được xoá các section khác trong config.yaml."""
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "target_groups:\n  - id: '1'\n    url: u\n\ndiscovery:\n  regions: [Ha Noi]\n\n"
        "active_profile: standard\n",
        encoding="utf-8",
    )
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    save_profile(cfg, default_profiles()["quick"], str(path))

    after = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "quick" in after["scan_profiles"], after.keys()
    assert after["target_groups"][0]["id"] == "1", "phải giữ section target_groups"
    assert after["discovery"]["regions"] == ["Ha Noi"], "phải giữ discovery"


def test_save_profile_creates_backup(tmp_path):
    """Lần ghi đè phải chừa lại .bak vì yaml.safe_dump xoá hết comment."""
    path = tmp_path / "cfg.yaml"
    path.write_text("target_groups:\n  - id: '1'\n    url: u\n", encoding="utf-8")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    save_profile(cfg, default_profiles()["quick"], str(path))
    assert Path(str(path) + ".bak").exists(), "thiếu file .bak"


@pytest.fixture
def db(tmp_path):
    return tmp_path / "delta.db"


def _group() -> MonitoredGroup:
    g = MonitoredGroup(group_id="111", name="Nhom A", url="u",
                       privacy=GroupPrivacy.PUBLIC, member_count=1000)
    g.admins.append(AdminProfile(name="Admin 1", profile_url="facebook.com/a1", role="admin"))
    g.recent_posts.append(GroupPost(author_name="X", content="post 1"))
    return g


def test_snapshot_first_run_has_no_delta(db):
    g = _group()
    save_groups(db, [g])
    save_snapshot(db, g)
    assert diff_snapshots(db) == [], "lần đầu không được có delta"


def test_snapshot_detects_change(db):
    g = _group()
    save_groups(db, [g])
    save_snapshot(db, g)

    g.member_count = 1250
    g.admins.append(AdminProfile(name="Admin 2", profile_url="facebook.com/a2",
                                 role="moderator"))
    g.recent_posts.append(GroupPost(author_name="Y", content="post 2"))
    save_groups(db, [g])
    save_snapshot(db, g)

    notes = " ".join(n for c in diff_snapshots(db) for n in c["note"])
    assert "Thành viên: 1000 -> 1250" in notes, notes
    assert "Admin mới" in notes and "facebook.com/a2" in notes, notes


def test_saved_db_has_user_version(db):
    """Migration phải chạy: user_version phải bằng SCHEMA_VERSION."""
    import sqlite3

    from storage import SCHEMA_VERSION

    save_groups(db, [_group()])
    ver = sqlite3.connect(str(db)).execute("PRAGMA user_version").fetchone()[0]
    assert ver == SCHEMA_VERSION, f"user_version={ver}, cần {SCHEMA_VERSION}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
