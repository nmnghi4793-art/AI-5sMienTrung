# Bot báo cáo ảnh 5s theo kho (Telegram)

## Cấu hình
- Biến môi trường bắt buộc:
  - BOT_TOKEN — lấy từ @BotFather
- Biến khuyến nghị:
  - GROUP_ID — ID group để bot gửi báo cáo lúc chốt (nếu không set, bot sẽ gửi vào group gần nhất nó thấy)
  - TZ_NAME — mặc định Asia/Ho_Chi_Minh
  - CHOT_HOUR — giờ chốt trong ngày (mặc định 21)
  - CHOT_MINUTE — phút chốt (mặc định 0)
  - EXCEL_PATH — đường dẫn file Excel danh sách kho (mặc định danh_sach_kho_theo_doi.xlsx)

## File dữ liệu
- danh_sach_kho_theo_doi.xlsx — sheet mặc định, cột bắt buộc: id_kho, ten_kho

## Cú pháp tin nhắn trong group
<ID Kho> - <Tên Kho>
Ngày: dd/mm/yyyy   (dòng này có thể bỏ qua)

Sau đó gửi ảnh trong vòng 5 giây. Bot sẽ ghi nhận kho đã báo.

## Chạy trên máy (test nhanh)
pip install -r requirements.txt
export BOT_TOKEN=...   # macOS/Linux
python bot.py

## Railway (online 24/7 – khuyến nghị)
1. Tạo tài khoản tại railway.app và tạo Project mới.
2. Upload 4 file: bot.py, requirements.txt, danh_sach_kho_theo_doi.xlsx, (tuỳ chọn) README_DEPLOY.md.
3. Trong Variables:
   - BOT_TOKEN, TZ_NAME=Asia/Ho_Chi_Minh, CHOT_HOUR=21, CHOT_MINUTE=0
   - (Tuỳ chọn) GROUP_ID
4. Start Command: python bot.py

## PythonAnywhere (lưu ý)
- Free tier thường chặn outbound internet tới Telegram. Khuyến nghị dùng Railway.
- Nếu dùng bản trả phí: tạo virtualenv, cài requirements, chạy bot.py dưới Always-on task.
