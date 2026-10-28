"""Pydantic models định nghĩa cấu trúc dữ liệu chuẩn cho toàn bộ pipeline."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


def utcnow() -> datetime:
    """Thời điểm hiện tại theo UTC, dạng naive.

    `datetime.utcnow()` bị deprecated từ Python 3.12. Ta vẫn giữ datetime
    *naive* (không kèm timezone) để mọi so sánh với thời gian bóc từ Facebook
    không bị lỗi TypeError "can't compare offset-naive and offset-aware".
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class GroupPrivacy(str, Enum):
    PUBLIC = "public"
    PRIVATE = "private"       # Chỉ ghi nhận, KHÔNG cào nội dung
    CLOSED = "closed"
    SECRET = "secret"
    UNKNOWN = "unknown"


class SocialLink(BaseModel):
    platform: str = Field(..., description="Tên nền tảng: facebook, tiktok...")
    url: str
    handle: Optional[str] = None


class PhoneCandidate(BaseModel):
    raw: str = Field(..., description="Chuỗi gốc được tìm thấy")
    normalized: str = Field(..., description="Số đã chuẩn hoá E.164-ish")
    source: str = Field(default="", description="Ngữ cảnh nơi tìm thấy")


class AdminProfile(BaseModel):
    """Thông tin tài khoản quản trị / kiểm duyệt viên (từ public sources)."""
    fb_user_id: Optional[str] = None
    name: Optional[str] = None
    profile_url: Optional[str] = None
    role: str = Field(default="unknown", description="admin | moderator | member")
    is_verified: bool = False
    join_date: Optional[str] = None
    phones: List[PhoneCandidate] = Field(default_factory=list)
    social_links: List[SocialLink] = Field(default_factory=list)
    source: Optional[str] = Field(
        None,
        description="Nguồn vai trò: 'badge' = nhãn trên trang, 'pos' = suy theo vị trí",
    )
    extracted_at: datetime = Field(default_factory=utcnow)


class GroupPost(BaseModel):
    post_id: Optional[str] = None
    author_name: Optional[str] = None
    author_url: Optional[str] = None
    content: Optional[str] = None
    created_at: Optional[datetime] = None
    time_raw: Optional[str] = None
    likes: Optional[int] = None
    comments_count: Optional[int] = None
    shares: Optional[int] = None
    # False = trang đã lưu KHÔNG chứa số tương tác (không phải là 0 lượt thích)
    metrics_available: bool = False
    # Vai trò của người đăng lấy từ huy hiệu trên bài: admin / moderator / None
    author_role: Optional[str] = None
    media_urls: List[str] = Field(default_factory=list)
    comments: List[str] = Field(default_factory=list)


class PersonProfile(BaseModel):
    """Thông tin tài khoản Facebook CÁ NHÂN thu thập được (chế độ tự lưu HTML)."""
    fb_user_id: Optional[str] = None
    name: Optional[str] = None
    username: Optional[str] = None
    profile_url: Optional[str] = None
    bio: Optional[str] = None
    phones: List[PhoneCandidate] = Field(default_factory=list)
    emails: List[str] = Field(default_factory=list)
    social_links: List[SocialLink] = Field(default_factory=list)
    gender: Optional[str] = None          # Nam / Nữ / Khác
    birthday: Optional[str] = None        # 12/05/1990 hoặc 12/05
    location: Optional[str] = None        # Sống tại
    hometown: Optional[str] = None        # Đến từ
    job: Optional[str] = None
    company: Optional[str] = None
    school: Optional[str] = None
    relationship: Optional[str] = None    # Độc thân / Đã kết hôn...
    website: Optional[str] = None
    # Từ khóa rút từ bio / website / mô tả bản thân
    keywords: List[str] = Field(default_factory=list)
    is_verified: bool = False
    source_file: Optional[str] = None
    extracted_at: datetime = Field(default_factory=utcnow)


class GroupActivity(BaseModel):
    posts_per_day: Optional[float] = None
    members_per_day: Optional[float] = None
    last_seen_post_at: Optional[datetime] = None
    growth_note: Optional[str] = None


