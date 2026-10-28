"""Lưu trữ kết quả vào SQLite: idempotent, hỗ trợ lịch sử."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from models import MonitoredGroup

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    group_id TEXT PRIMARY KEY,
    name TEXT,
    url TEXT,
    privacy TEXT,
    member_count INTEGER,
    description TEXT,
    last_scanned TEXT,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT,
    fb_user_id TEXT,
    name TEXT,
    profile_url TEXT,
    role TEXT,
    is_verified INTEGER,
    phones TEXT,
    social_links TEXT,
    extracted_at TEXT,
    UNIQUE(group_id, profile_url)
);
CREATE INDEX IF NOT EXISTS idx_admins_group ON admins(group_id);
CREATE TABLE IF NOT EXISTS moderators (
    group_id TEXT,
    fb_user_id TEXT,
    name TEXT,
    profile_url TEXT,
    role TEXT,
    is_verified INTEGER,
    phones TEXT,
    social_links TEXT,
    extracted_at TEXT,
    UNIQUE(group_id, profile_url)
);
CREATE TABLE IF NOT EXISTS members (
    group_id TEXT,
    fb_user_id TEXT,
    name TEXT,
    profile_url TEXT,
    role TEXT,
    is_verified INTEGER,
    phones TEXT,
    social_links TEXT,
    extracted_at TEXT,
    UNIQUE(group_id, profile_url)
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT,
    scanned_at TEXT,
    member_count INTEGER,
    privacy TEXT,
    admins_json TEXT,
    posts_count INTEGER,
    posts_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_group ON snapshots(group_id, scanned_at);
CREATE TABLE IF NOT EXISTS persons (
    fb_user_id TEXT PRIMARY KEY,
    name TEXT,
    username TEXT,
    profile_url TEXT,
    phones TEXT,
    emails TEXT,
    social_links TEXT,
    gender TEXT,
    birthday TEXT,
    location TEXT,
    hometown TEXT,
    job TEXT,
    company TEXT,
    school TEXT,
    relationship TEXT,
    website TEXT,
    keywords TEXT,
    is_verified INTEGER,
    source_file TEXT,
    extracted_at TEXT,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_persons_name ON persons(name);
CREATE TABLE IF NOT EXISTS entity_clusters (
    cluster_id TEXT PRIMARY KEY,
    title TEXT,
    confidence TEXT,
    shared_signals TEXT,
    group_ids TEXT,
    profile_urls TEXT,
    member_count INTEGER,
    created_at TEXT,
    payload TEXT
);
"""


SCHEMA_VERSION = 2

# Nâng version + câu lệnh ALTER cho các bản nâng cấp tiếp theo.
# `CREATE TABLE IF NOT EXISTS` KHÔNG thêm cột mới vào bảng đã tồn tại, nên
# mọi thay đổi cấu trúc đều phải khai báo ở đây.
MIGRATIONS: Dict[int, List[str]] = {
    # 1 -> 2: index phục vụ people_union / truy vấn theo tên
    2: [
        "CREATE INDEX IF NOT EXISTS idx_admins_name ON admins(name)",
        "CREATE INDEX IF NOT EXISTS idx_moderators_group ON moderators(group_id)",
        "CREATE INDEX IF NOT EXISTS idx_members_group ON members(group_id)",
    ],
}


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Đưa DB lên SCHEMA_VERSION mới nhất, idempotent.

    Cài mới: executescript(SCHEMA) đã tạo đủ bảng, chỉ cần đánh dấu version.
    Nâng cấp: chạy các ALTER/INDEX còn thiếu theo thứ tự version.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version in range(current + 1, SCHEMA_VERSION + 1):
        for stmt in MIGRATIONS.get(version, []):
            conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {int(version)}")
    conn.commit()


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # busy_timeout: Streamlit chạy nhiều session cùng lúc, tránh "database is locked"
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
    # WAL cho phép đọc song song với ghi
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    _apply_migrations(conn)
    return conn


