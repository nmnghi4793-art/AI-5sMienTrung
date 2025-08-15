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
import random, time

SCORING_ENABLED = os.getenv("SCORING_ENABLED","0") == "1"
SCORING_MODE = os.getenv("SCORING_MODE","rule").strip().lower() or "rule"
# Default weights per area
_DEFAULT_WEIGHTS = {
    "HangHoa": {"align": 40, "tidy": 40, "aisle": 20},
    "WC":      {"stain": 60, "trash": 25, "dry": 15},
    "KhoBai":  {"clean": 50, "obstacle": 30, "line": 20},
    "VanPhong":{"desk_tidy": 45, "surface_clean": 35, "cable": 20}

}
# Ngưỡng để nêu vấn đề/khuyến nghị (0..1)
_AREA_RULE_THRESHOLDS = {
    "HangHoa": {"align": 0.70, "tidy": 0.60, "aisle": 0.70},
    "WC":      {"stain": 0.80, "trash": 0.80, "dry": 0.70},
    "KhoBai":  {"clean": 0.80, "obstacle": 0.75, "line": 0.70},
    "VanPhong":{"desk_tidy": 0.75, "surface_clean": 0.80, "cable": 0.70}
}
try:
    AREA_RULE_THRESHOLDS = json.loads(os.getenv("AREA_RULE_THRESHOLDS","") or "{}")
    for k, v in _AREA_RULE_THRESHOLDS.items():
        AREA_RULE_THRESHOLDS.setdefault(k, v)
except Exception:
    AREA_RULE_THRESHOLDS = _AREA_RULE_THRESHOLDS
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

# ========= SCORING: helper to compact message & delayed send (5s) =========
def compact_scoring_text(full_md: str) -> str:
    lines = [ln for ln in (full_md or '').splitlines() if ln.strip()]
    kept = []
    for ln in lines:
        st = ln.strip()
        if st.startswith("- KV:"):
            continue
        if st.startswith("- Chất lượng:"):
            continue
        kept.append(ln)
    return "\n".join(kept)

async def _send_scoring_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    text = data.get("text")
    if chat_id and text:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass


def _day_key(d):
    return d.isoformat()


# ==== GỘP TIN NHẮN CHẤM ĐIỂM (AGGREGATE) ====
from collections import defaultdict

# ---- Duplicate similarity tracking (pHash) ----
def _phash_cv(img_bgr):
    import cv2, numpy as np
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
    dct = cv2.dct(np.float32(small))
    block = dct[:8, :8]
    med = np.median(block[1:])
    bits = (block > med).flatten()
    h = 0
    for b in bits:
        h = (h << 1) | int(bool(b))
    return int(h) & ((1<<64)-1)

def _hamming64(a: int, b: int) -> int:
    return int(bin((a ^ b) & ((1<<64)-1)).count("1"))

from collections import deque
DUP_HISTORY = {}  # key: f"{chat_id}|{id_kho}" -> deque[(phash:int, ngay_str:str)]
_DUP_MAX = 60

def _dup_key(chat_id: int, id_kho: str) -> str:
    return f"{chat_id}|{id_kho}"

def _dup_best_match(dup_key: str, phash: int):
    dq = DUP_HISTORY.get(dup_key, deque())
    best_sim, best_date = 0.0, None
    for h, dstr in dq:
        ham = _hamming64(h, phash)
        sim = 1.0 - (ham / 64.0)
        if sim > best_sim:
            best_sim, best_date = sim, dstr
    return best_sim, best_date

def _dup_push(dup_key: str, phash: int, ngay_str: str):
    dq = DUP_HISTORY.setdefault(dup_key, deque(maxlen=_DUP_MAX))
    dq.append((phash, ngay_str))
SCORING_BUFFER = defaultdict(list)  # key -> list[dict]
SCORING_JOBS = {}

def _scoring_key(chat_id: int, id_kho: str, ngay_str: str) -> str:
    return f"{chat_id}|{id_kho}|{ngay_str}"



# ============ DIAGNOSTICS VARIETY ============

