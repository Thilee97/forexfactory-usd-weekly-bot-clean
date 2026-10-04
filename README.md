# forexfactory-usd-weekly-bot

Telegram bot + GitHub Actions theo dõi **lịch kinh tế USD từ Forex Factory**.

## Chức năng

- Lấy Weekly Export JSON của Forex Factory.
- Chỉ lọc các sự kiện `USD`.
- Chuyển thời gian sang **Asia/Ho_Chi_Minh (GMT+7)**.
- Tạo ảnh PNG lịch USD **tuần này** và tự động gửi 1 lần vào **đầu tuần (06:00 sáng Thứ Hai)**.
- Tự động gửi tóm tắt lịch USD trong ngày vào mỗi **06:00 sáng hàng ngày**.
- Ở cột **Impact**, ảnh chỉ hiển thị **chấm tròn màu nhỏ**:
  - đỏ = High
  - cam = Medium
  - vàng = Low
- Tự động xóa sạch menu phím bấm tương tác để giao diện chat Telegram luôn sạch sẽ.
- Nhắc tin High/Medium trước **30 phút** và **10 phút**.
- Sau giờ công bố, thử lấy `Actual` trong thời gian ngắn.
- Có state chống gửi trùng.
- GitHub Actions chạy tự động mỗi **5 phút**.

## Lịch gửi tự động

1. **Lịch Tuần**: 06:00 sáng Thứ Hai hàng tuần (gửi ảnh PNG trọn gói cả tuần).
2. **Lịch Ngày**: 06:00 sáng mỗi ngày (gửi danh sách các sự kiện USD trong ngày kèm dự báo & kỳ trước).
3. **Cảnh báo Realtime**: Nhắc trước 30m / 10m và cập nhật kết quả Actual sau khi ra tin.

## Chế độ low-request

Bot ưu tiên Weekly JSON và hạn chế truy cập HTML:

- Lịch tuần, Forecast và Previous dùng Weekly JSON.
- Không scrape HTML ở mọi lượt workflow.
- Chỉ tự đọc bảng live khi có tin USD High/Medium vừa đến giờ công bố.
- Mỗi sự kiện chỉ thử lấy `Actual` tối đa **4 lần trong 45 phút**.
- Một lần đọc bảng live có thể phục vụ nhiều sự kiện.
- Nút `🔄 Cập nhật ngay` cho phép chủ động thử cập nhật live một lần.
- Nút `⏭ Tuần sau` chỉ khi bạn bấm mới mở trang `calendar?week=next`, lọc USD và tạo ảnh. Không ảnh hưởng reminder của tuần hiện tại.

## GitHub Secrets

Tạo trong:

`Settings → Secrets and variables → Actions`

Hai secret cần thiết:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

## Test ngay

Vào:

`Actions → ForexFactory USD Weekly Bot → Run workflow`

Bật:

`Send the weekly USD calendar image now = true`

rồi chạy workflow.

## Lịch tự động

```yaml
schedule:
  - cron: "*/10 * * * *"
```

Ảnh tuần mặc định được gửi ở lần chạy đầu tiên sau **07:00 sáng thứ Hai**
theo giờ Việt Nam.

Trong `bot.py`:

```python
WEEKLY_SEND_WEEKDAY = 0
WEEKLY_SEND_HOUR = 7
```

## Reminder

Mặc định bot nhắc các tin:

```python
NOTIFY_IMPACTS = {"High", "Medium"}
```

## State

`state/state.json` lưu:

- tuần đã gửi ảnh tự động;
- reminder đã gửi;
- Actual đã gửi;
- số lần thử lấy Actual;
- Telegram update ID đã xử lý.

## Cấu trúc repo

```text
forexfactory-usd-weekly-bot/
├── .github/
│   └── workflows/
│       └── bot.yml
├── state/
│   └── state.json
├── bot.py
├── requirements.txt
└── README.md
```

## Nguồn dữ liệu

Weekly Export:

```text
https://nfs.faireconomy.media/ff_calendar_thisweek.json

Tuần sau không có endpoint `ff_calendar_nextweek.json`; bot đọc trang:

```text
https://www.forexfactory.com/calendar?week=next
```

Phần `Actual` được thử lấy từ bảng Calendar live chỉ gần giờ công bố.

## Lưu ý

- GitHub scheduled workflows có thể chạy trễ vài phút.
- Thời gian sự kiện kinh tế có thể thay đổi.
- Nếu phần live không đọc được, lịch tuần và reminder từ Weekly JSON vẫn tiếp tục hoạt động.
