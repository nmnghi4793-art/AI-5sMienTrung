# bot.py
import os
import re
import json
import hashlib
from datetime import datetime, date, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ========= CẤU HÌNH =========
EXCEL_PATH = "danh_sach_nv_theo_id_kho.xlsx"  # Excel: cột id_kho, ten_kho
HASH_DB_PATH = "hashes.json"                  # lưu hash ảnh (phát hiện trùng)
SUBMIT_DB_PATH = "submissions.json"           # lưu ID đã nộp theo ngày
TZ = ZoneInfo("Asia/Ho_Chi_Minh")             # múi giờ VN
REPORT_HOUR = 21                              # 21:00 hằng ngày
TEXT_PAIR_TIMEOUT = 120                       # giây giữ caption dùng chung
REPORT_CHAT_IDS = [-1002688907477]            # ID group nhận báo cáo 21:00 (có thể thêm nhiều id)

# ENV: BOT_TOKEN (bắt buộc). Có thể thêm REPORT_CHAT_IDS trong ENV để override, ví dụ: "-1001,-1002"

# ========= JSON UTILS =========
def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def load_hash_db():
    return _load_json(HASH_DB_PATH, {"items": []})

def save_hash_db(db):
    _save_json(HASH_DB_PATH, db)

def load_submit_db():
    return _load_json(SUBMIT_DB_PATH, {})  # { "YYYY-MM-DD": ["id1","id2",...] }

def save_submit_db(db):
    _save_json(SUBMIT_DB_PATH, db)

# ========= KHO MAP =========
def load_kho_map():
    df = pd.read_excel(EXCEL_PATH)
    cols = {c.lower().strip(): c for c in df.columns}
    if "id_kho" not in cols or "ten_kho" not in cols:
        raise RuntimeError("Excel phải có cột 'id_kho' và 'ten_kho'")
    id_col = cols["id_kho"]; ten_col = cols["ten_kho"]
    df = df[[id_col, ten_col]].dropna()
    df[id_col] = df[id_col].astype(str).str.strip()
    df[ten_col] = df[ten_col].astype(str).str.strip()
    return dict(zip(df[id_col], df[ten_col]))

# ========= PARSE TEXT =========
ID_RX = re.compile(r"(\d{1,10})")
DATE_RX = re.compile(r"(?:ngày|date|ngay)\s*[:\-]?\s*(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{2,4})", re.IGNORECASE)

def parse_text_for_id_and_date(text: str):
    """Có ID là được; ngày (tuỳ chọn) 'Ngày: dd/mm/yyyy'."""
    _id = None
    _date = date.today()
    if not text:
        return None, _date

    m_id = ID_RX.search(text)
    if m_id:
        _id = m_id.group(1)

    m_d = DATE_RX.search(text)
    if m_d:
        d, m, y = m_d.groups()
        d, m, y = int(d), int(m), int(y)
        if y < 100: y += 2000
        try:
            _date = date(y, m, d)
        except Exception:
            _date = date.today()

    return _id, _date

# ========= ẢNH & HASH =========
async def get_file_bytes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bytes:
    photo = update.message.photo[-1]  # ảnh lớn nhất
    tg_file = await context.bot.get_file(photo.file_id)
    b = await tg_file.download_as_bytearray()
    return bytes(b)

