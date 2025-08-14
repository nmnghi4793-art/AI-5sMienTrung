# bot.py
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

# ===== Scoring imports & ENV =====
import cv2
import numpy as np

SCORING_ENABLED = os.getenv("SCORING_ENABLED","0") == "1"
SCORING_MODE = os.getenv("SCORING_MODE","rule").strip().lower() or "rule"
# Default weights per area
_DEFAULT_WEIGHTS = {
    "HangHoa": {"align": 40, "tidy": 40, "aisle": 20},
    "WC":      {"stain": 60, "trash": 25, "dry": 15},
    "KhoBai":  {"clean": 50, "obstacle": 30, "line": 20},
    "VanPhong":{"desk_tidy": 45, "surface_clean": 35, "cable": 20}
}
try:
    AREA_RULE_WEIGHTS = json.loads(os.getenv("AREA_RULE_WEIGHTS","") or "{}")
    if not AREA_RULE_WEIGHTS:
        AREA_RULE_WEIGHTS = _DEFAULT_WEIGHTS
except Exception:
    AREA_RULE_WEIGHTS = _DEFAULT_WEIGHTS

# Parameters for quality scoring
BRIGHT_MIN, BRIGHT_MAX = 70, 200
MIN_SHORT_EDGE = 600
LAPLACIAN_GOOD = 250.0

# Regex to detect KV in caption/text, e.g., "KV: HangHoa" or "Khu_vuc=WC"
AREA_RX = re.compile(r'(?:\bkv\b|\bkhu[_\s]*vuc)\s*[:=]\s*([a-zA-Z0-9_-]{2,})', re.I)


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


# ========= 5S SCORING HELPERS (rule-based) =========
def _score_quality_components(img_bgr):
    if img_bgr is None or img_bgr.size == 0:
        return 0.0, 0.0, 0.0, (0,0)
    h, w = img_bgr.shape[:2]
    img_gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    sharp_val = float(cv2.Laplacian(img_gray, cv2.CV_64F).var())
    sharp_score = 1.0 if sharp_val >= LAPLACIAN_GOOD else max(0.0, sharp_val / LAPLACIAN_GOOD)
    mean_bright = float(img_gray.mean())
    if mean_bright < BRIGHT_MIN:
        bright_score = max(0.0, mean_bright/BRIGHT_MIN)
    elif mean_bright > BRIGHT_MAX:
        bright_score = max(0.0, (255.0-mean_bright)/(255.0-BRIGHT_MAX+1e-6))
    else:
        bright_score = 1.0
    short_edge = min(h, w)
    size_score = 1.0 if short_edge >= MIN_SHORT_EDGE else max(0.0, short_edge/MIN_SHORT_EDGE)
    return sharp_score, bright_score, size_score, (w,h)

def _edge_density(img_gray, t1=80, t2=200):
    edges = cv2.Canny(img_gray, t1, t2)
    return float((edges>0).mean())

def _score_hanghoa(img_bgr):
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 80, 200)
    lines = cv2.HoughLines(edges, 1, np.pi/180, 120)
    angles = []
    if lines is not None:
        for l in lines[:100]:
            rho, theta = l[0]
            angles.append(theta)
    def angle_dev(theta):
        return min(abs(theta-0), abs(theta-np.pi/2))
    if angles:
        devs = np.array([angle_dev(t) for t in angles], dtype=np.float32)
        align_score = float(max(0.0, 1.0 - (devs.mean()/(np.pi/8))))
    else:
        align_score = 0.6
    clutter = _edge_density(gray)  # 0..1
    tidy_score = float(max(0.0, 1.0 - min(clutter/0.25, 1.0)))
    h, w = gray.shape
    lower = gray[int(h*0.55):, :]
    lower_edges = cv2.Canny(lower, 80, 200)
    empty_ratio = 1.0 - float((lower_edges>0).mean())
    aisle_score = float(max(0.0, min(empty_ratio, 1.0)))
    return {"align": align_score, "tidy": tidy_score, "aisle": aisle_score}