class ScanLimits(BaseModel):
    """Giới hạn quy mô MỘTLƯỢT quét - chống quá tải nguồn & bất thường."""
    max_groups: int = Field(5, description="Số nhóm tối đa mỗi lượt")
    max_accounts: int = Field(10, description="Số tài khoản (admin+mod) tối đa soi mỗi nhóm")
    max_admins_per_group: int = Field(
        10,
        description="DEPRECATED: đồng nghĩa max_accounts, giữ để đọc config cũ",
    )
    max_members_per_group: int = Field(20, description="0 = không lấy danh sách thành viên")
    max_posts_per_group: int = Field(20)
    max_comments_per_post: int = Field(0, description="0 = không lấy bình luận")

    @model_validator(mode="after")
    def _sync_deprecated_alias(self) -> "ScanLimits":
        """Giữ max_accounts và max_admins_per_group luôn khớp nhau.

        Trước đây wizard ghi `max_accounts` còn dashboard lại ghi
        `max_admins_per_group`, khiến hai nơi hiểu cùng một ý theo hai cách.
        Nay `max_accounts` là giá trị chuẩn; nếu một config cũ chỉ đặt
        `max_admins_per_group` thì giá trị đó được nâng lên `max_accounts`.
        """
        default_accounts = type(self).model_fields["max_accounts"].default
        default_alias = type(self).model_fields["max_admins_per_group"].default
        if self.max_accounts == default_accounts and self.max_admins_per_group != default_alias:
            # Config cũ chỉ đặt max_admins_per_group -> tôn trọng giá trị đó
            self.max_accounts = self.max_admins_per_group
        self.max_admins_per_group = self.max_accounts
        return self


class ContentSelection(BaseModel):
    """Chính xác MUỐN QUÉT CÁI GÌ - mỗi ô là 1 lựa chọn bật/tắt."""
    group_meta: bool = Field(True, description="Tên nhóm, privacy, mô tả, ảnh bìa")
    member_count: bool = Field(True, description="Số thành viên + biến động")
    member_list: bool = Field(False, description="Danh sách thành viên (top N)")
    new_members: bool = Field(False, description="Thành viên mới tham gia gần đây")
    admins: bool = Field(True, description="Danh sách admin/kiểm duyệt + vai trò")
    admin_contacts: bool = Field(True, description="SĐT / social / email của admin")
    admin_bio: bool = Field(True, description="Bio, ngày tham gia, tích xanh")
    posts: bool = Field(True, description="Bài đăng trong khoảng thời gian chọn")
    post_reactions: bool = Field(True, description="Lượt thích/bình luận/chia sẻ")
    post_media: bool = Field(False, description="Link ảnh/video trong bài")
    comments: bool = Field(False, description="Bình luận của bài đăng")
    activity_stats: bool = Field(True, description="Tần suất đăng, giờ hoạt động")
    keywords: bool = Field(True, description="Từ khoá nổi bật trong bài")
    link_verify: bool = Field(False, description="KIỂM TRA link còn sống (chủ động, mặc định off)")
    screenshot_evidence: bool = Field(False, description="Chụp màn hình + SHA256 làm bằng chứng")


class TimeWindow(BaseModel):
    """Khoảng thời gian muốn quét bài đăng (tính lùi từ hiện tại)."""
    days_back: int = Field(7, description="7 = bài trong 7 ngày gần nhất; 0 = không giới hạn")
    max_posts: int = Field(20, description="Số bài tối đa mỗi nhóm")


class ScanProfile(BaseModel):
    """Một bộ cấu hình quét hoàn chỉnh, lưu trong config.yaml / profiles.yaml."""
    name: str
    description: str = ""
    content: ContentSelection = Field(default_factory=ContentSelection)
    window: TimeWindow = Field(default_factory=TimeWindow)
    limits: ScanLimits = Field(default_factory=ScanLimits)
    rate_limit_seconds: float = Field(3.0, description="Giãn cách giữa 2 request")
    # Thực hiện quyết định có nên quét nhóm private ở profile này không
    include_private: bool = False


class IdentityRecord(BaseModel):
    """Một tài khoản đã thu thập được, coi là nút trong đồ thị danh tính."""
    key: str = Field(..., description="ID duy nhất nội bộ của bản ghi")
    kind: str = Field("unknown", description="admin | moderator | person")
    name: Optional[str] = None
    profile_url: Optional[str] = None
    fb_user_id: Optional[str] = None
    phones: List[str] = Field(default_factory=list)     # đã chuẩn hoá E.164
    emails: List[str] = Field(default_factory=list)
    socials: List[str] = Field(default_factory=list)    # "platform|handle"
    group_ids: List[str] = Field(default_factory=list)
    verified: bool = False
    source: Optional[str] = None


class EntityCluster(BaseModel):
    """Cụm tài khoản cùng 1 chủ thể (hoặc có liên hệ chặt)."""
    cluster_id: str
    title: str
    confidence: str = Field(..., description="certain | high")
    shared_signals: List[str] = Field(default_factory=list)
    members: List[IdentityRecord] = Field(default_factory=list)
    group_ids: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class MonitoredGroup(BaseModel):
    group_id: str
    name: Optional[str] = None
    url: str
    privacy: GroupPrivacy = GroupPrivacy.UNKNOWN
    member_count: Optional[int] = None
    description: Optional[str] = None
    admins: List[AdminProfile] = Field(default_factory=list)
    moderators: List[AdminProfile] = Field(default_factory=list)
    members: List[AdminProfile] = Field(default_factory=list)
    recent_posts: List[GroupPost] = Field(default_factory=list)
    activity: GroupActivity = Field(default_factory=GroupActivity)
    last_scanned: datetime = Field(default_factory=utcnow)
