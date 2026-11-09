import os
import re
import json
import shutil
import subprocess
import threading
import time
import uuid
import argparse
import sys
import errno
from datetime import datetime, timedelta
import requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ─── CLI args (child bot mode) ───────────────────────────────────────────────
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument('--child-token', default=None)
_args, _ = _parser.parse_known_args()

MAIN_TOKEN  = '8710828818:AAGzFcbEWfLM37OVmKQE2Q3QC6I_s72SHFY'
TOKEN       = _args.child_token or MAIN_TOKEN
IS_CHILD    = _args.child_token is not None
bot         = telebot.TeleBot(TOKEN)

BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
M_FILE_PATH      = os.path.join(BASE_DIR, 'FPSDisplay.m')
BACKUP_FILE_PATH = os.path.join(BASE_DIR, 'FPSDisplay_backup.m')
DATA_FILE        = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.json')
ICON_PATH        = os.path.join(BASE_DIR, 'AppIcon.png')

# QR: tìm ở thư mục bot trước, rồi 3006iOS/project/
_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
QR_PATH  = next(
    (p for p in [
        os.path.join(_BOT_DIR, 'qr_payment.jpg'),
        os.path.join(BASE_DIR, 'qr_payment.jpg'),
    ] if os.path.exists(p)),
    os.path.join(_BOT_DIR, 'qr_payment.jpg')   # fallback (sẽ báo lỗi nếu thiếu)
)

ADMIN_IDS  = [7356705583, 7576164473]
MAX_DAILY  = 5
MAX_GETS   = 5

CHILD_BOT_PRICE = '80.000đ'   # Giá thuê bot con / tháng

# Gói gia hạn bot con
RENEWAL_OPTIONS = [
    {'label': '📅 1 Tháng  —  80.000đ',  'months': 1,  'price':  '80.000đ'},
    {'label': '📅 3 Tháng  — 230.000đ',  'months': 3,  'price': '230.000đ'},
    {'label': '📅 6 Tháng  — 280.000đ',  'months': 6,  'price': '280.000đ'},
    {'label': '📅 12 Tháng — 450.000đ',  'months': 12, 'price': '450.000đ'},
]

# Tự lấy username bot
BOT_USERNAME = bot.get_me().username

# API rút gọn link
LINK4M_API    = '6ab55d30f3aa7e517b147bae'
LINK2M_API    = '6abf549b0a2213657a3be114'

# Gói mua lượt
PURCHASE_OPTIONS = [
    {'label': '🎮 5 Lượt  —  5.000đ',  'builds': 5,  'price': '5.000đ'},
    {'label': '🎮 10 Lượt  —  10.000đ', 'builds': 10, 'price': '10.000đ'},
    {'label': '🎮 50 Lượt  —  35.000đ', 'builds': 50, 'price': '35.000đ'},
    {'label': '🎮 100 Lượt — 60.000đ',  'builds': 100,'price': '60.000đ'},
]

# Trạng thái in-memory
pending_links        = {}   # {user_id: {'nonce': str}}
pending_bills        = {}   # {user_id: {'builds': int, 'price': str, 'username': str}}
pending_fps          = {}   # {user_id: fps_name}
pending_child_rental = {}   # {user_id: {'token': str, 'username': str}} — chờ bill thuê bot con
pending_renewal      = {}   # {user_id: {'token': str, 'months': int, 'price': str}} — chờ bill gia hạn
child_procs          = {}   # {token: Popen} — chỉ dùng ở bot mẹ
expiry_notified      = set() # token đã bị thông báo hết hạn (tránh spam)

# Lưu token theo short_id để tránh callback_data > 64 byte (giới hạn Telegram)
_token_store         = {}   # {short_id: token}

def store_token(token):
    """Lưu token, trả về short_id 8 ký tự"""
    for sid, tok in _token_store.items():
        if tok == token:
            return sid
    sid = uuid.uuid4().hex[:8]
    _token_store[sid] = token
    return sid

def get_token(short_id):
    return _token_store.get(short_id, '')

# ─── Data ────────────────────────────────────────────────────────────────────

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {
        'approved_groups': [],
        'daily_used':  {},
        'gets_used':   {},
        'purchased':   {},
        'maintenance': False,
        'child_bots':  {},   # {token: {'owner_id': int, 'owner_name': str, 'expiry': 'YYYY-MM-DD'}}
    }

def save_data(data):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ─── Checks ──────────────────────────────────────────────────────────────────

def is_admin(user_id):
    return user_id in ADMIN_IDS

def is_allowed(message):
    if message.chat.type == 'private':
        return True
    return message.chat.id in load_data()['approved_groups']

def is_maintenance():
    return load_data()['maintenance']

def get_user_stats(user_id):
    data = load_data()
    uid  = str(user_id)
    return {
        'daily_used': data.get('daily_used', {}).get(uid, 0),
        'gets_used':  data.get('gets_used',  {}).get(uid, 0),
        'purchased':  data.get('purchased',  {}).get(uid, 0),
    }

def can_build(user_id):
    if is_admin(user_id): return True
    s = get_user_stats(user_id)
    return (MAX_DAILY - s['daily_used']) > 0 or s['purchased'] > 0

def use_build(user_id):
    data = load_data()
    uid  = str(user_id)
    s    = get_user_stats(user_id)
    if (MAX_DAILY - s['daily_used']) > 0:
        data.setdefault('daily_used', {})[uid] = s['daily_used'] + 1
    else:
        data.setdefault('purchased', {})[uid] = max(0, s['purchased'] - 1)
    save_data(data)

def get_remaining(user_id):
    if is_admin(user_id): return '∞'
    s = get_user_stats(user_id)
    return max(0, MAX_DAILY - s['daily_used']) + s['purchased']

def add_purchased(user_id, builds):
    data = load_data()
    uid  = str(user_id)
    data.setdefault('purchased', {})[uid] = data.get('purchased', {}).get(uid, 0) + builds
    save_data(data)

# ─── Reset 4h sáng ───────────────────────────────────────────────────────────

def reset_scheduler():
    already_reset = False
    while True:
        now = datetime.now()
        if now.hour == 4 and now.minute == 0:
            if not already_reset:
                data = load_data()
                data['daily_used'] = {}
                data['gets_used']  = {}
                save_data(data)
                already_reset = True
                for aid in ADMIN_IDS:
                    try: bot.send_message(aid, "🔄 4:00 SA — Đã Reset Lượt Build Cho Tất Cả User!")
                    except: pass
        else:
            already_reset = False
        time.sleep(30)

threading.Thread(target=reset_scheduler, daemon=True).start()

# ─── Link shortener ──────────────────────────────────────────────────────────

def shorten_link(dest_url):
    apis = [
        f"https://link4m.co/api-shorten/v2?api={LINK4M_API}&url={dest_url}",
        f"https://link2m.net/api-shorten/v2?api={LINK2M_API}&url={dest_url}",
    ]
    for url in apis:
        try:
            r = requests.get(url, timeout=10)
            d = r.json()
            if d.get('status') == 'success':
                short = d.get('shortenedUrl') or d.get('shorten') or d.get('short_url')
                if short:
                    return short
        except:
            continue
    return dest_url

# ─── XOR encode ──────────────────────────────────────────────────────────────

def xor_encode_name(name, key=0x42):
    b = name.encode('utf-8')
    encoded = ', '.join([f'0x{x ^ key:02x}' for x in b])
    return f'xorDecode((const unsigned char[]){{{encoded}}}, {len(b)}, 0x{key:02x})'

# ─── Child Bot System ─────────────────────────────────────────────────────────

def start_child_bot(token, owner_id, owner_name, months=1):
    """Khởi động bot con dưới dạng subprocess"""
    if token in child_procs:
        try:
            if child_procs[token].poll() is None:
                return False  # Đang chạy rồi
        except: pass

    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            f'child_{token[:8]}.log')
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), '--child-token', token],
        stdout=open(log_path, 'w'),
        stderr=subprocess.STDOUT
    )
    child_procs[token] = proc

    expiry = (datetime.now() + timedelta(days=30 * months)).strftime('%Y-%m-%d')
    data = load_data()
    existing = data.setdefault('child_bots', {}).get(token, {})
    data['child_bots'][token] = {
        'owner_id':   owner_id,
        'owner_name': owner_name,
        'expiry':     existing.get('expiry', expiry),  # giữ nguyên nếu đã có
    }
    save_data(data)
    expiry_notified.discard(token)
    return True

