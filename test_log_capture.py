"""Kiểm tra log của mỗi lần quét không lẫn sang lần quét khác.

Đây là test bảo mật: log của tool chứa SĐT, tên và link hồ sơ. Nếu hai phiên
quét chạy trên cùng server mà nhìn thấy log của nhau thì đó là rò dữ liệu cá
nhân sang người dùng khác.
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest

from scraper.log_capture import LogCapture, RunIdFilter, ScanSession

# Không in log thật ra console trong test.
@pytest.fixture(autouse=True)
def _quiet_osint_logger():
    log = logging.getLogger("osint")
    log.handlers = [logging.NullHandler()]
    log.propagate = False
    old = log.level
    log.setLevel(logging.INFO)
    yield
    log.setLevel(old)


def _say(name, msg):
    logging.getLogger(f"osint.{name}").info(msg)


def test_two_sessions_do_not_see_each_other():
    """Hai lần quét nối tiếp (hoặc ở 2 thread) không được chia sẻ log.

    `ScanSession` đóng dấu theo thread, nên chạy song song ở hai thread thì mỗi
    thread có run_id riêng. Đây là hình thức "rò dữ liệu" thật của dashboard:
    hai người dùng bấm Quét cùng lúc.
    """
    import threading

    results = {}

    def scan(tag, phone):
        with ScanSession() as s:
            _say("pipeline", f"{tag} xem so dien thoai {phone}")
            results[tag] = s.render()

    t1 = threading.Thread(target=scan, args=("A", "0901112222"))
    t2 = threading.Thread(target=scan, args=("B", "0933334444"))
    t1.start(), t2.start()
    t1.join(), t2.join()

    assert "0901112222" in results["A"], results
    assert "0901112222" not in results["B"], "RÒ: phiên B thấy SĐT của phiên A"
    assert "0933334444" in results["B"], results
    assert "0933334444" not in results["A"], "RÒ: phiên A thấy SĐT của phiên B"


def test_session_collects_all_its_own_modules():
    with ScanSession() as s:
        _say("pipeline", "quet nhom")
        _say("group_scraper", "doc trang")
        _say("person_scraper", "boc ho so")
    assert len(s.handler.lines) == 3
    assert "quet nhom" in s.render()


def test_untagged_records_are_dropped():
    """Log phát ra ngoài phiên quét (VD thư viện khác) không bị thu."""
    cap = LogCapture("run-a")
    rec = logging.LogRecord("osint.x", logging.INFO, __file__, 1,
                            "khong theo phien nao", None, None)
    assert cap.filter(rec) is False


def test_handler_removed_after_session():
    log = logging.getLogger("osint")
    before = len(log.handlers)
    with ScanSession() as s:
        assert s.handler in log.handlers
    assert len(log.handlers) == before
    assert s.handler not in log.handlers


def test_filter_removed_after_session():
    log = logging.getLogger("osint")
    before = len(log.handlers)
    with ScanSession() as s:
        assert s.handler in log.handlers
    assert len(log.handlers) == before


def test_level_restored_even_on_crash():
    log = logging.getLogger("osint")
    log.setLevel(logging.WARNING)
    with pytest.raises(RuntimeError):
        with ScanSession():
            raise RuntimeError("quet no")
    assert log.level == logging.WARNING
    assert not any(isinstance(h, LogCapture) for h in log.handlers)


def test_buffer_is_bounded():
    """Quét nhóm lớn không được làm phình bộ đệm vô hạn."""
    with ScanSession() as s:
        for i in range(5000):
            _say("pipeline", f"dong {i}")
        assert len(s.handler.lines) <= s.handler.max_lines


def test_tail_and_render():
    with ScanSession() as s:
        for i in range(10):
            _say("pipeline", f"dong {i}")
        assert len(s.handler.tail(3)) == 3
        assert s.handler.tail(3)[-1] == "I dong 9"
        assert s.render(2).splitlines() == ["I dong 8", "I dong 9"]


def test_scan_lock_is_exclusive():
    from scraper.log_capture import SCAN_LOCK

    assert SCAN_LOCK.acquire(blocking=False) is True
    try:
        # Lần quét thứ 2 phải bị từ chối, không được chen vào giữa lần đầu.
        assert SCAN_LOCK.acquire(blocking=False) is False
    finally:
        SCAN_LOCK.release()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