def save_group(db_path: Path, group: MonitoredGroup) -> None:
    conn = _connect(db_path)
    with conn:
        conn.execute(
            """INSERT INTO groups(group_id,name,url,privacy,member_count,description,last_scanned,payload)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(group_id) DO UPDATE SET
                 name=excluded.name, privacy=excluded.privacy,
                 member_count=excluded.member_count, description=excluded.description,
                 last_scanned=excluded.last_scanned, payload=excluded.payload""",
            (
                group.group_id, group.name, group.url, group.privacy.value,
                group.member_count, group.description,
                datetime.utcnow().isoformat(), group.model_dump_json(),
            ),
        )
        for bucket, plist in (
            ("admins", group.admins),
            ("moderators", group.moderators),
            ("members", group.members),
        ):
            for profile in plist:
                conn.execute(
                    f"""INSERT INTO {bucket}(group_id,fb_user_id,name,profile_url,role,is_verified,phones,social_links,extracted_at)
                       VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(group_id,profile_url) DO UPDATE SET
                         role=excluded.role, phones=excluded.phones,
                         social_links=excluded.social_links, extracted_at=excluded.extracted_at""",
                    (
                        group.group_id, profile.fb_user_id, profile.name,
                        profile.profile_url, profile.role, int(profile.is_verified),
                        json.dumps([p.model_dump() for p in profile.phones], ensure_ascii=False),
                        json.dumps([s.model_dump() for s in profile.social_links], ensure_ascii=False),
                        profile.extracted_at.isoformat(),
                    ),
                )
    conn.close()


def save_groups(db_path: Path, groups: Iterable[MonitoredGroup]) -> None:
    for g in groups:
        save_group(db_path, g)


def load_groups(db_path: Path) -> List[MonitoredGroup]:
    conn = _connect(db_path)
    cur = conn.execute("SELECT payload FROM groups ORDER BY last_scanned DESC")
    rows = cur.fetchall()
    conn.close()
    return [MonitoredGroup.model_validate_json(r[0]) for r in rows]


def query_admins(db_path: Path, group_id: Optional[str] = None) -> List[dict]:
    conn = _connect(db_path)
    if group_id:
        cur = conn.execute("SELECT * FROM admins WHERE group_id=?", (group_id,))
    else:
        cur = conn.execute("SELECT * FROM admins")
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return rows


def _stable_person_key(p) -> str:
    """Khoá ổn định cho tài khoản không lấy được FB ID.

    `hash()` của Python thay đổi theo tiến trình -> mỗi lần nạp lại sẽ
    tạo dòng MỚI thay vì cập nhật. Dùng hash mật mã để trùng thì hợp nhất.
    """
    if p.fb_user_id:
        return p.fb_user_id
    seed = (p.profile_url or p.username or p.name or "").strip().lower()
    if seed:
        return "noid_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    return "noid_" + hashlib.sha1(repr(sorted(p.model_dump().items())).encode("utf-8")).hexdigest()[:16]


def save_persons(db_path: Path, persons) -> None:
    conn = _connect(db_path)
    with conn:
        for p in persons:
            conn.execute(
                """INSERT INTO persons(fb_user_id,name,username,profile_url,phones,emails,
                       social_links,gender,birthday,location,hometown,job,company,school,
                       relationship,website,keywords,is_verified,source_file,extracted_at,payload)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(fb_user_id) DO UPDATE SET
                     name=excluded.name, username=excluded.username,
                     profile_url=excluded.profile_url, phones=excluded.phones,
                     emails=excluded.emails, social_links=excluded.social_links,
                     gender=excluded.gender, birthday=excluded.birthday,
                     location=excluded.location, hometown=excluded.hometown,
                     job=excluded.job, company=excluded.company, school=excluded.school,
                     relationship=excluded.relationship, website=excluded.website,
                     keywords=excluded.keywords, is_verified=excluded.is_verified,
                     source_file=excluded.source_file, extracted_at=excluded.extracted_at,
                     payload=excluded.payload""",
                (
                    _stable_person_key(p),
                    p.name, p.username, p.profile_url,
                    json.dumps([x.model_dump() for x in p.phones], ensure_ascii=False),
                    json.dumps(p.emails, ensure_ascii=False),
                    json.dumps([s.model_dump() for s in p.social_links], ensure_ascii=False),
                    p.gender, p.birthday, p.location, p.hometown, p.job, p.company,
                    p.school, p.relationship, p.website,
                    json.dumps(p.keywords, ensure_ascii=False),
                    int(p.is_verified),
                    p.source_file, p.extracted_at.isoformat(), p.model_dump_json(),
                ),
            )
    conn.close()


