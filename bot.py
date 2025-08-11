
import os
import re
import logging
import json
from datetime import time as dtime, datetime
from zoneinfo import ZoneInfo

import pandas as pd
from PIL import Image
import io
import imagehash
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


# ===== DUPLICATE DETECTION STORAGE (with sender & date) =====
DUP_STORAGE = os.getenv("DUP_STORAGE", "seen_images.json")
PHASH_THRESHOLD = int(os.getenv("PHASH_THRESHOLD", "8"))

def _load_seen():
    try:
        with open(DUP_STORAGE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_seen(data):
    try:
        with open(DUP_STORAGE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("Cannot save seen storage: %s", e)

def _today_key():
    return datetime.now(TZ).strftime("%Y-%m-%d")

def _find_duplicate_info(kho_id: str, file_uid: str, phash_hex: str):
    data = _load_seen()
    # Search all days to find when it was first seen
    try:
        from imagehash import hex_to_hash
        h_new = hex_to_hash(phash_hex) if phash_hex else None
    except Exception:
        h_new = None

    for day, dday in data.items():
        bucket = dday.get(kho_id, {})
        for rec in bucket.get("uids", []):
            if file_uid and rec.get("uid") == file_uid:
                return True, day, rec.get("sender")
        if h_new and "phash" in bucket:
            for rec in bucket["phash"]:
                try:
                    if rec.get("hex") and (h_new - hex_to_hash(rec["hex"]) <= PHASH_THRESHOLD):
                        return True, day, rec.get("sender")
                except Exception:
                    continue
    return False, None, None

def _remember_image(kho_id: str, file_uid: str, phash_hex: str, sender: str):
    data = _load_seen()
    day = _today_key()
    dday = data.setdefault(day, {})
    bucket = dday.setdefault(kho_id, {"uids": [], "phash": []})
    if file_uid and not any(rec.get("uid") == file_uid for rec in bucket["uids"]):
        bucket["uids"].append({"uid": file_uid, "sender": sender})
    if phash_hex and not any(rec.get("hex") == phash_hex for rec in bucket["phash"]):
        bucket["phash"].append({"hex": phash_hex, "sender": sender})
    _save_seen(data)


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

    

# === duplicate detection with metadata ===
file_uid = None
phash_hex = None
if msg.photo:
    try:
        file = await msg.photo[-1].get_file()
        file_uid = getattr(file, "file_unique_id", None) or getattr(msg.photo[-1], "file_unique_id", None)
        buf = io.BytesIO()
        await file.download(out=buf)
        buf.seek(0)
        img = Image.open(buf).convert("RGB")
        phash_hex = str(imagehash.phash(img))
    except Exception as e:
        logger.warning("Download/Hash failed: %s", e)

sender_name = (update.effective_user.full_name if update.effective_user else "") or ""

if file_uid or phash_hex:
    is_dup, day_prev, sender_prev = _find_duplicate_info(id_kho, file_uid, phash_hex)
    if is_dup:
        who = f" bởi {sender_prev}" if sender_prev else ""
        when = f" ngày {day_prev}" if day_prev else ""
        await msg.reply_text(f"⚠️ Ảnh này trùng/na ná ảnh đã gửi{who}{when}. Vui lòng chụp ảnh mới.")
        return
    _remember_image(id_kho, file_uid or "", phash_hex or "", sender_name)
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