def extend_child_bot(token, months):
    """Gia hạn bot con thêm N tháng"""
    data = load_data()
    info = data.get('child_bots', {}).get(token)
    if not info:
        return None
    old_expiry = datetime.strptime(info.get('expiry', datetime.now().strftime('%Y-%m-%d')), '%Y-%m-%d')
    # Nếu đã hết hạn thì tính từ hôm nay
    base = max(old_expiry, datetime.now())
    new_expiry = base + timedelta(days=30 * months)
    info['expiry'] = new_expiry.strftime('%Y-%m-%d')
    save_data(data)
    expiry_notified.discard(token)
    return new_expiry.strftime('%d/%m/%Y')

def stop_child_bot(token, remove_data=True):
    """Dừng bot con"""
    if token in child_procs:
        try: child_procs[token].terminate()
        except: pass
        child_procs.pop(token, None)

    if remove_data:
        data = load_data()
        data.get('child_bots', {}).pop(token, None)
        save_data(data)
    return True

def auto_restart_child_bots():
    """Tự khởi động lại các bot con chưa hết hạn khi bot mẹ restart"""
    if IS_CHILD:
        return
    data  = load_data()
    today = datetime.now().date()
    for token, info in list(data.get('child_bots', {}).items()):
        expiry_str = info.get('expiry', '')
        if expiry_str:
            try:
                if datetime.strptime(expiry_str, '%Y-%m-%d').date() < today:
                    print(f"[AUTO] Bot con {token[:10]}... đã hết hạn, bỏ qua")
                    continue
            except: pass
        try:
            start_child_bot(token, info['owner_id'], info.get('owner_name', '?'))
            print(f"[AUTO] Đã restart bot con: {token[:10]}...")
        except Exception as e:
            print(f"[AUTO] Lỗi restart bot con {token[:10]}: {e}")

def renewal_keyboard(token):
    """Keyboard gia hạn bot con — dùng short_id tránh > 64 byte"""
    sid = store_token(token)
    kb  = InlineKeyboardMarkup(row_width=2)
    for opt in RENEWAL_OPTIONS:
        kb.add(InlineKeyboardButton(
            opt['label'],
            callback_data=f"renew_select:{sid}:{opt['months']}:{opt['price']}"
        ))
    kb.add(InlineKeyboardButton(
        "✏️ Tuỳ Chỉnh (Gửi Số Tháng/Năm)",
        callback_data=f"renew_custom:{sid}"
    ))
    return kb

def expiry_checker():
    """Kiểm tra hết hạn mỗi 30 phút — chỉ chạy ở bot mẹ"""
    if IS_CHILD:
        return
    while True:
        try:
            data  = load_data()
            today = datetime.now().date()
            for token, info in list(data.get('child_bots', {}).items()):
                expiry_str = info.get('expiry', '')
                if not expiry_str:
                    continue
                try:
                    exp_date = datetime.strptime(expiry_str, '%Y-%m-%d').date()
                except:
                    continue

                owner_id = info.get('owner_id')
                days_left = (exp_date - today).days

                # Cảnh báo trước 3 ngày (chỉ báo 1 lần / ngày)
                warn_key = f"{token}_warn_{today}"
                if 0 < days_left <= 3 and warn_key not in expiry_notified:
                    expiry_notified.add(warn_key)
                    try:
                        bot.send_message(owner_id,
                            f"⚠️ BOT CON SẮP HẾT HẠN!\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"📅 Còn {days_left} ngày nữa ({expiry_str})\n"
                            f"Gia hạn ngay để không bị gián đoạn!",
                            reply_markup=renewal_keyboard(token)
                        )
                    except: pass

                # Hết hạn → dừng bot
                if days_left < 0 and token not in expiry_notified:
                    expiry_notified.add(token)
                    stop_child_bot(token, remove_data=False)
                    try:
                        bot.send_message(owner_id,
                            f"🚫 BOT CON ĐÃ HẾT HẠN & BỊ TẮT!\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"📅 Hết hạn: {expiry_str}\n"
                            f"Bot của bạn đã dừng hoạt động.\n"
                            f"Gia hạn ngay để khởi động lại!",
                            reply_markup=renewal_keyboard(token)
                        )
                    except: pass
        except Exception as e:
            print(f"[EXPIRY] Lỗi: {e}")
        time.sleep(1800)  # 30 phút

# ─── Helper keyboards ─────────────────────────────────────────────────────────

def buy_options_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    for opt in PURCHASE_OPTIONS:
        kb.add(InlineKeyboardButton(
            opt['label'],
            callback_data=f"buy:{opt['builds']}:{opt['price']}"
        ))
    return kb

def get_verify_keyboard(user_id, nonce):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(
        "✅ Tôi Đã Hoàn Thành Link",
        callback_data=f"get_verify:{user_id}:{nonce}"
    ))
    return kb

def bill_admin_keyboard(user_id, builds):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Duyệt",   callback_data=f"bill_ok:{user_id}:{builds}"),
        InlineKeyboardButton("❌ Từ Chối", callback_data=f"bill_no:{user_id}")
    )
    return kb

def child_bot_admin_keyboard(user_id, token):
    sid = store_token(token)   # short_id thay vì token đầy đủ
    kb  = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Duyệt Bot Con",  callback_data=f"child_ok:{user_id}:{sid}"),
        InlineKeyboardButton("❌ Từ Chối",        callback_data=f"child_no:{user_id}")
    )
    return kb

def renewal_admin_keyboard(user_id, token, months, price):
    sid = store_token(token)
    kb  = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Duyệt Gia Hạn",  callback_data=f"renew_ok:{user_id}:{sid}:{months}"),
        InlineKeyboardButton("❌ Từ Chối",         callback_data=f"renew_no:{user_id}")
    )
    return kb

def build_type_keyboard(user_id):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📦 DYLIB", callback_data=f"build_type:{user_id}:dylib"),
        InlineKeyboardButton("📱 TIPA",  callback_data=f"build_type:{user_id}:tipa"),
        InlineKeyboardButton("📲 IPA",   callback_data=f"build_type:{user_id}:ipa"),
    )
    return kb

# ─── Commands: User ──────────────────────────────────────────────────────────

@bot.message_handler(commands=['start'])
def send_welcome(message):
    parts = message.text.split(' ', 1)
    if len(parts) > 1 and parts[1].startswith('get_'):
        handle_get_deeplink(message, parts[1])
        return

    if is_maintenance() and not is_admin(message.from_user.id):
        bot.reply_to(message, "🔧 Bot Đang Bảo Trì, Chờ Xíu Nha!"); return
    bot.reply_to(message,
        "👋 Chào Bạn! Tôi Là 3006iOS Bot\n"
        "Chuyên Làm FPS Cho Game & App 🎮\n\n"
        f"📌 /fps TênFPS — Build DYLIB / TIPA / IPA\n"
        f"  ├ 📦 DYLIB  — Dùng Với TrollStore (Tweak)\n"
        f"  ├ 📱 TIPA   — App TrollStore\n"
        f"  └ 📲 IPA    — Cài Qua Esign / SideStore / AltStore\n\n"
        f"🎁 /get — Nhận thêm lượt (tối đa {MAX_GETS} lần/ngày)\n"
        f"🛒 /mualuot — Mua thêm lượt vĩnh viễn\n"
        f"🤖 /thuebot — Thuê Bot Con Riêng\n"
        f"📊 /limit — Xem lượt còn lại\n\n"
        f"🔄 Lượt Miễn Phí Reset Lúc 4:00 SA Mỗi Ngày"
    )

def handle_get_deeplink(message, param):
    try:
        _, uid_s, nonce = param.split('_', 2)
        uid_target = int(uid_s)
    except Exception:
        bot.reply_to(message, "❌ Link Không Hợp Lệ!"); return

    if message.from_user.id != uid_target:
        bot.reply_to(message, "❌ Link Này Không Phải Của Bạn!"); return

    pending = pending_links.get(uid_target)
    if not pending or pending['nonce'] != nonce:
        bot.reply_to(message, "❌ Link Đã Hết Hạn Hoặc Đã Dùng Rồi!"); return

    data  = load_data()
    uid_s = str(uid_target)
    gets_used = data.get('gets_used', {}).get(uid_s, 0)

    if gets_used >= MAX_GETS:
        bot.reply_to(message, f"❌ Đã Get {MAX_GETS} Lần Hôm Nay Rồi!\n🔄 Reset Lúc 4:00 SA")
        del pending_links[uid_target]
        return

    daily_used = data.get('daily_used', {}).get(uid_s, 0)
    if daily_used > 0:
        data.setdefault('daily_used', {})[uid_s] = daily_used - 1
    else:
        data.setdefault('purchased', {})[uid_s] = data.get('purchased', {}).get(uid_s, 0) + 1
    data.setdefault('gets_used', {})[uid_s] = gets_used + 1
    save_data(data)

    del pending_links[uid_target]
    remaining_gets = MAX_GETS - gets_used - 1
    bot.reply_to(message,
        f"🎉 Vượt Link Thành Công!\n"
        f"🎁 +1 Lượt Build Đã Được Cộng!\n"
        f"📊 Tổng Lượt Còn: {get_remaining(uid_target)}\n"
        f"🎁 Get Còn Lại Hôm Nay: {remaining_gets}/{MAX_GETS}"
    )

