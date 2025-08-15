# bot.py
import cv2 as _cv
import numpy as _np

# Bot 5S hoàn chỉnh + GỘP TIN NHẮN XÁC NHẬN (không còn câu "Còn thiếu X ảnh")
# ---------------------------------------------------------------------------------
# Tính năng:
# - Nhận ảnh theo ID kho (không cần tag). Có thể gửi 1 text chứa ID/Ngày trước rồi gửi nhiều ảnh liền sau (không caption).
# - Ghép caption trong 2 phút (text trước áp cho ảnh sau).
# - Kiểm tra trùng ảnh: trong cùng lô gửi (album), trùng trong ngày theo kho, và trùng với lịch sử (log là "ảnh quá khứ").
# - Đếm số ảnh mỗi kho/ngày; phản hồi **gộp** vào 1 tin duy nhất cho mỗi (kho, ngày) và update tiến độ 1/4 → 2/4 → 3/4 → 4/4.
# - Báo cáo 21:00 theo format bạn yêu cầu:
#   (1) Các kho chưa báo cáo 5S → "- `ID` - Tên kho"
#   (2) Kho sử dụng ảnh cũ/quá khứ → "- `ID`: trùng ảnh ngày dd/mm/yyyy" hoặc "Không có"
#   (3) Chỉ liệt kê kho CHƯA ĐỦ; nếu không có thì "Tất cả kho đã gửi đủ số lượng ảnh theo quy định"
# - Lệnh: /chatid (xem chat id), /report_now (gửi báo cáo ngay).
#
# Cấu hình ENV bắt buộc: BOT_TOKEN
# Tuỳ chọn ENV: REPORT_CHAT_IDS="-100111,-100222", REQUIRED_PHOTOS="4"
# Excel bắt buộc: danh_sach_nv_theo_id_kho.xlsx có cột: id_kho, ten_kho

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
SUBMIT_DB_PATH = "submissions.json"           # { "YYYY-MM-DD": ["id1","id2",...] }
COUNT_DB_PATH = "counts.json"                 # { "YYYY-MM-DD": { "id_kho": count } }
PAST_DB_PATH  = "past_uses.json"              # { "YYYY-MM-DD": [ {id_kho, prev_date, hash} ] }
TZ = ZoneInfo("Asia/Ho_Chi_Minh")             # múi giờ VN
REPORT_HOUR = 21                              # 21:00 hằng ngày
TEXT_PAIR_TIMEOUT = 120                       # giây: giữ caption/text dùng chung
REQUIRED_PHOTOS = int(os.getenv("REQUIRED_PHOTOS", "4"))
# Cố định group nhận báo cáo (có thể override bằng ENV REPORT_CHAT_IDS)
DEFAULT_REPORT_CHAT_IDS = [-1002688907477]


# ========= CẢNH BÁO TRỄ (6s) =========
# Lưu job cảnh báo theo (chat_id, id_kho, day_key) để tránh spam khi gửi liên tiếp
WARN_JOBS = {}  # {(chat_id, id_kho, day_key): Job}

def _day_key(d):
    return d.isoformat()

async def _warning_job(context):
    """Job chạy sau 6s kể từ lần ghi nhận gần nhất."""
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    id_kho = data.get("id_kho")
    day = data.get("day")

    # Đọc lại count mới nhất để cảnh báo chính xác
    try:
        count_db = load_count_db()
        cur = int(count_db.get(day, {}).get(str(id_kho), 0))
    except Exception:
        cur = 0

    if cur > REQUIRED_PHOTOS:
        await context.bot.send_message(chat_id, f"⚠️ Đã gởi quá số ảnh so với quy định ( {REQUIRED_PHOTOS} ảnh )")
    elif cur < REQUIRED_PHOTOS:
        await context.bot.send_message(chat_id, f"⚠️ Còn {REQUIRED_PHOTOS - cur} thiếu 1 ảnh so với quy định ( {REQUIRED_PHOTOS} ảnh )")
    # = 4 thì không gửi gì