def hash_bytes(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()  # phát hiện trùng 100% file

def find_duplicates(hash_db, h: str):
    """Trả về tất cả bản ghi có cùng hash (để liệt kê lịch sử trùng)."""
    return [item for item in hash_db["items"] if item.get("hash") == h]

def add_hash_record(hash_db, h: str, info: dict):
    # info: { "ts": "...", "chat_id": ..., "user_id": ..., "id_kho": ..., "date": "YYYY-MM-DD" }
    hash_db["items"].append({"hash": h, **info})

# ========= SUBMISSION =========
def mark_submitted(submit_db, id_kho: str, d: date):
    key = d.isoformat()
    lst = submit_db.get(key, [])
    if id_kho not in lst:
        lst.append(id_kho)
    submit_db[key] = lst

# ========= GIỮ CAPTION DÙNG CHUNG =========
_last_text = {}  # chat_id -> (text, ts)

def upsert_last_text(chat_id: int, text: str):
    _last_text[chat_id] = (text, datetime.now(TZ).timestamp())

def get_last_text(chat_id: int):
    data = _last_text.get(chat_id)
    if not data:
        return None
    text, ts = data
    if datetime.now(TZ).timestamp() - ts > TEXT_PAIR_TIMEOUT:
        return None
    return text

# ========= HANDLERS =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "✅ Bot sẵn sàng!\n\n"
        "*Cú pháp linh hoạt* (chỉ cần *có ID* trong caption):\n"
        "`<ID_KHO> - <Tên kho>`\n"
        "`Ngày: dd/mm/yyyy` *(tuỳ chọn)*\n\n"
        "➡️ Dùng 1 caption cho nhiều ảnh:\n"
        "  1) Gửi 1 tin nhắn text chứa ID/Ngày\n"
        "  2) Gửi nhiều ảnh liên tiếp (không cần caption) — bot sẽ áp cùng caption trong 2 phút."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)

# NEW: /chatid để lấy chat id nhanh
async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(str(update.effective_chat.id))

