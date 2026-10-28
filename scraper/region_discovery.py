"""Phát hiện hội nhóm Facebook theo KHU VỰC + CHỦ Đề qua search engine.

Bối cảnh (đã kiểm chứng thực tế 2026):
  - Meta đã GỠ Graph API tìm nhóm theo region; toàn bộ search FB công khai
    đều bắt đăng nhập và cá nhân hóa kết quả.
  - Bing đã BỎ `site:` operator -> dorking tự động không còn hiệu quả.
  - DuckDuckGo html trả HTTP 202, Brave 429 -> chặn automation.
  - Google trực tiếp: CAPTCHA.

=> Engine duy nhất KHẢ DỤNG và hợp pháp là **Google Custom Search JSON API**
   (miễn phí 100 query/ngày), trả JSON có link trực tiếp. Cần API key.
   Module cũng giữ 1 tùy chọn dự phòng không cần key (duckduckgo POST) nhưng
   thường bị chặn -> chỉ mang tính thử nghiệm.

Kết quả của module là SEED LIST (yaml) để nạp vào pipeline quét. Module
KHÔNG đụng Facebook.

Cài đặt API key (khuyến nghích, set biến môi trường thay vì để trong repo):
    setx GOOGLE_API_KEY "AIza..."                 (Google Cloud)
    setx GOOGLE_CSE_ID  "search-engine-id"        (Programmable Search Engine)
Đăng ký: https://programmablesearchengine.google.com/
         https://developers.google.com/custom-search/v1/overview
"""
from __future__ import annotations

import os
import re
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus, unquote

import requests
import yaml

# Nhận diện link nhóm FB (mọi subdomain m.facebook / web.facebook...)
_GROUP_ID_RE = re.compile(
    r"(?i)(?:https?://)?(?:www\.|m\.|web\.|mbasic\.)?(?:facebook|fb)\.com/"
    r"(?:groups/|group\.php\?id=)(\d{6,})"
)
_FB_DOMAIN_RE = re.compile(r"(?i)(?:^|\.)(?:facebook|fb)\.com")

GOOGLE_CSE_ENDPOINT = (
    "https://www.googleapis.com/customsearch/v1"
    "?key={key}&cx={cx}&q={q}&num=10&start={start}"
)
DDG_POST_ENDPOINT = "https://html.duckduckgo.com/html/"


# ------------------------------------------------------------
# Query building
# ------------------------------------------------------------
def _build_queries(cfg: dict) -> List[Tuple[str, str, str]]:
    d = cfg.get("discovery", {})
    regions: List[str] = d.get("regions", [])
    topics: List[str] = d.get("topics", [])
    templates: List[str] = d.get(
        "templates", ['site:facebook.com/groups/ "{region}" {topic}']
    )
    engines: List[str] = d.get("engines", ["google_cse"])
    qpd: List[Tuple[str, str, str]] = []
    for engine in engines:
        for region in regions:
            for topic in topics:
                for tpl in templates:
                    q = tpl.format(region=region, topic=topic)
                    qpd.append((engine, q, f"{engine} | {region} | {topic}"))
    return qpd


def _extract_group_id(url: str) -> Optional[str]:
    m = _GROUP_ID_RE.search(url or "")
    return m.group(1) if m else None


def _is_fb_group_url(url: str) -> bool:
    if not _FB_DOMAIN_RE.search(url or ""):
        return False
    return bool(_GROUP_ID_RE.search(url))


# ------------------------------------------------------------
# Providers
# ------------------------------------------------------------
class Provider:
    name = "base"
    needs_key = False

    def __init__(self, cfg: dict):
        d = cfg.get("discovery", {})
        self.delay = float(d.get("request_delay", 1.5))
        self.max_pages = int(d.get("max_pages", 1))
        self.timeout = int(cfg.get("scraper", {}).get("timeout", 15))
        self.proxies = cfg.get("scraper", {}).get("proxies")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                d.get("user_agent")
                or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept-Language": "vi,en;q=0.9",
        })
        self._last = 0.0

    def _throttle(self) -> None:
        gap = time.time() - self._last
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last = time.time()

    def search(self, query: str) -> List[str]:
        raise NotImplementedError