def schedule_delayed_warning(context, chat_id, id_kho, d):
    """Đặt/cập nhật 1 job cảnh báo chạy sau 6 giây."""
    key = (chat_id, str(id_kho), _day_key(d))
    # Huỷ job cũ nếu có để chỉ gửi 1 cảnh báo sau lần gửi cuối
    old = WARN_JOBS.pop(key, None)
    if old:
        try:
            old.schedule_removal()
        except Exception:
            pass
    # Tạo job mới (6s)
    job = context.job_queue.run_once(
        _warning_job, when=6,
        data={"chat_id": chat_id, "id_kho": str(id_kho), "day": _day_key(d)},
        name=f"warn_{chat_id}_{id_kho}_{_day_key(d)}"
    )
    WARN_JOBS[key] = job


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
    return _load_json(SUBMIT_DB_PATH, {})

def save_submit_db(db):
    _save_json(SUBMIT_DB_PATH, db)

def load_count_db():
    return _load_json(COUNT_DB_PATH, {})

def save_count_db(db):
    _save_json(COUNT_DB_PATH, db)

def load_past_db():
    return _load_json(PAST_DB_PATH, {})

def save_past_db(db):
    _save_json(PAST_DB_PATH, db)

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
    """Trích ID kho & ngày (tuỳ chọn 'Ngày: dd/mm/yyyy'). Nếu không có ngày -> hôm nay."""
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

# ========= GIỮ TEXT DÙNG CHUNG =========
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

# ========= SUBMISSION/COUNTS =========
def mark_submitted(submit_db, id_kho: str, d: date):
    key = d.isoformat()
    lst = submit_db.get(key, [])
    if id_kho not in lst:
        lst.append(id_kho)
    submit_db[key] = lst

def inc_count(count_db, id_kho: str, d: date, step: int = 1) -> int:
    key = d.isoformat()
    day = count_db.get(key, {})
    cur = int(day.get(id_kho, 0)) + step
    day[id_kho] = cur
    count_db[key] = day
    return cur

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

# ========= GỘP TIN NHẮN TIẾN ĐỘ (mỗi kho/mỗi ngày 1 tin) =========
PROGRESS_MSG = {}  # {(chat_id, id_kho, yyyy-mm-dd): {'msg_id': int|None, 'lines': list[str]}}

def day_key(d: date) -> str:
    return d.isoformat()  # YYYY-MM-DD

async def ack_photo_progress(context: ContextTypes.DEFAULT_TYPE, chat_id: int, id_kho: str, ten_kho: str, d: date, cur_count: int):
    """
    Gom toàn bộ tiến độ gửi ảnh của 1 kho trong 1 ngày vào 1 tin nhắn.
    Không còn câu 'Còn thiếu X ảnh'.
    Khi đủ REQUIRED_PHOTOS ảnh thì thêm dòng 'ĐÃ ĐỦ ... Cảm ơn bạn!'.
    """
    key = (chat_id, str(id_kho), day_key(d))
    state = PROGRESS_MSG.setdefault(key, {'msg_id': None, 'lines': []})
    date_text = d.strftime("%d/%m/%Y")

    if cur_count < REQUIRED_PHOTOS:
        line = f"✅ Đã ghi nhận ảnh {cur_count}/{REQUIRED_PHOTOS} cho {ten_kho} (ID `{id_kho}`) - Ngày {date_text}."
    else:
        line = f"✅ ĐÃ ĐỦ {REQUIRED_PHOTOS}/{REQUIRED_PHOTOS} ảnh cho {ten_kho} (ID `{id_kho}`) - Ngày {date_text}. Cảm ơn bạn!"

    state['lines'].append(line)
    text = "\n".join(state['lines'])

    if state['msg_id'] is None:
        m = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        state['msg_id'] = m.message_id
    else:
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=state['msg_id'],
                                                text=text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        except Exception:
            m = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
            state['msg_id'] = m.message_id