# Kho câu theo từng KV & hạng mục
SIMPLE_ISSUE_BANK = {
    "HangHoa": {
        "align": [
            "Hàng chưa thẳng hàng, lệch so với mép kệ",
            "Các kiện/thùng xếp không song song, tạo cảm giác lộn xộn",
            "Một số pallet bị xoay khác hướng còn lại",
            "Có khoảng hở/nhô ra ở dãy xếp gây mất thẩm mỹ"
        ],
        "tidy": [
            "Khu vực bề bộn, nhiều vật nhỏ rời rạc",
            "Thùng rỗng/chai lọ chưa gom về khu tập kết",
            "Vật dụng tạm đặt sai khu vực quy định",
            "Có rác vụn/bao bì thừa trên bề mặt"
        ],
        "aisle": [
            "Lối đi bị hẹp, có vật cản lấn line",
            "Khoảng di chuyển chưa thông thoáng",
            "Pallet/kiện hàng đặt sát hoặc đè lên vạch kẻ",
            "Hành lang không đảm bảo an toàn khi lưu thông"
        ]
    },
    "WC": {
        "stain": [
            "Bồn/sàn có vệt ố hoặc bám bẩn thấy rõ",
            "Vách/thiết bị vệ sinh còn vệt nước và cặn",
            "Góc cạnh/khó vệ sinh còn bám dơ"
        ],
        "trash": [
            "Sàn còn rác/giấy vụn",
            "Thùng rác đầy hoặc không có nắp",
            "Một số khu vực thiếu điểm tập kết rác"
        ],
        "dry": [
            "Sàn ướt, có nguy cơ trơn trượt",
            "Chưa lau khô sau khi vệ sinh",
            "Thiếu biển cảnh báo khu vực sàn ướt"
        ]
    },
    "KhoBai": {
        "clean": [
            "Sàn kho còn bụi bẩn/mảng tối",
            "Dầu mỡ/đất cát chưa xử lý triệt để",
            "Khu vực tải/xếp dỡ bám dơ"
        ],
        "obstacle": [
            "Lối đi có vật cản/đặt đồ tạm",
            "Hàng tạm thời chưa quy hoạch, che khu line",
            "Chướng ngại làm cản trở xe nâng/người đi bộ"
        ],
        "line": [
            "Vạch kẻ chỉ dẫn mờ/khó nhìn",
            "Biển báo vị trí chưa nổi bật",
            "Thiếu nhãn/vạch kẻ tại một số ô/khu"
        ]
    },
    "VanPhong": {
        "desk_tidy": [
            "Bàn làm việc bừa bộn, nhiều vật chưa phân loại",
            "Tài liệu/đồ cá nhân chưa để đúng vị trí",
            "Thiếu khay/hộp giúp sắp xếp gọn"
        ],
        "surface_clean": [
            "Bề mặt có bụi/vệt bẩn",
            "Màn hình/thiết bị có dấu tay/bám bẩn",
            "Khăn lau/dung dịch vệ sinh chưa sử dụng thường xuyên"
        ],
        "cable": [
            "Dây điện/cáp chưa gom gọn",
            "Dây thả tự do gây rối mắt/khó dọn",
            "Thiếu kẹp/ống bọc để cố định dây"
        ]
    }
}

SIMPLE_REC_BANK = {
    "HangHoa": {
        "align": [
            "Căn thẳng theo mép kệ hoặc vạch; xoay cùng một hướng",
            "Dùng nêm/chặn mép để giữ thẳng hàng",
            "Rà soát pallet lệch, điều chỉnh lại ngay"
        ],
        "tidy": [
            "Gom thùng rỗng về khu tập kết; bỏ vật cản",
            "Phân loại theo SKU/khu vực, dán nhãn rõ",
            "Thiết lập thùng/kệ tạm cho vật nhỏ dễ rơi"
        ],
        "aisle": [
            "Giữ lối đi ≥ 1m, không lấn vạch",
            "Di dời vật cản khỏi line; bố trí khu đồ tạm riêng",
            "Nhắc nhở ca làm việc không xếp chặn lối đi"
        ]
    },
    "WC": {
        "stain": [
            "Cọ rửa định kỳ; dùng dung dịch tẩy phù hợp",
            "Tập trung vệ sinh góc khuất/vết ố khó xử lý",
            "Thiết lập checklist vệ sinh theo ca"
        ],
        "trash": [
            "Thu gom rác ngay; dùng thùng rác có nắp",
            "Bố trí thêm thùng rác ở điểm phát sinh",
            "Nhắc đổ rác cuối ca để tránh tồn đọng"
        ],
        "dry": [
            "Lau khô sàn sau vệ sinh",
            "Đặt biển cảnh báo khi sàn ướt",
            "Kiểm tra rò rỉ để xử lý nguồn nước"
        ]
    },
    "KhoBai": {
        "clean": [
            "Quét/lau sàn theo lịch; xử lý dầu tràn ngay",
            "Dụng cụ vệ sinh đặt sẵn tại khu thao tác",
            "Áp dụng 5S cuối ca tại khu xếp dỡ"
        ],
        "obstacle": [
            "Quy định khu đồ tạm, không đặt trên line",
            "Lập sơ đồ lối đi & nhắc nhở tuân thủ",
            "Dọn chướng ngại để xe nâng lưu thông an toàn"
        ],
        "line": [
            "Sơn/kẻ lại vạch, bổ sung biển báo",
            "Chuẩn hóa nhãn vị trí tại ô kệ",
            "Kiểm tra định kỳ độ rõ của line"
        ]
    },
    "VanPhong": {
        "desk_tidy": [
            "Dùng khay/hộp phân loại; dọn bàn cuối ngày",
            "Thiết lập quy tắc 1 phút dọn bàn giữa ca",
            "Cất đồ cá nhân vào ngăn/locker"
        ],
        "surface_clean": [
            "Lau bề mặt với dung dịch phù hợp",
            "Lập tần suất vệ sinh hàng ngày/tuần",
            "Chuẩn bị khăn lau/giấy tại chỗ"
        ],
        "cable": [
            "Gom dây về một mép bàn, dùng kẹp/ống bọc",
            "Dán nhãn đầu dây để dễ quản lý",
            "Cố định ổ cắm/dây nguồn để gọn mắt"
        ]
    }
}

