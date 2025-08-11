
import os
import re
import logging
from datetime import time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

# ===== CONFIG =====
TZ_NAME = os.getenv("TZ_NAME", "Asia/Ho_Chi_Minh")
TZ = ZoneInfo(TZ_NAME)

BOT_TOKEN = os.getenv("BOT_TOKEN")           # set trong Railway
EXCEL_PATH = os.getenv("EXCEL_PATH", "danh_sach_nv_theo_id_kho.xlsx")
GROUP_ID = os.getenv("GROUP_ID")             # "-100xxxxxxxxxx" (string) - tuỳ chọn

CHOT_HOUR = int(os.getenv("CHOT_HOUR", "21"))
CHOT_MINUTE = int(os.getenv("CHOT_MINUTE", "0"))

# ===== LOGGING =====
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ===== STATE =====
REQUIRED = set()      # set(id_kho)
KHO_MAP = {}          # id_kho -> ten_kho (normalized)
REPORTED_TODAY = set()
SEEN_CHATS = set()

# Regex “thoáng”: ID = 2-12 ký tự chữ/số, linh hoạt khoảng trắng quanh dấu '-'
LINE1_RE = re.compile(r'^\s*(?P<id>[A-Za-z0-9]{2,12})\s*-\s*(?P<name>.+?)\s*$')

def norm_text(s: str) -> str:
    return ' '.join(str(s).strip().lower().split())

def load_required_excel(path: str):
    global REQUIRED, KHO_MAP
    df = pd.read_excel(path)[['id_kho', 'ten_kho']].dropna()
    df['id_kho'] = df['id_kho'].astype(str).str.strip()
    df['ten_kho'] = df['ten_kho'].astype(str).str.strip()
    REQUIRED = set(df['id_kho'].tolist())
    KHO_MAP = {row['id_kho']: norm_text(row['ten_kho']) for _, row in df.iterrows()}
    logger.info("Loaded %d kho from %s", len(REQUIRED), path)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Cú pháp:\n<ID Kho> - <Tên Kho>\nVD: DN01 - GXT Đà Nẵng\n"
        "Gửi kèm ảnh 5S (không cần reply). 21:00 sẽ báo kho chưa báo."
    )

def parse_id_from_text(text: str):
    if not text:
        return None, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = LINE1_RE.match(line)
        if m:
            id_kho = m.group('id').strip()
            ten_kho_norm = norm_text(m.group('name'))
            return id_kho, ten_kho_norm
    return None, None

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    SEEN_CHATS.add(chat.id)

    msg = update.effective_message
    text = msg.caption or msg.text
    if not text:
        return

    id_kho, ten_norm = parse_id_from_text(text)

    # Chỉ nhắc khi có dấu '-' mà không parse được (tránh làm phiền chat thường)
    if id_kho is None:
        if '-' in text:
            await msg.reply_text("❌ Sai cú pháp.\nMẫu: <ID Kho> - <Tên Kho>\nVD: DN01 - GXT Đà Nẵng")
        return

    if id_kho not in REQUIRED:
        await msg.reply_text(f"❌ Không có ID kho '{id_kho}' trong danh sách theo dõi.")
        return

    REPORTED_TODAY.add(id_kho)
    display_name = KHO_MAP.get(id_kho, ten_norm or "")
    try:
        await msg.reply_text(f"✅ Đã ghi nhận: {id_kho} - {display_name}")
    except Exception as e:
        logger.warning("Reply failed: %s", e)

async def job_send_report(context: ContextTypes.DEFAULT_TYPE):
    missing = sorted(list(REQUIRED - REPORTED_TODAY))
    if not missing:
        report = "✅ Tất cả kho đã có báo cáo 5S hôm nay."
    else:
        lines = ["❌ Chưa có báo cáo 5S hôm nay:"]
        for idk in missing:
            lines.append(f"- {idk} - {KHO_MAP.get(idk, '')}")
        report = "\n".join(lines)

    targets = set()
    if GROUP_ID:
        targets.add(int(GROUP_ID))
    else:
        targets |= SEEN_CHATS

    for chat_id in targets:
        try:
            await context.bot.send_message(chat_id=chat_id, text=report)
        except Exception as e:
            logger.warning("Send report to %s failed: %s", chat_id, e)

    REPORTED_TODAY.clear()

def schedule_jobs(app):
    jq = app.job_queue
    if jq is None:
        logger.warning("JobQueue is None. Install PTB with [job-queue].")
        return
    jq.run_daily(job_send_report, time=dtime(hour=CHOT_HOUR, minute=CHOT_MINUTE, tzinfo=TZ), name="daily_report")

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN env var")

    load_required_excel(EXCEL_PATH)

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, on_message))

    schedule_jobs(app)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