# ========= HANDLERS =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "✅ Bot sẵn sàng!\n\n"
        "*Cú pháp đơn giản (không cần tag):*\n"
        "`<ID_KHO> - <Tên kho>`\n"
        "`Ngày: dd/mm/yyyy` *(tuỳ chọn)*\n\n"
        f"Số ảnh yêu cầu mỗi kho/ngày: *{REQUIRED_PHOTOS}*.\n"
        "➡️ Mẹo: Gửi 1 tin nhắn text có ID/Ngày rồi gửi nhiều ảnh liên tiếp (không caption) — bot sẽ áp cùng caption 2 phút.\n\n"
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
    if text:
        upsert_last_text(update.effective_chat.id, text)

    id_kho, d = parse_text_for_id_and_date(text)
    kho_map = context.bot_data["kho_map"]

    if not id_kho:
        return  # không làm phiền

    if id_kho not in kho_map:
        await update.message.reply_text(
            f"❌ ID `{id_kho}` *không có* trong danh sách. Kiểm tra lại!",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    cur = get_count(load_count_db(), id_kho, d)
    await update.message.reply_text(
        f"✅ Đã nhận ID `{id_kho}` ({kho_map[id_kho]}). Hôm nay hiện có *{cur} / {REQUIRED_PHOTOS}* ảnh. "
        "Gửi ảnh ngay sau đó (không cần caption).",
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

    # ---- FALLBACK: dùng text đã lưu trong 2 phút ----
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
            f"❌ ID `{id_kho}` *không có* trong danh sách Excel. Kiểm tra lại!",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # tải bytes ảnh & hash
    photo = msg.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    b = await tg_file.download_as_bytearray()
    b = bytes(b)
    h = hashlib.md5(b).hexdigest()

    # ===== CẢNH BÁO TRÙNG TRONG CÙNG LÔ (album) =====
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

    # ===== TRÙNG TRONG NGÀY / LỊCH SỬ =====
    # Trùng cùng ngày/kho
    same_day_dups = [
        item for item in hash_db["items"]
        if item.get("hash") == h and item.get("id_kho") == id_kho and item.get("date") == d.isoformat()
    ]
    if same_day_dups:
        await msg.reply_text(
            f"⚠️ Kho *{kho_map[id_kho]}* hôm nay đã có 1 ảnh *giống hệt* ảnh này. Vui lòng thay ảnh khác.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Trùng lịch sử -> log quá khứ (lấy ngày sớm nhất)
    dups = [item for item in hash_db["items"] if item.get("hash") == h]
    if dups:
        prev_dates = sorted(set([it.get("date") for it in dups if it.get("date") != d.isoformat()]))
        if prev_dates:
            log_past_use(id_kho=id_kho, prev_date=prev_dates[0], h=h, today=d)
        await msg.reply_text(
            "⚠️ Ảnh *trùng* với ảnh đã gửi trước đây. Vui lòng chụp ảnh mới khác để tránh trùng lặp.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # ===== GHI NHẬN ẢNH HỢP LỆ =====
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
        "date": d.isoformat(),
    }
    hash_db["items"].append({"hash": h, **info})
    save_hash_db(hash_db)

    # đếm số ảnh và **GỘP** phản hồi theo kho/ngày
    count_db = load_count_db()
    cur = inc_count(count_db, id_kho, d, step=1)
    save_count_db(count_db)

    await ack_photo_progress(context, msg.chat_id, id_kho, kho_map[id_kho], d, cur)
    # Đặt cảnh báo trễ 6s sau mỗi lần ghi nhận (job sẽ tự kiểm tra và chỉ gửi nếu <4 hoặc >4)
    schedule_delayed_warning(context, msg.chat_id, id_kho, d)


    # ==== CHẤM ĐIỂM & GỬI TIN GỢP SAU 5S ====
    try:
        total, grade, _m = _img_metrics_from_bytes(b)
        key = (msg.chat_id, str(id_kho), _day_key(d))
        SCORING_BUFFER.setdefault(key, []).append({'total': total, 'grade': grade})
        if cur >= REQUIRED_PHOTOS:
            kv = _detect_kv_from_text(caption_from_group or caption)
            schedule_scoring_aggregate(context, msg.chat_id, id_kho, d, kho_map[id_kho], kv)
    except Exception:
        pass
# ========= 5S SCORING & FEEDBACK (injected) =========
SCORING_BUFFER = {}

def _img_metrics_from_bytes(b: bytes):
    arr = _np.frombuffer(b, dtype=_np.uint8)
    img = _cv.imdecode(arr, _cv.IMREAD_GRAYSCALE)
    if img is None:
        return 50, "C", {"sharp": 0.0, "bright": 0.0}
    sharp_var = float(_cv.Laplacian(img, _cv.CV_64F).var())
    bright = float(img.mean())
    sharp_score = max(0.0, min(100.0, (sharp_var / 3000.0) * 100.0))
    bright_score = max(0.0, min(100.0, 100.0 - abs(bright - 128.0) / 128.0 * 100.0))
    total = int(round(0.6 * sharp_score + 0.4 * bright_score))
    grade = "A" if total >= 85 else ("B" if total >= 70 else "C")
    return total, grade, {"sharp": sharp_var, "bright": bright, "sharp_score": sharp_score, "bright_score": bright_score}

SIMPLE_ISSUE_BANK = {
    "VanPhong": {
        "tidy": ["Bàn có nhiều bụi","Giấy tờ để lộn xộn","Dụng cụ chưa gọn","Màn hình chưa sạch","Dây cáp rối",
                 "Ly tách/đồ ăn để trên bàn","Khăn giấy bừa bộn","Ngăn kéo lộn xộn","Bề mặt dính bẩn","Bàn phím/bàn di bẩn",
                 "Ghế không ngay vị trí","Thùng rác đầy","Nhiều vật nhỏ rơi vãi","Kệ tài liệu chưa phân khu","Bảng ghi chú rối mắt"],
        "align": ["Vật dụng chưa ngay ngắn","Đồ đạc lệch vị trí","Tài liệu chưa xếp thẳng mép","Màn hình/đế đỡ lệch","Bút, sổ chưa theo hàng"],
        "aisle": ["Lối đi bị vướng đồ","Có vật cản dưới chân bàn","Dây điện vắt ngang lối đi","Thùng carton chắn lối","Túi đồ để dưới chân ghế"]
    },
    "WC": {
        "stain": ["Bồn/bề mặt còn vết bẩn","Gương, tay nắm chưa sạch","Vết ố quanh vòi","Vệt nước trên gương","Vách ngăn bám bẩn","Sàn bám cặn"],
        "trash": ["Thùng rác đầy","Rác chưa gom","Túi rác không thay","Rác rơi ra ngoài"],
        "dry": ["Sàn còn ướt","Có vệt nước đọng","Khăn giấy rơi xuống sàn","Chưa đặt biển cảnh báo khi sàn ướt"],
        "supply": ["Thiếu giấy/xà phòng","Thiếu khăn lau tay","Bình xịt trống","Chưa bổ sung vật tư"]
    },
    "HangHoa": {
        "align": ["Hàng chưa thẳng hàng","Pallet xoay khác hướng","Có khoảng hở trong dãy xếp","Thùng nhô ra mép kệ","Kiện cao thấp không đều","Hàng lệch line vạch","Thùng xẹp/biến dạng","Xếp chồng mất cân bằng"],
        "tidy": ["Khu vực còn bừa bộn","Thùng rỗng chưa gom","Vật tạm đặt sai chỗ","Màng PE rách vương vãi","Dụng cụ chưa trả về vị trí","Bao bì rách chưa xử lý","Nhãn mác bong tróc","Có hàng đặt trực tiếp xuống sàn"],
        "aisle": ["Lối đi bị lấn","Đồ cản trở đường đi","Pallet để dưới line","Hàng vượt vạch an toàn","Khu vực thao tác chật hẹp"],
        "bulky": ["Hàng cồng kềnh chưa cố định","Dây đai lỏng","Điểm tựa không chắc","Đặt sai hướng nâng hạ","Thiếu nẹp góc/đệm bảo vệ","Chưa dán nhãn cảnh báo kích thước/tải trọng"]
    },
    "LoiDi": {
        "aisle": ["Lối đi có vật cản","Vạch sơn mờ","Hàng lấn sang lối đi","Có chất lỏng rơi vãi","Thiếu biển chỉ dẫn","Lối thoát hiểm chưa thông thoáng","Xe đẩy dừng sai vị trí"]
    },
    "KePallet": {
        "align": ["Pallet không ngay hàng","Cạnh pallet lệch mép kệ","Kiện chồng quá cao","Thanh giằng không cân đối"],
        "tidy": ["Pallet hỏng chưa loại bỏ","Mảnh gỗ vụn trên sàn","Tem cũ chưa bóc","Màng PE dư chưa xử lý"]
    }
}
SIMPLE_REC_BANK = {
    "VanPhong": {
        "tidy": ["Lau bụi bề mặt","Xếp giấy tờ theo nhóm","Cất dụng cụ vào khay","Lau sạch màn hình","Buộc gọn dây cáp","Bỏ đồ ăn/ly tách đúng chỗ","Dán nhãn khay/ngăn kéo","Dọn rác ngay","Dùng khăn lau khử khuẩn","Sắp xếp bút sổ vào giá"],
        "align": ["Đặt đồ ngay ngắn","Cố định vị trí dùng thường xuyên","Căn thẳng theo mép bàn/kệ","Dùng khay chia ô cho phụ kiện"],
        "aisle": ["Dẹp vật cản khỏi lối đi","Bó gọn dây điện sát tường","Không đặt thùng/hộp dưới lối chân","Tận dụng kệ treo cho đồ lặt vặt"]
    },
    "WC": {
        "stain": ["Cọ rửa bằng dung dịch phù hợp","Lau gương, tay nắm","Chà sạch vết ố quanh vòi","Vệ sinh vách ngăn và sàn"],
        "trash": ["Đổ rác ngay","Thay túi rác mới","Dùng thùng có nắp"],
        "dry": ["Lau khô sàn","Đặt biển cảnh báo khi sàn ướt","Kiểm tra rò rỉ, xử lý ngay"],
        "supply": ["Bổ sung giấy/xà phòng","Thêm khăn lau tay","Nạp đầy bình xịt"]
    },
    "HangHoa": {
        "align": ["Căn theo mép kệ/vạch","Xoay cùng một hướng","Bổ sung nẹp góc giữ thẳng","San phẳng chiều cao chênh lệch"],
        "tidy": ["Gom thùng rỗng về khu tập kết","Dọn vật tạm đặt sai chỗ","Quấn lại màng PE gọn gàng","In/dán lại nhãn mác rõ ràng","Đặt hàng trên pallet, không đặt sàn"],
        "aisle": ["Giữ lối đi thông thoáng","Di dời vật cản khỏi line","Chừa khoảng an toàn ≥ 1m"],
        "bulky": ["Đai cố định chắc chắn","Thêm nẹp góc/đệm bảo vệ","Đặt hướng thuận lợi nâng hạ","Ghi chú kích thước/tải trọng rõ ràng","Chèn chống xê dịch"]
    },
    "LoiDi": {
        "aisle": ["Dọn sạch vật cản","Sơn lại vạch dẫn hướng","Đặt lại hàng vượt vạch","Lau sạch chất lỏng rơi vãi","Đảm bảo lối thoát hiểm thông suốt","Quy định vị trí dừng cho xe đẩy"]
    },
    "KePallet": {
        "align": ["Căn thẳng mép pallet","Không chồng quá quy định","Kiểm tra thanh giằng, cân chỉnh"],
        "tidy": ["Loại bỏ pallet hỏng","Quét dọn mảnh gỗ vụn","Cắt bỏ màng PE thừa","Bóc tem cũ trước khi dán tem mới"]
    }
}

def _detect_kv_from_text(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in ["wc", "tolet", "toilet", "nhà vệ sinh"]): return "WC"
    if any(k in t for k in ["bàn", "ban lam viec", "văn phòng", "van phong", "desk", "office"]): return "VanPhong"
    if any(k in t for k in ["lối đi", "loi di", "aisle", "hành lang"]): return "LoiDi"
    if any(k in t for k in ["pallet", "kệ", "ke", "rack", "ke pallet"]): return "KePallet"
    return "HangHoa"

def compose_simple_feedback(kv: str, max_issues: int = 5, max_recs: int = 5) -> str:
    import random
    cat_pref = {
        "HangHoa": ["align", "tidy", "aisle", "bulky"],
        "VanPhong": ["tidy", "align", "aisle"],
        "WC": ["stain", "trash", "dry", "supply"],
        "LoiDi": ["aisle"],
        "KePallet": ["align", "tidy"],
    }
    cats = cat_pref.get(kv, ["tidy","align","aisle"])
    def pick(bank, limit):
        items = []
        kv_bank = bank.get(kv, {})
        for c in cats: items.extend(kv_bank.get(c, []))
        random.shuffle(items)
        seen=set(); out=[]
        for s in items:
            if s not in seen:
                seen.add(s); out.append(s)
            if len(out)>=limit: break
        return out
    issues = pick(SIMPLE_ISSUE_BANK, max_issues)
    recs   = pick(SIMPLE_REC_BANK,   max_recs)
    lines=[]; 
    if issues:
        lines.append("⚠️ Vấn đề:")
        lines.extend([f" • {s}" for s in issues])
    if recs:
        lines.append("🛠️ Khuyến nghị:")
        lines.extend([f" • {s}" for s in recs])
    return "\n".join(lines)

def _compose_aggregate_message(id_kho: str, ten_kho: str, d: date, items: list, kv: str) -> str:
    lines = []
    lines.append("🧮 Điểm 5S cho lô ảnh này")
    lines.append(f"- Kho: {id_kho} · Ngày: {d.strftime('%d/%m/%Y')}")
    lines.append("")
    any_low = False
    for idx, it in enumerate(items, 1):
        lines.append(f"• Ảnh #{idx}: {it['total']}/100 → Loại {it['grade']}")
        if it['total'] < 95: any_low = True
    if any_low:
        lines.append("")
        lines.append(compose_simple_feedback(kv))
    return "\n".join(lines)

def schedule_scoring_aggregate(context, chat_id: int, id_kho: str, d: date, ten_kho: str, kv: str):
    key = (chat_id, str(id_kho), _day_key(d))
    def _job(ctx):
        bucket = SCORING_BUFFER.get(key, [])
        if not bucket:
            return
        text = _compose_aggregate_message(str(id_kho), ten_kho, d, bucket, kv)
        try:
            ctx.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            ctx.bot.send_message(chat_id=chat_id, text=text)
        SCORING_BUFFER.pop(key, None)
    context.job_queue.run_once(lambda c: _job(c), when=5, name=f"score_{chat_id}_{id_kho}_{_day_key(d)}")
# ========= END SCORING & FEEDBACK =========


# ========= BÁO CÁO 21:00 =========
def get_missing_ids_for_day(kho_map, submit_db, d: date):
    submitted = set(submit_db.get(d.isoformat(), []))
    all_ids = set(kho_map.keys())
    return sorted(all_ids - submitted)

async def send_daily_report(context: ContextTypes.DEFAULT_TYPE):
    # danh sách chat nhận báo cáo
    chat_ids = DEFAULT_REPORT_CHAT_IDS[:]
    env = os.getenv("REPORT_CHAT_IDS", "").strip()
    if env:
        chat_ids = [int(x.strip()) for x in env.split(",") if x.strip()]
    if not chat_ids:
        return

    kho_map = context.bot_data["kho_map"]
    submit_db = load_submit_db()
    count_db = load_count_db()
    past_db = load_past_db()
    today = datetime.now(TZ).date()

    # 1) Chưa báo cáo
    missing_ids = get_missing_ids_for_day(kho_map, submit_db, today)

    # 2) Ảnh cũ/quá khứ: gom theo kho, lấy 1 ngày đại diện (sớm nhất) để báo gọn
    past_uses = past_db.get(today.isoformat(), [])
    past_by_kho = {}
    for it in past_uses:
        kid = it.get("id_kho"); prev = it.get("prev_date")
        if not kid or not prev:
            continue
        s = past_by_kho.setdefault(kid, set())
        s.add(prev)
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
    # 1) Chưa báo cáo 5S — HIỂN THỊ ID - TÊN KHO
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
        sec3 = ["*3) Các kho chưa gửi đủ số lượng ảnh:*"]
        for kid, c in sorted(not_enough_list):
            sec3.append(f"- `{kid}`: {c}/{REQUIRED_PHOTOS}")
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