def _pick_many(pool: list, k: int = 2) -> list:
    if not pool: return []
    k = min(k, len(pool))
    return random.sample(pool, k)

def _kv_for_variety(kv_key: str) -> str:
    return kv_key if kv_key in SIMPLE_ISSUE_BANK else 'HangHoa'

def _diagnose_varied(kv_key: str, parts: dict) -> tuple[list, list]:
    """
    Sinh 'Vấn đề/Khuyến nghị' đa dạng theo KV & hạng mục bị dưới ngưỡng.
    """
    kv = _kv_for_variety(kv_key)
    th = AREA_RULE_THRESHOLDS.get(kv, _AREA_RULE_THRESHOLDS[kv])

    # Seed theo thời điểm để câu chữ đổi linh hoạt
    random.seed(hash(f"{kv}{time.time_ns()}") % (2**32))

    issues, recs = [], []
    for metric, val in parts.items():
        thr = th.get(metric, 0.75)
        if float(val) < float(thr):  # dưới ngưỡng → nêu vấn đề & gợi ý
            issues += _pick_many(SIMPLE_ISSUE_BANK.get(kv, {}).get(metric, []), k=2)
            recs   += _pick_many(SIMPLE_REC_BANK.get(kv, {}).get(metric, []),   k=2)

    # Khử trùng lặp & rút gọn tối đa 5 ý mỗi phần
    def _dedup(xs, limit=5):
        seen, out = set(), []
        for x in xs:
            if not x or x in seen: continue
            seen.add(x); out.append(x)
            if len(out) >= limit: break
        return out

    return _dedup(issues, 5), _dedup(recs, 5)
# ========== END DIAGNOSTICS VARIETY ==========
def apply_scoring_struct(photo_bytes: bytes, kv_active: str|None, is_duplicate: bool, dup_key: str, ngay_str: str):
    """
    Trả về cấu trúc cho gộp: {'total','grade','issues','recs','dup','sim','dup_date'}
    Bắt buộc có vấn đề/khuyến nghị nếu total < 95.
    Tính tương đồng ảnh bằng pHash và lưu lịch sử theo (chat_id|id_kho).
    """
    if not SCORING_ENABLED:
        return {'total': 0, 'grade': 'C', 'issues': [], 'recs': [], 'dup': is_duplicate, 'sim': 0.0, 'dup_date': None}

    # 1) Đọc ảnh + pHash
    img = cv2.imdecode(np.frombuffer(photo_bytes, np.uint8), cv2.IMREAD_COLOR)
    try:
        phash = _phash_cv(img)
    except Exception:
        phash = None

    # 2) Chất lượng
    sharp_s, bright_s, size_s, (w, h) = _score_quality_components(img)
    q_score = 0.2 * (0.6 * sharp_s + 0.4 * bright_s)

    # 3) Nội dung theo KV
    parts, kv_key = _score_by_kv(photo_bytes, kv_active or "")
    weights = AREA_RULE_WEIGHTS.get(kv_key, _DEFAULT_WEIGHTS[kv_key])
    total_w = float(sum(weights.values())) or 100.0
    content_s = 0.0
    for name, val in parts.items():
        w_part = float(weights.get(name, 0.0))
        content_s += (float(val) * (w_part / total_w) * 0.8)

    # 4) So trùng (pHash) trên lịch sử nhiều ngày
    sim_best, sim_date = 0.0, None
    if phash is not None and dup_key:
        sim_best, sim_date = _dup_best_match(dup_key, phash)
        if sim_best >= 0.90:
            is_duplicate = True

    # 5) Tổng điểm
    dup_penalty = 0.10 if is_duplicate else 0.0
    total_norm = max(0.0, q_score + content_s - dup_penalty)
    total = int(round(total_norm * 100))
    grade = "A" if total >= 80 else ("B" if total >= 65 else "C")

    # 6) Vấn đề / Khuyến nghị
    issues, recs = _diagnose_varied(kv_key, parts)
    if sharp_s < 0.80:
        issues.append("Ảnh hơi mờ/thiếu nét")
        recs.append("Giữ chắc tay hoặc tựa vào bề mặt; chụp gần hơn nếu cần")
    if bright_s < 0.80:
        issues.append("Ảnh quá tối/hoặc quá sáng")
        recs.append("Chụp nơi đủ sáng, tránh ngược sáng; bật đèn khu vực")
    if size_s < 1.0:
        issues.append("Kích thước ảnh nhỏ/thiếu chi tiết")
        recs.append("Dùng độ phân giải cao hơn hoặc đứng gần đối tượng hơn")
    if is_duplicate:
        pct = int(round(sim_best * 100)) if sim_best > 0 else None
        if pct is not None and sim_date:
            issues.append(f"Ảnh trùng ~{pct}% so với ảnh đã gửi ngày {sim_date}")
            recs.append("Chụp lại ảnh mới, đổi góc chụp để phản ánh hiện trạng")
        else:
            issues.append("Ảnh bị trùng lặp với ảnh đã gửi")
            recs.append("Gửi ảnh mới chụp cho khu vực tương ứng")

    if total < 95 and not issues:
        issues.append("Điểm chưa đạt 95/100 theo chuẩn 5S")
        recs.append("Xem lại sắp xếp/vệ sinh/lối đi và chụp lại ảnh rõ hơn nếu cần")

    # 7) Lưu lịch sử pHash
    try:
        if phash is not None and dup_key:
            _dup_push(dup_key, phash, ngay_str)
    except Exception:
        pass

    return {'total': total, 'grade': grade, 'issues': issues, 'recs': recs, 'dup': is_duplicate, 'sim': sim_best, 'dup_date': sim_date}