@bot.message_handler(commands=['fps'])
def handle_fps(message):
    if is_maintenance() and not is_admin(message.from_user.id):
        bot.reply_to(message, "🔧 Bot Đang Bảo Trì, Chờ Xíu Nha!"); return
    if not is_allowed(message):
        bot.reply_to(message, "❌ Nhóm Này Chưa Được Duyệt!\nLiên Hệ Admin."); return

    uid = message.from_user.id
    if not can_build(uid):
        bot.reply_to(message,
            "❌ Hết Lượt Rồi!\n"
            "🎁 /get — Nhận lượt miễn phí\n"
            "🛒 /mualuot — Mua thêm lượt\n"
            "🔄 Reset Lúc 4:00 SA"
        ); return

    args = message.text.split(' ', 1)
    if len(args) != 2 or not args[1].strip():
        bot.reply_to(message, "⚠️ Thiếu Tên! VD: /fps TênCủaBạn 🔥"); return

    fps_name = args[1].strip()
    pending_fps[uid] = fps_name
    bot.reply_to(message,
        f"🎮 Tên FPS: {fps_name}\n"
        f"📦 Chọn Định Dạng Build:",
        reply_markup=build_type_keyboard(uid)
    )

@bot.message_handler(commands=['get'])
def get_free_build(message):
    if is_maintenance() and not is_admin(message.from_user.id):
        bot.reply_to(message, "🔧 Bot Đang Bảo Trì, Chờ Xíu Nha!"); return
    if is_admin(message.from_user.id):
        bot.reply_to(message, "😄 Admin Dùng Vĩnh Viễn Rồi, Khỏi Get!"); return

    uid  = message.from_user.id
    data = load_data()
    uid_s = str(uid)
    gets_used = data.get('gets_used', {}).get(uid_s, 0)

    if gets_used >= MAX_GETS:
        bot.reply_to(message,
            f"❌ Đã Get {MAX_GETS}/{MAX_GETS} Lần Hôm Nay!\n"
            f"🔄 Reset Lúc 4:00 SA\n"
            f"🛒 /mualuot — Mua Thêm Lượt"
        ); return

    nonce     = uuid.uuid4().hex[:8]
    deep_link = f"https://t.me/{BOT_USERNAME}?start=get_{uid}_{nonce}"

    try:
        short_url = shorten_link(deep_link)
    except Exception:
        short_url = deep_link

    pending_links[uid] = {'nonce': nonce}
    remaining_gets = MAX_GETS - gets_used - 1

    bot.reply_to(message,
        f"🎁 Vượt Link Để Nhận 1 Lượt Build!\n\n"
        f"🔗 {short_url}\n\n"
        f"👆 Click Vào Link, Vượt Xong Telegram Tự Mở Bot Và Cộng Lượt!\n"
        f"🎁 Get Còn Lại Hôm Nay: {remaining_gets}/{MAX_GETS}"
    )

@bot.message_handler(commands=['mualuot'])
def buy_builds(message):
    if is_maintenance() and not is_admin(message.from_user.id):
        bot.reply_to(message, "🔧 Bot Đang Bảo Trì, Chờ Xíu Nha!"); return
    bot.reply_to(message,
        "🛒 CHỌN GÓI MUA LƯỢT\n"
        "━━━━━━━━━━━━━━━━\n"
        "Lượt Mua Vĩnh Viễn, Không Reset Mỗi Ngày!\n"
        "Chọn Gói Bên Dưới:",
        reply_markup=buy_options_keyboard()
    )

@bot.message_handler(commands=['limit'])
def check_limit(message):
    uid = message.from_user.id
    if is_admin(uid):
        bot.reply_to(message, "👑 Bạn Là Admin — Lượt Vĩnh Viễn ∞"); return
    s = get_user_stats(uid)
    daily_left = max(0, MAX_DAILY - s['daily_used'])
    gets_left  = max(0, MAX_GETS  - s['gets_used'])
    bot.reply_to(message,
        f"📊 LƯỢT CỦA BẠN\n"
        f"━━━━━━━━━━━━━\n"
        f"📅 Lượt Ngày: {daily_left}/{MAX_DAILY}\n"
        f"🎁 Get Còn: {gets_left}/{MAX_GETS}\n"
        f"🛒 Lượt Mua: {s['purchased']}\n"
        f"📦 Tổng Còn: {get_remaining(uid)}\n"
        f"━━━━━━━━━━━━━\n"
        f"🔄 Reset Lúc 4:00 SA"
    )

@bot.message_handler(commands=['thuebot'])
def rent_child_bot(message):
    """User đăng ký thuê bot con"""
    if IS_CHILD:
        bot.reply_to(message, "❌ Lệnh Này Chỉ Dùng Được Ở Bot Mẹ!"); return
    if is_maintenance() and not is_admin(message.from_user.id):
        bot.reply_to(message, "🔧 Bot Đang Bảo Trì!"); return

    args = message.text.split(' ', 1)
    if len(args) != 2 or not args[1].strip():
        bot.reply_to(message,
            "🤖 THUÊ BOT CON RIÊNG\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"💰 Giá: {CHILD_BOT_PRICE} / tháng\n"
            "✅ Bot con hoạt động y hệt bot mẹ\n"
            "✅ Token riêng của bạn, độc lập\n\n"
            "📌 Cách dùng:\n"
            "1. Tạo bot mới qua @BotFather\n"
            "2. Copy token bot của bạn\n"
            "3. Gửi: /thuebot <token>\n\n"
            "VD: /thuebot 123456:ABCdef..."
        ); return

    token_input = args[1].strip()
    uid   = message.from_user.id
    uname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name

    # Kiểm tra token hợp lệ
    try:
        test_bot = telebot.TeleBot(token_input)
        bot_info = test_bot.get_me()
    except Exception:
        bot.reply_to(message, "❌ Token Không Hợp Lệ!\nKiểm Tra Lại Token Từ @BotFather"); return

    # Kiểm tra token chưa được dùng
    data = load_data()
    if token_input in data.get('child_bots', {}):
        bot.reply_to(message, "❌ Token Này Đã Được Đăng Ký Rồi!"); return

    pending_child_rental[uid] = {
        'token':      token_input,
        'username':   uname,
        'bot_name':   f"@{bot_info.username}",
    }

    if os.path.exists(QR_PATH):
        with open(QR_PATH, 'rb') as qr:
            bot.send_photo(message.chat.id, qr,
                caption=(
                    f"🤖 ĐĂNG KÝ BOT CON\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"🆔 Bot: @{bot_info.username}\n"
                    f"👤 Chủ: {uname}\n"
                    f"💰 Giá: {CHILD_BOT_PRICE}\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"Chuyển Khoản Theo QR Rồi Gửi Bill Vào Đây!"
                )
            )
    else:
        bot.reply_to(message,
            f"🤖 ĐĂNG KÝ BOT CON\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🆔 Bot: @{bot_info.username}\n"
            f"💰 Giá: {CHILD_BOT_PRICE}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Liên Hệ Admin Để Lấy Số TK Chuyển Khoản\n"
            f"Sau Đó Gửi Bill Vào Đây!"
        )