# NEW: /report_now để gửi báo cáo ngay (không chờ 21:00)
async def report_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_daily_report(context)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text:
        upsert_last_text(update.effective_chat.id, text)  # lưu để áp cho nhiều ảnh

    id_kho, d = parse_text_for_id_and_date(text)
    kho_map = context.bot_data["kho_map"]

    if not id_kho:
        if "ngày" in text.lower() or any(ch.isdigit() for ch in text):
            await update.message.reply_text(
                "⚠️ Cú pháp chưa rõ ID. Vui lòng *gửi ảnh kèm caption có ID kho* hoặc gửi text trước rồi gửi ảnh trong 2 phút.\n"
                "Ví dụ:\n`12345 - Kho ABC\nNgày: 11/08/2025`",
                parse_mode=ParseMode.MARKDOWN
            )
        return

    if id_kho not in kho_map:
        await update.message.reply_text(
            "❌ ID `{}` *không có* trong danh sách. Kiểm tra lại!".format(id_kho),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    await update.message.reply_text(
        "✅ Đã nhận ID `{}` ({}). Bạn có thể gửi *nhiều ảnh liên tiếp* (không cần caption) trong 2 phút, bot sẽ áp cùng caption.".format(
            id_kho, kho_map[id_kho]
        ),
        parse_mode=ParseMode.MARKDOWN
    )

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    # ---- ALBUM / MEDIA GROUP ----
    caption = (msg.caption or "").strip()
    caption_from_group = caption
    mgid = msg.media_group_id
    if mgid:
        albums = context.chat_data.setdefault("albums", {})
        rec = albums.setdefault(mgid, {"caption": None, "ts": datetime.now(TZ).timestamp()})
        if caption and not rec["caption"]:
            rec["caption"] = caption
        if not caption and rec["caption"]:
            caption_from_group = rec["caption"]

    # ---- FALLBACK: dùng caption đã lưu trong 2 phút cho nhiều ảnh ----
    if not caption_from_group:
        caption_from_group = get_last_text(msg.chat_id) or ""

    # parse
    id_kho, d = parse_text_for_id_and_date(caption_from_group)
    kho_map = context.bot_data["kho_map"]

    if not id_kho:
        await msg.reply_text(
            "⚠️ *Thiếu ID kho.* Hãy thêm ID vào caption, hoặc gửi 1 text có ID trước rồi gửi ảnh (trong 2 phút).",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if id_kho not in kho_map:
        await msg.reply_text(
            "❌ ID `{}` *không có* trong danh sách Excel. Kiểm tra lại!".format(id_kho),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # tải bytes ảnh & hash
    b = await get_file_bytes(update, context)
    h = hashlib.md5(b).hexdigest()

    # kiểm tra ảnh trùng → liệt kê toàn bộ lịch sử
    hash_db = load_hash_db()
    dups = [item for item in hash_db["items"] if item.get("hash") == h]
    if dups:
        lines = []
        for item in dups:
            old_date = item.get("date", "?")
            try:
                pretty = datetime.fromisoformat(old_date).strftime("%d/%m/%Y")
            except Exception:
                pretty = old_date
            old_id = item.get("id_kho", "?")
            old_name = kho_map.get(old_id, "(không rõ)")
            lines.append("- Ngày *{}*: `{}` — {}".format(pretty, old_id, old_name))
        MAX_SHOW = 10
        shown = lines[:MAX_SHOW]
        tail = "\n… và {} lần trùng khác.".format(len(lines)-MAX_SHOW) if len(lines) > MAX_SHOW else ""
        await msg.reply_text(
            "⚠️ Ảnh *trùng* với ảnh đã gửi trước đây:\n" + "\n".join(shown) + tail +
            "\n\n➡️ Vui lòng chụp *ảnh mới* khác để tránh trùng lặp.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # ghi nhận nộp
    submit_db = load_submit_db()
    mark_submitted(submit_db, id_kho, d)
    save_submit_db(submit_db)

    # lưu hash
    info = {
        "ts": datetime.now(TZ).isoformat(timespec="seconds"),
        "chat_id": msg.chat_id,
        "user_id": msg.from_user.id,
        "id_kho": id_kho,
        "date": d.isoformat()
    }
    hash_db["items"].append({"hash": h, **info})
    save_hash_db(hash_db)

    await msg.reply_text(
        "✅ Đã ghi nhận ảnh cho *{}* (ID `{}`) - Ngày *{}*.".format(
            kho_map[id_kho], id_kho, d.strftime("%d/%m/%Y")
        ),
        parse_mode=ParseMode.MARKDOWN
    )

# ========= BÁO CÁO 21:00 =========
def get_missing_ids_for_day(kho_map, submit_db, d: date):
    submitted = set(submit_db.get(d.isoformat(), []))
    all_ids = set(kho_map.keys())
    return sorted(all_ids - submitted)

async def send_daily_report(context: ContextTypes.DEFAULT_TYPE):
    # ưu tiên dùng danh sách cài cứng; nếu rỗng thì lấy ENV
    chat_ids = REPORT_CHAT_IDS[:]
    if not chat_ids:
        env = os.getenv("REPORT_CHAT_IDS", "").strip()
        if env:
            chat_ids = [int(x.strip()) for x in env.split(",") if x.strip()]
    if not chat_ids:
        return

    kho_map = context.bot_data["kho_map"]
    submit_db = load_submit_db()
    today = datetime.now(TZ).date()

    missing = get_missing_ids_for_day(kho_map, submit_db, today)
    if not missing:
        text = "📢 *BÁO CÁO 5S - {}*\nTất cả kho đã báo cáo 5S đủ ✅".format(today.strftime("%d/%m/%Y"))
    else:
        lines = ["- `{}`: {}".format(mid, kho_map[mid]) for mid in missing]
        text = "📢 *BÁO CÁO 5S - {}*\nChưa nhận ảnh 5S từ {} kho:\n{}".format(
            today.strftime("%d/%m/%Y"), len(missing), "\n".join(lines)
        )

    for cid in chat_ids:
        try:
            await context.bot.send_message(cid, text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass

# ========= MAIN =========
def build_app() -> Application:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Thiếu biến môi trường BOT_TOKEN")

    app = ApplicationBuilder().token(token).build()
    app.bot_data["kho_map"] = load_kho_map()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("chatid", chatid))         # NEW
    app.add_handler(CommandHandler("report_now", report_now))  # NEW
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    app.job_queue.run_daily(
        send_daily_report,
        time=dtime(hour=REPORT_HOUR, minute=0, tzinfo=TZ),
        name="daily_report_21h"
    )
    return app

def main():
    app = build_app()
    print("Bot is running...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
