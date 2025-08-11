# bot.py
# Bot nhận ảnh 5S theo ID kho (không cần tag), xác nhận NGAY mỗi ảnh,
# phát hiện ảnh trùng (trong lô/cùng ngày/lịch sử), báo cáo 21:00, /chatid, /report_now.
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
EXCEL_PATH = "danh_sach_nv_theo_id_kho.xlsx"  # Excel phải có cột: id_kho, ten_kho
HASH_DB_PATH = "hashes.json"                  # lưu hash ảnh (phát hiện trùng)
SUBMIT_DB_PATH = "submissions.json"           # lưu ID đã nộp theo ngày
TZ = ZoneInfo("Asia/Ho_Chi_Minh")             # múi giờ VN
REPORT_HOUR = 21                              # 21:00 hằng ngày
TEXT_PAIR_TIMEOUT = 120                       # giây giữ caption dùng chung
REPORT_CHAT_IDS = [-1002688907477]            # ID group nhận báo cáo 21:00 (có thể thêm nhiều)

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
    # dạng: { "YYYY-MM-DD": ["id1","id2",...] }
    return _load_json(SUBMIT_DB_PATH, {})

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
    """Chỉ cần có ID; ngày (tuỳ chọn) 'Ngày: dd/mm/yyyy'."""
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

# ========= SUBMISSION =========
def mark_submitted(submit_db, id_kho: str, d: date):
    key = d.isoformat()
    lst = submit_db.get(key, [])
    if id_kho not in lst:
        lst.append(id_kho)
    submit_db[key] = lst

# ========= HANDLERS =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "✅ Bot sẵn sàng!\n\n"
        "*Cú pháp đơn giản (không cần tag):*\n"
        "`<ID_KHO> - <Tên kho>`\n"
        "`Ngày: dd/mm/yyyy` *(tuỳ chọn)*\n\n"
        "➡️ Mẹo: Gửi 1 tin nhắn text có ID/Ngày rồi gửi nhiều ảnh liên tiếp (không caption) — bot sẽ áp cùng caption 2 phút."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)

async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(str(update.effective_chat.id))

async def report_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_daily_report(context)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text:
        upsert_last_text(update.effective_chat.id, text)

    id_kho, d = parse_text_for_id_and_date(text)
    kho_map = context.bot_data["kho_map"]

    if not id_kho:
        return  # không làm phiền

    if id_kho not in kho_map:
        await update.message.reply_text(
            "❌ ID `{}` *không có* trong danh sách. Kiểm tra lại!".format(id_kho),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    await update.message.reply_text(
        "✅ Đã nhận ID `{}` ({}). Gửi ảnh ngay sau đó (không cần caption).".format(
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
            "⚠️ *Thiếu ID kho.* Thêm ID vào caption hoặc gửi 1 text có ID trước rồi gửi ảnh (trong 2 phút).",
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
    photo = msg.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    b = await tg_file.download_as_bytearray()
    b = bytes(b)
    h = hashlib.md5(b).hexdigest()

    # ===== CẢNH BÁO TRÙNG TRONG CÙNG LÔ/KHO/NGÀY =====
    mg_hashes = context.chat_data.setdefault("mg_hashes", {})
    if mgid:
        seen = mg_hashes.setdefault(mgid, set())
        if h in seen:
            await msg.reply_text(
                "⚠️ Có ít nhất 2 ảnh *giống nhau* trong cùng lô gửi. Vui lòng chọn ảnh khác.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        seen.add(h)

    hash_db = load_hash_db()
    same_day_dups = [
        item for item in hash_db["items"]
        if item.get("hash") == h
        and item.get("id_kho") == id_kho
        and item.get("date") == d.isoformat()
    ]
    if same_day_dups:
        await msg.reply_text(
            "⚠️ Kho *{}* hôm nay đã có 1 ảnh *giống hệt* ảnh này. Vui lòng thay ảnh khác.".format(kho_map[id_kho]),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # kiểm tra ảnh trùng toàn hệ thống (lịch sử)
    dups = [item for item in hash_db["items"] if item.get("hash") == h]
    if dups:
        await msg.reply_text(
            "⚠️ Ảnh *trùng* với ảnh đã gửi trước đây. Vui lòng chụp ảnh mới khác để tránh trùng lặp.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # ghi nhận nộp (không cần tag)
    submit_db = load_submit_db()
    mark_submitted(submit_db, id_kho, d)
    save_submit_db(submit_db)

    # lưu hash
    info = {
        "ts": datetime.now(TZ).isoformat(timespec="seconds"),
        "chat_id": msg.chat_id,
        "user_id": msg.from_user.id,
        "id_kho": id_kho,
        "date": d.isoformat(),
    }
    hash_db["items"].append({"hash": h, **info})
    save_hash_db(hash_db)

    # XÁC NHẬN NGAY MỖI ẢNH
    await msg.reply_text(
        "✅ Đã ghi nhận ảnh 5S cho *{}* (ID `{}`) - Ngày *{}*.".format(
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
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(CommandHandler("report_now", report_now))
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