def _compose_aggregate_message(items: list, id_kho: str, ngay_str: str) -> str:
    header = "🧮 *Điểm 5S cho lô ảnh này*\n" + f"- Kho: `{id_kho}` · Ngày: `{ngay_str}`\n"
    lines = []
    agg_issues, agg_recs = [], []
    for idx, it in enumerate(items, 1):
        if it.get('dup'):
            pct = int(round(it.get('sim',0)*100)) if it.get('sim') else None
            if pct and it.get('dup_date'):
                dup_txt = f"❌ ~{pct}% (ảnh ngày {it['dup_date']})"
            else:
                dup_txt = "❌"
        else:
            dup_txt = "✅"
        lines.append(f"• Ảnh #{idx}: *{it['total']}/100* → Loại *{it['grade']}*")
        agg_issues.extend(it.get('issues', [])); agg_recs.extend(it.get('recs', []))
    def _uniq_first(xs, limit=5):
        seen, out = set(), []
        for x in xs:
            if not x or x in seen: continue
            seen.add(x); out.append(x)
            if len(out) >= limit: break
        return out
    issues_u = _uniq_first(agg_issues, 5); recs_u = _uniq_first(agg_recs, 5)
    msg = header + "\n" + "\n".join(lines)
    if issues_u or recs_u:
        msg += "\n\n⚠️ *Vấn đề:*" + "".join([f"\n • {x}" for x in issues_u])
        msg += "\n\n🛠️ *Khuyến nghị:*" + "".join([f"\n • {x}" for x in recs_u])
    return msg

