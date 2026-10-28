"""GIAO DIỆN ĐẦY ĐỦ - nhập link, chọn mục, bấm chạy, xem kết quả.

Chạy:  streamlit run dashboard.py
Toàn bộ thao tác trên trình duyệt, không cần gõ lệnh.
"""
from __future__ import annotations

import io
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from typing import Dict, List

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))

import yaml  # noqa: E402

from exporter import export_workbook, _ROLE_VN  # noqa: E402
from scraper.log_capture import SCAN_LOCK, ScanSession  # noqa: E402
from scraper.safe_load import reset_seen, safe_load  # noqa: E402
from models import (  # noqa: E402
    ContentSelection,
    MonitoredGroup,
    ScanLimits,
    ScanProfile,
    TimeWindow,
)
from scraper.manual_importer import (  # noqa: E402
    _merge_group,
    importer_state,
    import_uploads,
)
from scraper.scan_profile import list_profiles, save_profile  # noqa: E402
from storage import load_groups, load_persons  # noqa: E402
from pipeline import build_client, enrich_admins, finalize, process_targets  # noqa: E402

# Logger của module. Trước đây `_clear_all_data` gọi `logger.warning(...)` mà
# chưa từng định nghĩa `logger`, nên nhánh "từ chối xoá ngoài thư mục dự án"
# sẽ ném NameError — đúng lúc cần bảo vệ dữ liệu nhất.
logger = logging.getLogger("osint.dashboard")

