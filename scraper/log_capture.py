"""Thu log theo từng phiên quét, không rò dữ liệu sang phiên khác.

Vì sao tách riêng file này: `dashboard.py` gọi `st.set_page_config()` ngay ở
cấp module nên không import được ngoài Streamlit. Phần đa số code ở đây
không cần Streamlit nên tách ra để test được bình thường.

Vấn đề giải quyết: logger là đối tượng dùng chung cho cả tiến trình. Nếu
hai người dùng bấm "Quét" đồng thời trên cùng một server, handler của người
này sẽ nhận log của người kia — mà log của tool có chứa SĐT, tên và link hồ
sơ. Mỗi lần quét giờ mang một `run_id` riêng và chỉ nhận dòng log đúng của
mình.
"""
from __future__ import annotations

import logging
import threading
import uuid
from typing import List, Optional


class RunIdFilter(logging.Filter):
    """Đánh dấu record thuộc lần quét đang chạy.

    Gắn filter lên logger cha KHÔNG đủ: `callHandlers` chỉ chạy filter của
    logger phát sinh record, không chạy filter của logger cha. Mọi module đều
    gọi `get_logger("ten")` -> `osint.ten`, nên record đến từ `osint.pipeline`
    sẽ không bao giờ thấy filter đặt trên `osint`.

    Vì vậy việc đóng dấu nằm ở `RecordFactory` (xem `run_id_factory`) và
    class này giữ lại cho tương thích / để test.
    """

    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.osint_run_id = self.run_id
        return True


# Run id của lần quét đang chạy trong thread này.
_active = threading.local()

# RecordFactory gốc của thư viện, và bản đã thay thế (None = chưa thay).
_base_factory = logging.getLogRecordFactory()
_installed_factory = None


def _stamping_factory(name, level, pathname, lineno, msg, args, exc_info,
                      func=None, sinfo=None) -> logging.LogRecord:
    """RecordFactory của logging: nhận tham số, trả về LogRecord đã đóng dấu."""
    record = _base_factory(name, level, pathname, lineno, msg, args, exc_info,
                           func, sinfo)
    run_id = getattr(_active, "run_id", None)
    if run_id is not None:
        record.osint_run_id = run_id
    return record


def run_id_factory(run_id: str):
    """Context manager: mọi log ghi ra trong `with` đều mang `run_id`."""
    return _RunIdScope(run_id)


class _RunIdScope:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self._prev = None
        self._installed = False

    def __enter__(self):
        self._prev = getattr(_active, "run_id", None)
        _active.run_id = self.run_id
        global _installed_factory
        if _installed_factory is None:
            logging.setLogRecordFactory(_stamping_factory)
            _installed_factory = _stamping_factory
            self._installed = True
        return self

    def __exit__(self, *exc):
        _active.run_id = self._prev
        global _installed_factory
        # Chỉ gỡ factory nếu chính scope này cài, và không scope nào khác
        # đang dùng. Gỡ khi còn session khác sẽ làm hỏng log của session đó.
        if self._installed and getattr(_active, "run_id", None) is None:
            if logging.getLogRecordFactory() is _installed_factory:
                logging.setLogRecordFactory(_base_factory)
            _installed_factory = None
        return False


class LogCapture(logging.Handler):
    """Handler chỉ thu dòng log thuộc đúng `run_id` đã cấp."""

    def __init__(self, run_id: str, max_lines: int = 500):
        super().__init__()
        self.run_id = run_id
        self.max_lines = max_lines
        self.lines: List[str] = []

    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(record, "osint_run_id", None) == self.run_id

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(f"{record.levelname[0]} {record.getMessage()}")
            # Chặn bộ đệm phình vô hạn khi quét nhóm lớn.
            if len(self.lines) > self.max_lines:
                del self.lines[: len(self.lines) - self.max_lines]
        except Exception:  # noqa: BLE001
            # emit() không được ném — logging sẽ in cảnh báo nội bộ.
            pass

    def tail(self, n: int = 25) -> List[str]:
        return self.lines[-n:]

    def render(self, n: int = 25) -> str:
        return "\n".join(self.tail(n))


class ScanSession:
    """Một lần quét: đóng dấu record + gắn handler vào logger `osint`.

    Dùng `with` để luôn gỡ sạch kể cả khi quét nổi:
        with ScanSession() as s:
            run_the_scan()
            s.render()

    Handler gắn vào `osint` nên nhận được log của mọi module con
    (`osint.pipeline`, `osint.group_scraper`, ...).
    """

    def __init__(self, logger_name: str = "osint", level: int = logging.INFO,
                 run_id: Optional[str] = None):
        self.logger = logging.getLogger(logger_name)
        self.run_id = run_id or uuid.uuid4().hex
        self.level = level
        self.handler = LogCapture(self.run_id)
        self.handler.setLevel(level)
        self._scope = run_id_factory(self.run_id)
        self._old_level = self.logger.level

    def __enter__(self) -> "ScanSession":
        self._scope.__enter__()
        self.logger.setLevel(self.level)
        self.logger.addHandler(self.handler)
        return self

    def __exit__(self, *exc) -> None:
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self._old_level)
        self._scope.__exit__(*exc)

    def render(self, n: int = 25) -> str:
        return self.handler.render(n)


# Chỉ cho phép 1 lần quét tại một thời điểm: hai lần quét dùng chung thư mục
# reports/ và cùng file DB nên sẽ ghi đè kết quả của nhau.
SCAN_LOCK = threading.Lock()