async def _send_scoring_aggregate(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    key = data.get("key"); chat_id = data.get("chat_id"); id_kho = data.get("id_kho"); ngay_str = data.get("ngay")
    items = SCORING_BUFFER.pop(key, []); SCORING_JOBS.pop(key, None)
    if not items: return
    text = _compose_aggregate_message(items, id_kho, ngay_str)
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass

def schedule_scoring_aggregate(context, chat_id: int, id_kho: str, ngay_str: str, delay_seconds: int = 5):
    key = _scoring_key(chat_id, id_kho, ngay_str)
    old = SCORING_JOBS.get(key)
    if old:
        try: old.schedule_removal()
        except Exception: pass
    job = context.job_queue.run_once(
        _send_scoring_aggregate, when=delay_seconds,
        data={"key": key, "chat_id": chat_id, "id_kho": id_kho, "ngay": ngay_str},
        name=f"score_agg_{key}"
    )
    SCORING_JOBS[key] = job
# ==== HẾT PHẦN GỘP ====
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

def _score_by_kv(photo_bytes: bytes, kv_text: str):
    img = cv2.imdecode(np.frombuffer(photo_bytes, np.uint8), cv2.IMREAD_COLOR)
    kv_key = _kv_key_from_text(kv_text)
    if kv_key == "HangHoa":
        parts = _score_hanghoa(img)
    elif kv_key == "WC":
        parts = _score_wc(img)
    elif kv_key == "KhoBai":
        parts = _score_khobai(img)
    else:
        parts = _score_vanphong(img)
        kv_key = "VanPhong"
    return parts, kv_key

def _diagnose(kv_key: str, parts: dict):
    th = AREA_RULE_THRESHOLDS.get(kv_key, _AREA_RULE_THRESHOLDS[kv_key])
    issues, recs = [], []
    if kv_key == "HangHoa":
        if parts.get("align",1) < th["align"]:
            issues.append("Hàng hóa chưa thẳng hàng / không song song kệ")
            recs.append("Chỉnh thẳng kiện/thùng theo line hoặc mép kệ; dùng pallet một hướng")
        if parts.get("tidy",1) < th["tidy"]:
            issues.append("Khu vực bừa bộn, nhiều vật nhỏ rời rạc")
            recs.append("Gom thùng rỗng, bỏ vật cản; phân khu rõ theo loại hàng")
        if parts.get("aisle",1) < th["aisle"]:
            issues.append("Lối đi bị hẹp hoặc có vật chắn")
            recs.append("Giữ lối đi thông thoáng (≥ 1m), không xếp hàng lấn line")
    elif kv_key == "WC":
        if parts.get("stain",1) < th["stain"]:
            issues.append("Bồn/sàn có vết bẩn/ố"); recs.append("Cọ rửa bồn, sàn; dùng dung dịch tẩy rửa định kỳ")
        if parts.get("trash",1) < th["trash"]:
            issues.append("Có rác/giấy vụn trên sàn"); recs.append("Thu gom rác; thêm thùng rác nắp; đổ rác cuối ca")
        if parts.get("dry",1) < th["dry"]:
            issues.append("Sàn ướt hoặc còn vệt nước"); recs.append("Lau khô sàn; treo biển cảnh báo sàn ướt khi vệ sinh")
    elif kv_key == "KhoBai":
        if parts.get("clean",1) < th["clean"]:
            issues.append("Sàn kho bẩn / có nhiều mảng tối"); recs.append("Quét dọn/lau sàn theo tần suất; xử lý dầu tràn ngay")
        if parts.get("obstacle",1) < th["obstacle"]:
            issues.append("Có chướng ngại/lộn xộn ở lối đi"); recs.append("Di dời vật cản; quy định khu đặt đồ tạm không lấn line")
        if parts.get("line",1) < th["line"]:
            issues.append("Line kẻ chỉ dẫn mờ/khó thấy"); recs.append("Sơn/kẻ lại line; bổ sung biển báo vị trí")
    else:  # VanPhong
        if parts.get("desk_tidy",1) < th["desk_tidy"]:
            issues.append("Bàn làm việc lộn xộn"); recs.append("Sắp xếp vật dụng; dùng khay/hộp phân loại; dọn bàn cuối ngày")
        if parts.get("surface_clean",1) < th["surface_clean"]:
            issues.append("Bề mặt có bụi/vết bẩn"); recs.append("Lau bề mặt bằng dung dịch phù hợp; lịch vệ sinh hằng ngày")
        if parts.get("cable",1) < th["cable"]:
            issues.append("Dây điện/cáp lộn xộn"); recs.append("Dùng kẹp/ống bọc dây; gom dây về một mép bàn/đế cố định")
    return issues, recs
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
    # duplicate similarity across days
    sim_best, sim_date = (0.0, None)
    if 'phash' in locals() and phash is not None and dup_key:
        sim_best, sim_date = _dup_best_match(dup_key, phash)
        if sim_best >= 0.90:
            is_duplicate = True
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
            f"⚠️ *{kho_map[id_kho]}* hôm nay đã có 1 ảnh *giống hệt* ảnh này. Vui lòng thay ảnh khác.",
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
        dupkey = _dup_key(msg.chat_id, str(id_kho))
        ngay_text = d.strftime('%d/%m/%Y')
        item = apply_scoring_struct(b, kv_text or "", False, dupkey, ngay_text)
        key = _scoring_key(msg.chat_id, str(id_kho), ngay_text)
        SCORING_BUFFER[key].append(item)

    await ack_photo_progress(context, msg.chat_id, id_kho, kho_map[id_kho], d, cur)
    # Đặt cảnh báo trễ 6s sau mỗi lần ghi nhận (job sẽ tự kiểm tra và chỉ gửi nếu <4 hoặc >4)
    schedule_delayed_warning(context, msg.chat_id, id_kho, d)

    # Gửi đánh giá 5S thành 1 tin nhắn, trễ 5 giây sau khi báo ghi nhận
    if SCORING_ENABLED and SCORING_MODE == "rule":
        try:
            schedule_scoring_aggregate(context, chat_id=msg.chat_id, id_kho=str(id_kho), ngay_str=d.strftime('%d/%m/%Y'), delay_seconds=5)
        except Exception:
            pass
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


# ======= SIMPLE BANKS (ngắn gọn, dễ hiểu, 10–20 ý mỗi nhóm) =======
SIMPLE_SIMPLE_ISSUE_BANK = {
    "VanPhong": {
        "tidy": [
            "Bàn có nhiều bụi","Giấy tờ để lộn xộn","Dụng cụ chưa gọn","Màn hình chưa sạch","Dây cáp rối",
            "Ly tách, thức ăn để trên bàn","Khăn giấy bừa bộn","Ngăn kéo lộn xộn","Bề mặt dính bẩn","Bàn phím/bàn di bẩn",
            "Ghế không ngay vị trí","Thùng rác đầy","Nhiều vật nhỏ rơi vãi","Kệ tài liệu chưa phân khu","Bảng ghi chú rối mắt"
        ],
        "align": [
            "Vật dụng đặt chưa ngay ngắn","Đồ đạc lệch vị trí","Tài liệu chưa xếp thẳng mép",
            "Màn hình/đế đỡ lệch","Bút, sổ không theo hàng"
        ],
        "aisle": [
            "Lối đi bị vướng đồ","Có vật cản dưới chân bàn","Dây điện vắt ngang lối đi",
            "Thùng carton chắn lối","Túi đồ để dưới chân ghế"
        ]
    },
    "WC": {
        "stain": [
            "Bồn/bề mặt còn vết bẩn","Gương, tay nắm chưa sạch","Vết ố quanh vòi","Vệt nước trên gương",
            "Vách ngăn bám bẩn","Sàn bám cặn"
        ],
        "trash": [
            "Thùng rác đầy","Rác chưa gom","Túi rác không thay","Rác rơi ra ngoài"
        ],
        "dry": [
            "Sàn còn ướt","Có vệt nước đọng","Khăn giấy rơi xuống sàn","Chưa đặt biển cảnh báo khi sàn ướt"
        ],
        "supply": [
            "Thiếu giấy/ xà phòng","Không có khăn lau tay","Bình xịt trống","Chưa bổ sung vật tư"
        ]
    },
    "HangHoa": {
        "align": [
            "Hàng chưa thẳng hàng","Pallet xoay khác hướng","Có khoảng hở trong dãy xếp","Thùng nhô ra mép kệ",
            "Kiện cao thấp không đều","Hàng lệch line vạch","Thùng xẹp/biến dạng","Xếp chồng mất cân bằng"
        ],
        "tidy": [
            "Khu vực còn bừa bộn","Thùng rỗng chưa gom","Vật tạm đặt sai chỗ","Màng PE rách vương vãi",
            "Dụng cụ chưa trả về vị trí","Bao bì rách nhưng chưa xử lý","Nhãn mác bong tróc","Có hàng đặt trực tiếp xuống sàn"
        ],
        "aisle": [
            "Lối đi bị lấn","Đồ cản trở đường đi","Pallet để dưới line","Hàng đẩy qua vạch an toàn",
            "Khu vực thao tác chật hẹp"
        ],
        "bulky": [
            "Hàng cồng kềnh chưa cố định","Dây đai lỏng","Điểm tựa không chắc","Đặt sai hướng nâng hạ",
            "Thiếu nẹp góc/đệm bảo vệ","Chưa dán nhãn cảnh báo kích thước/tải trọng"
        ]
    },
    "LoiDi": {
        "aisle": [
            "Lối đi có vật cản","Vạch sơn mờ","Hàng lấn sang lối đi","Có chất lỏng rơi vãi",
            "Thiếu biển chỉ dẫn","Lối thoát hiểm chưa thông thoáng","Xe đẩy dừng sai vị trí"
        ]
    },
    "KePallet": {
        "align": [
            "Pallet không ngay hàng","Cạnh pallet lệch mép kệ","Kiện chồng quá cao","Thanh giằng không cân đối"
        ],
        "tidy": [
            "Pallet hỏng chưa loại bỏ","Mảnh gỗ vụn trên sàn","Tem cũ chưa bóc","Màng PE dư chưa xử lý"
        ]
    }
}

SIMPLE_SIMPLE_REC_BANK = {
    "VanPhong": {
        "tidy": [
            "Lau bụi bề mặt","Xếp giấy tờ theo nhóm","Cất dụng cụ vào khay","Lau sạch màn hình","Buộc gọn dây cáp",
            "Bỏ thức ăn/ly tách đúng chỗ","Dán nhãn khay/ngăn kéo","Dọn rác ngay","Dùng khăn lau khử khuẩn",
            "Sắp xếp bút, sổ vào ống/kệ"
        ],
        "align": [
            "Đặt đồ ngay ngắn","Cố định vị trí dùng thường xuyên","Căn thẳng theo mép bàn/kệ",
            "Dùng khay chia ô cho phụ kiện"
        ],
        "aisle": [
            "Dẹp vật cản khỏi lối đi","Bó gọn dây điện sát tường","Không đặt thùng/hộp dưới lối chân",
            "Tận dụng kệ treo cho đồ lặt vặt"
        ]
    },
    "WC": {
        "stain": [
            "Cọ rửa bằng dung dịch phù hợp","Lau gương, tay nắm","Chà sạch vết ố quanh vòi",
            "Vệ sinh vách ngăn và sàn"
        ],
        "trash": [
            "Đổ rác ngay","Thay túi rác mới","Đặt thùng có nắp"
        ],
        "dry": [
            "Lau khô sàn","Đặt biển cảnh báo khi sàn ướt","Kiểm tra rò rỉ, xử lý ngay"
        ],
        "supply": [
            "Bổ sung giấy/ xà phòng","Thêm khăn lau tay","Nạp đầy bình xịt"
        ]
    },
    "HangHoa": {
        "align": [
            "Căn theo mép kệ/vạch","Xoay cùng một hướng","Bổ sung nẹp góc giữ thẳng","San phẳng chiều cao chênh lệch"
        ],
        "tidy": [
            "Gom thùng rỗng về khu tập kết","Dọn vật tạm đặt sai chỗ","Quấn lại màng PE gọn gàng",
            "In/dán lại nhãn mác rõ ràng","Đặt hàng trên pallet, không đặt sàn"
        ],
        "aisle": [
            "Giữ lối đi thông thoáng","Di dời vật cản khỏi line","Chừa khoảng an toàn ≥ 1m"
        ],
        "bulky": [
            "Đai cố định chắc chắn","Thêm nẹp góc/đệm bảo vệ","Đặt hướng thuận lợi nâng hạ",
            "Ghi chú kích thước/tải trọng rõ ràng","Bổ sung điểm chèn chống xê dịch"
        ]
    },
    "LoiDi": {
        "aisle": [
            "Dọn sạch vật cản","Sơn lại vạch dẫn hướng","Đặt lại hàng vượt vạch","Lau sạch chất lỏng rơi vãi",
            "Đảm bảo lối thoát hiểm thông suốt","Quy định vị trí dừng cho xe đẩy"
        ]
    },
    "KePallet": {
        "align": [
            "Căn thẳng mép pallet","Không chồng quá quy định","Kiểm tra thanh giằng, cân chỉnh"
        ],
        "tidy": [
            "Loại bỏ pallet hỏng","Quét dọn mảnh gỗ vụn","Cắt bỏ màng PE thừa","Bóc tem cũ trước khi dán tem mới"
        ]
    }
}
# ======= END SIMPLE BANKS =======



# ========= USER SIMPLE PHRASES (ngắn gọn – đa dạng, ưu tiên HangHoa) =========
def _prepend_unique(dst: dict, kv: str, cat: str, items: list):
    kvd = dst.setdefault(kv, {})
    arr = kvd.setdefault(cat, [])
    for s in reversed(items):
        if s not in arr:
            arr.insert(0, s)

def _apply_user_simple_overlay_all():
    # ===== HANG HOA (ưu tiên hàng cồng kềnh) =====
    _prepend_unique(SIMPLE_ISSUE_BANK, "HangHoa", "tidy", [
        "Hàng hóa không được sắp xếp gọn gàng",
        "Cần cải thiện vệ sinh khu vực",
        "Thiếu nhãn mác cho hàng hóa",
        "Thùng rỗng chưa gom",
        "Màng PE thừa/chưa cắt gọn",
        "Bao bì rách chưa xử lý",
        "Dụng cụ tạm đặt sai vị trí",
        "Khu vực chất hàng bừa bộn",
        "Có hàng đặt trực tiếp xuống sàn",
        "Tem cũ chưa bóc trước khi dán tem mới"
    ])
    _prepend_unique(SIMPLE_ISSUE_BANK, "HangHoa", "align", [
        "Một số pallet có hàng hóa chất đống",
        "Hàng không thẳng hàng theo mép kệ",
        "Pallet xoay khác hướng còn lại",
        "Có khoảng hở giữa các kiện",
        "Kiện chồng cao, dễ mất cân bằng",
        "Thùng nhô ra mép pallet",
        "Xếp chồng chưa đồng đều chiều cao",
        "Nẹp góc thiếu hoặc lỏng",
        "Thùng méo/xẹp ảnh hưởng xếp chồng",
        "Hàng đặt lệch line đánh dấu"
    ])
    _prepend_unique(SIMPLE_ISSUE_BANK, "HangHoa", "aisle", [
        "Không có lối đi rõ ràng giữa các khu vực",
        "Lối đi bị lấn bởi hàng hóa",
        "Vạch an toàn mờ/khó thấy",
        "Có vật cản trong đường đi xe nâng",
        "Chất lỏng rơi vãi trên sàn",
        "Hàng vượt qua vạch giới hạn"
    ])
    _prepend_unique(SIMPLE_ISSUE_BANK, "HangHoa", "bulky", [
        "Hàng cồng kềnh chưa cố định",
        "Dây đai lỏng hoặc thiếu",
        "Thiếu nẹp góc cho kiện lớn",
        "Đặt sai hướng nâng hạ",
        "Thiếu cảnh báo kích thước/tải trọng",
        "Điểm tựa/đệm kê không chắc chắn"
    ])

    _prepend_unique(SIMPLE_REC_BANK, "HangHoa", "tidy", [
        "Sắp xếp hàng hóa theo loại và kích thước",
        "Dọn dẹp khu vực để đảm bảo sạch sẽ",
        "Thêm nhãn mác cho hàng hóa",
        "Thực hiện kiểm tra định kỳ về 5S",
        "Gom thùng rỗng về khu tập kết",
        "Cắt gọn màng PE thừa",
        "Dán lại nhãn rõ ràng, dễ đọc",
        "Loại bỏ bao bì rách, thay mới",
        "Thu hồi dụng cụ về đúng vị trí",
        "Không đặt hàng trực tiếp xuống sàn"
    ])
    _prepend_unique(SIMPLE_REC_BANK, "HangHoa", "align", [
        "Căn thẳng theo mép kệ/vạch chỉ dẫn",
        "Xoay cùng một hướng cho toàn bộ kiện",
        "San phẳng chiều cao giữa các lớp",
        "Bổ sung nẹp góc để giữ thẳng",
        "Đặt sát mép trong của pallet",
        "Kiểm tra cân bằng trước khi rời vị trí"
    ])
    _prepend_unique(SIMPLE_REC_BANK, "HangHoa", "aisle", [
        "Tạo lối đi rõ ràng giữa các pallet",
        "Giữ lối đi thông thoáng ≥ 1m",
        "Sơn/khôi phục lại vạch an toàn",
        "Di dời vật cản khỏi đường xe nâng",
        "Lau khô sàn, xử lý ngay chất đổ",
        "Không vượt qua vạch giới hạn"
    ])
    _prepend_unique(SIMPLE_REC_BANK, "HangHoa", "bulky", [
        "Đai cố định chắc chắn các kiện lớn",
        "Thêm nẹp góc/đệm bảo vệ cho cạnh bén",
        "Sắp xếp theo hướng thuận lợi nâng hạ",
        "Ghi rõ kích thước/tải trọng trên nhãn",
        "Chèn thêm điểm tựa chống xê dịch"
    ])

    # ===== KE PALLET =====
    _prepend_unique(SIMPLE_ISSUE_BANK, "KePallet", "align", [
        "Pallet lệch mép kệ",
        "Kiện chồng quá cao mức cho phép",
        "Thanh giằng không cân đối",
        "Khoảng cách an toàn đỉnh kệ không đủ"
    ])
    _prepend_unique(SIMPLE_ISSUE_BANK, "KePallet", "tidy", [
        "Pallet hỏng chưa loại bỏ",
        "Mảnh gỗ vụn còn trên sàn",
        "Tem cũ còn sót lại",
        "Màng PE dư chưa cắt"
    ])
    _prepend_unique(SIMPLE_REC_BANK, "KePallet", "align", [
        "Căn thẳng mép pallet theo tiêu chuẩn",
        "Không chồng quá quy định chiều cao",
        "Kiểm tra thanh giằng và cân chỉnh lại",
        "Đảm bảo khoảng cách an toàn phần đầu kệ"
    ])
    _prepend_unique(SIMPLE_REC_BANK, "KePallet", "tidy", [
        "Loại bỏ pallet hỏng ngay",
        "Quét dọn sạch mảnh gỗ vụn",
        "Bóc tem cũ trước khi dán mới",
        "Cắt gọn màng PE thừa"
    ])

    # ===== LOI DI =====
    _prepend_unique(SIMPLE_ISSUE_BANK, "LoiDi", "aisle", [
        "Lối đi có vật cản",
        "Vạch dẫn hướng mờ/đứt đoạn",
        "Hàng lấn sang lối đi",
        "Có chất lỏng rơi vãi",
        "Thiếu biển hướng dẫn",
        "Lối thoát hiểm chưa thông thoáng"
    ])
    _prepend_unique(SIMPLE_REC_BANK, "LoiDi", "aisle", [
        "Dọn sạch vật cản ngay",
        "Sơn lại vạch dẫn hướng",
        "Sắp xếp lại hàng vượt vạch",
        "Lau sạch và xử lý chất đổ",
        "Bổ sung biển hướng dẫn rõ ràng",
        "Đảm bảo lối thoát hiểm thông suốt"
    ])

    # ===== VAN PHONG =====
    _prepend_unique(SIMPLE_ISSUE_BANK, "VanPhong", "tidy", [
        "Bàn có bụi và giấy tờ lộn xộn",
        "Dụng cụ tản mát, chưa có khay",
        "Màn hình/bàn phím bám bẩn",
        "Dây cáp rối dưới chân bàn",
        "Thùng rác đầy chưa đổ",
        "Nhiều vật nhỏ rơi vãi"
    ])
    _prepend_unique(SIMPLE_ISSUE_BANK, "VanPhong", "align", [
        "Vật dụng đặt chưa ngay ngắn",
        "Tài liệu chưa xếp thẳng mép",
        "Màn hình/đế đỡ lệch"
    ])
    _prepend_unique(SIMPLE_ISSUE_BANK, "VanPhong", "aisle", [
        "Lối đi bị vướng đồ",
        "Túi đồ để dưới chân ghế"
    ])
    _prepend_unique(SIMPLE_REC_BANK, "VanPhong", "tidy", [
        "Lau bụi bề mặt, khử khuẩn",
        "Xếp giấy tờ theo nhóm/chủ đề",
        "Dùng khay/hộp chia ô cho dụng cụ",
        "Buộc gọn dây cáp sát chân bàn",
        "Đổ rác ngay khi đầy"
    ])
    _prepend_unique(SIMPLE_REC_BANK, "VanPhong", "align", [
        "Sắp xếp đồ ngay ngắn, cố định vị trí",
        "Căn thẳng theo mép bàn/kệ"
    ])
    _prepend_unique(SIMPLE_REC_BANK, "VanPhong", "aisle", [
        "Dẹp đồ khỏi lối đi",
        "Không đặt túi đồ dưới lối chân"
    ])

    # ===== WC =====
    _prepend_unique(SIMPLE_ISSUE_BANK, "WC", "stain", [
        "Bề mặt/thiết bị còn vết bẩn",
        "Gương và tay nắm chưa sạch",
        "Vết ố quanh vòi rửa"
    ])
    _prepend_unique(SIMPLE_ISSUE_BANK, "WC", "trash", [
        "Thùng rác đầy",
        "Rác chưa gom gọn",
        "Túi rác không thay"
    ])
    _prepend_unique(SIMPLE_ISSUE_BANK, "WC", "dry", [
        "Sàn còn ướt",
        "Có vệt nước đọng"
    ])
    _prepend_unique(SIMPLE_REC_BANK, "WC", "stain", [
        "Cọ rửa bằng dung dịch phù hợp",
        "Lau sạch gương, tay nắm",
        "Chà sạch vết ố quanh vòi"
    ])
    _prepend_unique(SIMPLE_REC_BANK, "WC", "trash", [
        "Đổ rác ngay khi đầy",
        "Thay túi rác mới, dùng thùng có nắp"
    ])
    _prepend_unique(SIMPLE_REC_BANK, "WC", "dry", [
        "Lau khô sàn",
        "Đặt biển cảnh báo khi sàn ướt"
    ])

try:
    _apply_user_simple_overlay_all()
except Exception:
    pass
# ========= END USER SIMPLE PHRASES =========