class GoogleCSEProvider(Provider):
    """Google Custom Search JSON API — chính thức, ổn định, 100 query/ngày."""

    name = "google_cse"
    needs_key = True

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        d = cfg.get("discovery", {})
        self.api_key = (
            d.get("google_api_key")
            or os.environ.get("GOOGLE_API_KEY", "")
        )
        self.cse_id = (
            d.get("google_cse_id")
            or os.environ.get("GOOGLE_CSE_ID", "")
        )

    def ready(self) -> bool:
        return bool(self.api_key and self.cse_id)

    def search(self, query: str) -> List[str]:
        from .utils import get_logger

        logger = get_logger("discover")
        urls: List[str] = []
        q = quote_plus(query)
        for page in range(self.max_pages):
            start = page * 10 + 1
            url = GOOGLE_CSE_ENDPOINT.format(
                key=self.api_key, cx=self.cse_id, q=q, start=start
            )
            self._throttle()
            try:
                r = self.session.get(url, timeout=self.timeout, proxies=self.proxies)
            except requests.RequestException as exc:
                logger.warning("Google CSE lỗi mạng: %s", exc)
                return urls
            if r.status_code == 429:
                logger.warning("Google CSE: hết quota 100 query/ngày")
                return urls
            if r.status_code != 200:
                logger.warning(
                    "Google CSE HTTP %s - kiểm tra GOOGLE_API_KEY / GOOGLE_CSE_ID",
                    r.status_code,
                )
                return urls
            try:
                data = r.json()
            except ValueError:
                return urls
            items = data.get("items") or []
            if not items:
                break
            for it in items:
                link = it.get("link", "")
                if _is_fb_group_url(link):
                    urls.append(link)
            if "nextPage" not in data.get("queries", {}):
                break
        return urls


class DuckDuckGoProvider(Provider):
    """Dự phòng KHÔNG cần key. Hiện DDG hay trả 202/challenge -> hay rỗng."""

    name = "duckduckgo"
    needs_key = False

    def search(self, query: str) -> List[str]:
        self._throttle()
        try:
            r = self.session.post(
                DDG_POST_ENDPOINT,
                data={"q": query},
                timeout=self.timeout,
                proxies=self.proxies,
            )
        except requests.RequestException:
            return []
        if r.status_code != 200 or "result__a" not in r.text:
            return []
        raw = re.findall(r'(?i)uddg=([^"&]+)', r.text)
        return [unquote(u) for u in raw if _is_fb_group_url(unquote(u))]


_PROVIDERS = {
    "google_cse": GoogleCSEProvider,
    "duckduckgo": DuckDuckGoProvider,
}


# ------------------------------------------------------------
# Main entry
# ------------------------------------------------------------
def discover(cfg: dict) -> List[dict]:
    """Chạy provider -> trả về list target_groups (dict) đã dedup."""
    from .utils import get_logger

    logger = get_logger("discover")
    d = cfg.get("discovery", {})
    max_queries = int(d.get("max_queries", 20))

    engines = d.get("engines", ["google_cse"])
    providers: Dict[str, Provider] = {}
    for name in engines:
        cls = _PROVIDERS.get(name)
        if not cls:
            logger.warning("Bo qua engine khong ho tro: %s", name)
            continue
        prov = cls(cfg)
        if prov.needs_key and not prov.ready():  # type: ignore[attr-defined]
            logger.warning(
                "Engine %s can API key (set bien moi truong %s va %s) - bo qua.",
                name, "GOOGLE_API_KEY", "GOOGLE_CSE_ID",
            )
            continue
        providers[name] = prov

    if not providers:
        raise SystemExit(
            "Khong co engine nao kha dung.\n"
            "Cach 1 (khuyen dung): lay Google API key + CSE id, set:\n"
            "  setx GOOGLE_API_KEY <key>\n"
            "  setx GOOGLE_CSE_ID  <cx>\n"
            "Cach 2: tu tao seed_groups.yaml (xem README muc 4)."
        )

    queries = _build_queries(cfg)[:max_queries]
    found: Dict[str, dict] = {}
    n = 0
    for engine, query, note in queries:
        prov = providers.get(engine)
        if not prov:
            continue
        logger.info("QUERY [%s] %s", engine, query)
        urls = prov.search(query)
        n += 1
        hit = 0
        for u in urls:
            gid = _extract_group_id(u)
            if gid and gid not in found:
                found[gid] = {
                    "id": gid,
                    "url": f"https://www.facebook.com/groups/{gid}",
                    "note": note,
                }
                hit += 1
        logger.info("  -> %d link hop le (%d moi)", sum(1 for x in urls), hit)
        if n >= max_queries:
            break

    logger.info("TIM THAY %d nhom tu %d query", len(found), n)
    return list(found.values())


def run_discovery(cfg_path: str = "config.yaml") -> List[dict]:
    """CLI entry: chạy discover rồi ghi seed_groups.yaml ra đĩa."""
    from .utils import get_logger
    from pipeline import load_config

    cfg = load_config(cfg_path)
    targets = discover(cfg)
    out = cfg.get("discovery", {}).get("output", "seed_groups.yaml")
    with open(out, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            {"target_groups": targets}, f, allow_unicode=True, sort_keys=False
        )
    get_logger("discover").info(
        "Da ghi %d nhom vao %s. Chay: python main.py run --seed %s",
        len(targets), out, out,
    )
    return targets
