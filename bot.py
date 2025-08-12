# bot.py
# Bot 5S hoàn chỉnh + CHÍNH SÁCH 2 PHÚT:
# - Trong vòng 2 phút kể từ lần báo gần nhất của cùng (kho, ngày) => EDIT tin nhắn cũ
# - Quá 2 phút => GỬI TIN NHẮN MỚI (mở một "đợt" xác nhận mới)
#
# Các chức năng giữ nguyên:
# - Nhận ảnh theo ID kho (không cần tag). Có thể gửi 1 text chứa ID/Ngày trước rồi gửi nhiều ảnh liền sau (không caption).
# - Ghép caption trong 2 phút (text trước áp cho ảnh sau).
# - Kiểm tra trùng ảnh: trong cùng lô (album), trùng trong ngày theo kho, trùng lịch sử (log ảnh quá khứ).
# - Đếm số ảnh/kho/ngày; phản hồi không còn câu “Còn thiếu X ảnh”.
# - Gộp tiến độ theo phiên 2 phút (1 tin/phiên) và cập nhật 1/4 → 2/4 → 3/4 → 4/4; đủ thì thêm câu cảm ơn.
# - Báo cáo 21:00: (1) Kho chưa báo cáo (ID - Tên), (2) Ảnh cũ/quá khứ, (3) Kho chưa đủ ảnh.
# - Lệnh: /chatid, /report_now
#
# ENV cần: BOT_TOKEN
# Tuỳ chọn ENV: REPORT_CHAT_IDS, REQUIRED_PHOTOS (mặc định 4)
# Yêu cầu file Excel: danh_sach_nv_theo_id_kho.xlsx cột: id_kho, ten_kho

import os
import re
import json
import hashlib
from datetime import datetime, date, time as dtime, timezone, timedelta
from zoneinfo import ZoneInfo

# ==== Timezone & cut-off helpers ====
from zoneinfo import ZoneInfo
TZ = ZoneInfo("Asia/Ho_Chi_Minh")
CUTOFF_HOUR = 20
CUTOFF_MINUTE = 30

def _cutoff_time():
    return time(CUTOFF_HOUR, CUTOFF_MINUTE, tzinfo=TZ)

def effective_today(now: datetime | None = None):
    """Business day used for counting submissions.
    If time >= 20:30, roll to next calendar day; else use today's date (VN)."""
    now = now.astimezone(TZ) if now else datetime.now(TZ)
    return (now + timedelta(days=1)).date() if now.timetz() >= _cutoff_time() else now.date()

def report_business_date(now: datetime | None = None):
    """Which date to report at 'now'.
    If time >= 20:30, report today's effective date; otherwise report yesterday."""
    now = now.astimezone(TZ) if now else datetime.now(TZ)
    return now.date() if now.timetz() >= _cutoff_time() else (now - timedelta(days=1)).date()
# ====================================


import pandas as pd
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ========= CẤU HÌNH =========
EXCEL_PATH = "danh_sach_nv_theo_id_kho.xlsx"
HASH_DB_PATH = "hashes.json"
SUBMIT_DB_PATH = "submissions.json"
COUNT_DB_PATH = "counts.json"
PAST_DB_PATH  = "past_uses.json"
TZ = ZoneInfo("Asia/Ho_Chi_Minh")
REPORT_HOUR = 21
TEXT_PAIR_TIMEOUT = 120
REQUIRED_PHOTOS = int(os.getenv("REQUIRED_PHOTOS", "4"))
DEFAULT_REPORT_CHAT_IDS = [-1002688907477]  # có thể override bằng ENV REPORT_CHAT_IDS

# --- Cut-off giờ chốt ngày VN ---
from datetime import time as _time, timedelta as _timedelta, timezone as _timezone
CUTOFF_TIME = _time(20, 30)  # 20:30 theo yêu cầu

def effective_report_date_from_dt_utc(msg_dt_utc):
    """
    Quy đổi message.date (UTC) sang ngày báo cáo theo VN + cut-off 20:30.
    - Nếu local_time <= 20:30: tính cho NGÀY HIỆN TẠI (VN)
    - Nếu local_time >  20:30: chuyển sang NGÀY HÔM SAU (VN)
    """
    if msg_dt_utc is None:
        # fallback: dùng ngày VN hiện tại
        return datetime.now(TZ).date()
    if msg_dt_utc.tzinfo is None:
        msg_dt_utc = msg_dt_utc.replace(tzinfo=_timezone.utc)
    local_dt = msg_dt_utc.astimezone(TZ)
    if (local_dt.hour, local_dt.minute, local_dt.second) <= (CUTOFF_TIME.hour, CUTOFF_TIME.minute, CUTOFF_TIME.second):
        return local_dt.date()
    else:
        return (local_dt + _timedelta(days=1)).date()