st.set_page_config(
    page_title="OSINT Group Monitor",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Đường dẫn neo theo VỊ TRÍ FILE SCRIPT, không theo thư mục chạy.
# Trước đây dùng đường dẫn tương đối nên chạy `streamlit run` từ thư mục
# khác sẽ đọc/ghi/xoá nhầm dữ liệu của dự án khác.
BASE_DIR = Path(__file__).resolve().parent
CFG_PATH = BASE_DIR / "config.yaml"
# Thư mục báo cáo lấy từ config qua _reports_dir(), không hardcode ở đây.
DUMP_DIR = BASE_DIR / "html_dump"


# ------------------------- helpers -------------------------
def _load_cfg() -> dict:
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _data_dir() -> Path:
    cfg = _load_cfg()
    return BASE_DIR / cfg.get("scraper", {}).get("data_dir", "data")


def _db_path() -> Path:
    return _data_dir() / "monitor.db"


def _reports_dir() -> Path:
    cfg = _load_cfg()
    return BASE_DIR / cfg.get("scraper", {}).get("report_dir", "reports")


def _is_inside_project(path: Path) -> bool:
    """Đường dẫn có nằm trong thư mục dự án không (chống xoá nhầm)."""
    try:
        Path(path).resolve().relative_to(BASE_DIR.resolve())
        return True
    except (ValueError, OSError):
        return False


def _parse_targets(text: str) -> list:
    """Mỗi dòng: link nhóm hoặc ID. Trả về [{'id','url','note'}]."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.search(r"(?i)groups/(\d{6,})", line) or re.search(r"(?i)\bid=(\d{6,})", line)
        gid = m.group(1) if m else (line if line.isdigit() else None)
        if not gid:
            continue
        url = line if line.startswith("http") else f"https://www.facebook.com/groups/{gid}"
        out.append({"id": gid, "url": url, "note": "nhập từ dashboard"})
    # khử trùng
    seen, uniq = set(), []
    for t in out:
        if t["id"] not in seen:
            seen.add(t["id"])
            uniq.append(t)
    return uniq


# Thu log theo từng phiên nằm ở scraper/log_capture.py để test được ngoài
# Streamlit (dashboard.py gọi st.set_page_config() ngay ở cấp module).
_SCAN_LOCK = SCAN_LOCK


# ------------------------- sidebar -------------------------
def sidebar_input(cfg: dict):
    st.sidebar.title("🎛️ Bảng điều khiển")

    with st.sidebar.expander("① Nhóm mục tiêu", expanded=True):
        links_text = st.text_area(
            "Dán link/ID nhóm (mỗi dòng 1 nhóm)",
            height=120,
            placeholder="https://www.facebook.com/groups/123456789\nhttps://www.facebook.com/groups/987654321",
            key="links_text",
        )
        quick = "\n".join(
            f"{t['url']}" for t in (cfg.get("target_groups") or [])
            if str(t.get("id")) != "123456789"
        )
        if quick.strip() and st.button("↺ Nạp lại từ config.yaml"):
            st.session_state["links_text"] = quick
            st.rerun()

    with st.sidebar.expander("② Quét những gì", expanded=True):
        profiles = list_profiles(cfg)
        names = list(profiles)
        active = cfg.get("active_profile", "standard")
        profile_name = st.selectbox(
            "Kiểu quét", names,
            index=names.index(active) if active in names else 0,
            key="profile_name",
            format_func=lambda n: f"{n} — {profiles[n].description[:48]}",
        )
        base = profiles[profile_name]

        if st.checkbox("Tùy chỉnh chi tiết", key="customize"):
            c = base.content
            with st.container():
                st.caption("Số lượng")
                max_groups = st.number_input("Nhóm / lượt", 1, 30, base.limits.max_groups)
                max_accounts = st.number_input("TK admin / nhóm", 1, 20, base.limits.max_accounts)
                max_members = st.number_input("Thành viên / nhóm (0=bỏ)", 0, 100,
                                              base.limits.max_members_per_group)
                st.caption("Bài đăng")
                max_posts = st.number_input("Bài / nhóm", 1, 100, base.window.max_posts)
                days_back = st.number_input("Chỉ lấy bài trong (ngày) — 0 = tất cả",
                                            0, 365, base.window.days_back)
                st.caption("Nội dung")
                checks = {
                    "group_meta": st.checkbox("Meta nhóm (tên/privacy/mô tả)", c.group_meta),
                    "member_count": st.checkbox("Số thành viên + biến động", c.member_count),
                    "member_list": st.checkbox("Danh sách thành viên", c.member_list),
                    "new_members": st.checkbox("Thành viên mới", c.new_members),
                    "admins": st.checkbox("Admin + kiểm duyệt", c.admins),
                    "admin_contacts": st.checkbox("★ Liên hệ admin (SĐT/social/email)", c.admin_contacts),
                    "admin_bio": st.checkbox("Bio admin, tích xanh", c.admin_bio),
                    "posts": st.checkbox("Bài đăng", c.posts),
                    "post_reactions": st.checkbox("Lượt thích/bình luận/chia sẻ", c.post_reactions),
                    "post_media": st.checkbox("Link ảnh/video trong bài", c.post_media),
                    "comments": st.checkbox("Nội dung bình luận", c.comments),
                    "activity_stats": st.checkbox("Thống kê hoạt động", c.activity_stats),
                    "keywords": st.checkbox("Từ khoá nổi bật", c.keywords),
                    "link_verify": st.checkbox("Kiểm tra link còn sống (chủ động)", c.link_verify),
                    "screenshot": st.checkbox("Chụp màn hình bằng chứng", c.screenshot_evidence),
                }
                rate = st.number_input("Giãn cách request (giây)", 0.5, 30.0,
                                       float(base.rate_limit_seconds))
                include_private = st.checkbox("Bao gồm nhóm riêng tư", base.include_private)

            profile = ScanProfile(
                name="tuychinh_ui",
                description="Tùy chỉnh từ dashboard",
                content=ContentSelection(**{k: v for k, v in checks.items()
                                            if k != "screenshot"}),
                window=TimeWindow(days_back=int(days_back), max_posts=int(max_posts)),
                limits=ScanLimits(
                    max_groups=int(max_groups), max_accounts=int(max_accounts),
                    max_admins_per_group=int(max_accounts),
                    max_members_per_group=int(max_members),
                    max_posts_per_group=int(max_posts),
                ),
                rate_limit_seconds=float(rate),
                include_private=include_private,
            )
            if checks.get("screenshot"):
                profile.content.screenshot_evidence = True
            if st.button("💾 Lưu profile tùy chỉnh"):
                save_profile(_load_cfg(), profile, CFG_PATH)
                st.success(f"Đã lưu profile '{profile.name}'")
        else:
            profile = base

    with st.sidebar.expander("③ Lấy dữ liệu từ Facebook", expanded=True):
        st.caption(
            "Facebook bắt buộc đăng nhập cho mọi nhóm (kể cả công khai) "
            "nên phải lấy dữ liệu từ trình duyệt của bạn."
        )
        mode = st.radio(
            "Cách lấy dữ liệu",
            ["📋 Script (khuyên dùng)", "💾 Tải file HTML lên"],
            horizontal=True,
        )

        if mode.startswith("📋"):
            st.markdown(
                "1. Mở nhóm trên Facebook *(đã đăng nhập)*\n"
                "2. `F12` → tab **Console**\n"
                "3. Mở file `browser\\export_group.js`, **copy toàn bộ** rồi dán vào Console → Enter\n"
                "4. Trình duyệt tự tải file `group_<id>_<timestamp>.json`\n"
                "5. Bấm **📂 Nạp html_dump** bên dưới"
            )
            js_path = Path("browser") / "export_group.js"
            if js_path.exists():
                st.download_button(
                    "⬇️ Tải script export_group.js",
                    js_path.read_text(encoding="utf-8"),
                    "export_group.js",
                    "text/javascript",
                )
        else:
            st.caption(
                "**Cách lưu:**\n\n"
                "Nhấn **`Ctrl+Shift+S`** (luôn mở hộp thoại) → chọn *Webpage, "
                "HTML Only* → lưu vào thư mục.\n\n"
                "*(`Ctrl+S` có thể lưu thẳng vào **Downloads** không hỏi — "
                "khi đó chỉ cần dán đường dẫn Downloads vào ô thư mục ở dưới.)*\n\n"
                "**Để lấy đủ dữ liệu, lưu 2 lần:**\n"
                "1. Tab *Mọi người*, cuộn hết thành viên → lưu\n"
                "2. Bấm *Xem tất cả* ở mục Quản trị viên → lưu lần 2\n\n"
                "Tên file **tùy ý** — hệ thống tự nhận diện nhóm từ nội dung trang, "
                "gộp dồn và loại trùng."
            )

        uploaded = st.file_uploader(
            "Kéo thả file .html hoặc .json vào đây",
            type=["html", "htm", "json"], accept_multiple_files=True,
        )
        if uploaded:
            st.success(f"Đã chọn {len(uploaded)} file — bấm **📂 Nạp dữ liệu** để phân tích.")

    keep_files = st.sidebar.checkbox(
            "Giữ lại file đã nạp (để kiểm tra/chẩn đoán)",
            value=False,
            key="keep_files",
            help="Bật khi muốn kỹ thuật viên xem cấu trúc HTML thật.",
        )

    st.sidebar.markdown("---")
    col1, col2 = st.sidebar.columns(2)
    run_clicked = col1.button("▶️ CHẠY PHÂN TÍCH", type="primary", width='stretch')
    import_clicked = col2.button("📂 Nạp dữ liệu", width='stretch')

    with st.sidebar.expander("🗑️ Quản lý dữ liệu", expanded=False):
        st.caption("Xoá toàn bộ dữ liệu đã lưu: cơ sở dữ liệu SQLite và "
                   "các báo cáo trong thư mục `reports/`. Không thể hoàn tác.")
        clear_clicked = st.button("🗑️ XOÁ TOÀN BỘ DỮ LIỆU", type="secondary")

    return (
        profile, links_text, run_clicked, import_clicked,
        uploaded, clear_clicked, keep_files,
    )


# ------------------------- run actions -------------------------
def _run_scan(targets: list, profile: ScanProfile):
    cfg = _load_cfg()
    cfg["active_profile"] = profile.name
    log_box = st.empty()

    if not _SCAN_LOCK.acquire(blocking=False):
        st.warning("Đang có một lần quét khác chạy. Chờ xong rồi thử lại.")
        return

    prog = st.progress(0, text="Đang khởi động…")
    t0 = time.time()
    try:
        with ScanSession() as session:
            def _cb(done, total, msg):
                prog.progress(min(0.95, done / max(1, total)), text=msg)
                log_box.code(session.render(25), height=260)
                time.sleep(0)

            # Một client cho cả 2 pha: trước đây mỗi hàm tự tạo client riêng,
            # nghĩa là 2 bộ đếm rate-limit và 2 lần khởi tạo session.
            client = build_client(cfg, profile)

            prog.progress(0.05, text="Đang quét nhóm…")
            groups = process_targets(cfg, targets, profile, progress_cb=_cb,
                                     client=client)
            prog.progress(0.6, text="Đang soi tài khoản quản trị…")
            groups = enrich_admins(cfg, groups, profile, progress_cb=_cb, client=client)
            prog.progress(0.9, text="Đang lưu & xuất báo cáo…")
            finalize(cfg, groups)
            prog.progress(1.0, text=f"Xong trong {time.time()-t0:.0f}s")
            log_box.code(session.render(25), height=260)
            st.success(f"Hoàn tất: {len(groups)} nhóm trong {time.time()-t0:.0f}s")
    except SystemExit as exc:
        st.error(str(exc))
    except Exception as exc:  # noqa: BLE001
        st.error(f"Lỗi: {exc}")
        import traceback
        st.code(traceback.format_exc()[-1500:])
    finally:
        _SCAN_LOCK.release()
    reset_seen()
    _LOAD_ERRORS.clear()
    st.cache_data.clear()


def _clear_all_data(notice=None):
    """Xoá toàn bộ dữ liệu đã lưu: CSDL, báo cáo, cache.

    Chỉ xoá trong đúng thư mục dự án (được kiểm tra bằng `_is_inside_project`)
    để tránh xoá nhầm thư mục `reports/` không liên quan.
    """
    import shutil

    def _say(msg):
        if notice is not None:
            notice.info(msg)

    _say("Đang xoá dữ liệu...")
    db = _db_path()
    removed: List[str] = []
    if db.exists():
        db.unlink()
        removed.append(str(db))
    # SQLite ở chế độ WAL còn 2 file phụ. Chỉ xoá `.db` mà để lại `-wal` thì
    # lần mở DB kế tiếp có thể phát lại WAL và làm sống lại đúng dữ liệu vừa
    # xoá (kể cả SĐT) — người dùng tưởng đã xoá sạch.
    for sidecar in (db.with_name(db.name + "-wal"),
                    db.with_name(db.name + "-shm")):
        if sidecar.exists():
            try:
                sidecar.unlink()
                removed.append(str(sidecar))
            except OSError as exc:
                logger.warning("Không xoá được %s: %s", sidecar, exc)
    reports = _reports_dir()
    if reports.is_dir():
        if _is_inside_project(reports):
            shutil.rmtree(reports, ignore_errors=False)
            removed.append(str(reports))
        else:
            # KHÔNG xoá nếu đường dẫn nằm ngoài thư mục dự án.
            if notice is not None:
                notice.warning(
                    f"Bỏ qua xoá báo cáo: `{reports}` nằm ngoài thư mục dự án."
                )
            logger.warning("Từ chối xoá ngoài phạm vi dự án: %s", reports)
    # xoá cache hiển thị để dữ liệu mới hiện ngay
    try:
        st.cache_data.clear()
    except Exception:  # noqa: BLE001
        pass
    reset_seen()
    _LOAD_ERRORS.clear()
    st.session_state.pop("last_targets", None)

    if removed:
        if notice is not None:
            notice.success(
                f"Đã xoá {len(removed)} mục: cơ sở dữ liệu và toàn bộ báo cáo trong `reports/`."
            )
    else:
        if notice is not None:
            notice.info("Chưa có dữ liệu nào để xoá.")


def _run_import(uploaded: List, notice=None, keep_files: bool = False):
    """Xử lý các file người dùng vừa tải lên.

    keep_files=True -> lưu bản sao vào html_dump/ để kiểm tra cấu trúc HTML.
    """
    def _say(kind, msg):
        if notice is not None:
            getattr(notice, kind)(msg)

    if not uploaded:
        _say("warning", "Chưa chọn file nào. Kéo thả file .html hoặc .json vào ô ③ ở trên rồi bấm lại nút này.")
        return

    # Lưu bản sao nếu được yêu cầu (để chẩn đoán)
    if keep_files:
        try:
            DUMP_DIR.mkdir(exist_ok=True)
            for f in uploaded:
                (DUMP_DIR / Path(f.name).name).write_bytes(f.read())
            _say("info", f"Đã lưu {len(uploaded)} file vào `{DUMP_DIR}` để kiểm tra.")
        except Exception as exc:  # noqa: BLE001
            _say("warning", f"Không lưu được file: {exc}")

    prog = st.progress(0.1, text="Đang đọc & phân tích...")
    all_groups: Dict[str, MonitoredGroup] = {}
    persons: List = []
    with st.spinner("Đang bóc tách dữ liệu..."):
        groups = import_uploads(uploaded)
        persons.extend(importer_state.get("persons", []))
        for g in groups:
            if g.group_id in all_groups:
                _merge_group(all_groups[g.group_id], g)
            else:
                all_groups[g.group_id] = g
        groups = list(all_groups.values())

        prog.progress(0.8, text="Đang lưu & xuất báo cáo...")
        if groups or persons:
            cfg = _load_cfg()
            from scraper.entity_linker import link_identities, load_records_from_db

            db = finalize(cfg, groups, persons)
            clusters = link_identities(load_records_from_db(db))
            prog.progress(1.0, text="Xong")
            n_adm = sum(len(g.admins) for g in groups)
            n_mod = sum(len(g.moderators) for g in groups)
            n_mem = sum(len(g.members) for g in groups)
            _say(
                "success",
                f"**Xong** — {len(uploaded)} file · {len(groups)} nhóm · "
                f"{n_adm} quản trị · {n_mod} kiểm duyệt · "
                f"{n_mem} thành viên · {len(clusters)} cụm danh tính",
            )

            # Báo cáo đóng góp của từng file
            reports = importer_state.get("file_reports") or []
            if reports:
                with st.expander("📂 File này đóng góp gì?", expanded=True):
                    st.dataframe(pd.DataFrame([{
                        "File": r["file"],
                        "Dung lượng (KB)": r["size_kb"],
                        "Nhận diện": r["kind"],
                        "Quản trị": r["admins"],
                        "Kiểm duyệt": r["moderators"],
                        "Thành viên": r["members"],
                        "Bài đăng": r["posts"],
                        "Mục MỚI thêm": r.get("new_items", 0),
                    } for r in reports]), width='stretch',
                        hide_index=True)
                    no_new = [r["file"] for r in reports if not r.get("new_items")]
                    if no_new:
                        st.warning(
                            "Các file **không đóng góp gì mới** (trùng dữ liệu đã có): "
                            + ", ".join(no_new)
                        )
                    else:
                        st.caption(
                            "File nào có *Mục MỚI thêm = 0* nghĩa là dữ liệu đã có sẵn — "
                            "bạn có thể không cần nạp lại file đó."
                        )
        else:
            prog.progress(1.0, text="Không có dữ liệu")
            _say(
                "warning",
                "Không đọc được nhóm nào. Kiểm tra: file .html mở được bằng trình duyệt "
                "và là trang Facebook nhóm (không phải trang đăng nhập).",
            )
    st.cache_data.clear()


# ------------------------- results -------------------------
# Nạp dữ liệu hỏng KHÔNG được hiện thành "chưa có dữ liệu": lỗi DB (file khoá,
# schema cũ, disk lỗi) sẽ khiến người dùng tưởng đã quét mà thực ra chưa có
# gì. `_safe_load` ghi lại lỗi và hiện cảnh báo, nhưng vẫn trả giá trị rỗng để
# dashboard còn chạy được.
_LOAD_ERRORS: List[str] = []


def _tell_user(msg: str) -> None:
    _LOAD_ERRORS.append(msg)
    st.error(f"Không đọc được dữ liệu — `{msg}`")
    st.caption(
        "Thường do DB đang bị mở ở chỗ khác hoặc schema cũ. "
        "Thử `python main.py list` để kiểm tra."
    )


def _safe_load(loader, empty):
    """Bọc `scraper.safe_load` để hiện lỗi ngay trên giao diện."""
    return safe_load(loader, empty, on_error=_tell_user)


@st.cache_data(ttl=20)
def _cached_groups() -> tuple:
    return _safe_load(lambda: tuple(load_groups(_db_path())), tuple())


@st.cache_data(ttl=20)
def _cached_persons() -> list:
    return _safe_load(lambda: load_persons(_db_path()), [])


@st.cache_data(ttl=20, show_spinner=False)
def _workbook_bytes(groups: tuple, persons: tuple) -> bytes:
    """Dựng file .xlsx một lần rồi dùng lại cho cả 2 nút tải.

    Trước đây `export_workbook` chạy ở cả 2 chỗ mỗi lần Streamlit rerun —
    cùng một dữ liệu, tốn thời gian và RAM mỗi lần người dùng tương tác.
    """
    buf = io.BytesIO()
    export_workbook(list(groups), buf, persons=list(persons))
    return buf.getvalue()


def _render_profile_checklist(groups, persons) -> None:
    """Bảng theo dõi: quản trị/kiểm duyệt nào đã có hồ sơ, ai còn thiếu.

    Hệ thống KHÔNG tự tải được trang hồ sơ (không có phiên đăng nhập Facebook),
    nên người dùng phải tự lưu từng trang bằng Ctrl+S rồi tải lên.
    Bảng này chỉ rõ chính xác còn thiếu ai.
    """
    from scraper.entity_linker import canonical_fb

    wanted = []   # (role_vn, name, profile_url)
    for g in groups:
        for p in g.admins:
            wanted.append(("Quản trị viên", p.name, p.profile_url))
        for p in g.moderators:
            wanted.append(("Kiểm duyệt viên", p.name, p.profile_url))
    if not wanted:
        return

    have_urls = {canonical_fb(x.profile_url) for x in persons if x.profile_url}
    # canonical_fb trả None cho URL không phải hồ sơ (nhóm/bài viết) -> bỏ None
    # để hồ sơ chưa có link không bị coi nhầm là "đã có".
    have_urls.discard(None)
    have_names = {(x.name or "").strip().lower() for x in persons if x.name}

    rows = []
    for role, name, url in wanted:
        cf = canonical_fb(url)
        got = bool(cf and cf in have_urls) or \
            ((name or "").strip().lower() in have_names)
        rows.append({
            "Trạng thái": "✅ Đã có hồ sơ" if got else "⬜ Cần lưu trang",
            "Vai trò": role,
            "Tên": name or "(chưa rõ)",
            "Link hồ sơ": url or "",
        })
    rows.sort(key=lambda r: (r["Trạng thái"], r["Vai trò"], r["Tên"] or ""))

    n_missing = sum(1 for r in rows if r["Trạng thái"].startswith("⬜"))
    with st.expander(
        f"🧑 Hồ sơ quản trị/kiểm duyệt — {len(rows) - n_missing}/{len(rows)} đã có"
        + (f" · còn {n_missing} trang cần lưu" if n_missing else " · đủ hết"),
        expanded=bool(n_missing),
    ):
        if n_missing:
            st.info(
                "Hệ thống **không tự tải được** trang hồ sơ (không có phiên đăng nhập "
                "Facebook). Cách lấy: mở link ở cột *Link hồ sơ* → `Ctrl+S` "
                "(chọn *Webpage, HTML Only*) → tải file `.html` lên mục **③**.\n\n"
                "Có thể tải **nhiều file cùng lúc**, hệ thống tự gộp theo tài khoản. "
                "Kéo cột *Link hồ sơ* để mở nhanh."
            )
        st.dataframe(pd.DataFrame(rows), width='stretch',
                     hide_index=True)


def show_results():
    groups = list(_cached_groups())
    persons = _cached_persons()

    st.markdown("---")
    if not groups and not persons:
        st.info(
            "**Chưa có dữ liệu.** Cách nhanh nhất:\n\n"
            "1. Trên Facebook (**đã đăng nhập**), mở nhóm cần xem\n"
            "2. `F12` → **Console** → dán script `browser/export_group.js` → Enter\n"
            "3. File `.json` tự tải về → **kéo thả vào mục ③** hoặc bỏ vào thư mục `html_dump/`\n"
            "4. Bấm **📂 Nạp html_dump**\n\n"
            "Nhóm kín: dùng `Ctrl+S` (chọn *Webpage, HTML Only*) rồi tải file `.html` lên mục ③."
        )
        return

    n_admin = sum(len(g.admins) for g in groups)
    n_mod = sum(len(g.moderators) for g in groups)
    n_mem = sum(len(g.members) for g in groups)
    n_post = sum(len(g.recent_posts) for g in groups)
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Nhóm", len(groups))
    m2.metric("Quản trị viên", n_admin)
    m3.metric("Kiểm duyệt", n_mod)
    m4.metric("Thành viên", n_mem)
    m5.metric("Bài public", n_post)

    # ---- Checklist hồ sơ quản trị/kiểm duyệt: cần lưu trang nào? ----
    _render_profile_checklist(groups, persons)

    # nút tải
    _XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    xlsx = _workbook_bytes(tuple(groups), tuple(persons))
    st.download_button(
        "⬇️ Tải Excel báo cáo (.xlsx)", xlsx,
        file_name=f"bao_cao_{datetime.now():%Y%m%d_%H%M}.xlsx",
        mime=_XLSX_MIME,
    )

    tabs = st.tabs([
        "🏠 Nhóm", "👥 Người đã biết", "📄 Bài đăng",
        "🧑 Hồ sơ cá nhân", "🔗 Cụm danh tính", "📈 Thay đổi",
    ])

    # --- Tab nhóm ---
    with tabs[0]:
        st.dataframe(pd.DataFrame([{
            "Mã nhóm": g.group_id, "Tên nhóm": g.name,
            "Loại": g.privacy.value, "Thành viên (FB)": g.member_count,
            "Quản trị": len(g.admins), "Kiểm duyệt": len(g.moderators),
            "Thành viên (lấy được)": len(g.members),
            "Bài": len(g.recent_posts), "Quét lúc": g.last_scanned.strftime("%d/%m %H:%M"),
            "Link": g.url,
        } for g in groups]), width='stretch', hide_index=True)

        st.caption(
            "🟢 **badge** = vai trò lấy từ nhãn trên trang (đáng tin). "
            "🟡 **vị trí** = suy đoán từ vị trí trong trang (chỉ dùng khi không có nhãn)."
        )
        detail = []
        for g in groups:
            for p in list(g.admins) + list(g.moderators) + list(g.members):
                ev = (p.source or "").lower()
                detail.append({
                    "Nhóm": g.name,
                    "Tên": p.name or "(chưa rõ)",
                    "Vai trò": {"admin": "Quản trị viên",
                                "moderator": "Kiểm duyệt viên",
                                "member": "Thành viên"}.get(p.role, p.role),
                    "Bằng chứng": {"badge": "🟢 badge",
                                    "pos": "🟡 vị trí"}.get(ev, "⚪ không rõ"),
                    "Profile": p.profile_url,
                })
        with st.expander("🔎 Kiểm chứng vai trò từng người"):
            st.dataframe(pd.DataFrame(detail), width='stretch',
                         hide_index=True)

    # --- Tab người đã biết (gộp từ mọi file đã nạp) ---
    with tabs[1]:
        from storage import people_union
        all_people = _safe_load(lambda: people_union(_db_path()), [])
        if all_people:
            dfp = pd.DataFrame([{
                "Nhóm": r["group_id"],
                "Vai trò": {"admin": "Quản trị viên",
                            "moderator": "Kiểm duyệt viên",
                            "member": "Thành viên"}.get(r["role"], r["role"]),
                "Tên": r["name"] or "(chưa rõ)",
                "SĐT": "; ".join(
                    x.get("normalized", "") for x in json.loads(r["phones"] or "[]")
                ),
                "Social": "; ".join(
                    f"{s.get('platform')}: {s.get('url')}"
                    for s in json.loads(r["social_links"] or "[]")
                ),
                "Profile": r["profile_url"],
            } for r in all_people])
            c1, c2, c3 = st.columns(3)
            c1.metric("Quản trị viên", (dfp["Vai trò"] == "Quản trị viên").sum())
            c2.metric("Kiểm duyệt viên", (dfp["Vai trò"] == "Kiểm duyệt viên").sum())
            c3.metric("Thành viên", (dfp["Vai trò"] == "Thành viên").sum())
            st.dataframe(dfp, width='stretch', hide_index=True)
            st.download_button(
                "⬇️ Tải danh sách người (.csv)",
                dfp.to_csv(index=False).encode("utf-8-sig"),
                "nguoi_da_biet.csv", "text/csv",
            )
        else:
            st.caption("Chưa có dữ liệu người dùng.")

    # --- Tab bài đăng ---
    with tabs[2]:
        rows = []
        for g in groups:
            for post in g.recent_posts:
                rows.append({
                    "Nhóm": g.name, "Tác giả": post.author_name,
                    "Vai trò": _ROLE_VN.get(post.author_role or "", ""),
                    "Nội dung": (post.content or "")[:300],
                    "Thích": post.likes, "BL": post.comments_count,
                    "Share": post.shares,
                    "Số tương tác": "có" if post.metrics_available else "không có trong file",
                    "Ảnh/VD": len(post.media_urls),
                    "Thời gian": post.time_raw or (
                        post.created_at.strftime("%d/%m %H:%M") if post.created_at else ""),
                })
        if rows:
            st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)
            n_no = sum(1 for r in rows if r["Số tương tác"].startswith("không"))
            if n_no:
                st.caption(
                    f"⚠️ {n_no}/{len(rows)} bài không có số lượt thích/chia sẻ trong file. "
                    "Khi lưu trang bằng Ctrl+S, Facebook thường KHÔNG kèm số này — "
                    "ô trống nghĩa là *không lấy được*, không phải 0 lượt thích."
                )
        else:
            st.caption("Chưa có bài đăng.")

    # --- Tab hồ sơ cá nhân ---
    with tabs[3]:
        if persons:
            # Chọn theo CHỈ SỐ, không theo tên hiển thị: hai hồ sơ trùng tên
            # (rất thường gặp) sẽ luôn mở nhầm về hồ sơ đầu tiên.
            labels = [p.name or str(p.profile_url) or "(không tên)" for p in persons]
            pick = st.selectbox(
                "Chọn hồ sơ", list(range(len(persons))),
                format_func=lambda i: labels[i],
            )
            p = persons[pick]
            c1, c2, c3 = st.columns(3)
            c1.write("**SĐT:** " + ("; ".join(x.normalized for x in p.phones) or "-"))
            c1.write("**Email:** " + ("; ".join(p.emails) or "-"))
            c2.write("**Sống tại:** " + (p.location or "-"))
            c2.write("**Đến từ:** " + (p.hometown or "-"))
            c3.write("**CV:** " + (p.job or "-"))
            c3.write("**Quan hệ:** " + (p.relationship or "-"))
            st.divider()
            for s in p.social_links:
                st.markdown(f"- {s.platform}: `{s.url}`")
            st.divider()
            st.dataframe(pd.DataFrame([{
                "Tên": q.name, "Username": q.username,
                "SĐT": "; ".join(x.normalized for x in q.phones),
                "Email": "; ".join(q.emails),
                "Social": len(q.social_links), "Tích xanh": q.is_verified,
                "Nguồn": q.source_file,
            } for q in persons]), width='stretch', hide_index=True)
            st.download_button("⬇️ hồ sơ cá nhân", xlsx,
                               f"ho_so_{datetime.now():%Y%m%d}.xlsx",
                               _XLSX_MIME)
        else:
            st.caption("Chưa có hồ sơ cá nhân.")

    # --- Tab cụm danh tính ---
    with tabs[4]:
        import sqlite3
        from storage import _connect

        def _read_clusters():
            # Dùng _connect để có WAL + busy_timeout + migration; mở trực tiếp
            # bằng sqlite3.connect sẽ bỏ qua hết và dễ gặp "database is locked".
            conn = _connect(_db_path())
            try:
                conn.row_factory = sqlite3.Row
                return [dict(r) for r in conn.execute(
                    "SELECT title,confidence,member_count,shared_signals,group_ids,"
                    "profile_urls FROM entity_clusters ORDER BY member_count DESC")]
            finally:
                conn.close()

        rows = _safe_load(_read_clusters, [])
        if rows:
            st.caption("Các tài khoản trùng SĐT / email / social handle — có thể cùng 1 chủ thể.")
            st.dataframe(pd.DataFrame([{
                "Chủ thể": r["title"],
                "Tin cậy": {"certain": "Chắc chắn", "high": "Cao"}.get(r["confidence"], r["confidence"]),
                "Số TK": r["member_count"],
                "Tín hiệu": (r["shared_signals"] or "")[:100],
                "Nhóm": (r["group_ids"] or "")[:40],
                "Tài khoản": (r["profile_urls"] or "")[:120],
            } for r in rows]), width='stretch', hide_index=True)
        else:
            st.caption("Chưa có cụm danh tính nào.")

    # --- Tab thay đổi ---
    with tabs[5]:
        from storage import diff_snapshots, group_history
        hist = _safe_load(lambda: group_history(_db_path()), [])
        if hist:
            st.caption("Lịch sử các lần đã nạp dữ liệu (mỗi lần nạp = 1 mốc)")
            dfh = pd.DataFrame(hist)
            gids = dfh["group_id"].unique().tolist()
            pick_g = st.selectbox("Nhóm", gids, key="hist_gid")
            sub = dfh[dfh["group_id"] == pick_g].copy()
            try:
                sub["scanned_at"] = pd.to_datetime(sub["scanned_at"])
            except Exception:  # noqa: BLE001
                pass
            try:
                st.line_chart(
                    sub.set_index("scanned_at")[["member_count", "n_admins", "posts_count"]]
                )
            except Exception as exc:  # noqa: BLE001
                st.caption(f"Không vẽ được biểu đồ: {exc}")
            st.dataframe(sub, width='stretch', hide_index=True)

        changes = _safe_load(lambda: diff_snapshots(_db_path()), [])
        st.divider()
        st.subheader("Thay đổi lần quét gần nhất")
        if changes:
            for ch in changes:
                st.markdown(f"**{ch['group_id']}**")
                for note in ch["note"]:
                    st.markdown(f"- {note}")
        else:
            st.caption("Chưa có thay đổi giữa 2 lần nạp gần nhất.")


# ------------------------- main -------------------------
def main():
    st.title("📊 OSINT Group Monitor")
    st.caption("Giám sát hội nhóm Facebook — quét, bóc tách liên hệ, ghép danh tính")
    # Khu thông báo luôn ở đầu trang để không bỏ sót
    notice = st.empty()

    cfg = _load_cfg()
    (
        profile, links_text, run_clicked, import_clicked,
        uploaded, clear_clicked, keep_files,
    ) = sidebar_input(cfg)

    if clear_clicked:
        _clear_all_data(notice)

    if run_clicked:
        targets = _parse_targets(links_text)
        if not targets:
            notice.error("Chưa có link/ID nhóm hợp lệ. Dán vào mục ① bên trái.")
        else:
            _run_scan(targets, profile)

    if import_clicked:
        _run_import(uploaded, notice, keep_files)

    show_results()

    st.markdown("---")
    st.caption(
        "Chỉ thu thập dữ liệu công khai. Việc xử lý dữ liệu cá nhân phải tuân thủ "
        "Nghị định 13/2023/NĐ-CP và Điều khoản Facebook."
    )


main()
