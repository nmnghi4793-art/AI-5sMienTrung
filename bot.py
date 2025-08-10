import logging
import pandas as pd
from datetime import datetime, time
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from telegram.ext import CommandHandler
import asyncio

# ====== CẤU HÌNH ======
BOT_TOKEN = "NHẬP_TOKEN_BOT_TẠI_ĐÂY"
EXCEL_FILE = "danh_sach_nv_theo_id_kho.xlsx"
GROUP_ID = -1001234567890  # ID nhóm cần gửi báo cáo (số âm)
REPORT_TIME = time(21, 0)  # 21:00

# ====== LOGGING ======
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ====== ĐỌC DANH SÁCH KHO ======
df_kho = pd.read_excel(EXCEL_FILE)
df_kho["id_kho"] = df_kho["id_kho"].astype(str)
reported_kho = set()

# ====== HÀM XỬ LÝ TIN NHẮN ======
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.caption or update.message.text
    if not text:
        return
    
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) >= 1 and "-" in lines[0]:
        kho_id = lines[0].split("-")[0].strip()
        if kho_id in df_kho["id_kho"].values:
            reported_kho.add(kho_id)
            logger.info(f"Kho {kho_id} đã báo cáo")
        else:
            logger.info(f"Kho không tồn tại: {kho_id}")

# ====== HÀM GỬI BÁO CÁO ======
async def send_report(context: ContextTypes.DEFAULT_TYPE):
    missing = df_kho[~df_kho["id_kho"].isin(reported_kho)]
    if missing.empty:
        msg = "✅ Tất cả kho đã báo cáo 5S hôm nay."
    else:
        msg = "⚠️ Kho chưa báo cáo 5S hôm nay:\n"
        for _, row in missing.iterrows():
            msg += f"- {row['id_kho']} - {row['ten_kho']}\n"

    await context.bot.send_message(chat_id=GROUP_ID, text=msg)

# ====== LỆNH /start ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot đã hoạt động. Gửi tin nhắn theo cú pháp:\n<ID Kho> - <Tên Kho>\nSau đó gửi ảnh 5S."
    )

# ====== MAIN ======
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_message))

    app.job_queue.run_daily(send_report, REPORT_TIME)

    app.run_polling()

if __name__ == "__main__":
    main()
