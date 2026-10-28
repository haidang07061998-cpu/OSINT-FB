"""Tiện ích: HTTP client có rate-limit, retry, logging chuẩn."""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from typing import Dict, Optional

import requests
from tenacity import Retrying, stop_after_attempt, wait_exponential, retry_if_exception_type


# ------------------------- Logging -------------------------
def get_logger(name: str = "osint") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        fmt = logging.Formatter(
            "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        handler.setFormatter(fmt)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


logger = get_logger()


class LoginRequired(Exception):
    """Facebook yêu cầu đăng nhập — cần chuyển sang chế độ tự lưu HTML."""


# --------------------- Config loader -----------------------
@dataclass
class ScraperConfig:
    request_delay: float = 3.0
    max_retries: int = 3
    timeout: int = 15
    proxies: Optional[Dict[str, str]] = None
    user_agent: str = "Mozilla/5.0 (compatible; OSINTGroupMonitor/1.0)"


# --------------------- HTTP client -------------------------
class HttpClient:
    """Requests wrapper tôn trọng nguồn dữ liệu công khai."""

    def __init__(self, cfg: ScraperConfig):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": cfg.user_agent,
            "Accept-Language": "vi,en;q=0.9",
        })
        self._last_request = 0.0
        # max_retries từ config được dùng thật thay vì con số 3 hardcode.
        # Phải tạo Retrying trong __init__ vì @retry được đánh giá lúc nạp class,
        # không đọc được self.cfg.
        self._retrying = Retrying(
            reraise=True,
            stop=stop_after_attempt(max(1, int(cfg.max_retries))),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            retry=retry_if_exception_type(requests.RequestException),
        )

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        if elapsed < self.cfg.request_delay:
            time.sleep(self.cfg.request_delay - elapsed)
        self._last_request = time.time()

    def get(self, url: str, **kwargs) -> requests.Response:
        """GET có throttle + retry. Login-wall ném LoginRequired (không retry)."""
        return self._retrying(self._get_once, url=url, **kwargs)

    def _get_once(self, url: str, **kwargs) -> requests.Response:
        self._throttle()
        logger.info("GET %s", url)
        resp = self.session.get(
            url,
            timeout=self.cfg.timeout,
            proxies=self.cfg.proxies,
            **kwargs,
        )
        # Login-wall: FB chuyển sang trang đăng nhập (kể cả nhóm công khai)
        final = (resp.url or "").lower()
        if "/login" in final or "login.php" in final:
            raise LoginRequired(
                f"Facebook yêu cầu đăng nhập cho {url}. "
                "Hãy mở trang trong trình duyệt (đã đăng nhập) rồi Ctrl+S "
                "lưu HTML vào html_dump/ để nạp qua `import`."
            )
        if resp.status_code == 400 and "error" in resp.text[:400].lower():
            raise LoginRequired(
                f"Facebook trả về lỗi 400 (chặn request tự động) cho {url}. "
                "Hãy dùng chế độ tự lưu HTML: Ctrl+S trên trang đã đăng nhập."
            )
        resp.raise_for_status()
        return resp


# --------------------- Text utils --------------------------
def clean_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return " ".join(value.split()).strip()


def clean_fb_url(url: Optional[str]) -> Optional[str]:
    """Bỏ tham số theo dõi của Facebook (`__cft__`, `__tn__`, `mibextid`...)

    nhưng GIỮ `?id=...` vì đó là định danh ổn định của tài khoản.
    """
    if not url:
        return None
    base, sep, query = url.partition("?")
    if not sep:
        return base or None
    keep = []
    for part in query.split("&"):
        if not part:
            continue
        key = part.split("=", 1)[0].lower()
        if key in ("id", "comment_id", "story_fbid"):
            keep.append(part)
    if not keep:
        return base or None
    return base + "?" + "&".join(keep)