# ─── Nhận bill từ user ───────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text and not m.text.startswith('/'))
def handle_text(message):
    """Xử lý text — nhận số tháng/năm tuỳ chỉnh gia hạn"""
    uid  = message.from_user.id
    info = pending_renewal.get(uid, {})

    if not info.get('waiting_custom'):
        return

    text = message.text.strip().lower()
    # Nhận dạng "X tháng" hoặc "X năm"
    m = re.match(r'^(\d+)\s*(tháng|thang|năm|nam)$', text, re.IGNORECASE)
    if not m:
        bot.reply_to(message, "❌ Không hiểu! Gửi kiểu: <b>2 tháng</b> hoặc <b>1 năm</b>", parse_mode='HTML')
        return

    amount = int(m.group(1))
    unit   = m.group(2).lower()
    months = amount * 12 if unit in ('năm', 'nam') else amount

    if months <= 0 or months > 120:
        bot.reply_to(message, "❌ Số tháng phải từ 1 đến 120!"); return

    # Tính giá: 80k/tháng, năm = 450k
    if months >= 12:
        price_k = (months // 12) * 450 + (months % 12) * 38
    elif months >= 6:
        price_k = months * 47
    elif months >= 3:
        price_k = months * 77
    else:
        price_k = months * 80

    price_str = f"{price_k}.000đ"
    label     = f"{amount} {'năm' if unit in ('năm','nam') else 'tháng'}"

    info['months']         = months
    info['price']          = price_str
    info['waiting_custom'] = False
    pending_renewal[uid]   = info

    bot.reply_to(message,
        f"💳 GIA HẠN TUỲ CHỈNH\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📅 Thời Hạn: {label} ({months} tháng)\n"
        f"💰 Giá: {price_str}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Chuyển Khoản Rồi Gửi Bill Vào Đây!"
    )
    if os.path.exists(QR_PATH):
        with open(QR_PATH, 'rb') as qr:
            bot.send_photo(message.chat.id, qr, caption="📱 Quét QR Để Chuyển Khoản")

@bot.message_handler(content_types=['photo', 'document'])
def handle_bill_photo(message):
    uid = message.from_user.id

    # Bill gia hạn bot con
    if uid in pending_renewal and not pending_renewal[uid].get('waiting_custom') \
            and pending_renewal[uid].get('months') is not None:
        info   = pending_renewal[uid]
        token  = info['token']
        months = info['months']
        price  = info['price']
        uname  = info['username']
        label  = f"{months} tháng" if months < 12 else f"{months // 12} năm ({months} tháng)"

        for aid in ADMIN_IDS:
            try:
                caption = (
                    f"🔄 BILL GIA HẠN BOT CON\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"👤 User: {uname}\n"
                    f"🆔 ID: {uid}\n"
                    f"📅 Gia hạn: {label}\n"
                    f"💰 Giá: {price}\n"
                    f"🔑 Token: {token[:20]}...\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"Duyệt Để Gia Hạn Bot Con"
                )
                safe = token.replace(':', '_')
                if message.content_type == 'photo':
                    bot.send_photo(aid, message.photo[-1].file_id,
                                   caption=caption,
                                   reply_markup=renewal_admin_keyboard(uid, token, months, price))
                else:
                    bot.send_document(aid, message.document.file_id,
                                      caption=caption,
                                      reply_markup=renewal_admin_keyboard(uid, token, months, price))
            except Exception as e:
                print(f"Lỗi gửi admin {aid}: {e}")

        del pending_renewal[uid]
        bot.reply_to(message,
            "✅ Đã Gửi Bill Gia Hạn Cho Admin!\n"
            "⏳ Chờ Admin Duyệt, Bot Sẽ Được Gia Hạn Sau."
        )
        return

    # Bill thuê bot con
    if uid in pending_child_rental:
        info     = pending_child_rental[uid]
        token    = info['token']
        uname    = info['username']
        bot_name = info['bot_name']

        for aid in ADMIN_IDS:
            try:
                caption = (
                    f"🤖 BILL THUÊ BOT CON\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"👤 User: {uname}\n"
                    f"🆔 ID: {uid}\n"
                    f"🤖 Bot: {bot_name}\n"
                    f"🔑 Token: {token[:20]}...\n"
                    f"💰 Giá: {CHILD_BOT_PRICE}\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"Duyệt Để Khởi Động Bot Con"
                )
                safe_token = token.replace(':', '_')
                if message.content_type == 'photo':
                    bot.send_photo(aid, message.photo[-1].file_id,
                                   caption=caption,
                                   reply_markup=child_bot_admin_keyboard(uid, token))
                else:
                    bot.send_document(aid, message.document.file_id,
                                      caption=caption,
                                      reply_markup=child_bot_admin_keyboard(uid, token))
            except Exception as e:
                print(f"Lỗi gửi admin {aid}: {e}")

        del pending_child_rental[uid]
        bot.reply_to(message,
            "✅ Đã Gửi Bill Cho Admin!\n"
            "⏳ Chờ Admin Duyệt, Bot Con Sẽ Được Khởi Động Sau."
        )
        return

    # Bill mua lượt
    if uid not in pending_bills:
        return

    info     = pending_bills[uid]
    username = info['username']
    builds   = info['builds']
    price    = info['price']

    for aid in ADMIN_IDS:
        try:
            caption = (
                f"💳 BILL MUA LƯỢT\n"
                f"━━━━━━━━━━━━━━\n"
                f"👤 User: {username}\n"
                f"🆔 ID: {uid}\n"
                f"🎮 Gói: {builds} Lượt\n"
                f"💰 Giá: {price}\n"
                f"━━━━━━━━━━━━━━\n"
                f"Duyệt Để Cộng Lượt Cho User"
            )
            if message.content_type == 'photo':
                bot.send_photo(aid, message.photo[-1].file_id,
                               caption=caption,
                               reply_markup=bill_admin_keyboard(uid, builds))
            else:
                bot.send_document(aid, message.document.file_id,
                                  caption=caption,
                                  reply_markup=bill_admin_keyboard(uid, builds))
        except Exception as e:
            print(f"Lỗi gửi admin {aid}: {e}")

    del pending_bills[uid]
    bot.reply_to(message,
        "✅ Đã Gửi Bill Cho Admin!\n"
        "⏳ Chờ Admin Duyệt, Lượt Sẽ Được Cộng Sau."
    )

# ─── Callback queries ────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith('build_type:'))
def cb_build_type(call):
    parts    = call.data.split(':')
    uid      = int(parts[1])
    btype    = parts[2]

    if call.from_user.id != uid:
        bot.answer_callback_query(call.id, "❌ Không Phải Của Bạn!"); return

    fps_name = pending_fps.pop(uid, None)
    if not fps_name:
        bot.answer_callback_query(call.id, "❌ Hết Hạn, Gõ Lại /fps")
        try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except: pass
        return

    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except: pass

    # Tin nhắn lệnh gốc của user (message có nút reply_to, hoặc chính message đó)
    orig_msg = call.message.reply_to_message or call.message
    orig_mid = orig_msg.message_id

    label_map = {'dylib': 'DYLIB', 'tipa': 'TIPA', 'ipa': 'IPA'}
    label = label_map.get(btype, btype.upper())

    try:
        with BuildLock():
            backup_m_file()
            modify_m_file(fps_name)
            msg = bot.send_message(call.message.chat.id,
                f"⏳ Đang Build {label}, Ngồi Đợi Xíu...",
                reply_to_message_id=orig_mid
            )
            try:
                if btype == 'tipa':
                    run_make_tipa(msg)
                    use_build(uid)
                    send_tipa_file(orig_msg, msg, fps_name)
                elif btype == 'ipa':
                    run_make_ipa(msg)
                    use_build(uid)
                    send_ipa_file(orig_msg, msg, fps_name)
                else:
                    run_make_commands(msg)
                    use_build(uid)
                    send_dylib_file(orig_msg, msg, fps_name)
            finally:
                restore_m_file()
    except Exception as e:
        bot.send_message(call.message.chat.id, f"💥 Toang Rồi: {e}",
                         reply_to_message_id=orig_mid)

@bot.callback_query_handler(func=lambda c: c.data.startswith('child_ok:'))
def cb_child_approve(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Không Có Quyền!"); return

    parts = call.data.split(':')
    uid   = int(parts[1])
    token = get_token(parts[2])   # tra cứu token thật từ short_id
    if not token:
        bot.answer_callback_query(call.id, "❌ Phiên Hết Hạn, Restart Bot!"); return

    try:
        data      = load_data()
        owner     = data.get('child_bots', {}).get(token, {})
        uname     = pending_child_rental.get(uid, {}).get('username', '?')

        # Lấy tên chủ từ caption nếu không có
        caption_text = call.message.caption or ''
        for line in caption_text.split('\n'):
            if 'User:' in line:
                uname = line.split('User:')[1].strip()
                break

        ok = start_child_bot(token, uid, uname, months=1)  # 1 tháng mặc định
        if ok:
            bot.answer_callback_query(call.id, "✅ Đã Khởi Động Bot Con!")
        else:
            bot.answer_callback_query(call.id, "⚠️ Bot Con Có Thể Đang Chạy Rồi")

        try:
            old = call.message.caption or ''
            bot.edit_message_caption(
                f"{old}\n\n✅ ĐÃ DUYỆT BỞI {call.from_user.first_name}",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=None
            )
        except: pass

        exp_date = (datetime.now() + timedelta(days=30)).strftime('%d/%m/%Y')
        try:
            bot.send_message(uid,
                f"🎉 Admin Đã Duyệt!\n"
                f"🤖 Bot Con Của Bạn Đã Được Khởi Động!\n"
                f"📅 Hết Hạn: {exp_date} (1 tháng)\n"
                f"✅ Vào Bot Và Thử /start Để Kiểm Tra\n"
                f"💡 Bot Con Chạy Độc Lập, Dữ Liệu Riêng"
            )
        except: pass

    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ Lỗi: {e}")

@bot.callback_query_handler(func=lambda c: c.data.startswith('child_no:'))
def cb_child_deny(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Không Có Quyền!"); return

    uid = int(call.data.split(':')[1])
    bot.answer_callback_query(call.id, "❌ Đã Từ Chối!")

    try:
        old = call.message.caption or ''
        bot.edit_message_caption(
            f"{old}\n\n❌ ĐÃ TỪ CHỐI BỞI {call.from_user.first_name}",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=None
        )
    except: pass

    try:
        bot.send_message(uid,
            "❌ Admin Đã Từ Chối Yêu Cầu Thuê Bot Con\n"
            "Vui Lòng Liên Hệ Admin Để Biết Thêm Chi Tiết"
        )
    except: pass

@bot.callback_query_handler(func=lambda c: c.data.startswith('renew_select:'))
def cb_renew_select(call):
    """User chọn gói gia hạn sẵn có"""
    parts  = call.data.split(':')
    token  = get_token(parts[1])
    months = int(parts[2])
    price  = parts[3]
    if not token:
        bot.answer_callback_query(call.id, "❌ Phiên Hết Hạn, Restart Bot!"); return
    uid    = call.from_user.id
    uname  = f"@{call.from_user.username}" if call.from_user.username else call.from_user.first_name

    pending_renewal[uid] = {'token': token, 'months': months, 'price': price, 'username': uname}
    bot.answer_callback_query(call.id)

    label = f"{months} tháng" if months < 12 else f"{months // 12} năm"
    payment_text = (
        f"💳 GIA HẠN BOT CON\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📅 Thời Hạn: {label}\n"
        f"💰 Giá: {price}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Chuyển Khoản Rồi Gửi Bill Vào Đây!"
    )
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except: pass
    bot.send_message(call.message.chat.id, payment_text)
    if os.path.exists(QR_PATH):
        with open(QR_PATH, 'rb') as qr:
            bot.send_photo(call.message.chat.id, qr, caption="📱 Quét QR Để Chuyển Khoản")

@bot.callback_query_handler(func=lambda c: c.data.startswith('renew_custom:'))
def cb_renew_custom(call):
    """User chọn tuỳ chỉnh — hướng dẫn reply"""
    token = get_token(call.data.split(':')[1])
    uid   = call.from_user.id
    if not token:
        bot.answer_callback_query(call.id, "❌ Phiên Hết Hạn, Restart Bot!"); return
    pending_renewal[uid] = {'token': token, 'months': None, 'price': None, 'username':
        f"@{call.from_user.username}" if call.from_user.username else call.from_user.first_name,
        'waiting_custom': True}
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except: pass
    bot.send_message(call.message.chat.id,
        "✏️ GIA HẠN TUỲ CHỈNH\n"
        "━━━━━━━━━━━━━━━\n"
        "Gửi số tháng hoặc năm muốn gia hạn.\n\n"
        "VD:\n"
        "  • <b>2 tháng</b>\n"
        "  • <b>1 năm</b>\n"
        "  • <b>18 tháng</b>",
        parse_mode='HTML'
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith('renew_ok:'))
def cb_renew_approve(call):
    """Admin duyệt gia hạn"""
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Không Có Quyền!"); return

    parts  = call.data.split(':')
    uid    = int(parts[1])
    token  = get_token(parts[2])
    months = int(parts[3])
    if not token:
        bot.answer_callback_query(call.id, "❌ Phiên Hết Hạn, Restart Bot!"); return

    new_exp = extend_child_bot(token, months)

    # Nếu bot đã dừng (hết hạn trước đó) → khởi động lại
    data = load_data()
    info = data.get('child_bots', {}).get(token, {})
    proc = child_procs.get(token)
    if not proc or proc.poll() is not None:
        try:
            start_child_bot(token, info.get('owner_id', uid),
                            info.get('owner_name', '?'), months=0)
        except: pass

    label = f"{months} tháng" if months < 12 else f"{months // 12} năm"
    bot.answer_callback_query(call.id, f"✅ Đã Gia Hạn {label}!")

    try:
        old = call.message.caption or call.message.text or ''
        bot.edit_message_caption(
            f"{old}\n\n✅ ĐÃ DUYỆT GIA HẠN BỞI {call.from_user.first_name}",
            chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)
    except:
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except: pass

    try:
        bot.send_message(uid,
            f"🎉 Admin Đã Duyệt Gia Hạn!\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📅 Gia hạn: +{label}\n"
            f"✅ Hết hạn mới: {new_exp}\n"
            f"🤖 Bot Con Đã Được Khởi Động Lại!"
        )
    except: pass

@bot.callback_query_handler(func=lambda c: c.data.startswith('renew_no:'))
def cb_renew_deny(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Không Có Quyền!"); return

    uid = int(call.data.split(':')[1])
    bot.answer_callback_query(call.id, "❌ Đã Từ Chối Gia Hạn!")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except: pass
    try:
        bot.send_message(uid, "❌ Admin Đã Từ Chối Yêu Cầu Gia Hạn Bot Con\nVui Lòng Liên Hệ Admin!")
    except: pass

@bot.callback_query_handler(func=lambda c: c.data.startswith('get_verify:'))
def cb_get_verify(call):
    parts   = call.data.split(':')
    uid     = int(parts[1])
    nonce   = parts[2]

    if call.from_user.id != uid:
        bot.answer_callback_query(call.id, "❌ Không Phải Của Bạn!"); return

    pending = pending_links.get(uid)
    if not pending or pending['nonce'] != nonce:
        bot.answer_callback_query(call.id, "❌ Link Đã Hết Hạn!")
        try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except: pass
        return

    data  = load_data()
    uid_s = str(uid)
    gets_used = data.get('gets_used', {}).get(uid_s, 0)
    if gets_used >= MAX_GETS:
        bot.answer_callback_query(call.id, f"❌ Hết {MAX_GETS} Lần Get Hôm Nay!")
        return

    daily_used = data.get('daily_used', {}).get(uid_s, 0)
    if daily_used > 0:
        data.setdefault('daily_used', {})[uid_s] = daily_used - 1
    else:
        data.setdefault('purchased', {})[uid_s] = data.get('purchased', {}).get(uid_s, 0) + 1
    data.setdefault('gets_used', {})[uid_s] = gets_used + 1
    save_data(data)

    del pending_links[uid]
    remaining_gets = MAX_GETS - gets_used - 1
    bot.answer_callback_query(call.id, "🎉 Nhận Lượt Thành Công!")
    try:
        bot.edit_message_text(
            f"✅ Nhận Được 1 Lượt Build!\n"
            f"📊 Tổng Lượt Còn: {get_remaining(uid)}\n"
            f"🎁 Get Còn Lại Hôm Nay: {remaining_gets}/{MAX_GETS}",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )
    except: pass

@bot.callback_query_handler(func=lambda c: c.data.startswith('buy:'))
def cb_buy_select(call):
    parts  = call.data.split(':')
    builds = int(parts[1])
    price  = parts[2]
    uid    = call.from_user.id
    uname  = f"@{call.from_user.username}" if call.from_user.username else call.from_user.first_name

    pending_bills[uid] = {'builds': builds, 'price': price, 'username': uname}

    payment_text = (
        f"💳 THÔNG TIN THANH TOÁN\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🎮 Gói: {builds} Lượt\n"
        f"💰 Giá: {price}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Chuyển Khoản Theo QR Bên Dưới\n"
        f"Sau Đó Gửi Ảnh Bill Vào Đây\n"
        f"Admin Sẽ Duyệt Và Cộng Lượt Cho Bạn!"
    )

    try:
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            payment_text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )
    except: pass

    if os.path.exists(QR_PATH):
        with open(QR_PATH, 'rb') as qr:
            bot.send_photo(call.message.chat.id, qr,
                caption="📱 Quét QR Để Chuyển Khoản")
    else:
        bot.send_message(call.message.chat.id,
            "⚠️ File QR Chưa Được Setup\nLiên Hệ Admin Để Lấy Số TK")

@bot.callback_query_handler(func=lambda c: c.data.startswith('bill_ok:'))
def cb_bill_approve(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Không Có Quyền!"); return

    parts  = call.data.split(':')
    uid    = int(parts[1])
    builds = int(parts[2])

    add_purchased(uid, builds)
    bot.answer_callback_query(call.id, f"✅ Đã Duyệt +{builds} Lượt!")

    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        old = call.message.caption or call.message.text or ''
        bot.edit_message_caption(
            f"{old}\n\n✅ ĐÃ DUYỆT BỞI {call.from_user.first_name}",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )
    except: pass

    try:
        bot.send_message(uid,
            f"🎉 Admin Đã Duyệt Bill!\n"
            f"🎮 +{builds} Lượt Đã Được Cộng Vào Tài Khoản\n"
            f"📊 Tổng Lượt Còn: {get_remaining(uid)}\n"
            f"Cảm Ơn Bạn Đã Ủng Hộ! 💖"
        )
    except: pass

@bot.callback_query_handler(func=lambda c: c.data.startswith('bill_no:'))
def cb_bill_deny(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Không Có Quyền!"); return

    uid = int(call.data.split(':')[1])
    bot.answer_callback_query(call.id, "❌ Đã Từ Chối Bill!")

    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        old = call.message.caption or call.message.text or ''
        bot.edit_message_caption(
            f"{old}\n\n❌ ĐÃ TỪ CHỐI BỞI {call.from_user.first_name}",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )
    except: pass

    try:
        bot.send_message(uid,
            "❌ Admin Đã Từ Chối Bill Của Bạn\n"
            "Vui Lòng Kiểm Tra Lại Và Liên Hệ Admin"
        )
    except: pass

# ─── Commands: Admin ─────────────────────────────────────────────────────────

@bot.message_handler(commands=['panel'])
def admin_panel(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Chỉ Admin Mới Dùng Được!"); return
    data      = load_data()
    mt        = "🔧 BẬT" if data['maintenance'] else "✅ TẮT"
    grps      = '\n'.join([f"  • {g}" for g in data['approved_groups']]) or "  (chưa có)"
    pb        = len(pending_bills)
    child_cnt = len(data.get('child_bots', {}))
    bot.reply_to(message,
        f"━━━━ 🛠 ADMIN PANEL ━━━━\n"
        f"🔧 Bảo Trì: {mt}\n"
        f"👤 Users Hôm Nay: {len(data.get('daily_used', {}))}\n"
        f"💳 Bill Đang Chờ Duyệt: {pb}\n"
        f"🤖 Bot Con Đang Chạy: {child_cnt}\n"
        f"👥 Nhóm Duyệt:\n{grps}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"/approve <id>  — Duyệt nhóm\n"
        f"/remove <id>   — Xoá nhóm\n"
        f"/addlimit <uid> <n> — Cộng lượt thủ công\n"
        f"/resetuser <uid> — Reset lượt ngày\n"
        f"/listbot — Danh sách bot con\n"
        f"/stopbot <token> — Dừng bot con\n"
        f"/maintenance on/off\n"
        f"/panel"
    )

@bot.message_handler(commands=['listbot'])
def list_child_bots(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Chỉ Admin!"); return
    if IS_CHILD:
        bot.reply_to(message, "❌ Lệnh Này Chỉ Dùng Được Ở Bot Mẹ!"); return

    data  = load_data()
    bots  = data.get('child_bots', {})
    if not bots:
        bot.reply_to(message, "🤖 Chưa Có Bot Con Nào Đang Chạy!"); return

    today = datetime.now().date()
    lines = ["🤖 DANH SÁCH BOT CON\n━━━━━━━━━━━━━━━"]
    for i, (token, info) in enumerate(bots.items(), 1):
        proc     = child_procs.get(token)
        running  = proc and proc.poll() is None
        status   = "🟢 Đang chạy" if running else "🔴 Đã dừng"
        exp_str  = info.get('expiry', '?')
        try:
            days_left = (datetime.strptime(exp_str, '%Y-%m-%d').date() - today).days
            exp_label = f"{exp_str} ({days_left}d)" if days_left >= 0 else f"{exp_str} ⚠️HẾT HẠN"
        except:
            exp_label = exp_str
        lines.append(
            f"{i}. {info.get('owner_name', '?')}\n"
            f"   Token: {token[:15]}...\n"
            f"   {status} | 📅 {exp_label}"
        )
    bot.reply_to(message, '\n'.join(lines))

@bot.message_handler(commands=['stopbot'])
def stop_child_bot_cmd(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Chỉ Admin!"); return
    if IS_CHILD:
        bot.reply_to(message, "❌ Lệnh Này Chỉ Dùng Được Ở Bot Mẹ!"); return

    args = message.text.split(' ', 1)
    if len(args) != 2:
        bot.reply_to(message, "⚠️ Dùng: /stopbot <token>"); return

    token = args[1].strip()
    if stop_child_bot(token):
        bot.reply_to(message, f"✅ Đã Dừng Bot Con: {token[:15]}...")
    else:
        bot.reply_to(message, "❌ Không Tìm Thấy Bot Con Với Token Này!")

@bot.message_handler(commands=['addlimit'])
def add_limit_cmd(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Chỉ Admin!"); return
    args = message.text.split()
    if len(args) != 3:
        bot.reply_to(message, "⚠️ Dùng: /addlimit <user_id> <số lượt>"); return
    try:
        uid_target = int(args[1])
        builds     = int(args[2])
        add_purchased(uid_target, builds)
        bot.reply_to(message, f"✅ Đã Cộng {builds} Lượt Cho User {uid_target}")
        try: bot.send_message(uid_target,
            f"🎉 Admin Đã Cộng {builds} Lượt Cho Bạn!\n"
            f"📊 Tổng Còn: {get_remaining(uid_target)}")
        except: pass
    except ValueError:
        bot.reply_to(message, "❌ Giá Trị Không Hợp Lệ!")

@bot.message_handler(commands=['resetuser'])
def reset_user(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Chỉ Admin!"); return
    args = message.text.split()
    if len(args) != 2:
        bot.reply_to(message, "⚠️ Dùng: /resetuser <user_id>"); return
    try:
        uid_s = str(int(args[1]))
        data  = load_data()
        data.setdefault('daily_used', {})[uid_s] = 0
        data.setdefault('gets_used',  {})[uid_s] = 0
        save_data(data)
        bot.reply_to(message, f"✅ Đã Reset Lượt Ngày Cho User: {uid_s}")
    except ValueError:
        bot.reply_to(message, "❌ User ID Không Hợp Lệ!")

@bot.message_handler(commands=['approve'])
def approve_group(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Chỉ Admin!"); return
    args = message.text.split()
    if len(args) != 2:
        bot.reply_to(message, "⚠️ Dùng: /approve <group_id>"); return
    try:
        gid  = int(args[1])
        data = load_data()
        if gid not in data['approved_groups']:
            data['approved_groups'].append(gid)
            save_data(data)
            bot.reply_to(message, f"✅ Đã Duyệt Nhóm: {gid}")
        else:
            bot.reply_to(message, f"⚠️ Nhóm {gid} Đã Duyệt Rồi!")
    except ValueError:
        bot.reply_to(message, "❌ ID Không Hợp Lệ!")

@bot.message_handler(commands=['remove'])
def remove_group(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Chỉ Admin!"); return
    args = message.text.split()
    if len(args) != 2:
        bot.reply_to(message, "⚠️ Dùng: /remove <group_id>"); return
    try:
        gid  = int(args[1])
        data = load_data()
        if gid in data['approved_groups']:
            data['approved_groups'].remove(gid)
            save_data(data)
            bot.reply_to(message, f"✅ Đã Xoá Nhóm: {gid}")
        else:
            bot.reply_to(message, f"⚠️ Nhóm {gid} Không Có!")
    except ValueError:
        bot.reply_to(message, "❌ ID Không Hợp Lệ!")

@bot.message_handler(commands=['maintenance'])
def toggle_maintenance(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Chỉ Admin!"); return
    args = message.text.split()
    if len(args) != 2 or args[1] not in ['on', 'off']:
        bot.reply_to(message, "⚠️ Dùng: /maintenance on hoặc off"); return
    data = load_data()
    data['maintenance'] = (args[1] == 'on')
    save_data(data)
    bot.reply_to(message, f"Bảo Trì: {'🔧 BẬT' if data['maintenance'] else '✅ TẮT'}")

# ─── Core build ───────────────────────────────────────────────────────────────
# Bot mẹ & bot con dùng chung thư mục → dùng lock có hàng chờ (không báo lỗi ngay)

BUILD_LOCK_PATH = os.path.join(BASE_DIR, '.build.lock')
BUILD_WAIT_SEC  = 90   # chờ tối đa 90 giây

def _pid_alive(pid):
    """Kiểm tra PID có đang chạy không (cross-platform Termux-safe)"""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False

def _clear_stale_lock():
    """Xóa lock file nếu PID bên trong đã chết (stale lock)"""
    try:
        with open(BUILD_LOCK_PATH, 'r') as f:
            pid = int(f.read().strip())
        if not _pid_alive(pid):
            os.unlink(BUILD_LOCK_PATH)
    except Exception:
        # Không đọc được / không phải số → cũng xóa đi cho an toàn
        try: os.unlink(BUILD_LOCK_PATH)
        except: pass

class BuildLock:
    """Lock cross-process có hàng chờ + tự xóa stale lock"""
    def __enter__(self):
        waited = 0
        while True:
            try:
                self._fd = os.open(BUILD_LOCK_PATH,
                                   os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode())
                return self          # lấy được lock → tiến hành build
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise
                # Thử xóa stale lock trước khi đợi
                _clear_stale_lock()
                if waited >= BUILD_WAIT_SEC:
                    raise RuntimeError(
                        f"⏳ Hàng Chờ Build Quá Lâu ({BUILD_WAIT_SEC}s)\n"
                        "Vui Lòng Thử Lại Sau!"
                    )
                time.sleep(3)
                waited += 3

    def __exit__(self, *_):
        try:
            os.close(self._fd)
        except: pass
        try:
            os.unlink(BUILD_LOCK_PATH)
        except: pass

def backup_m_file():
    shutil.copy(M_FILE_PATH, BACKUP_FILE_PATH)

def restore_m_file():
    if os.path.exists(BACKUP_FILE_PATH):
        shutil.copy(BACKUP_FILE_PATH, M_FILE_PATH)

def modify_m_file(fps_name):
    with open(M_FILE_PATH, 'r', encoding='utf-8') as f:
        content = f.read()
    if 'xorDecode' not in content:
        raise ValueError("Không tìm thấy xorDecode trong FPSDisplay.m")
    new_content = re.sub(
        r'xorDecode\(\(const unsigned char\[\]\)\{[^}]+\}, \d+, 0x42\)',
        xor_encode_name(fps_name),
        content
    )
    if new_content == content:
        raise ValueError("Regex không khớp — kiểm tra lại FPSDisplay.m")
    with open(M_FILE_PATH, 'w', encoding='utf-8') as f:
        f.write(new_content)

def run_make_commands(msg):
    subprocess.run(['make', 'clean'], cwd=BASE_DIR,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    bot.edit_message_text("⏳ Đang Build 10%...",
                          chat_id=msg.chat.id, message_id=msg.message_id)
    result = subprocess.run(['make'], cwd=BASE_DIR, text=True, capture_output=True)
    bot.edit_message_text("⏳ Đang Build 90%...",
                          chat_id=msg.chat.id, message_id=msg.message_id)
    dylib = os.path.join(BASE_DIR, '.theos', 'obj', 'debug', 'FPSByThanhPrdiOSAndMrPewiOS.dylib')
    if not os.path.exists(dylib):
        raise RuntimeError(f"Build thất bại!\n{result.stderr[-500:] or 'Không có log'}")

def send_dylib_file(message, msg, fps_name):
    dylib = os.path.join(BASE_DIR, '.theos', 'obj', 'debug', 'FPSByThanhPrdiOSAndMrPewiOS.dylib')
    uid   = message.from_user.id
    uname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    rem   = '∞' if is_admin(uid) else str(get_remaining(uid))
    with open(dylib, 'rb') as f:
        bot.send_document(
            message.chat.id, f,
            caption=(
                f"✅ Build Xong Rồi!\n"
                f"👤 {uname}\n"
                f"🎮 FPS: {fps_name}\n"
                f"📊 Lượt Còn Lại: {rem}"
            ),
            reply_to_message_id=message.message_id
        )
    bot.edit_message_text("✅ Xong 100%! File Đã Gửi!",
                          chat_id=msg.chat.id, message_id=msg.message_id)

def build_app_icons(tmp_dir):
    if not os.path.exists(ICON_PATH):
        return []

    SIZES = {
        'AppIcon60@2x.png':        120,
        'AppIcon60@3x.png':        180,
        'AppIcon76@2x~ipad.png':   152,
        'AppIcon83.5@2x~ipad.png': 167,
        'AppIcon1024.png':        1024,
    }

    result = []

    try:
        from PIL import Image
        img = Image.open(ICON_PATH).convert('RGBA')
        for filename, px in SIZES.items():
            out = os.path.join(tmp_dir, filename)
            img.resize((px, px), Image.LANCZOS).save(out, 'PNG')
            result.append((filename, out))
        return result
    except Exception:
        pass

    try:
        r = subprocess.run(['convert', '--version'], capture_output=True, timeout=5)
        if r.returncode == 0:
            for filename, px in SIZES.items():
                out = os.path.join(tmp_dir, filename)
                subprocess.run(
                    ['convert', ICON_PATH, '-resize', f'{px}x{px}!',
                     '-define', 'png:color-type=6', out],
                    capture_output=True, timeout=30
                )
                if os.path.exists(out):
                    result.append((filename, out))
            if result:
                return result
    except Exception:
        pass

    try:
        out = os.path.join(tmp_dir, 'AppIcon1024.png')
        shutil.copy(ICON_PATH, out)
        return [('AppIcon1024.png', out)]
    except Exception:
        return []

def make_info_plist(app_name, has_icons=False):
    info = {
        'CFBundleExecutable':         app_name,
        'CFBundleIdentifier':         'com.3006ios.fpsbythanhprdiiosandmrpewios',
        'CFBundleName':               app_name,
        'CFBundleDisplayName':        'FPS By 3006iOS',
        'CFBundleVersion':            '1.0',
        'CFBundleShortVersionString': '1.0',
        'CFBundlePackageType':        'APPL',
        'MinimumOSVersion':           '14.0',
        'UIDeviceFamily':             [1, 2],
        'UIRequiredDeviceCapabilities': ['arm64'],
        'NSPrincipalClass':           'UIApplication',
        'UILaunchStoryboardName':     '',
    }
    if has_icons:
        info['CFBundleIcons'] = {
            'CFBundlePrimaryIcon': {
                'CFBundleIconFiles': ['AppIcon60@2x', 'AppIcon60@3x'],
                'CFBundleIconName':  'AppIcon',
            }
        }
        info['CFBundleIcons~ipad'] = {
            'CFBundlePrimaryIcon': {
                'CFBundleIconFiles': ['AppIcon76@2x~ipad', 'AppIcon83.5@2x~ipad'],
                'CFBundleIconName':  'AppIcon',
            }
        }
    return info

def _find_clang():
    """Tìm clang trên Termux hoặc Linux thông thường"""
    import shutil
    # Thử tìm trong PATH trước
    found = shutil.which('clang')
    if found:
        return found
    # Các đường dẫn thường gặp trên Termux
    for p in [
        '/data/data/com.termux/files/usr/bin/clang',
        '/data/data/com.termux/files/usr/bin/clang-18',
        '/data/data/com.termux/files/usr/bin/clang-17',
        '/usr/bin/clang',
    ]:
        if os.path.exists(p):
            return p
    raise FileNotFoundError(
        "Không tìm thấy clang!\n"
        "Cài bằng: pkg install clang"
    )

def compile_stub(stub_m, stub_bin, sdk):
    clang = _find_clang()
    base_flags = [
        clang,
        '-arch', 'arm64',
        '-isysroot', sdk,
        '-target', 'arm64-apple-ios14.0',
        '-mios-version-min=14.0',
        '-fobjc-arc',
        '-fuse-ld=lld',
        '-framework', 'UIKit',
        '-framework', 'Foundation',
        '-framework', 'QuartzCore',
        '-framework', 'AVFoundation',
        '-framework', 'CoreMedia',
        '-I', BASE_DIR,
        stub_m, M_FILE_PATH,
        '-o', stub_bin,
    ]
    r = subprocess.run(base_flags + ['-Xlinker', '-adhoc_codesign'],
                       capture_output=True, text=True)
    if r.returncode == 0 and os.path.exists(stub_bin):
        return
    r2 = subprocess.run(base_flags, capture_output=True, text=True)
    if r2.returncode != 0 or not os.path.exists(stub_bin):
        raise RuntimeError(f"Compile thất bại:\n{r2.stderr[-400:]}")

STUB_CODE = '''\
#import <UIKit/UIKit.h>
#import <AVFoundation/AVFoundation.h>

@interface VideoViewController : UIViewController
@property (strong, nonatomic) AVPlayer       *player;
@property (strong, nonatomic) AVPlayerLayer  *playerLayer;
@end

@implementation VideoViewController

- (void)viewDidLoad {
    [super viewDidLoad];
    self.view.backgroundColor = [UIColor blackColor];

    NSString *path = [[NSBundle mainBundle] pathForResource:@"ThanhPrdiOS" ofType:@"mp4"];
    if (!path) return;

    NSURL *url = [NSURL fileURLWithPath:path];
    self.player = [AVPlayer playerWithURL:url];
    self.player.actionAtItemEnd = AVPlayerActionAtItemEndNone;
    self.player.volume = 0;

    self.playerLayer = [AVPlayerLayer playerLayerWithPlayer:self.player];
    self.playerLayer.frame = self.view.bounds;
    self.playerLayer.videoGravity = AVLayerVideoGravityResizeAspectFill;
    [self.view.layer addSublayer:self.playerLayer];

    [[NSNotificationCenter defaultCenter]
        addObserver:self
           selector:@selector(videoDidEnd:)
               name:AVPlayerItemDidPlayToEndTimeNotification
             object:self.player.currentItem];

    [self.player play];
}

- (void)videoDidEnd:(NSNotification *)note {
    [self.player seekToTime:kCMTimeZero];
    [self.player play];
}

- (void)viewDidLayoutSubviews {
    [super viewDidLayoutSubviews];
    self.playerLayer.frame = self.view.bounds;
}

@end

@interface AppDelegate : UIResponder <UIApplicationDelegate>
@property (strong, nonatomic) UIWindow *window;
@end

@implementation AppDelegate
- (BOOL)application:(UIApplication *)app
    didFinishLaunchingWithOptions:(NSDictionary *)opts {
    self.window = [[UIWindow alloc] initWithFrame:[UIScreen mainScreen].bounds];
    self.window.rootViewController = [[VideoViewController alloc] init];
    [self.window makeKeyAndVisible];
    return YES;
}
@end

int main(int argc, char *argv[]) {
    return UIApplicationMain(argc, argv, nil,
        NSStringFromClass([AppDelegate class]));
}
'''

def _pack_bundle(out_path, app_name, stub_bin, video_src, icon_files, ext_label):
    import zipfile, plistlib
    has_icons = len(icon_files) > 0
    info      = make_info_plist(app_name, has_icons)
    bundle    = f'Payload/{app_name}.app'
    with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.write(stub_bin,  f'{bundle}/{app_name}')
        zf.writestr(f'{bundle}/Info.plist', plistlib.dumps(info))
        if os.path.exists(video_src):
            zf.write(video_src, f'{bundle}/ThanhPrdiOS.mp4')
        else:
            raise FileNotFoundError("Không tìm thấy ThanhPrdiOS.mp4!")
        for arc_name, disk_path in icon_files:
            zf.write(disk_path, f'{bundle}/{arc_name}')

def create_tipa_from_dylib(dylib_path):
    import tempfile
    app_name  = 'FPSByThanhPrdiOSAndMrPewiOS'
    pkg_dir   = os.path.join(BASE_DIR, 'packages')
    os.makedirs(pkg_dir, exist_ok=True)
    out_path  = os.path.join(pkg_dir, f'{app_name}.tipa')
    stub_m    = os.path.join(BASE_DIR, '_stub.m')
    stub_bin  = os.path.join(BASE_DIR, '_stub')
    sdk       = os.path.expanduser('~/theos/sdks/iPhoneOS16.5.sdk')
    video_src = os.path.join(BASE_DIR, 'ThanhPrdiOS.mp4')

    with open(stub_m, 'w') as f:
        f.write(STUB_CODE)

    compile_stub(stub_m, stub_bin, sdk)

    with tempfile.TemporaryDirectory() as tmp:
        icon_files = build_app_icons(tmp)
        _pack_bundle(out_path, app_name, stub_bin, video_src, icon_files, 'TIPA')

    for f in [stub_m, stub_bin]:
        try: os.remove(f)
        except: pass

    return out_path

def create_ipa_from_dylib(dylib_path):
    import tempfile
    app_name  = 'FPSByThanhPrdiOSAndMrPewiOS'
    pkg_dir   = os.path.join(BASE_DIR, 'packages')
    os.makedirs(pkg_dir, exist_ok=True)
    out_path  = os.path.join(pkg_dir, f'{app_name}.ipa')
    stub_m    = os.path.join(BASE_DIR, '_stub_ipa.m')
    stub_bin  = os.path.join(BASE_DIR, '_stub_ipa')
    sdk       = os.path.expanduser('~/theos/sdks/iPhoneOS16.5.sdk')
    video_src = os.path.join(BASE_DIR, 'ThanhPrdiOS.mp4')

    with open(stub_m, 'w') as f:
        f.write(STUB_CODE)

    compile_stub(stub_m, stub_bin, sdk)

    with tempfile.TemporaryDirectory() as tmp:
        icon_files = build_app_icons(tmp)
        _pack_bundle(out_path, app_name, stub_bin, video_src, icon_files, 'IPA')

    for f in [stub_m, stub_bin]:
        try: os.remove(f)
        except: pass

    return out_path

def run_make_tipa(msg):
    subprocess.run(['make', 'clean'], cwd=BASE_DIR,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    bot.edit_message_text("⏳ Đang Build 10%...",
                          chat_id=msg.chat.id, message_id=msg.message_id)
    result = subprocess.run(['make'], cwd=BASE_DIR, text=True, capture_output=True)
    dylib_path = os.path.join(BASE_DIR, '.theos', 'obj', 'debug', 'FPSByThanhPrdiOSAndMrPewiOS.dylib')
    if not os.path.exists(dylib_path):
        raise RuntimeError(f"Build thất bại!\n{result.stderr[-400:] or 'Không có log'}")
    bot.edit_message_text("⏳ Đang Đóng Gói TIPA 80%...",
                          chat_id=msg.chat.id, message_id=msg.message_id)
    create_tipa_from_dylib(dylib_path)

def send_tipa_file(message, msg, fps_name):
    pkg_dir    = os.path.join(BASE_DIR, 'packages')
    tipa_files = sorted(
        [f for f in os.listdir(pkg_dir) if f.endswith('.tipa')],
        key=lambda f: os.path.getmtime(os.path.join(pkg_dir, f)),
        reverse=True
    )
    if not tipa_files:
        raise FileNotFoundError("Không tìm thấy file TIPA sau khi build!")

    tipa_path = os.path.join(pkg_dir, tipa_files[0])
    uid   = message.from_user.id
    uname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    rem   = '∞' if is_admin(uid) else str(get_remaining(uid))

    with open(tipa_path, 'rb') as f:
        bot.send_document(
            message.chat.id, f,
            caption=(
                f"✅ Build TIPA Xong!\n"
                f"👤 {uname}\n"
                f"🎮 FPS: {fps_name}\n"
                f"📱 Cài Bằng TrollStore\n"
                f"📊 Lượt Còn Lại: {rem}"
            ),
            reply_to_message_id=message.message_id
        )
    bot.edit_message_text("✅ Xong 100%! TIPA Đã Gửi!",
                          chat_id=msg.chat.id, message_id=msg.message_id)

def run_make_ipa(msg):
    subprocess.run(['make', 'clean'], cwd=BASE_DIR,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    bot.edit_message_text("⏳ Đang Build IPA 10%...",
                          chat_id=msg.chat.id, message_id=msg.message_id)
    result = subprocess.run(['make'], cwd=BASE_DIR, text=True, capture_output=True)
    dylib_path = os.path.join(BASE_DIR, '.theos', 'obj', 'debug', 'FPSByThanhPrdiOSAndMrPewiOS.dylib')
    if not os.path.exists(dylib_path):
        raise RuntimeError(f"Build thất bại!\n{result.stderr[-400:] or 'Không có log'}")
    bot.edit_message_text("⏳ Đang Đóng Gói IPA 80%...",
                          chat_id=msg.chat.id, message_id=msg.message_id)
    create_ipa_from_dylib(dylib_path)

def send_ipa_file(message, msg, fps_name):
    pkg_dir   = os.path.join(BASE_DIR, 'packages')
    ipa_files = sorted(
        [f for f in os.listdir(pkg_dir) if f.endswith('.ipa')],
        key=lambda f: os.path.getmtime(os.path.join(pkg_dir, f)),
        reverse=True
    )
    if not ipa_files:
        raise FileNotFoundError("Không tìm thấy file IPA sau khi build!")

    ipa_path = os.path.join(pkg_dir, ipa_files[0])
    uid   = message.from_user.id
    uname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    rem   = '∞' if is_admin(uid) else str(get_remaining(uid))

    with open(ipa_path, 'rb') as f:
        bot.send_document(
            message.chat.id, f,
            caption=(
                f"✅ Build IPA Xong!\n"
                f"👤 {uname}\n"
                f"🎮 FPS: {fps_name}\n"
                f"📲 Cài Bằng Esign / SideStore / AltStore\n"
                f"📊 Lượt Còn Lại: {rem}"
            ),
            reply_to_message_id=message.message_id
        )
    bot.edit_message_text("✅ Xong 100%! IPA Đã Gửi!",
                          chat_id=msg.chat.id, message_id=msg.message_id)

# ─── Auto restart child bots + start polling ─────────────────────────────────

if not IS_CHILD:
    auto_restart_child_bots()
    threading.Thread(target=expiry_checker, daemon=True).start()

bot.polling(none_stop=True, interval=1)