# Chính sách EDIT trong vòng X phút, mặc định 2 phút
EDIT_WINDOW_MINUTES = int(os.getenv("EDIT_WINDOW_MINUTES", "2"))
EDIT_WINDOW_SECONDS = EDIT_WINDOW_MINUTES * 60

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

def load_hash_db():   return _load_json(HASH_DB_PATH, {"items": []})
def save_hash_db(db): _save_json(HASH_DB_PATH, db)
def load_submit_db(): return _load_json(SUBMIT_DB_PATH, {})
def save_submit_db(db): _save_json(SUBMIT_DB_PATH, db)
def load_count_db():  return _load_json(COUNT_DB_PATH, {})
def save_count_db(db): _save_json(COUNT_DB_PATH, db)
def load_past_db():   return _load_json(PAST_DB_PATH, {})
def save_past_db(db): _save_json(PAST_DB_PATH, db)

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
    _id = None
    _date = date.today()
    if not text:
        return None, _date
    m_id = ID_RX.search(text)
    if m_id: _id = m_id.group(1)
    m_d = DATE_RX.search(text)
    if m_d:
        d, m, y = (int(x) for x in m_d.groups())
        if y < 100: y += 2000
        try: _date = date(y, m, d)
        except Exception: _date = date.today()
    return _id, _date

# ========= GIỮ TEXT DÙNG CHUNG =========
_last_text = {}  # chat_id -> (text, ts)
def upsert_last_text(chat_id: int, text: str):
    _last_text[chat_id] = (text, datetime.now(TZ).timestamp())
def get_last_text(chat_id: int):
    data = _last_text.get(chat_id)
    if not data: return None
    text, ts = data
    if datetime.now(TZ).timestamp() - ts > TEXT_PAIR_TIMEOUT: return None
    return text

# ========= SUBMISSION/COUNTS =========
def mark_submitted(submit_db, id_kho: str, d: date):
    key = d.isoformat()
    lst = submit_db.get(key, [])
    if id_kho not in lst: lst.append(id_kho)
    submit_db[key] = lst

def inc_count(count_db, id_kho: str, d: date, step: int = 1) -> int:
    key = d.isoformat()
    day = count_db.get(key, {})
    cur = int(day.get(id_kho, 0)) + step
    day[id_kho] = cur; count_db[key] = day; return cur

def get_count(count_db, id_kho: str, d: date) -> int:
    return int(count_db.get(d.isoformat(), {}).get(id_kho, 0))

# ========= PAST-USE LOG =========
def log_past_use(id_kho: str, prev_date: str, h: str, today: date):
    db = load_past_db()
    key = today.isoformat()
    arr = db.get(key, [])
    arr.append({"id_kho": id_kho, "prev_date": prev_date, "hash": h})
    db[key] = arr
    save_past_db(db)

# ========= GỘP THEO PHIÊN 2 PHÚT =========
# Lưu trạng thái mỗi (chat_id, id_kho, yyyy-mm-dd) cho "phiên" 2 phút
PROGRESS_SESS = {}  # key -> {'msg_id': int|None, 'last_ts': float, 'lines': list[str]}
def _sess_key(chat_id: int, id_kho: str, d: date) -> str:
    return f"{chat_id}|{id_kho}|{d.isoformat()}"

async def ack_photo_progress(context: ContextTypes.DEFAULT_TYPE, chat_id: int, id_kho: str, ten_kho: str, d: date, cur_count: int):
    """
    Trong vòng EDIT_WINDOW_MINUTES kể từ lần cập nhật gần nhất của kho/ngày -> EDIT tin cũ.
    Nếu quá cửa sổ -> tạo tin mới (reset lines).
    Không dùng câu 'Còn thiếu X ảnh' theo yêu cầu.
    """
    now_ts = datetime.now(timezone.utc).timestamp()
    key = _sess_key(chat_id, id_kho, d)
    sess = PROGRESS_SESS.get(key)

    # tạo dòng hiện tại
    date_text = d.strftime("%d/%m/%Y")
    if cur_count < REQUIRED_PHOTOS:
        line = f"✅ Đã ghi nhận ảnh {cur_count}/{REQUIRED_PHOTOS} cho {ten_kho} (ID `{id_kho}`) - Ngày {date_text}."
    else:
        line = f"✅ ĐÃ ĐỦ {REQUIRED_PHOTOS}/{REQUIRED_PHOTOS} ảnh cho {ten_kho} (ID `{id_kho}`) - Ngày {date_text}. Cảm ơn bạn!"

    # nếu chưa có phiên hoặc đã quá thời gian -> mở phiên mới
    if not sess or (now_ts - sess.get('last_ts', 0) > EDIT_WINDOW_SECONDS):
        sess = {'msg_id': None, 'last_ts': now_ts, 'lines': [line]}
        PROGRESS_SESS[key] = sess
        text = "\n".join(sess['lines'])
        m = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        sess['msg_id'] = m.message_id
        sess['last_ts'] = now_ts
        return

    # còn trong cửa sổ -> append dòng và EDIT tin cũ
    sess['lines'].append(line)
    text = "\n".join(sess['lines'])
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=sess['msg_id'],
                                            text=text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    except Exception:
        m = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        sess['msg_id'] = m.message_id
    finally:
        sess['last_ts'] = now_ts

