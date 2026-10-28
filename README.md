# OSINT Group Monitor — Thu thập thông tin hội nhóm Facebook

Bộ công cụ giúp **khám phá và giám sát** thông tin công khai từ các hội
nhóm Facebook: tên nhóm, loại nhóm, số lượng thành viên, quản trị viên /
kiểm duyệt viên, bài đăng mới nhất và hoạt động của nhóm. Đặc biệt, công cụ
tự động **nhận diện & bốc tách số điện thoại cùng liên kết mạng xã hội**
của tài khoản quản trị / kiểm duyệt, hỗ trợ giảm đáng kể thời gian giám sát
và xác minh chủ tài khoản.

> **Chỉ thu thập dữ liệu CÔNG KHAI.** Công cụ KHÔNG bypass đăng nhập, KHÔNG
> phá vỡ cơ chế bảo mật, KHÔNG chạm vào nội dung riêng tư. Người dùng tự
> chịu trách nhiệm tuân thủ Điều khoản dịch vụ (ToS) của Facebook và pháp
> luật địa phương (ví dụ Nghị định 13/2023/NĐ-CP về dữ liệu cá nhân tại
> Việt Nam, GDPR tại EU). Chỉ dùng cho mục đích hợp pháp.

---

## 1. Kiến trúc

```
fsafa/
├── main.py                  # CLI entrypoint
├── pipeline.py              # Orchestrator kết nối các module
├── models.py                # Pydantic models chuẩn
├── storage.py               # SQLite (idempotent, có lịch sử)
├── exporter.py              # Xuất CSV/JSON
├── dashboard.py             # Streamlit dashboard
├── config.yaml              # Cấu hình mục tiêu & giới hạn
├── requirements.txt
├── test_extractor.py        # Unit test phần bốc tách liên hệ
├── scraper/
│   ├── utils.py             # HttpClient rate-limit + retry
│   ├── group_scraper.py     # Meta nhóm (name, privacy, members)
│   ├── profile_scraper.py   # Admin/mod profile + vai trò
│   ├── person_scraper.py    # ★ Profile CÁ NHÂN (email, giới tính, sinh nhật...)
│   ├── post_scraper.py      # Bài đăng & tín hiệu hoạt động
│   ├── region_discovery.py  # Seed nhóm theo khu vực + chủ đề (Google CSE)
│   ├── entity_linker.py       # ★ Ghép tài khoản cùng chủ thể (offline)
│   ├── scan_profile.py      # ★ Profile quét (preset + wizard chọn lựa)
│   ├── fb_time.py           # ★ Parse thời gian FB -> lọc theo khoảng ngày
│   ├── manual_importer.py   # ★ Import HTML tự lưu (nhóm riêng tư)
│   └── link_extractor.py    # ★ Bốc tách SĐT/email + social links
```

## 2. Cài đặt

```powershell
cd D:\fsafa
python -m pip install -r requirements.txt
```

## 3. Cấu hình mục tiêu

Sửa `config.yaml`:

```yaml
target_groups:
  - id: "123456789"
    url: "https://www.facebook.com/groups/123456789"
    note: "Nhóm công khai cần giám sát"
scraper:
  request_delay: 3.0     # giây giữa 2 request (chống quá tải nguồn)
```

## 4. Chọn cách quét (Scan Profile)

Mỗi lần quét được định nghĩa bởi 1 **profile** gồm: quét bao nhiêu nhóm / tài
khoản / bài đăng, trong khoảng thời gian nào, và quét những nội dung gì.

```powershell
python main.py profiles      # xem các profile
python main.py configure     # WIZARD tự chọn: profile sẽ ghi vào config.yaml
python main.py run --profile standard   # chạy theo profile
```

**5 preset có sẵn:**

| Profile | Quy mô/lượt | Nội dung | Phù hợp |
|---|---|---|---|
| `quick` | 5 nhóm, 5 bài/nhóm, 3 ngày | Meta + thành viên + admin | Quét nhanh mỗi ngày |
| `standard` | 10 nhóm, 20 bài/nhóm, 7 ngày | Đầy đủ + liên hệ admin | Mặc định |
| `deep` | 20 nhóm, 50 bài/nhóm, 30 ngày | + media, từ khoá | Quét sâu định kỳ |
| `private_daily` | 2 nhóm, 30 bài/nhóm, 1 ngày | + bằng chứng | Nhóm riêng tư, hằng ngày |
| `audit_evidence` | 3 nhóm, 100 bài/nhóm, 90 ngày | + bình luận + bằng chứng | Phục vụ báo cáo/đối chứng |

