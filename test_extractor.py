"""Test nhanh cho module trích xuất liên hệ (không mạng)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from scraper.link_extractor import extract_contacts, extract_phones, extract_social_links


def test_phones_vn():
    text = "Liên hệ: 0912345678 hoặc +84987654321, email a@b.com"
    phones = extract_phones(text)
    norms = {p.normalized for p in phones}
    assert "+84912345678" in norms, norms
    assert "+84987654321" in norms, norms


def test_social_links():
    text = "FB: facebook.com/abc.def, IG: instagram.com/xyz, web: example.com/home"
    links = extract_social_links(text)
    pairs = {(s.platform, s.handle) for s in links}
    assert ("Facebook", "abc.def") in pairs, pairs
    assert ("Instagram", "xyz") in pairs, pairs
    assert not any(s.platform == "Website" for s in links)


def test_contacts_combined():
    phones, links = extract_contacts(["zalo.me/0123456789 t.me/osint_channel"])
    assert any(p.normalized.endswith("123456789") for p in phones)
    assert any(s.platform == "Telegram" for s in links)
    assert any(s.platform == "Zalo" for s in links)


if __name__ == "__main__":
    test_phones_vn()
    test_social_links()
    test_contacts_combined()
    print("All tests passed.")