# ========= HANDLERS =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "✅ Bot sẵn sàng!\n\n"
        "*Cú pháp (không cần tag):*\n"
        "`<ID_KHO> - <Tên kho>`\n"
        "`Ngày: dd/mm/yyyy` *(tuỳ chọn)*\n\n"
        f"Số ảnh yêu cầu mỗi kho/ngày: *{REQUIRED_PHOTOS}*.\n"
        "➡️ Gửi 1 text có ID/Ngày rồi gửi nhiều ảnh (không caption) — bot dùng lại caption trong 2 phút.\n\n"
        "Lệnh: `/chatid` lấy chat id, `/report_now` gửi báo cáo ngay."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)

async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(str(update.effective_chat.id))

async def report_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_daily_report(context)
    await update.message.reply_text("✅ Đã gửi báo cáo 5S mới nhất vào các group cấu hình.")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text: upsert_last_text(update.effective_chat.id, text)
    id_kho, d = parse_text_for_id_and_date(text)
    _provided_date = bool(DATE_RX.search(text))
    if not _provided_date:
        d = effective_report_date_from_dt_utc(update.message.date)

    kho_map = context.bot_data["kho_map"]
    if not id_kho: return
    if id_kho not in kho_map:
        await update.message.reply_text(f"❌ ID `{id_kho}` *không có* trong danh sách. Kiểm tra lại!", parse_mode=ParseMode.MARKDOWN); return
    cur = get_count(load_count_db(), id_kho, d)
    await update.message.reply_text(f"✅ Đã nhận ID `{id_kho}` ({kho_map[id_kho]}). Hôm nay hiện có *{cur} / {REQUIRED_PHOTOS}* ảnh. Gửi ảnh ngay sau đó (không cần caption).", parse_mode=ParseMode.MARKDOWN)

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
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
    if not caption_from_group:
        caption_from_group = get_last_text(msg.chat_id) or ""
    id_kho, d = parse_text_for_id_and_date(caption_from_group)
    _provided_date = bool(DATE_RX.search(caption_from_group))
    if not _provided_date:
        d = effective_report_date_from_dt_utc(update.message.date)

    kho_map = context.bot_data["kho_map"]
    if not id_kho:
        await msg.reply_text("⚠️ *Thiếu ID kho.* Thêm ID vào caption hoặc gửi 1 text có ID trước rồi gửi ảnh (trong 2 phút).", parse_mode=ParseMode.MARKDOWN); return
    if id_kho not in kho_map:
        await msg.reply_text(f"❌ ID `{id_kho}` *không có* trong danh sách Excel. Kiểm tra lại!", parse_mode=ParseMode.MARKDOWN); return

    # tải bytes ảnh & hash
    photo = msg.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    b = await tg_file.download_as_bytearray(); b = bytes(b)
    h = hashlib.md5(b).hexdigest()

    # trùng trong cùng lô
    mg_hashes = context.chat_data.setdefault("mg_hashes", {})
    if mgid:
        seen = mg_hashes.setdefault(mgid, set())
        if h in seen:
            await msg.reply_text("⚠️ Có ít nhất 2 ảnh *giống nhau* trong cùng lô gửi. Vui lòng chọn ảnh khác.", parse_mode=ParseMode.MARKDOWN); return
        seen.add(h)

    hash_db = load_hash_db()
    # trùng trong ngày/kho
    same_day_dups = [it for it in hash_db["items"] if it.get("hash")==h and it.get("id_kho")==id_kho and it.get("date")==d.isoformat()]
    if same_day_dups:
        await msg.reply_text(f"⚠️ Kho *{kho_map[id_kho]}* hôm nay đã có 1 ảnh *giống hệt* ảnh này. Vui lòng thay ảnh khác.", parse_mode=ParseMode.MARKDOWN); return

    # trùng lịch sử -> log quá khứ
    dups = [it for it in hash_db["items"] if it.get("hash")==h]
    if dups:
        prev_dates = sorted(set([it.get("date") for it in dups if it.get("date") != d.isoformat()]))
        if prev_dates:
            log_past_use(id_kho=id_kho, prev_date=prev_dates[0], h=h, today=d)
        await msg.reply_text("⚠️ Ảnh *trùng* với ảnh đã gửi trước đây. Vui lòng chụp ảnh mới khác để tránh trùng lặp.", parse_mode=ParseMode.MARKDOWN); return

    # ghi nhận
    submit_db = load_submit_db(); mark_submitted(submit_db, id_kho, d); save_submit_db(submit_db)
    info = {"ts": datetime.now(TZ).isoformat(timespec="seconds"), "chat_id": msg.chat_id, "user_id": msg.from_user.id, "id_kho": id_kho, "date": d.isoformat()}
    hash_db["items"].append({"hash": h, **info}); save_hash_db(hash_db)

    # đếm & phản hồi (2 phút/phiên)
    count_db = load_count_db(); cur = inc_count(count_db, id_kho, d, 1); save_count_db(count_db)
    await ack_photo_progress(context, msg.chat_id, id_kho, kho_map[id_kho], d, cur)