**15 mục nội dung chọn được** (wizard `configure`): meta nhóm, số thành viên,
danh sách thành viên, thành viên mới, admin + vai trò, liên hệ admin, bio
admin, bài đăng, reaction, media, bình luận, thống kê hoạt động, từ khoá,
kiểm tra link (mặc định off), chụp màn hình bằng chứng.

### Báo cáo thay đổi (quét định kỳ)

Mỗi lần quét lưu **snapshot** vào SQLite. Lần sau sẽ tự báo delta:

```
=== THAY ĐỔI SO VỚI LẦN TRƯỚC ===
  [987654321] Thành viên: 12000 -> 12350
  [987654321] Admin mới: ['facebook.com/nguyen-moi']
  [987654321] Số bài public: 42 -> 51
```

Xem lại bất cứ lúc nào: `python main.py diff`

### Ghép tài khoản cùng chủ thể (entity linking)

```powershell
python main.py entities
```

Tự chạy lại sau mỗi lần `run`/`import`. **Không tốn thêm request** — chỉ ghép
dữ liệu đã có trong DB theo 4 tín hiệu chắc chắn (trùng = cùng người):

| Tín hiệu | Mức tin cậy |
|---|---|
| Cùng profile FB (id/username) | Chắc chắn |
| Cùng SĐT (đã chuẩn hoá `+84...`) | Chắc chắn |
| Cùng email | Chắc chắn |
| Cùng social handle (TikTok/IG/Telegram...) | Cao |

Không gộp theo "tên giống nhau" để tránh báo cáo sai. Kết quả:

```
E001 | certain | Nguyen Van A | 2 tài khoản | phone:+84912345678; social:tiktok|nguyena
                                             nhóm liên quan: 987654321
```

Xuất `reports/cum_danh_tinh.xlsx` (sheet *Cụm danh tính*) + `.csv`.

> Lưu ý pháp lý: đây là **suy luận kỹ thuật** từ dữ liệu công khai, không
> phải xác nhận pháp lý danh tính — cần xác minh thêm trước khi kết luận về
> một cá nhân cụ thể.

## 5. Hai chế độ thu thập

### Mode A — Quét nhóm CÔNG KHAI (không cần đăng nhập)

```powershell
python main.py run --profile standard
```

### Chế độ seed theo khu vực + chủ đề (phát hiện nhóm mới)

Kết quả **kiểm chứng thực tế (2026)** — Meta đã gỡ Graph API tìm nhóm theo
region; Facebook search công khai bắt đăng nhập và cá nhân hóa kết quả; Bing
đã **bỏ `site:` operator**; DuckDuckGo trả HTTP 202, Brave 429 (chặn tự động).
Con đường khả dụng duy nhất là **Google Custom Search JSON API** (miễn phí
100 query/ngày, link trực tiếp, JSON ổn định):

```powershell
# 1. Lấy key 1 lần: https://programmablesearchengine.google.com/
setx GOOGLE_API_KEY "AIza..."
setx GOOGLE_CSE_ID  "cx..."

# 2. Sửa discovery trong config.yaml: regions + topics của bạn
python main.py discover                      # -> seed_groups.yaml

# 3. Quét các nhóm vừa tìm được
python main.py run --seed seed_groups.yaml
```

**Không có API key?** Tự tạo seed list rồi nạp vào pipeline:

```yaml
# seed_groups.yaml
target_groups:
  - id: "123456789"
    url: "https://www.facebook.com/groups/123456789"
    note: "tu tim tay"
```
```powershell
python main.py run --seed seed_groups.yaml
```

### Mode B — Nhóm RIÊNG TƯ: bạn tự xem, tool lo bóc tách

Quy trình (không automation → không có tín hiệu bot, không mất account):

