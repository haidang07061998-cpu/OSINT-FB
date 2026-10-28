"""Entity linking OFFLINE: ghép các tài khoản có cùng chủ thể.

Nguyên tắc: CHỈ dùng dữ liệu đã thu thập, không gửi thêm request nào.
Tín hiệu chắc chắn (trùng => cùng người):
  1. Cùng profile FB canonical (id hoặc username)
  2. Cùng số điện thoại (đã chuẩn hoá E.164)
  3. Cùng email
  4. Cùng social handle (FB/IG/TikTok/Telegram...)

Không gộp theo "tên giống nhau" để tránh báo cáo sai.

Lưu ý pháp lý: kết quả là SUY LUẬN KỸ THUẬT từ dữ liệu công khai, không
phải xác nhhận pháp lý danh tính. Dùng làm manh mối OSINT, cần xác minh
thêm trước khi kết luận về một cá nhân cụ thể.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from models import EntityCluster, IdentityRecord

from .utils import get_logger

logger = get_logger("entities")

# Trọng số tín hiệu -> mức tin cậy
_WEIGHT: Dict[str, int] = {
    "fb": 3,      # cùng profile FB
    "phone": 3,   # cùng SĐT
    "email": 3,   # cùng email
    "social": 2,  # cùng handle mạng xã hội
}


# ----------------- Normalizers -----------------
_NON_PROFILE_PATHS = {
    "groups", "pages", "pg", "people", "story", "watch", "photo", "stories",
    "events", "marketplace", "profile.php", "login", "reel", "search",
}


def canonical_fb(url: Optional[str]) -> Optional[str]:
    """'https://www.facebook.com/nguyena?ref=x' -> 'nguyena' ; id -> giữ nguyên.

    Trả về None cho URL KHÔNG phải profile (nhóm, page, post...).
    """
    if not url:
        return None
    if not re.match(r"(?i)^[a-z]+://", url):
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    host = re.sub(r"^(www|m|web|mbasic|basic)\.", "", host)
    if not (host == "facebook.com" or host == "fb.com"
            or host.endswith(".facebook.com") or host.endswith(".fb.com")):
        return None
    # /profile.php?id=123
    m = re.search(r"(?i)\bid=(\d{5,})", parsed.query)
    if m:
        return m.group(1)
    path = parsed.path.strip("/")
    if not path:
        return None
    first = path.split("/")[0].lower()
    if first in _NON_PROFILE_PATHS:
        return None
    return first


def _social_key(platform: str, handle: Optional[str], url: Optional[str]) -> Optional[str]:
    plat = (platform or "").strip().lower()
    h = (handle or "").strip().lower().lstrip("@")
    if not h and url:
        path = re.sub(r"https?://[^/]+", "", url).strip("/")
        parts = [p for p in path.split("/") if p]
        if parts:
            h = parts[-1].lower().lstrip("@")
    if not h or h in {"profile.php", "pages"}:
        return None
    return f"{plat}|{h}"


# -------------------------- DSU --------------------------
class _DSU:
    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}

    def add(self, x: str) -> None:
        self.parent.setdefault(x, x)

    def find(self, x: str) -> str:
        self.add(x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


# ----------------- Signals -----------------
def record_signals(rec: IdentityRecord) -> List[Tuple[str, str]]:
    """Trả về [(signal_type, signal_value), ...] cho 1 bản ghi."""
    sigs: List[Tuple[str, str]] = []
    fb = canonical_fb(rec.profile_url)
    if fb:
        sigs.append(("fb", fb))
    elif rec.fb_user_id:
        sigs.append(("fb", str(rec.fb_user_id)))
    for p in rec.phones:
        if p:
            sigs.append(("phone", p))
    for e in rec.emails:
        e2 = e.strip().lower()
        if e2:
            sigs.append(("email", e2))
    for s in rec.socials:
        if "|" in s:
            plat, handle = s.split("|", 1)
            key = _social_key(plat, handle, None) or s.lower()
            sigs.append(("social", key))
    return sigs


# ---------------------- Loader ----------------------
def load_records_from_db(db_path: Path) -> List[IdentityRecord]:
    """Lấy mọi bản ghi admin/mod + person từ DB làm danh sị danh tính."""
    from storage import query_admins, query_persons

    records: List[IdentityRecord] = []
    for row in query_admins(db_path):
        try:
            phones = [p.get("normalized") for p in json.loads(row.get("phones") or "[]")
                      if p.get("normalized")]
            socials_raw = json.loads(row.get("social_links") or "[]")
        except (ValueError, TypeError):
            phones, socials_raw = [], []
        socials = [
            _social_key(s.get("platform"), s.get("handle"), s.get("url"))
            for s in socials_raw
        ]
        socials = [s for s in socials if s]
        records.append(IdentityRecord(
            key=f"admin:{row.get('group_id')}:{row.get('profile_url')}",
            kind=row.get("role") or "unknown",
            name=row.get("name"),
            profile_url=row.get("profile_url"),
            fb_user_id=row.get("fb_user_id"),
            phones=phones,
            socials=socials,
            group_ids=[row["group_id"]] if row.get("group_id") else [],
            verified=bool(row.get("is_verified")),
            source="admins",
        ))

    for row in query_persons(db_path):
        try:
            phones = [p.get("normalized") for p in json.loads(row.get("phones") or "[]")
                      if p.get("normalized")]
            emails = json.loads(row.get("emails") or "[]")
            socials_raw = json.loads(row.get("social_links") or "[]")
        except (ValueError, TypeError):
            phones, emails, socials_raw = [], [], []
        socials = [
            _social_key(s.get("platform"), s.get("handle"), s.get("url"))
            for s in socials_raw
        ]
        socials = [s for s in socials if s]
        records.append(IdentityRecord(
            key=f"person:{row.get('profile_url') or row.get('name')}",
            kind="person",
            name=row.get("name"),
            profile_url=row.get("profile_url"),
            phones=phones,
            emails=[e for e in emails if e],
            socials=socials,
            verified=bool(row.get("is_verified")),
            source=row.get("source_file") or "persons",
        ))
    return records


# ---------------------- Linker ----------------------
def link_identities(records: Iterable[IdentityRecord]) -> List[EntityCluster]:
    """Gom record thành cụm dựa trên tín hiệu trùng (>=2 thành viên)."""
    records = list(records)
    if not records:
        return []

    dsu = _DSU()
    owners: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    rec_by_key: Dict[str, IdentityRecord] = {}

    for rec in records:
        dsu.add(rec.key)
        rec_by_key[rec.key] = rec
        seen = set()
        for sig in record_signals(rec):
            if sig in seen:
                continue
            seen.add(sig)
            owners[sig].append(rec.key)

    # Ghi lại signal nào gộp cụm nào (để báo cáo)
    sigs_by_root: Dict[str, List[Tuple[str, str]]] = defaultdict(list)

    for (stype, sval), keys in owners.items():
        uniq = []
        for k in keys:
            if k not in uniq:
                uniq.append(k)
        if len(uniq) < 2:
            continue
        root = dsu.find(uniq[0])
        for k in uniq[1:]:
            dsu.union(k, uniq[0])
            root2 = dsu.find(k)
            sigs_by_root.setdefault(root2, [])
        sigs_by_root[root].append((stype, sval))
        # sau union, root có thể đổi; chuẩn hoá lại sau mỗi vòng union
        new_root = dsu.find(uniq[0])
        if new_root != root:
            sigs_by_root[new_root].extend(sigs_by_root.pop(root, []))
            root = new_root

    grouped: Dict[str, List[IdentityRecord]] = defaultdict(list)
    for rec in records:
        grouped[dsu.find(rec.key)].append(rec)

    clusters: List[EntityCluster] = []
    idx = 0
    for root, members in grouped.items():
        if len(members) < 2:
            continue
        idx += 1
        sigs = [(t, v) for (t, v) in sigs_by_root.get(root, [])]
        used_types = {t for t, _ in sigs}
        confidence = (
            "certain" if used_types & {"fb", "phone", "email"}
            else "high" if used_types & {"social"}
            else "medium"
        )
        title = _pick_title(members)
        group_ids = sorted({g for m in members for g in m.group_ids})
        clusters.append(EntityCluster(
            cluster_id=f"E{idx:03d}",
            title=title,
            confidence=confidence,
            shared_signals=[f"{t}:{v}" for t, v in sigs],
            members=members,
            group_ids=group_ids,
        ))
    clusters.sort(key=lambda c: (-len(c.members), c.cluster_id))
    # Gán lại mã cụm theo NỘI DUNG, không theo thứ tự.
    # Trước đây mã là E001, E002... theo vị trí, nên chỉ cần dữ liệu đổi thứ tự
    # là "E002" của tháng trước âm thầm biến thành cụm người khác, còn mã cũ
    # thì vẫn còn trong DB -> báo cáo sai mà không có báo lỗi nào.
    for c in clusters:
        c.cluster_id = _stable_cluster_id(c)
    clusters.sort(key=lambda c: c.cluster_id)
    logger.info("Gom được %d cụm danh tính từ %d bản ghi", len(clusters), len(records))
    return clusters


def _stable_cluster_id(cluster: EntityCluster) -> str:
    """Mã cụm bám vào tập tài khoản + tín hiệu chung.

    Cùng một cụm người thì luôn ra cùng mã, kể cả khi thứ tự bản ghi thay đổi
    hay có cụm mới chen vào giữa.
    """
    seed = "|".join([
        ",".join(sorted(m.key for m in cluster.members)),
        ",".join(sorted(cluster.shared_signals)),
    ])
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8].upper()
    return f"C-{digest}"


def _pick_title(members: List[IdentityRecord]) -> str:
    """Tên phổ biến nhất trong cụm."""
    names = [m.name for m in members if m.name]
    if names:
        return Counter(names).most_common(1)[0][0]
    return members[0].profile_url or members[0].key


# ---------------------- Export ----------------------
def export_entities_csv(clusters: List[EntityCluster], out_path: Path) -> Path:
    import csv

    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [_cluster_row(c) for c in clusters]
    fields = list(rows[0].keys()) if rows else [
        "Mã cụm", "Tên hiển thị", "Độ tin cậy", "Số tài khoản",
        "Tín hiệu liên kết", "Các tài khoản", "Vai trò", "Nhóm liên quan",
        "SĐT liên kết", "Social liên kết", "Tích xanh", "Cập nhật",
    ]
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return out_path


def _cluster_row(c: EntityCluster) -> dict:
    phones = sorted({p for m in c.members for p in m.phones})
    socials = sorted({s for m in c.members for s in m.socials})
    emails = sorted({e for m in c.members for e in m.emails})
    roles = sorted({m.kind for m in c.members})
    return {
        "Mã cụm": c.cluster_id,
        "Tên hiển thị": c.title,
        "Độ tin cậy": {"certain": "Chắc chắn", "high": "Cao",
                       "medium": "Trung bình"}.get(c.confidence, c.confidence),
        "Số tài khoản": len(c.members),
        "Tín hiệu liên kết": "; ".join(c.shared_signals[:8]),
        "Các tài khoản": "; ".join(m.profile_url or m.name or "" for m in c.members),
        "Vai trò": ", ".join(roles),
        "Nhóm liên quan": ", ".join(c.group_ids),
        "SĐT liên kết": "; ".join(phones),
        "Email liên kết": "; ".join(emails),
        "Social liên kết": "; ".join(socials),
        "Tích xanh": any(m.verified for m in c.members),
        "Cập nhật": c.created_at.isoformat(),
    }


def export_entities_workbook(clusters: List[EntityCluster], out_path: Path) -> Path:
    import pandas as pd

    out_path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([_cluster_row(c) for c in clusters])
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Cum danh tinh", index=False)
        ws = writer.sheets["Cum danh tinh"]
        for col in ws.columns:
            width = max((len(str(c2.value)) for c2 in col if c2.value), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(width + 2, 70)
    return out_path


# ---------------------- Save + CLI ----------------------
def save_clusters(db_path: Path, clusters: List[EntityCluster]) -> None:
    """Ghi lại toàn bộ cụm danh tính, thay thế nội dung lần trước.

    Bảng `entity_clusters` là ảnh chụp của lần gộp HIỆN TẠI, không phải lịch sử.
    Trước đây chỉ upsert theo mã cụm nên các cụm đã tan rã ở lần trước vẫn nằm
    lại trong bảng và dashboard hiển thị như còn hiệu lực.
    """
    from storage import _connect

    conn = _connect(db_path)
    with conn:
        conn.execute("DELETE FROM entity_clusters")
        for c in clusters:
            conn.execute(
                """INSERT INTO entity_clusters(cluster_id,title,confidence,shared_signals,
                        group_ids,profile_urls,member_count,created_at,payload)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    c.cluster_id, c.title, c.confidence,
                    json.dumps(c.shared_signals, ensure_ascii=False),
                    json.dumps(c.group_ids, ensure_ascii=False),
                    json.dumps([m.profile_url for m in c.members], ensure_ascii=False),
                    len(c.members), c.created_at.isoformat(), c.model_dump_json(),
                ),
            )
    conn.close()