# ========= BÁO CÁO 21:00 =========
def get_missing_ids_for_day(kho_map, submit_db, d: date):
    submitted = set(submit_db.get(d.isoformat(), []))
    all_ids = set(kho_map.keys())
    return sorted(all_ids - submitted)

async def send_daily_report(context: ContextTypes.DEFAULT_TYPE):
    chat_ids = DEFAULT_REPORT_CHAT_IDS[:]
    env = os.getenv("REPORT_CHAT_IDS", "").strip()
    if env:
        chat_ids = [int(x.strip()) for x in env.split(",") if x.strip()]
    if not chat_ids: return

    kho_map = context.bot_data["kho_map"]
    submit_db = load_submit_db(); count_db = load_count_db(); past_db = load_past_db()
    today = effective_today()

    # 1) Chưa báo cáo
    missing_ids = get_missing_ids_for_day(kho_map, submit_db, today)

    # 2) Ảnh cũ/quá khứ
    past_uses = past_db.get(today.isoformat(), [])
    past_by_kho = {}
    for it in past_uses:
        kid = it.get("id_kho"); prev = it.get("prev_date")
        if not kid or not prev: continue
        s = past_by_kho.setdefault(kid, set()); s.add(prev)
    past_lines = []
    for kid, dates in sorted(past_by_kho.items()):
        rep = min(dates)
        rep_str = datetime.fromisoformat(rep).strftime("%d/%m/%Y")
        past_lines.append(f"- `{kid}`: trùng ảnh ngày {rep_str}")

    # 3) CHỈ liệt kê CHƯA ĐỦ số ảnh
    not_enough_list = []
    day_counts = count_db.get(today.isoformat(), {})
    for kid in kho_map.keys():
        c = int(day_counts.get(kid, 0))
        if 0 < c < REQUIRED_PHOTOS:
            not_enough_list.append((kid, c))

    parts = []
    # 1) Chưa báo cáo 5S — ID - Tên
    if missing_ids:
        lines = ["*1) Các kho chưa báo cáo 5S:*"]
        for mid in missing_ids:
            name = kho_map.get(mid, "(không rõ)")
            lines.append(f"- `{mid}` - {name}")
        parts.append("\n".join(lines))
    else:
        parts.append("*1) Các kho chưa báo cáo 5S:* Không có")

    # 2) Ảnh cũ/quá khứ
    if past_lines:
        parts.append("*2) Kho sử dụng ảnh cũ/quá khứ:*\n" + "\n".join(past_lines))
    else:
        parts.append("*2) Kho sử dụng ảnh cũ/quá khứ:* Không có")

    # 3) Chưa đủ
    if not_enough_list:
        sec3 = ["*3) Các kho chưa gửi đủ số lượng ảnh:*"] + [f"- `{kid}`: {c}/{REQUIRED_PHOTOS}" for kid, c in sorted(not_enough_list)]
        parts.append("\n".join(sec3))
    else:
        parts.append("*3) Tất cả kho đã gửi đủ số lượng ảnh theo quy định*")

    text = f"📢 *BÁO CÁO 5S - {today.strftime('%d/%m/%Y')}*\n\n" + "\n\n".join(parts)

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
    app.job_queue.run_daily(send_daily_report, time=dtime(hour=REPORT_HOUR, minute=0, tzinfo=TZ), name="daily_report_21h")
    return app

def main():
    app = build_app()
    print("Bot is running...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