def load_persons(db_path: Path) -> List:
    from models import PersonProfile

    conn = _connect(db_path)
    cur = conn.execute("SELECT payload FROM persons ORDER BY extracted_at DESC")
    rows = cur.fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            out.append(PersonProfile.model_validate_json(r[0]))
        except Exception:  # noqa: BLE001
            continue
    return out


def save_snapshot(db_path: Path, group) -> None:
    """Lưu ảnh chụp trạng thái nhóm để lần sau so sánh (delta report)."""
    conn = _connect(db_path)
    with conn:
        conn.execute(
            """INSERT INTO snapshots(group_id, scanned_at, member_count, privacy,
                                     admins_json, posts_count, posts_json)
               VALUES(?,?,?,?,?,?,?)""",
            (
                group.group_id, datetime.utcnow().isoformat(), group.member_count,
                group.privacy.value,
                json.dumps([{"name": a.name, "url": a.profile_url, "role": a.role}
                            for a in list(group.admins) + list(group.moderators)
                            + list(group.members)],
                           ensure_ascii=False),
                len(group.recent_posts),
                json.dumps([p.content for p in group.recent_posts if p.content],
                           ensure_ascii=False)[:20000],
            ),
        )
    conn.close()


def latest_snapshots(db_path: Path) -> Dict[str, dict]:
    """Mỗi nhóm -> snapshot mới nhất + liền trước đó."""
    conn = _connect(db_path)
    cur = conn.execute(
        "SELECT group_id, scanned_at, member_count, privacy, admins_json, posts_count, posts_json "
        "FROM snapshots ORDER BY scanned_at DESC"
    )
    rows = cur.fetchall()
    conn.close()
    latest: Dict[str, List[dict]] = {}
    for r in rows:
        gid = r[0]
        latest.setdefault(gid, []).append(dict(zip(
            ("group_id", "scanned_at", "member_count", "privacy",
             "admins_json", "posts_count", "posts_json"), r)))
    out: Dict[str, dict] = {}
    for gid, snaps in latest.items():
        out[gid] = {
            "current": snaps[0],
            "previous": snaps[1] if len(snaps) > 1 else None,
        }
    return out


def group_history(db_path: Path) -> List[dict]:
    """Lịch sử các lần quét của mỗi nhóm (dùng cho biểu đồ theo thời gian)."""
    conn = _connect(db_path)
    cur = conn.execute(
        "SELECT group_id, scanned_at, member_count, "
        "json_array_length(admins_json) AS n_admins, posts_count "
        "FROM snapshots ORDER BY group_id, scanned_at"
    )
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    for r in rows:
        # json_array_length đã trả số nguyên
        try:
            r["n_admins"] = int(r["n_admins"] or 0)
        except (TypeError, ValueError):
            r["n_admins"] = 0
    return rows