def _score_wc(img_bgr):
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7,7))
    blackhat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, k)
    _, th = cv2.threshold(blackhat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    stain_ratio = float((th>0).mean())
    stain_score = float(max(0.0, 1.0 - min(stain_ratio/0.10, 1.0)))
    h, w = gray.shape
    lower = gray[int(h*0.55):, :]
    lower_blur = cv2.GaussianBlur(lower, (5,5), 0)
    _, lower_th = cv2.threshold(lower_blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    cnts, _ = cv2.findContours(lower_th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    small_blobs = [c for c in cnts if 10 <= cv2.contourArea(c) <= 500]
    trash_density = float(len(small_blobs)) / max(1.0, (w*h/10000.0))
    trash_score = float(max(0.0, 1.0 - min(trash_density/1.5, 1.0)))
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    local_var = float(np.var(lap))
    dry_score = float(max(0.0, 1.0 - min(local_var/500.0, 1.0)))
    return {"stain": stain_score, "trash": trash_score, "dry": dry_score}

def _score_khobai(img_bgr):
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5,5), 0)
    _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    dirt_ratio = float((th == 0).mean())
    clean_score = float(max(0.0, 1.0 - min(dirt_ratio/0.20, 1.0)))
    h, w = gray.shape
    band = gray[int(h*0.45):int(h*0.75), :]
    obs_density = _edge_density(band)
    obstacle_score = float(max(0.0, 1.0 - min(obs_density/0.30, 1.0)))
    edges = cv2.Canny(gray, 80, 200)
    lines = cv2.HoughLines(edges, 1, np.pi/180, 150)
    line_score = 0.6 if lines is None else 1.0
    return {"clean": clean_score, "obstacle": obstacle_score, "line": float(line_score)}

def _score_vanphong(img_bgr):
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    band = gray[int(h*0.25):int(h*0.75), int(w*0.10):int(w*0.90)]
    band_blur = cv2.GaussianBlur(band, (5,5), 0)
    _, band_th = cv2.threshold(band_blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    cnts, _ = cv2.findContours(band_th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    small_items = [c for c in cnts if 20 <= cv2.contourArea(c) <= 1500]
    item_density = float(len(small_items)) / max(1.0, (w*h/10000.0))
    desk_tidy = float(max(0.0, 1.0 - min(item_density/1.5, 1.0)))
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (7,7))
    blackhat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, k)
    _, th = cv2.threshold(blackhat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    dirty_ratio = float((th>0).mean())
    surface_clean = float(max(0.0, 1.0 - min(dirty_ratio/0.15, 1.0)))
    margin = gray[:, :int(w*0.20)]
    edges = cv2.Canny(margin, 60, 160)
    cable_density = float((edges>0).mean())
    cable = float(max(0.0, 1.0 - min(cable_density/0.35, 1.0)))
    return {"desk_tidy": desk_tidy, "surface_clean": surface_clean, "cable": cable}

def _kv_key_from_text(kv_text):
    kv = (kv_text or "").strip().lower()
    if "hanghoa" in kv or "hàng" in kv or "hang" in kv:
        return "HangHoa"
    if "wc" in kv or "toilet" in kv or "vesinh" in kv or "vệ" in kv or "tolet" in kv:
        return "WC"
    if "kho" in kv or "kho bãi" in kv:
        return "KhoBai"
    if "văn" in kv or "vanphong" in kv or "ban lam viec" in kv or "ban" in kv:
        return "VanPhong"
    return "HangHoa"  # default

def apply_scoring_rule(photo_bytes: bytes, kv_text: str, is_duplicate: bool=False):
    img = cv2.imdecode(np.frombuffer(photo_bytes, np.uint8), cv2.IMREAD_COLOR)
    sharp_s, bright_s, size_s, (w,h) = _score_quality_components(img)
    q_score = 0.2 * (0.6*sharp_s + 0.4*bright_s)
    kv_key = _kv_key_from_text(kv_text)
    if kv_key == "HangHoa":
        parts = _score_hanghoa(img)
    elif kv_key == "WC":
        parts = _score_wc(img)
    elif kv_key == "KhoBai":
        parts = _score_khobai(img)
    else:
        parts = _score_vanphong(img)
    weights = AREA_RULE_WEIGHTS.get(kv_key, _DEFAULT_WEIGHTS[kv_key])
    total_w = float(sum(weights.values())) or 100.0
    content_s = 0.0
    for name, val in parts.items():
        content_s += (float(val) * (float(weights.get(name,0))/total_w) * 0.8)
    dup_penalty = 0.10 if is_duplicate else 0.0
    total_norm = max(0.0, q_score + content_s - dup_penalty)
    total = int(round(total_norm*100))
    grade = "A" if total >= 80 else ("B" if total >= 65 else "C")
    details = " · ".join([f"{k}:{v:.2f}" for k,v in parts.items()])
    text = (
        "🧮 *Điểm 5S cho ảnh này*\n"
        f"- Tổng: *{total}/100* → Loại *{grade}*\n"
        f"- KV: `{kv_key}` · Hạng mục: {details}\n"
        f"- Chất lượng: nét {sharp_s:.2f} · sáng {bright_s:.2f} · kích thước {w}×{h}"
    )
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

    
    # ===== CHẤM ĐIỂM 5S (rule-based, không ML) =====
    if SCORING_ENABLED and SCORING_MODE == "rule":
        # Lấy KV từ caption/text nếu có
        kv_text = None
        m_kv = AREA_RX.search(caption_from_group or "")
        if m_kv:
            kv_text = m_kv.group(1)
        reply_text = apply_scoring_rule(b, kv_text or "", is_duplicate=False)
        try:
            await msg.reply_text(reply_text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass

    await ack_photo_progress(context, msg.chat_id, id_kho, kho_map[id_kho], d, cur)
    # Đặt cảnh báo trễ 6s sau mỗi lần ghi nhận (job sẽ tự kiểm tra và chỉ gửi nếu <4 hoặc >4)
    schedule_delayed_warning(context, msg.chat_id, id_kho, d)
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