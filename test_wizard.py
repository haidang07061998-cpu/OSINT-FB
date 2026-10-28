"""Test wizard bằng mock input."""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import pytest

from scraper.scan_profile import wizard

# Thứ tự input: name, desc, groups, accounts, members, posts, comments, days,
# [members>0 nên hỏi member_list], new_members, admins, contacts, bio, posts,
# reactions, media, [comments>0 nên hỏi comments], activity, keywords,
# link_verify, screenshot, rate, private
ANSWERS = [
    "quyet-cua-toi", "test profile",          # 1-2: name, desc
    "5", "8", "20",                            # 3-5: groups, accounts, members
    "15", "3",                                 # 6-7: posts, comments
    "14",                                      # 8: days_back
    "y",                                       # 9: group_meta
    "y",                                       # 10: member_count
    "y",                                       # 11: member_list (members=20>0)
    "y",                                       # 12: new_members
    "y",                                       # 13: admins
    "y",                                       # 14: admin_contacts
    "y",                                       # 15: admin_bio
    "y",                                       # 16: posts
    "y",                                       # 17: post_reactions
    "n",                                       # 18: post_media
    "y",                                       # 19: comments (comments=3>0)
    "y",                                       # 20: activity_stats
    "y",                                       # 21: keywords
    "n",                                       # 22: link_verify
    "n",                                       # 23: screenshot_evidence
    "4.0",                                     # 24: rate
    "y",                                       # 25: include_private
]


def _run_wizard(answers):
    it = iter(answers)
    with patch("builtins.input", lambda *a: next(it)):
        return wizard()


@pytest.fixture
def p():
    return _run_wizard(list(ANSWERS))


def test_identity(p):
    assert p.name == "quyet-cua-toi"
    assert p.description == "test profile"


def test_limits(p):
    assert p.limits.max_groups == 5
    assert p.limits.max_accounts == 8
    assert p.limits.max_members_per_group == 20
    assert p.limits.max_comments_per_post == 3


def test_window(p):
    assert p.window.days_back == 14 and p.window.max_posts == 15


def test_content_group_flags(p):
    assert p.content.group_meta is True and p.content.member_count is True
    assert p.content.member_list is True        # members=20 > 0
    assert p.content.new_members is True


def test_content_admin_flags(p):
    assert p.content.admins is True and p.content.admin_contacts is True
    assert p.content.admin_bio is True


def test_content_post_flags(p):
    assert p.content.posts is True
    assert p.content.post_reactions is True
    assert p.content.post_media is False
    assert p.content.comments is True           # comments=3 > 0


def test_content_extra_flags(p):
    assert p.content.activity_stats is True and p.content.keywords is True
    assert p.content.link_verify is False       # mặc định off
    assert p.content.screenshot_evidence is False


def test_rate_and_private(p):
    assert p.rate_limit_seconds == 4.0
    assert p.include_private is True


def test_member_list_skipped_when_zero():
    """max_members_per_group = 0 -> wizard KHÔNG hỏi member_list."""
    answers = list(ANSWERS)
    answers[4] = "0"          # members = 0
    answers.pop(10)           # bỏ câu hỏi "member_list" (vị trí 11)
    p = _run_wizard(answers)
    assert p.limits.max_members_per_group == 0
    assert p.content.member_list is False


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