def people_union(db_path: Path) -> List[dict]:
    """Danh sách người đã biết của 1 nhóm, gộp từ cả 3 bảng + khử trùng."""
    conn = _connect(db_path)
    out: List[dict] = []
    for table, bucket in (("admins", "admin"), ("moderators", "moderator"),
                          ("members", "member")):
        cur = conn.execute(
            f"SELECT group_id, name, profile_url, role, phones, social_links "
            f"FROM {table}"
        )
        for r in cur.fetchall():
            out.append({
                "group_id": r[0], "name": r[1], "profile_url": r[2],
                "role": r[3] or bucket,
                "phones": r[4] or "[]", "social_links": r[5] or "[]",
            })
    conn.close()
    # khử trùng theo (group_id, profile_url), ưu tiên vai trò mạnh hơn
    rank = {"admin": 3, "moderator": 2, "unknown": 1, "member": 0}
    best: Dict[tuple, dict] = {}
    for row in out:
        key = (row["group_id"], row["profile_url"])
        cur_row = best.get(key)
        if cur_row is None or rank.get(row["role"], 1) > rank.get(cur_row["role"], 1):
            if cur_row and not row["name"]:
                row["name"] = cur_row["name"]
            best[key] = row

    # khử trùng theo tên (Facebook dùng cả ID số lẫn username cho 1 người)
    def _name_key(v: Optional[str]) -> str:
        return re.sub(r"\s+", " ", (v or "").strip().lower())

    def _has_uid(v: Optional[str]) -> bool:
        return bool(re.search(r"profile\.php\?id=\d+", v or ""))

    by_name: Dict[tuple, dict] = {}
    final: List[dict] = []
    for row in best.values():
        key = (row["group_id"], _name_key(row["name"]))
        if len(key[1]) < 3:
            final.append(row)
            continue
        prev = by_name.get(key)
        if prev is None:
            by_name[key] = row
            final.append(row)
            continue
        # giữ bản có ID số, ưu tiên vai trò mạnh hơn
        if _has_uid(row["profile_url"]) and not _has_uid(prev["profile_url"]):
            final[final.index(prev)] = row
            by_name[key] = row
        elif not _has_uid(row["profile_url"]) and _has_uid(prev["profile_url"]):
            continue
        elif rank.get(row["role"], 1) > rank.get(prev["role"], 1):
            final[final.index(prev)] = row
            by_name[key] = row
    return final


def diff_snapshots(db_path: Path) -> List[dict]:
    """So sánh snapshot mới nhất vs liền trước -> danh sách thay đổi."""
    changes: List[dict] = []
    for gid, pair in latest_snapshots(db_path).items():
        cur, prev = pair["current"], pair["previous"]
        if not prev:
            continue
        entry: dict = {"group_id": gid, "note": []}
        if (cur["member_count"] or 0) != (prev["member_count"] or 0):
            entry["note"].append(
                f"Thành viên: {prev['member_count']} -> {cur['member_count']}"
            )
        try:
            admins_old = {a["url"] for a in json.loads(prev["admins_json"] or "[]")}
            admins_new = {a["url"] for a in json.loads(cur["admins_json"] or "[]")}
            if admins_new - admins_old:
                entry["note"].append(f"Admin mới: {sorted(admins_new - admins_old)}")
            if admins_old - admins_new:
                entry["note"].append(f"Admin rời: {sorted(admins_old - admins_new)}")
        except (ValueError, KeyError):
            pass
        if cur["posts_count"] != prev["posts_count"]:
            entry["note"].append(
                f"Số bài public: {prev['posts_count']} -> {cur['posts_count']}"
            )
        if entry["note"]:
            changes.append(entry)
    return changes


def query_persons(db_path: Path, keyword: str = "") -> List[dict]:
    conn = _connect(db_path)
    if keyword:
        cur = conn.execute(
            "SELECT name,username,profile_url,phones,emails,social_links,is_verified,source_file,extracted_at "
            "FROM persons WHERE name LIKE ? OR username LIKE ?",
            (f"%{keyword}%", f"%{keyword}%"),
        )
    else:
        cur = conn.execute(
            "SELECT name,username,profile_url,phones,emails,social_links,is_verified,source_file,extracted_at "
            "FROM persons"
        )
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return rows
