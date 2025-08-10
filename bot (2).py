
import os
import re
import logging
from datetime import datetime, time as dtime
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

BOT_TOKEN = os.getenv("BOT_TOKEN")  # must be set in Railway Variables
EXCEL_PATH = os.getenv("EXCEL_PATH", "danh_sach_nv_theo_id_kho.xlsx")
# Optional fixed group to send report to; if not set, bot will send to all groups it has seen
GROUP_ID = os.getenv("GROUP_ID")  # e.g. "-1001234567890" (string)

CHOT_HOUR = int(os.getenv("CHOT_HOUR", "21"))
CHOT_MINUTE = int(os.getenv("CHOT_MINUTE", "0"))

# ===== LOGGING =====
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ===== STATE =====
# REQUIRED: set of id_kho strings
# KHO_MAP: id_kho -> normalized ten_kho (for display)
REQUIRED = set()
KHO_MAP = {}
REPORTED_TODAY = set()   # set of id_kho that have reported today
SEEN_CHATS = set()       # chat ids where the bot has seen messages (for broadcasting report)

# Regex: line like "DN01 - GXT Đà Nẵng" (allow extra spaces)
LINE1_RE = re.compile(r'^\s*(?P<id>[^\s-]+)\s*-\s*(?P<name>.+?)\s*$')

def norm_text(s: str) -> str:
    return ' '.join(str(s).strip().lower().split())

def load_required_excel(path: str):
    global REQUIRED, KHO_MAP
    df = pd.read_excel(path)
    # accept either two columns or with extras; require id_kho and ten_kho
    df = df[['id_kho', 'ten_kho']].dropna()
    df['id_kho'] = df['id_kho'].astype(str).str.strip()
    df['ten_kho'] = df['ten_kho'].astype(str).str.strip()
    REQUIRED = set(df['id_kho'].tolist())
    KHO_MAP = {row['id_kho']: norm_text(row['ten_kho']) for _, row in df.iterrows()}
    logger.info("Loaded %d kho from %s", len(REQUIRED), path)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot online. Cú pháp:\n"
        "<ID Kho> - <Tên Kho>\n"
        "Ví dụ:\nDN01 - GXT Đà Nẵng\n\n"
        "Gửi kèm ảnh 5S (không cần reply). 21:00 mỗi ngày bot sẽ báo kho chưa báo cáo."
    )

def parse_id_from_text(text: str):
    if not text:
        return None, None
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return None, None
    m1 = LINE1_RE.match(lines[0])
    if not m1:
        return None, None
    id_kho = m1.group('id').strip()
    ten_kho_norm = norm_text(m1.group('name'))
    return id_kho, ten_kho_norm

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    SEEN_CHATS.add(chat.id)

    # Prefer caption text if photo, else message text
    msg = update.effective_message
    text = None
    if msg.caption:
        text = msg.caption
    elif msg.text:
        text = msg.text

    if not text:
        return

    id_kho, ten_norm = parse_id_from_text(text)
    if not id_kho:
        return

    # Accept if id_kho exists in REQUIRED (ignore name mismatch to be tolerant)
    if id_kho not in REQUIRED:
        # Optional: notify unknown
        logger.info("Unknown id_kho in message: %s", id_kho)
        return

    # If there is a photo OR plain text ok (per user request)
    has_photo = bool(msg.photo)
    if has_photo or text:
        REPORTED_TODAY.add(id_kho)
        display_name = KHO_MAP.get(id_kho, ten_norm or "")
        try:
            await msg.reply_text(f"✅ Đã ghi nhận: {id_kho} - {display_name}")
        except Exception as e:
            logger.warning("Reply failed: %s", e)

async def job_send_report(context: ContextTypes.DEFAULT_TYPE):
    # Build message
    missing = sorted(list(REQUIRED - REPORTED_TODAY))
    if not missing:
        report = "✅ Tất cả kho đã có báo cáo 5S hôm nay."
    else:
        lines = ["❌ Chưa có báo cáo 5S hôm nay:"]
        for idk in missing:
            lines.append(f"- {idk} - {KHO_MAP.get(idk, '')}")
        report = "\n".join(lines)

    # Decide where to send
    targets = set()
    if GROUP_ID:
        targets.add(int(GROUP_ID))
    else:
        targets |= SEEN_CHATS

    # Send to all targets
    for chat_id in targets:
        try:
            await context.bot.send_message(chat_id=chat_id, text=report)
        except Exception as e:
            logger.warning("Send report to %s failed: %s", chat_id, e)

    # Reset for next day
    REPORTED_TODAY.clear()

def schedule_jobs(app):
    jq = app.job_queue
    if jq is None:
        logger.warning("JobQueue is None. Install PTB with [job-queue].")
        return
    jq.run_daily(
        job_send_report,
        time=dtime(hour=CHOT_HOUR, minute=CHOT_MINUTE, tzinfo=TZ),
        name="daily_report",
    )

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN env var")

    load_required_excel(EXCEL_PATH)

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, on_message))

    schedule_jobs(app)

    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