1. Bạn tự đăng nhập **account dự phòng**, mở trang nhóm riêng tư cần xem
2. `Ctrl+S` lưu từng trang vào thư mục `html_dump/` theo quy ước tên:

| Tên file | Ý nghĩa |
|---|---|
| `group_987654321.html` | Trang nhóm (tên, privacy, số thành viên) |
| `group_987654321_posts.html` | Tab "Bài viết" của nhóm (tuỳ chọn) |
| `profile_987654321_admin.html` | Trang cá nhân admin, gắn vào nhóm `987654321` |
| `profile_1_admin.html` | Trang admin (tự gắn vào nhóm duy nhất trong folder) |
| `profile_5_mod.html` | Trang kiểm duyết viên |
| `person_ten.html` | ★ **Profile CÁ NHÂN** — bóc đầy đủ hồ sơ công khai (xem bảng dưới) |

3. Import:

```powershell
python main.py import html_dump
```

Tool tự parse, **bốc tách SĐT + link mạng xã hội**, lưu DB và xuất `reports/bao_cao.xlsx`.

**Profile cá nhân** (`person_*.html`) bóc thêm: email, giới tính, sinh nhật,
sống tại, đến từ, công việc, nơi làm việc, học vấn, tình trạng quan hệ,
tích xanh — xuất ra `reports/ho_so_ca_nhan.csv` + sheet `Ho so ca nhan`.

> ⚠️ **Trách nhiệm pháp lý riêng cho dữ liệu cá nhân:** theo Nghị định
> 13/2023/NĐ-CP, việc thu thập xử lý dữ liệu cá nhân (SĐT, email, ngày
> sinh, địa chỉ…) bắt buộc phải có sự đồng ý của chủ thể hoặc căn cứ hợp
> pháp khác. Chỉ sử dụng với mục đích hợp pháp (xác minh chủ quản trị/kiểm
> duyệt trong khuôn khổ uỷ quyền, an ninh mạng, báo chí/học thuật). Mỗi bản
> ghi đều ghi `source_file` để truy vết nguồn gốc.

> Lưu ý: lưu HTML kiểu "Webpage, HTML Only" (không cần folder kèm theo).
> Mỗi lần nên xem với tốc độ tự nhiên (vài nhóm/ngày), tránh vào hàng chục
> nhóm liên tiếp vì đó bất thường so với người dùng thật.

## 7. Các lệnh khác

```powershell
python main.py profiles                       # Xem các profile quét
python main.py configure                      # Wizard cấu hình profile mới
python main.py discover                       # Seed nhóm theo khu vực + chủ đề
python main.py run --profile standard         # Quét theo profile
python main.py run --seed seed_groups.yaml    # Gộp thêm nhóm từ seed
python main.py diff                             # Báo cáo thay đổi lần quét trước
python main.py entities                         # Ghép tài khoản cùng chủ thể
python main.py import html_dump               # Parse HTML tự lưu
python main.py list                           # Nhóm đã lưu
python main.py group 123456789                # Liên hệ admin/mod của 1 nhóm
python main.py person                         # Hồ sơ cá nhân đã bóc tách
streamlit run dashboard.py                    # Dashboard trực quan
```

## 8. Kết quả đầu ra

Sau khi chạy, các file được sinh trong `reports/`:

| File | Nội dung |
|------|----------|
| `bao_cao.xlsx` | ★ **Excel** — 3 sheet: *Tổng quan nhóm*, *Liên hệ QT/KD*, *Bài đăng public* |
| `contacts.csv` | ★ Liên hệ quản trị/kiểm duyệt: vai trò, SĐT đã chuẩn hoá, link social |
| `summary.csv` | Tổng hợp từng nhóm: privacy, thành viên, số admin/mod, số bài public |
| `full.json` | Payload đầy đủ (mô hình hóa bằng Pydantic) |

CSDL: `data/monitor.db` (SQLite) giúp chạy lại idempotent, có lịch sử.

## 9. Phần bốc tách liên hệ (`scraper/link_extractor.py`)

- **Số điện thoại:** nhận diện dạng VN (`0xxx`, `+84`, `0084`) và quốc tế,
  chuẩn hoá về E.164 (`+84...`).
