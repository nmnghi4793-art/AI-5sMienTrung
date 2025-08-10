
import os
import re
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

# === CONFIG ===
TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Ho_Chi_Minh"))
CHOT_HOUR = int(os.getenv("CHOT_HOUR", "21"))  # 21:00
CHOT_MINUTE = int(os.getenv("CHOT_MINUTE", "0"))
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = os.getenv("GROUP_ID")  # optional; if None, bot will send to the chat it last saw

# Path to Excel
EXCEL_PATH = os.getenv("EXCEL_PATH", "danh_sach_kho_theo_doi.xlsx")

# Regex to match the two-line format:
# Line 1: <ID Kho> - <Tên Kho>
# Line 2: Ngày: dd/mm/yyyy   (ngày có thể bỏ qua)
LINE1_RE = re.compile(r'^\s*(?P<id>[^\s-]+)\s*-\s*(?P<name>.+?)\s*$', re.IGNORECASE)
LINE2_RE = re.compile(r'^\s*ngày\s*:\s*(?P<date>\d{1,2}/\d{1,2}/\d{4})\s*$', re.IGNORECASE)

# State
REQUIRED = set()     # set of tuples (id_kho, ten_kho_norm)
ACTIVE_TODAY = set() # kho (id_kho, ten_kho_norm) đã báo trong ngày (ảnh trong 5s)
PENDING = dict()     # user_id -> (t0, parsed_kho_tuple)

LAST_SEEN_CHAT_ID = None

def norm_text(s: str) -> str:
    return ' '.join(str(s).strip().lower().split())

def load_required_excel(path: str):
    df = pd.read_excel(path)
    df = df[['id_kho', 'ten_kho']].dropna()
    df['id_kho'] = df['id_kho'].astype(str).str.strip()
    df['ten_kho'] = df['ten_kho'].astype(str).str.strip()
    req = set()
    for _, row in df.iterrows():
        req.add((row['id_kho'], norm_text(row['ten_kho'])))
    return req

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""Bot online. Định dạng:
<ID Kho> - <Tên Kho>
Ngày: dd/mm/yyyy (tuỳ chọn)
Sau đó gửi ảnh trong 5 giây.""")

def parse_kho_from_text(text: str):
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return None
    m1 = LINE1_RE.match(lines[0])
    if not m1:
        return None
    id_kho = m1.group('id').strip()
    ten_kho = norm_text(m1.group('name'))
    # Optional line 2
    if len(lines) > 1:
        _ = LINE2_RE.match(lines[1])
    return id_kho, ten_kho

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LAST_SEEN_CHAT_ID
    msg = update.effective_message
    chat_id = update.effective_chat.id
    user = update.effective_user
    LAST_SEEN_CHAT_ID = chat_id

    if not msg or not msg.text:
        return

    parsed = parse_kho_from_text(msg.text)
    if not parsed:
        return

    id_kho, ten_kho = parsed
    key = (id_kho, ten_kho)

    # Only accept if kho exists in REQUIRED
    if REQUIRED and key not in REQUIRED:
        await msg.reply_text(f"⚠️ Không nhận diện kho trong danh sách theo dõi: {id_kho} - {ten_kho}")
        return

    # Create a 5s window for this user to send a photo
    now = datetime.now(TZ)
    PENDING[user.id] = (now, key)
    await msg.reply_text(f"⏱ Đã nhận '{id_kho} - {ten_kho}'. Vui lòng gửi ảnh trong 5 giây.")

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    now = datetime.now(TZ)

    pend = PENDING.get(user.id)
    if not pend:
        return  # no pending window

    t0, key = pend
    if now - t0 <= timedelta(seconds=5):
        ACTIVE_TODAY.add(key)
        PENDING.pop(user.id, None)
        id_kho, ten_kho = key
        await msg.reply_text(f"✅ Đạt 5s: {id_kho} - {ten_kho}")
    else:
        PENDING.pop(user.id, None)
        await msg.reply_text("❌ Ảnh quá thời gian 5 giây.")

async def job_chot(context: ContextTypes.DEFAULT_TYPE):
    target_chat = GROUP_ID or LAST_SEEN_CHAT_ID
    if not target_chat:
        return

    missing = sorted(list(REQUIRED - ACTIVE_TODAY))
    if not missing:
        await context.bot.send_message(chat_id=target_chat, text="✅ Tất cả kho đã có báo cáo 5s hôm nay.")
    else:
        lines = ["❌ Chưa có báo cáo 5s hôm nay:"]
        for id_kho, ten_kho in missing:
            lines.append(f"- {id_kho} - {ten_kho}")
        await context.bot.send_message(chat_id=target_chat, text="\n".join(lines))

    ACTIVE_TODAY.clear()
    PENDING.clear()

async def schedule_jobs(app: Application):
    # Schedule daily job at local time
    from datetime import time as dtime
    app.job_queue.run_daily(job_chot, time=dtime(hour=CHOT_HOUR, minute=CHOT_MINUTE, tzinfo=TZ), name="job_chot")

async def main():
    global REQUIRED
    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN env var")
    REQUIRED = load_required_excel(EXCEL_PATH)

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.PHOTO & (~filters.COMMAND), on_photo))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_text))

    await schedule_jobs(application)

    print("Bot is running...")
    await application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    asyncio.run(main())