def run_entities(cfg_path: str = "config.yaml") -> List[EntityCluster]:
    """CLI entry: ghép danh tính + lưu DB + xuất báo cáo."""
    from pipeline import load_config

    cfg = load_config(cfg_path)
    db = Path(cfg.get("scraper", {}).get("data_dir", "data")) / "monitor.db"
    records = load_records_from_db(db)
    if not records:
        logger.warning("DB chưa có tài khoản nào. Hãy chạy `run` hoặc `import` trước.")
        return []
    clusters = link_identities(records)
    save_clusters(db, clusters)
    if clusters:
        # Đọc report_dir từ config thay vì ghi cứng vào "reports".
        rdir = Path(cfg.get("scraper", {}).get("report_dir", "reports"))
        rdir.mkdir(parents=True, exist_ok=True)
        export_entities_csv(clusters, rdir / "cum_danh_tinh.csv")
        export_entities_workbook(clusters, rdir / "cum_danh_tinh.xlsx")
        logger.info(
            "Gộp %d cụm | CSV: %s | Excel: %s",
            len(clusters),
            rdir / "cum_danh_tinh.csv",
            rdir / "cum_danh_tinh.xlsx",
        )
        for c in clusters[:10]:
            logger.info(
                "  %s (%s, %d tài khoản) <- %s",
                c.title, c.confidence, len(c.members), ", ".join(c.shared_signals[:3]),
            )
    else:
        logger.info("Không có tài khoản nào liên kết được với nhau.")
    return clusters