- **Liên kết mạng xã hội:** Facebook, Instagram, TikTok, X/Twitter, LinkedIn,
  YouTube, Telegram (`t.me`), Zalo (`zalo.me`), VK, Reddit — kể cả dạng
  viết tay không có `https://`.

Ví dụ input:

```
Liên hệ: 0912 345 678 | facebook.com/abc.def | tiktok.com/@osint
```

Output chuẩn hoá: phone `+84912345678`, link `Facebook: facebook.com/abc.def`...

## 10. Giới hạn & lưu ý kỹ thuật

- Facebook thay đổi HTML liên tục và nhiều endpoint yêu cầu đăng nhập.
  Module chỉ **parse phần public render sẵn**; nếu gặp login-wall thì ghi
  log và bỏ qua (không cố bypass).
- Với bài đăng & danh sách admin, kết quả phụ thuộc vào public render;
  để đầy đủ hơn, cân nhắc dùng **Graph API chính thức** (yêu cầu đăng ký
  ứng dụng, `groups_manage`… chỉ với quyền hợp pháp).
- Luôn đặt `request_delay` hợp lý (≥ 2–3s); tôn trọng `robots.txt` & tải
  của máy chủ nguồn.

## 11. Khuyến cáo pháp lý & đạo đức

1. Chỉ xử lý dữ liệu công khai; không thu thập dữ liệu cá nhân nhạy cảm
   nếu không có cơ sở pháp lý hợp pháp.
2. Mục đích hợp lệ: nghiên cứu an ninh mạng, giám sát thương hiệu/cộng
   đồng được uỷ quyền, báo chí/học thuật.
3. Không dùng để quấy rối, thao túng, hoặc xâm phạm quyền riêng tư.
4. Bảo quản dữ liệu an toàn, không công khai hoá thông tin cá nhân.

## 12. Kiểm thử

Chạy toàn bộ bằng pytest (không cần mạng):

```powershell
pip install pytest
python -m pytest              # toàn bộ
python -m pytest -v           # chi tiết từng test
python -m pytest test_regressions.py -v   # chỉ nhóm hồi quy
```

| File | Phạm vi |
|---|---|
| `test_extractor.py` | Bốc tách SĐT / email / social link |
| `test_time.py` | Parse thời gian tiếng Việt của FB + lọc theo khoảng ngày |
| `test_profiles.py` | Preset profile, `save_profile`, delta snapshot |
| `test_wizard.py` | Wizard cấu hình (mock input) |
| `test_entities.py` | Gộp danh tính offline (union-find) |
| `test_regression_profile.py` | Nhận diện trang nhóm / trang hồ sơ |
| `test_log_capture.py` | Log của mỗi lần quét không lẫn sang phiên khác |
| `test_regressions.py` | Hồi quy cho các lỗi đã sửa (xem bên dưới) |

### Nhóm hồi quy

`test_regressions.py` ghi lại đúng các lỗi từng xảy ra, để chúng không quay
lại:

| Lỗi | Biểu hiện |
|---|---|
| `nghìn` có dấu không được nhân | "5 nghìn thành viên" ra `5` thay vì `5000` |
| `Kinh doanh` chứa chuỗi `kin` | Nhóm công khai bị gán nhầm là nhóm kín → sai bộ lọc |
| `dedupe_people` xoá trong lúc duyệt | Mất bản ghi, hoặc báo đã xoá mà bản ghi vẫn còn |
| `group(2)` trên regex 1 nhóm bắt | `IndexError` khi đếm số thành viên từ JSON |
| `max_retries` không dùng | Hardcode 3 lần thử, cấu hình bị bỏ qua |
| Nội dung ghi thô vào ô Excel | `=cmd|'/c calc'!A0` thành công thức khi mở file |
| `report_dir` bị hardcode `reports/` | Báo cáo ghi sai chỗ so với cấu hình |
| UID hồ sơ lấy từ toàn văn HTML | Gán nhầm ID của bạn bè cho chủ trang |
| Mã cụm danh tính theo vị trí | `E002` của tháng trước âm thầm thành cụm người khác |
| Log gắn vào logger root | Log của phiên này lọt sang màn hình phiên khác |
```
