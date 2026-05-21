"""
plugo_bot.py - Bot HTTP Universal untuk semua Plugo Platform stores v5.2 TURBO + PROXY + COOKIES
Fitur:
  - Support semua web Plugo (chambredelavain, telepatiche, praedaestudio, dll)
  - Auto-detect vendor ID dan site base dari URL produk
  - Keyword search - cari produk berdasarkan keyword
  - BARU: Cookie login - pakai cookies.json untuk auth (opsional)
    * Otomatis load cookies jika file ada, skip jika tidak ada
    * Token expired tetap dipakai (gas terus)
    * Auto refresh token via refresh token jika AT expired
    * Browser cookies (GA, clarity, dll) diterapkan ke session
  - Support semua jenis produk: dengan/tanpa size/variant
  - Multi-akun paralel/sekuensial
  - Data diri dari file txt
  - Telegram notif
  - Jam war mode
  - Auto checkout via Plugo Cart API v2
  - Retry interval 0.1s
  - ReCAPTCHA Enterprise support via CAPSolver
"""

import requests
from requests.adapters import HTTPAdapter
import re
import os
import time
import json
import threading
import random
import sys
import signal
import hashlib
import base64
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# =============================================
# GLOBAL CANCEL FLAG
# =============================================
CANCEL_EVENT = threading.Event()

def _signal_handler(sig, frame):
    print("\n")
    print("\033[41m\033[97m\033[1m CANCEL PAKSA! \033[0m Bot dihentikan.")
    CANCEL_EVENT.set()
    def _force_exit():
        time.sleep(2)
        os._exit(1)
    t = threading.Thread(target=_force_exit, daemon=True)
    t.start()
    sys.exit(0)

# =============================================
# CONFIG
# =============================================
ACCOUNTS_FILE = "datadiri.txt"
COOKIES_FILE  = "cookies.json"

TELEGRAM_BOT_TOKEN = "8629439238:AAGGWPZ6SNYoT6_XYquSat8MZZYuuyylvJw"   # Isi token bot Telegram
TELEGRAM_CHAT_ID   = "1559406078"   # Isi chat ID Telegram

CAPTCHA_API_KEY    = "CAP-5B9E2B8E9D67173DB8A113BA7AA36DADAA4E1769B8EF83AD1FD67FE2510D33E8"   # CAPSolver API Key


# Plugo Platform Config (auto-detected from product URL)
VENDOR_ID       = None   # Will be auto-detected
SITE_BASE       = None   # Will be auto-detected
API_BASE        = "https://api.plugo.world/v1"
CART_API_BASE   = "https://cart.plugo.world"
FAAS_BASE       = "https://faas.plugo.world"
SF_VERSION      = "hotfix-release-2026-05-06-5d5a1e0"

# Proxy Config (rotating proxy - setiap koneksi baru = IP beda)
PROXY_URL = "http://hwjgptue-rotate:php0f28h7vtv@p.webshare.io:80"
USE_PROXY = False   # True = pakai proxy, False = direct

# Retry Config
RETRY_INTERVAL        = 0.1
WAITING_ROOM_MAX_WAIT = 0     # 0 = retry tanpa batas
WAITING_ROOM_POLL     = 0.1

# Product URLs (legacy, now user inputs URL directly)
PRODUCTS = {}

# Bank code → display name mapping (dari Plugo JS)
BANK_CODE_MAP = {
    "BMRI": "Mandiri", "MDRC": "Mandiri", "BRIN": "BRI", "BNIN": "BNI",
    "CENA": "BCA", "BCAC": "BCA", "BNIA": "CIMB Niaga", "BBBA": "Permata",
    "BBBB": "Permata Syariah", "BDIN": "Danamon", "BSYI": "Bank Syariah Indonesia",
    "BSI": "BSI", "BJB": "Bank BJB", "BJBB": "Bank BJB", "IBBK": "Maybank",
    "HNBN": "KEB Hana", "OVOE": "OVO", "ESHP": "ShopeePay", "AKLP": "Akulaku",
    "ALMA": "Alfamart", "INDO": "Indomaret", "QSHP": "QRIS", "KDVI": "Kredivo",
}

# =============================================
# ANSI COLORS
# =============================================
if sys.platform == "win32":
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 7)
    except Exception:
        pass

class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[91m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    BLUE    = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN    = "\033[96m"
    WHITE   = "\033[97m"
    BG_GREEN  = "\033[42m"
    BG_RED    = "\033[41m"
    BG_YELLOW = "\033[43m"
    BG_BLUE   = "\033[44m"

def clr_ok(msg):    return C.GREEN + str(msg) + C.RESET
def clr_err(msg):   return C.RED + str(msg) + C.RESET
def clr_warn(msg):  return C.YELLOW + str(msg) + C.RESET
def clr_info(msg):  return C.CYAN + str(msg) + C.RESET
def clr_bold(msg):  return C.BOLD + str(msg) + C.RESET
def clr_dim(msg):   return C.DIM + str(msg) + C.RESET

def step_tag(n, total):
    return C.BG_BLUE + C.WHITE + C.BOLD + " STEP " + str(n) + "/" + str(total) + " " + C.RESET

def ok_tag():   return C.BG_GREEN + C.WHITE + C.BOLD + " OK " + C.RESET
def fail_tag(): return C.BG_RED + C.WHITE + C.BOLD + " GAGAL " + C.RESET
def warn_tag(): return C.BG_YELLOW + C.WHITE + C.BOLD + " WARN " + C.RESET

print_lock = threading.Lock()

def log(msg, end="\n", flush=True):
    ts = datetime.now().strftime("%H:%M:%S")
    with print_lock:
        print(C.DIM + "[" + ts + "]" + C.RESET + " " + msg, end=end, flush=flush)


# =============================================
# HTTP SESSION
# =============================================
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

def random_ua():
    return random.choice(_UA_POOL)

def make_headers():
    ua = random_ua()
    chrome_ver = re.search(r"Chrome/(\d+)", ua)
    ver = chrome_ver.group(1) if chrome_ver else "124"
    return {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
        "sec-ch-ua": '"Chromium";v="' + ver + '", "Google Chrome";v="' + ver + '", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "x-sf-version": SF_VERSION,
    }

def make_fast_session(use_proxy=False):
    s = requests.Session()
    adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=0)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update(make_headers())
    s.headers["Connection"] = "keep-alive"
    if use_proxy and USE_PROXY and PROXY_URL:
        s.proxies = {
            "http": PROXY_URL,
            "https": PROXY_URL,
        }
    return s


# =============================================
# COOKIES
# =============================================

def load_cookies_from_file():
    """Load semua cookies dari cookies.json jika ada.
    Returns list of cookie dicts, atau [] jika file tidak ada/error.
    """
    if not os.path.exists(COOKIES_FILE):
        return []
    try:
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        if isinstance(cookies, list):
            return cookies
    except Exception as e:
        log("  " + clr_warn("Gagal baca " + COOKIES_FILE + ": " + str(e)[:80]))
    return []


def apply_cookies_to_session(sess, cookies, domain_filter=None):
    """Terapkan SEMUA cookies dari list ke requests session.
    Termasuk auth cookies (plugo:*:at/rt) — server butuh cookie + header sekaligus.
    """
    count = 0
    for c in cookies:
        name = c.get("name", "")
        value = c.get("value", "")
        domain = c.get("domain", "").lstrip(".")
        path = c.get("path", "/")
        if not name or not value:
            continue
        if domain_filter and domain_filter not in domain:
            continue
        sess.cookies.set(name, value, domain=domain, path=path)
        count += 1
    return count


def load_cookie_tokens():
    """Load auth tokens (at + rt) dan semua cookies dari cookies.json.
    Returns dict: {at, rt, at_expired, rt_expired, cookies[]}.
    Token tetap di-return meskipun expired (gas terus).
    """
    result = {"at": None, "rt": None, "at_expired": False, "rt_expired": False, "cookies": []}

    cookies = load_cookies_from_file()
    if not cookies:
        return result
    result["cookies"] = cookies

    if VENDOR_ID is None:
        return result

    at_key = "plugo:" + str(VENDOR_ID) + ":at"
    rt_key = "plugo:" + str(VENDOR_ID) + ":rt"
    now = time.time()

    for c in cookies:
        name = c.get("name", "")
        if name == at_key and c.get("value"):
            result["at"] = c["value"]
            exp = c.get("expirationDate", 0)
            if exp and exp < now:
                result["at_expired"] = True
        elif name == rt_key and c.get("value"):
            result["rt"] = c["value"]
            exp = c.get("expirationDate", 0)
            if exp and exp < now:
                result["rt_expired"] = True

    return result


def refresh_access_token(sess, refresh_token):
    """Refresh access token pakai refresh token via Plugo auth.
    Returns (new_access_token, buyer_id) atau (None, None).
    """
    url = FAAS_BASE + "/auth/vendors/" + str(VENDOR_ID) + "/refresh-token"
    try:
        r = _request_with_retry(sess, "POST", url, json={"refreshToken": refresh_token}, headers={
            "Content-Type": "application/json",
            "Origin": SITE_BASE,
            "Referer": SITE_BASE + "/",
            "x-sf-version": SF_VERSION,
        }, timeout=15)
        if r.status_code == 200:
            data = r.json()
            token = data.get("token") or data.get("accessToken") or data.get("jwt")
            if token:
                buyer_id = get_buyer_id_from_token(token)
                return token, buyer_id
    except Exception:
        pass
    return None, None


def get_buyer_id_from_token(token):
    """Extract buyerId dari JWT token payload. Falls back to virtualId for anonymous tokens."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = base64.urlsafe_b64decode(payload)
        data = json.loads(decoded)
        bid = data.get("buyerId") or data.get("virtualId") or ""
        return str(bid) if bid else None
    except Exception:
        return None


# =============================================
# WAITING ROOM & RETRY
# =============================================
def _is_waiting_room(response):
    if response.status_code in (429, 503, 520, 521, 522, 523, 524, 525, 530):
        return True
    ct = response.headers.get("Content-Type", "")
    if "text/html" in ct:
        body = response.text[:5000].lower()
        indicators = [
            "waiting room", "please wait", "cf-browser-verification",
            "challenge-platform", "just a moment", "checking your browser",
            "antrian", "antrean",
        ]
        return any(ind in body for ind in indicators)
    return False


def _request_with_retry(sess, method, url, max_wait=None, **kwargs):
    if max_wait is None:
        max_wait = WAITING_ROOM_MAX_WAIT
    if "timeout" not in kwargs:
        kwargs["timeout"] = 30
    start = time.time()
    attempt = 0
    while True:
        if CANCEL_EVENT.is_set():
            raise Exception("DIBATALKAN")
        attempt += 1
        try:
            r = sess.request(method, url, **kwargs)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ReadTimeout,
                Exception) as e:
            if "DIBATALKAN" in str(e):
                raise
            elapsed = time.time() - start
            if max_wait > 0 and elapsed > max_wait:
                raise
            if attempt == 1:
                log("  Koneksi gagal, retry...")
            elif attempt % 100 == 0:
                log("  Masih retry... (" + str(int(elapsed)) + "s, " + str(attempt) + "x)")
            time.sleep(WAITING_ROOM_POLL)
            continue

        if _is_waiting_room(r):
            elapsed = time.time() - start
            if max_wait > 0 and elapsed > max_wait:
                raise Exception("Waiting room timeout setelah " + str(int(elapsed)) + "s")
            if attempt == 1:
                log("  Waiting room terdeteksi, retry terus...")
            elif attempt % 100 == 0:
                log("  Masih di waiting room... (" + str(int(elapsed)) + "s, " + str(attempt) + "x)")
            time.sleep(WAITING_ROOM_POLL)
            continue

        return r


# =============================================
# AUTO-DETECT SITE CONFIG
# =============================================

def extract_site_base(product_url):
    """Extract SITE_BASE (e.g. https://telepatiche.com) from a product URL."""
    m = re.match(r'(https?://[^/]+)', product_url)
    if m:
        return m.group(1)
    return None


def auto_detect_vendor_id(site_base):
    """Fetch the site homepage and extract the Plugo vendor ID.
    Uses multiple detection methods for maximum compatibility:
      1. vendors/XXXX or vendor/XXXX pattern in HTML (asset URLs)
      2. shop/XXXX pattern in HTML (API calls, manifest)
      3. favicon/XXXX pattern in HTML
      4. plugo-storefront-XXXX pattern in HTML
      5. Fallback: resolve domain via Plugo slug API
    """
    from collections import Counter

    s = make_fast_session(use_proxy=True)

    # --- Method 1-4: Parse HTML page for known ID patterns ---
    try:
        r = s.get(site_base + "/", timeout=15)
        if r.status_code == 200:
            html = r.text

            # Method 1: vendor/XXXX or vendors/XXXX (asset/image URLs)
            matches = re.findall(r'vendors?/(\d+)', html)
            if matches:
                counter = Counter(matches)
                vid = int(counter.most_common(1)[0][0])
                log("  " + clr_ok("Toko ditemukan (ID: " + str(vid) + ")"))
                return vid

            # Method 2: shop/XXXX (API calls, manifest)
            matches = re.findall(r'shop/(\d+)', html)
            if matches:
                counter = Counter(matches)
                vid = int(counter.most_common(1)[0][0])
                log("  " + clr_ok("Toko ditemukan (ID: " + str(vid) + ")"))
                return vid

            # Method 3: favicon/XXXX
            matches = re.findall(r'favicon/(\d+)', html)
            if matches:
                counter = Counter(matches)
                vid = int(counter.most_common(1)[0][0])
                log("  " + clr_ok("Toko ditemukan (ID: " + str(vid) + ")"))
                return vid

            # Method 4: plugo-storefront-XXXX
            matches = re.findall(r'plugo-storefront-(\d+)', html)
            if matches:
                vid = int(matches[0])
                log("  " + clr_ok("Toko ditemukan (ID: " + str(vid) + ")"))
                return vid

    except Exception as e:
        pass  # silently try fallback

    # --- Method 5: Fallback via Plugo slug API ---
    # Extract potential slug from domain name (e.g. etniraindonesia.com -> etniraindonesia)
    try:
        from urllib.parse import urlparse
        domain = urlparse(site_base).hostname or ""
        # Remove www. prefix and TLD to get potential slug
        domain_clean = domain.lstrip("www.")
        # Try the domain without TLD as slug (e.g. "etniraindonesia.com" -> "etniraindonesia")
        slug_candidates = []
        parts = domain_clean.split(".")
        if parts:
            slug_candidates.append(parts[0])  # e.g. "etniraindonesia"
            if len(parts) > 1:
                slug_candidates.append(".".join(parts[:-1]))  # e.g. "etnira.indonesia" for subdomains
            # Also try without hyphens/underscores
            slug_no_sep = parts[0].replace("-", "").replace("_", "")
            if slug_no_sep != parts[0]:
                slug_candidates.append(slug_no_sep)

        for slug in slug_candidates:
            try:
                api_url = API_BASE + "/shop/" + slug + "/"
                r2 = s.get(api_url, timeout=10)
                if r2.status_code == 200:
                    data = r2.json().get("data", {})
                    vid = data.get("id")
                    toko_domain = data.get("tokogoldDomain", {})
                    if isinstance(toko_domain, dict):
                        api_domain = toko_domain.get("domain", "")
                        # Verify the domain matches
                        if api_domain and api_domain in domain:
                            log("  " + clr_ok("Toko ditemukan (ID: " + str(vid) + ")"))
                            return int(vid)
                    # Even if domain doesn't match exactly, if slug matched, use it
                    if vid:
                        log("  " + clr_ok("Toko ditemukan (ID: " + str(vid) + ")"))
                        return int(vid)
            except Exception:
                continue

    except Exception as e:
        pass  # silently try next fallback

    # --- Method 6: Brute-force via manifest API with origin ---
    try:
        # Use the manifest API with the site's origin to find the shop ID
        # Try a range around known IDs (this is slow, only as last resort)
        pass  # trying manifest fallback
        manifest_url = API_BASE + "/shop/{}/manifest?origin=" + site_base
        # Quick scan of some common ranges
        for vid in list(range(1, 100)) + list(range(100, 500, 5)) + list(range(500, 5000, 10)) + list(range(5000, 15000, 50)):
            try:
                r3 = s.get(manifest_url.format(vid), timeout=5)
                if r3.status_code == 200:
                    mdata = r3.json()
                    start_url = mdata.get("start_url", "")
                    if start_url and site_base in start_url:
                        log("  " + clr_ok("Toko ditemukan (ID: " + str(vid) + ")"))
                        return vid
            except Exception:
                continue
    except Exception as e:
        log("  " + clr_warn("Manifest fallback gagal: " + str(e)[:80]))

    return None


def setup_site_config(product_url):
    """Auto-detect and set VENDOR_ID + SITE_BASE from the product URL.
    Updates the global variables so all API functions work correctly.
    """
    global VENDOR_ID, SITE_BASE

    site_base = extract_site_base(product_url)
    if not site_base:
        raise Exception("URL produk tidak valid: " + product_url)

    SITE_BASE = site_base

    vendor_id = auto_detect_vendor_id(site_base)
    if not vendor_id:
        raise Exception("Gagal detect vendor ID dari " + site_base + ". Pastikan site Plugo valid.")

    VENDOR_ID = vendor_id
    return vendor_id, site_base


# =============================================
# TIMER & WAR MODE
# =============================================
class Timer:
    def __init__(self):
        self.start = time.perf_counter()
    def elapsed(self):
        return time.perf_counter() - self.start
    def fmt(self):
        return "{:.2f}s".format(self.elapsed())

def parse_target_time(time_str):
    now = datetime.now()
    parts = time_str.split(":")
    if len(parts) < 2:
        raise ValueError("Format: HH:mm atau HH:mm:ss")
    h, m = int(parts[0]), int(parts[1])
    s = int(parts[2]) if len(parts) > 2 else 0
    target = now.replace(hour=h, minute=m, second=s, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target

def wait_until(target_dt, label=""):
    while datetime.now() < target_dt:
        if CANCEL_EVENT.is_set():
            return
        diff = (target_dt - datetime.now()).total_seconds()
        if diff > 60:
            log("  " + label + " Menunggu... " + str(int(diff)) + "s lagi")
            time.sleep(min(30, diff - 30))
        elif diff > 5:
            log("  " + label + " " + str(int(diff)) + "s...", end="\r")
            time.sleep(1)
        else:
            log("  " + label + " " + "{:.1f}s...".format(diff), end="\r")
            time.sleep(0.05)
    log("  " + label + " MULAI!")


# =============================================
# TELEGRAM
# =============================================
def send_telegram_notification(result):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        name        = result.get("name", "-")
        email       = result.get("email", "-")
        product     = result.get("product_name", "-")
        size        = result.get("size", "-")
        qty         = result.get("qty", 1)
        total       = result.get("total", "-")
        elapsed     = result.get("elapsed", 0)
        checkout_id = result.get("checkout_id", "-")
        order_url   = result.get("order_url", "-")

        msg = (
            "============================\n"
            "CHAMBREDELAVAIN BOT - ORDER BERHASIL!\n"
            "============================\n"
            "Nama       : " + name + "\n"
            "Email      : " + email + "\n"
            "Produk     : " + product + "\n"
            "Size       : " + str(size) + "\n"
            "Qty        : " + str(qty) + "\n"
            "Total      : " + str(total) + "\n"
            "Checkout ID: " + str(checkout_id) + "\n"
            "Waktu      : " + "{:.2f}s".format(elapsed) + "\n"
            "============================\n"
            "Link: " + str(order_url)
        )

        requests.post(
            "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10,
        )
    except Exception as e:
        log("  Telegram gagal: " + str(e))


# =============================================
# ACCOUNT LOADER
# =============================================
def load_accounts(filepath):
    """
    Format datadiri.txt (pipe-separated):
    nama|email|phone|subdistrik_search|alamat_detail|kecamatan_note|kode_pos_note|size|qty|fallback

    Kolom 4 (subdistrik_search): kata kunci untuk cari di dropdown
    Kolom 5 (alamat_detail): alamat lengkap
    Kolom 6 (kecamatan_note): isi note "District (Kecamatan)"
    Kolom 7 (kode_pos_note): isi note "Postal Code (Kode Pos)"
    Kolom 8 (size): size/color variant, bisa list prioritas dipisah koma
      - "M"         -> cari M
      - "M,S"       -> cari M dulu, kalau habis S
      - "BLACK,PINK" -> cari BLACK dulu, kalau habis PINK
      - "M,S,RANDOM" -> M, lalu S, lalu random dari yg ready
      - kosong      -> ambil yg ready
    Kolom 9 (qty): jumlah beli (default: 1)
    Kolom 10 (fallback): apa yg dilakukan kalau semua preferensi habis
      - "random" (default) -> ambil random dari yg ready
      - "wait"   -> tunggu retry 0.1s sampai muncul

    Contoh:
      Ahmad|ahmad@gmail.com|08111222333|Tanah Abang Jakarta Pusat|Jl. Merdeka No.10|Tanah Abang|10310|M,S,RANDOM|2|random
      Siti|siti@gmail.com|08222333444|Menteng Jakarta Pusat|Jl. Sudirman No.5|Menteng|10310|BLACK|1|wait
    """
    accounts = []
    if not os.path.exists(filepath):
        print("File '" + filepath + "' tidak ditemukan!")
        return accounts

    with open(filepath, "r", encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]

    for i, line in enumerate(lines):
        parts = [x.strip() for x in line.split("|")]

        if len(parts) < 3:
            print("  Baris " + str(i+1) + " dilewati (min 3 kolom): " + line)
            continue

        while len(parts) < 10:
            parts.append("")

        phone = parts[2].replace(" ", "").replace("-", "")
        if phone.startswith("0"):
            phone = "+62" + phone[1:]
        elif phone.startswith("62"):
            phone = "+" + phone
        elif phone.startswith("8"):
            phone = "+62" + phone
        elif not phone.startswith("+"):
            phone = "+62" + phone

        # Smart parse: jika parts[8] bukan angka tapi "random"/"wait", anggap fallback
        raw_qty = parts[8].strip().lower()
        raw_fallback = parts[9].strip().lower() if parts[9] else ""

        if raw_qty in ("random", "wait"):
            # User tulis |size|random atau |size|wait tanpa qty
            qty = 1
            fallback = raw_qty
        else:
            try:
                qty = int(parts[8]) if parts[8] else 1
            except ValueError:
                qty = 1
            fallback = raw_fallback if raw_fallback in ("random", "wait") else "wait"

        acc = {
            "name":              parts[0] if parts[0] else "User",
            "email":             parts[1],
            "phone":             phone,
            "subdistrict_search": parts[3] if parts[3] else "",
            "address":           parts[4] if parts[4] else "Jl. Contoh No. 1",
            "kecamatan_note":    parts[5] if parts[5] else "",
            "postal_code_note":  parts[6] if parts[6] else "",
            "size":              parts[7].upper().strip() if parts[7] else "",
            "qty":               qty,
            "fallback":          fallback,
        }

        if not acc["email"] and not acc["name"]:
            print("  Baris " + str(i+1) + " dilewati (nama/email wajib): " + line)
            continue

        accounts.append(acc)

    return accounts


def parse_account_selection(sel_input, total):
    if not sel_input or sel_input in ("semua", "all", "*"):
        return list(range(total))
    selected = []
    for part in sel_input.replace(" ", "").split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                a2, b2 = int(a), int(b)
                for x in range(a2, b2 + 1):
                    if 1 <= x <= total:
                        selected.append(x - 1)
            except Exception:
                pass
        else:
            try:
                x = int(part)
                if 1 <= x <= total:
                    selected.append(x - 1)
            except Exception:
                pass
    return selected if selected else list(range(total))


# =============================================
# PLUGO API - Product
# =============================================

def get_product_page(product_url, sess=None):
    """Fetch product page HTML and extract NUXT_DATA for variants."""
    s = sess or make_fast_session(use_proxy=True)

    if product_url.startswith("http"):
        url = product_url
    elif product_url.startswith("/"):
        url = SITE_BASE + product_url
    else:
        url = SITE_BASE + "/products/" + str(product_url)

    r = _request_with_retry(s, "GET", url, timeout=30)

    if r.status_code != 200:
        raise Exception("Gagal load halaman produk: HTTP " + str(r.status_code))

    html = r.text

    # Extract NUXT_DATA
    nuxt_match = re.search(r'id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not nuxt_match:
        raise Exception("NUXT_DATA tidak ditemukan di halaman")

    data = json.loads(nuxt_match.group(1))

    # Find product data with productVariations
    product_data = None
    for i, item in enumerate(data):
        if isinstance(item, dict) and "productVariations" in item and "name" in item:
            product_data = _resolve_nuxt(i, data)
            break

    if not product_data:
        raise Exception("Data produk tidak ditemukan")

    return product_data


def _resolve_nuxt(idx, data, depth=0):
    """Resolve NUXT_DATA references."""
    if depth > 15:
        return None
    if idx < 0 or idx >= len(data):
        return None
    item = data[idx]
    if isinstance(item, dict):
        result = {}
        for k, v in item.items():
            if isinstance(v, int) and 0 <= v < len(data):
                result[k] = _resolve_nuxt(v, data, depth + 1)
            else:
                result[k] = v
        return result
    elif isinstance(item, list):
        # Handle NUXT special markers
        if len(item) >= 2 and isinstance(item[0], str):
            tag = item[0]
            if tag in ("ShallowReactive", "Reactive", "Ref"):
                return _resolve_nuxt(item[1], data, depth + 1) if isinstance(item[1], int) else item[1]
            if tag == "EmptyRef":
                return None
        return [_resolve_nuxt(i, data, depth + 1) if isinstance(i, int) and 0 <= i < len(data) else i for i in item]
    return item


def parse_variations(product_data):
    """Parse product variations into a simple list.
    Combines all detail fields (e.g. color + size) into a single label.
    """
    variations = []
    for pv in product_data.get("productVariations", []):
        if pv is None:
            continue
        inv = pv.get("inventories", [{}])
        stock = inv[0].get("quantity", 0) if inv else 0
        details = pv.get("details", [])

        # Collect all detail values
        detail_parts = []
        variant_key = None
        for d in (details or []):
            if d and d.get("key") and d.get("value"):
                if not variant_key:
                    variant_key = d["key"].lower()
                detail_parts.append(d["value"])

        variant_label = " - ".join(detail_parts) if detail_parts else None

        variations.append({
            "id": pv.get("id"),
            "price": pv.get("price", 0),
            "stock": stock,
            "size": variant_label,
            "variant_key": variant_key,
            "product_id": pv.get("product", {}).get("id") if isinstance(pv.get("product"), dict) else None,
            "product_code": pv.get("product", {}).get("productCode") if isinstance(pv.get("product"), dict) else None,
        })
    return variations


# =============================================
# PLUGO API - Product Search by Keyword
# =============================================

def fetch_all_products(vendor_id, sess=None):
    """Fetch semua produk dari Plugo API untuk vendor tertentu."""
    s = sess or make_fast_session()
    url = API_BASE + "/shop/" + str(vendor_id) + "/products?limit=200"
    r = _request_with_retry(s, "GET", url, timeout=30)
    if r.status_code != 200:
        raise Exception("Gagal fetch products: HTTP " + str(r.status_code))
    data = r.json()
    return data.get("data", [])


def search_products_by_keyword(vendor_id, keyword, sess=None):
    """Cari produk berdasarkan keyword (case-insensitive match pada nama produk)."""
    products = fetch_all_products(vendor_id, sess)
    keyword_lower = keyword.lower().strip()
    keywords = [k.strip() for k in keyword_lower.split() if k.strip()]

    matched = []
    for p in products:
        name_lower = p.get("name", "").lower()
        if all(kw in name_lower for kw in keywords):
            matched.append(p)

    return matched


def build_product_url(site_base, product_id, product_name=""):
    """Bangun URL produk dari ID dan nama."""
    if product_name:
        slug = product_name.lower().strip()
        slug = re.sub(r'[^a-z0-9\s-]', '_', slug)
        slug = re.sub(r'\s+', '-', slug)
        slug = re.sub(r'-+', '-', slug)
        slug = slug.strip('-_')
        return site_base + "/products/" + str(product_id) + "/" + slug
    else:
        return site_base + "/products/" + str(product_id)


# =============================================
# PLUGO API - Auth & Cart
# =============================================

def get_anonymous_token(sess):
    """Get anonymous buyer token from Plugo."""
    url = FAAS_BASE + "/auth/vendors/" + str(VENDOR_ID) + "/anonymous-token"
    r = _request_with_retry(sess, "POST", url, json={"buyerIdToken": ""}, headers={
        "Content-Type": "application/json",
        "Origin": SITE_BASE,
        "Referer": SITE_BASE + "/",
        "x-sf-version": SF_VERSION,
    }, timeout=15)

    if r.status_code != 200:
        raise Exception("Gagal get anonymous token: HTTP " + str(r.status_code) + " " + r.text[:200])

    data = r.json()
    token = data.get("token") or data.get("accessToken") or data.get("jwt")
    buyer_id = data.get("buyerId") or data.get("buyer_id") or data.get("id")

    if not token:
        # Try to extract from response
        for key in data:
            if "token" in key.lower():
                token = data[key]
                break

    if not token:
        raise Exception("Token tidak ditemukan di response: " + json.dumps(data)[:300])

    return token, buyer_id


def create_real_buyer(sess, anon_token, virtual_id):
    """Create a real buyer record in Plugo DB.
    Anonymous tokens only have a virtualId (string) with no database row.
    The ;complete endpoint requires a real buyerId (numeric) that exists in the
    buyers table.  This function calls POST /shop/{vendorId}/buyers to create
    that record and returns (buyerIdToken, numeric_buyer_id).
    """
    url = API_BASE + "/shop/" + str(VENDOR_ID) + "/buyers"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": SITE_BASE,
        "Referer": SITE_BASE + "/",
        "Authorization": "Bearer " + anon_token,
        "x-jwt": "Bearer " + anon_token,
        "x-buyer-id": str(virtual_id),
        "x-sf-version": SF_VERSION,
    }
    r = _request_with_retry(sess, "POST", url, json={"name": "", "birthday": ""},
                            headers=headers, timeout=15)
    if r.status_code not in (200, 201):
        raise Exception("Gagal create buyer: HTTP " + str(r.status_code) + " " + r.text[:200])
    data = r.json()
    buyer_id_token = data.get("data", {}).get("buyerIdToken", "")
    if not buyer_id_token:
        raise Exception("buyerIdToken tidak ditemukan di response: " + json.dumps(data)[:300])
    buyer_id = get_buyer_id_from_token(buyer_id_token)
    return buyer_id_token, buyer_id


def _solve_hashcash(challenge_id, difficulty=3):
    """Solve hashcash proof-of-work challenge."""
    prefix = "0" * difficulty
    nonce = 0
    while True:
        h = hashlib.sha256((challenge_id + str(nonce)).encode()).hexdigest()
        if h.startswith(prefix):
            return nonce, h
        nonce += 1


# reCAPTCHA Enterprise Config
RECAPTCHA_SITE_KEY = "6LfjYfArAAAAAENhLKJJZ4ZXX6hwb7KBbg2B_NGw"
CAPSOLVER_API_BASE = "https://api.capsolver.com"


def _solve_recaptcha_via_capsolver(page_url, action="checkoutComplete"):
    """Solve reCAPTCHA Enterprise via CAPSolver API (fallback).
    Returns the reCAPTCHA token string, or None on failure.
    """
    if not CAPTCHA_API_KEY:
        log("    " + clr_err("CAPTCHA_API_KEY kosong! Tidak bisa solve via CAPSolver."))
        return None

    strategies = [
        {
            "name": "ReCaptchaV3Enterprise",
            "task": {
                "type": "ReCaptchaV3EnterpriseTaskProxyLess",
                "websiteURL": page_url,
                "websiteKey": RECAPTCHA_SITE_KEY,
                "pageAction": action,
                "minScore": 0.9,
            },
        },
        {
            "name": "ReCaptchaV2Enterprise (invisible)",
            "task": {
                "type": "ReCaptchaV2EnterpriseTaskProxyLess",
                "websiteURL": page_url,
                "websiteKey": RECAPTCHA_SITE_KEY,
                "isInvisible": True,
                "enterprisePayload": {"action": action},
            },
        },
    ]

    for strat in strategies:
        log("    " + clr_info("Solving reCAPTCHA via CAPSolver [" + strat["name"] + "]..."))

        create_body = {"clientKey": CAPTCHA_API_KEY, "task": strat["task"]}

        try:
            resp = requests.post(CAPSOLVER_API_BASE + "/createTask", json=create_body, timeout=30)
            data = resp.json()
        except Exception as e:
            log("    " + clr_err("CAPSolver createTask error: " + str(e)[:100]))
            continue

        if data.get("errorId") and data.get("errorId") != 0:
            err_desc = data.get("errorDescription", str(data)[:200])
            log("    " + clr_warn("CAPSolver [" + strat["name"] + "] error: " + err_desc))
            continue

        task_id = data.get("taskId")
        if not task_id:
            log("    " + clr_err("CAPSolver: no taskId returned: " + str(data)[:200]))
            continue

        log("    " + clr_info("CAPSolver task: " + str(task_id)))

        for attempt in range(40):
            time.sleep(1.5)
            try:
                result_resp = requests.post(
                    CAPSOLVER_API_BASE + "/getTaskResult",
                    json={"clientKey": CAPTCHA_API_KEY, "taskId": task_id},
                    timeout=15,
                )
                result = result_resp.json()
            except Exception as e:
                log("    " + clr_warn("CAPSolver poll error: " + str(e)[:80]))
                continue

            status = result.get("status", "")
            if status == "ready":
                token = result.get("solution", {}).get("gRecaptchaResponse", "")
                if token:
                    log("    " + clr_ok("reCAPTCHA solved! [" + strat["name"] + "] (token " + str(len(token)) + " chars)"))
                    return token
                else:
                    log("    " + clr_err("CAPSolver: solution has no gRecaptchaResponse"))
                    break
            elif status == "failed":
                log("    " + clr_warn("CAPSolver [" + strat["name"] + "] failed: " + result.get("errorDescription", "unknown")))
                break
            if attempt > 0 and attempt % 10 == 0:
                log("    " + clr_info("Masih solving reCAPTCHA... (" + str(int(attempt * 1.5)) + "s)"))

    log("    " + clr_err("Semua strategi CAPSolver gagal"))
    return None


def _solve_recaptcha_enterprise(page_url, action="checkoutComplete"):
    """Solve reCAPTCHA Enterprise via CAPSolver.
    Returns the reCAPTCHA token string, or None on failure.
    """
    return _solve_recaptcha_via_capsolver(page_url, action)


def _make_cart_headers(token, buyer_id=None):
    """Make headers for Cart/API calls (requires both x-jwt and Authorization)."""
    h = {
        "Content-Type": "application/json",
        "Origin": SITE_BASE,
        "Referer": SITE_BASE + "/",
        "Authorization": "Bearer " + token,
        "x-jwt": "Bearer " + token,
        "x-sf-version": SF_VERSION,
    }
    if buyer_id:
        h["x-buyer-id"] = str(buyer_id)
    return h


def _cart_request(sess, method, url, token, buyer_id=None, **kwargs):
    """Make a cart API request with hashcash + reCAPTCHA Enterprise challenge support."""
    if not buyer_id:
        buyer_id = get_buyer_id_from_token(token)
    headers = _make_cart_headers(token, buyer_id)
    if "headers" in kwargs:
        headers.update(kwargs.pop("headers"))

    r = _request_with_retry(sess, method, url, headers=headers, **kwargs)

    # Handle challenge (HTTP 428)
    if r.status_code == 428:
        challenge_id = r.headers.get("X-Challenge-ID", "")
        difficulty = int(r.headers.get("X-Challenge-Difficulty", "3"))

        if challenge_id and challenge_id.startswith("recaptcha:"):

            # Extract action from challenge_id (e.g., "recaptcha:checkoutComplete:xxx")
            parts = challenge_id.split(":")
            action = parts[1] if len(parts) > 1 else "checkoutComplete"

            # Build page URL from Referer or default
            page_url = headers.get("Referer", SITE_BASE + "/checkout")

            recaptcha_token = _solve_recaptcha_enterprise(page_url, action)
            if recaptcha_token:
                headers["X-Challenge-ID"] = challenge_id
                headers["X-Hash"] = recaptcha_token
                headers.pop("X-Nonce", None)
                r = _request_with_retry(sess, method, url, headers=headers, **kwargs)
            else:
                log("    " + clr_err("reCAPTCHA solve gagal, request akan fail"))
        elif challenge_id:
            # Standard hashcash challenge
            nonce, h = _solve_hashcash(challenge_id, difficulty)
            headers["X-Challenge-ID"] = challenge_id
            headers["X-Nonce"] = str(nonce)
            headers["X-Hash"] = h
            r = _request_with_retry(sess, method, url, headers=headers, **kwargs)

    return r


def get_existing_cart(sess, token):
    """Get existing cart (untuk logged-in user yang sudah punya cart)."""
    url = CART_API_BASE + "/v2/stores/" + str(VENDOR_ID) + "/carts"
    headers = _make_cart_headers(token)
    r = _request_with_retry(sess, "GET", url, headers=headers, timeout=15)
    if r.status_code == 200:
        data = r.json()
        cart = data.get("cart") or data
        if cart.get("id"):
            return cart.get("id"), cart
    return None, None


def clear_cart_items(sess, token, cart_id, cart_data):
    """Remove all line items from existing cart."""
    line_items = cart_data.get("lineItems", [])
    for li in line_items:
        li_id = li.get("id")
        if li_id:
            url = CART_API_BASE + "/v2/stores/" + str(VENDOR_ID) + "/carts/" + str(cart_id) + "/line-items/" + str(li_id)
            try:
                _cart_request(sess, "DELETE", url, token, timeout=10)
            except Exception:
                pass


def add_cart_item(sess, token, cart_id, product_id, variation_id, qty=1):
    """Add item to existing cart."""
    url = CART_API_BASE + "/v2/stores/" + str(VENDOR_ID) + "/carts/" + str(cart_id) + "/line-items"
    body = {
        "productId": product_id,
        "productVariationId": variation_id,
        "quantity": qty,
    }
    r = _cart_request(sess, "POST", url, token, json=body, timeout=30)
    if r.status_code in (200, 201):
        data = r.json()
        return data.get("cart") or data
    raise Exception("Gagal add item: HTTP " + str(r.status_code) + " " + r.text[:300])


def create_cart(sess, token, product_id, variation_id, qty=1):
    """Create a cart with line items. Handle 'cart already exists' for logged-in users."""
    url = CART_API_BASE + "/v2/stores/" + str(VENDOR_ID) + "/carts"

    body = {
        "storeId": VENDOR_ID,
        "lineItems": [
            {
                "productId": product_id,
                "productVariationId": variation_id,
                "quantity": qty,
            }
        ],
        "bundleItems": [],
    }

    r = _cart_request(sess, "POST", url, token, json=body, timeout=30)

    if r.status_code not in (200, 201):
        # Handle "cart already exists" for logged-in users
        if r.status_code == 500 and "cart already exists" in r.text.lower():
            existing_id, existing_cart = get_existing_cart(sess, token)
            if existing_id:
                clear_cart_items(sess, token, existing_id, existing_cart)
                updated_cart = add_cart_item(sess, token, existing_id, product_id, variation_id, qty)
                return existing_id, updated_cart
        raise Exception("Gagal create cart: HTTP " + str(r.status_code) + " " + r.text[:300])

    data = r.json()
    cart = data.get("cart") or data
    cart_id = cart.get("id")

    if not cart_id:
        raise Exception("Cart ID tidak ditemukan: " + json.dumps(data)[:300])

    return cart_id, cart


def start_checkout(sess, token, cart_id, cart_data, product_url=""):
    """Convert cart to checkout.
    Requires lineItemIds from cart response + eventId/eventPath params.
    """
    # Extract line item IDs from cart data
    line_items = cart_data.get("lineItems", [])
    line_item_ids = [li.get("id") for li in line_items if li.get("id")]

    if not line_item_ids:
        raise Exception("Tidak ada lineItemIds di cart response: " + json.dumps(cart_data)[:300])

    # Build event tracking params
    event_id = "checkout." + str(int(time.time() * 1000))
    event_path = ""
    if product_url:
        # Extract path from URL
        m = re.search(r'(\/products\/\d+\/[^\s?#]*)', product_url)
        if m:
            event_path = m.group(1)

    # Build URL with query params
    url = CART_API_BASE + "/v2/stores/" + str(VENDOR_ID) + "/carts/" + str(cart_id) + "/;checkout"
    params = {"eventId": event_id}
    if event_path:
        params["eventPath"] = event_path

    body = {
        "lineItemIds": line_item_ids,
        "lineBundleItemIds": [],
        "freeItems": [],
    }

    r = _cart_request(sess, "POST", url, token, json=body, timeout=30,
                      headers={"Referer": SITE_BASE + "/cart"}, params=params)

    if r.status_code not in (200, 201):
        raise Exception("Gagal start checkout: HTTP " + str(r.status_code) + " " + r.text[:300])

    data = r.json()
    checkout = data.get("checkout") or data
    checkout_id = checkout.get("id")

    if not checkout_id:
        raise Exception("Checkout ID tidak ditemukan: " + json.dumps(data)[:300])

    # Extract orderId jika ada di response
    order_id = checkout.get("orderId") or checkout.get("order_id") or data.get("orderId")

    return checkout_id, checkout, order_id


def search_subdistrict(sess, search_query):
    """Search sub-district via Plugo Places API.
    Returns list of place results matching the search query.
    """
    url = FAAS_BASE + "/places/ID/search"

    headers = {
        "Content-Type": "application/json",
        "Origin": SITE_BASE,
        "Referer": SITE_BASE + "/",
        "x-sf-version": SF_VERSION,
    }

    body = {
        "search": search_query,
        "limit": 20,
    }

    r = _request_with_retry(sess, "POST", url, json=body, headers=headers, timeout=15)

    if r.status_code != 200:
        raise Exception("Gagal search sub-district: HTTP " + str(r.status_code) + " " + r.text[:200])

    data = r.json()
    results = data if isinstance(data, list) else data.get("data", data.get("results", []))

    places = []
    for item in (results if isinstance(results, list) else []):
        postal_code = str(item.get("postalCodes", [""])[0]) if item.get("postalCodes") else ""
        postal_codes_all = ",".join(str(x) for x in item.get("postalCodes", []))

        # Transform to Plugo storefront format:
        # districtId → cityId, districtName → cityName (matching web frontend)
        # Keep IDs as integers (NOT strings!)
        place = {
            "id": item.get("id"),
            "countryCode": item.get("countryCode", "ID"),
            "cityId": item.get("districtId"),
            "provinceId": item.get("provinceId"),
            "subdistrictId": item.get("subdistrictId"),
            "subdistrictName": item.get("subdistrictName", ""),
            "subdistrictNameAlt": item.get("subdistrictNameAlt", ""),
            "provinceName": item.get("provinceName", ""),
            "provinceNameAlt": item.get("provinceNameAlt", ""),
            "cityName": item.get("districtName", ""),
            "cityNameAlt": item.get("districtNameAlt", ""),
            "postalCode": postal_code,
            "postalCodeOthers": postal_codes_all,
            "lat": 0,
            "lng": 0,
            "type": "",
        }
        display = place["subdistrictName"] + ", " + place["cityName"] + ", " + place["provinceName"]
        places.append({"place": place, "display": display})

    return places


def set_checkout_address(sess, token, checkout_id, acc, place_data=None):
    """Set delivery address on checkout — matching web frontend format."""
    url = CART_API_BASE + "/v2/stores/" + str(VENDOR_ID) + "/checkouts/" + str(checkout_id) + "/address"

    if not place_data:
        raise Exception("Sub-district (place) wajib diisi. Cek kolom 4 datadiri.txt (contoh: Tanah Abang Jakarta Pusat)")

    area_id = place_data.get("id")
    postal_code = place_data.get("postalCode", "")
    province = place_data.get("provinceName", "")
    body = {
        "address": {
            "name": acc["name"],
            "phone": acc["phone"],
            "email": acc["email"],
            "address": acc["address"],
            "areaId": area_id,
            "country": "id",
            "postalCode": postal_code,
            "province": province,
            "latLng": "",
        },
    }
    r = _cart_request(sess, "PUT", url, token, json=body, timeout=30,
                      headers={"Referer": SITE_BASE + "/checkout/" + str(checkout_id)})

    if r.status_code not in (200, 201):
        raise Exception("Gagal set address: HTTP " + str(r.status_code) + " " + r.text[:300])

    return r.json()


def get_vendor_note_format(sess, token):
    """Get vendor's custom note format from orderNote API."""
    url = API_BASE + "/shop/" + str(VENDOR_ID) + "/orderNote"
    headers = _make_cart_headers(token)
    headers["Referer"] = SITE_BASE + "/"
    try:
        r = _request_with_retry(sess, "GET", url, headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            fmt_raw = data.get("data", data)
            required = fmt_raw.get("required", False)
            fmt_str = fmt_raw.get("format", "[]")
            if isinstance(fmt_str, str):
                fmt = json.loads(fmt_str)
            else:
                fmt = fmt_str
            return {"format": fmt, "required": required}
    except Exception:
        pass
    return {"format": [{"title": "Note", "type": "text"}], "required": False}


def set_checkout_notes(sess, token, checkout_id, notes):
    """Set checkout custom notes using multipart/form-data (matching web frontend).
    notes should be a list of dicts with: type, title, order (int), text.
    """
    url = CART_API_BASE + "/v2/stores/" + str(VENDOR_ID) + "/checkouts/" + str(checkout_id) + "/notes;update-notes"

    # Build the notes payload matching web's Pw() function
    notes_payload = []
    for note in notes:
        entry = {
            "type": note.get("type", "text"),
            "title": note.get("title", ""),
            "order": note.get("order", 1),
        }
        if note.get("type") == "text":
            entry["text"] = note.get("text", "")
        notes_payload.append(entry)

    # Web sends as multipart/form-data with a 'data' field containing JSON
    form_data = {"data": json.dumps({"notes": notes_payload})}

    # Need to NOT set Content-Type header (let requests set multipart boundary)
    buyer_id = get_buyer_id_from_token(token)
    headers = _make_cart_headers(token, buyer_id)
    headers.pop("Content-Type", None)  # Remove JSON content type
    headers["Referer"] = SITE_BASE + "/checkout/" + str(checkout_id)

    r = _request_with_retry(sess, "POST", url, data=form_data, headers=headers, timeout=120)

    # Handle hashcash challenge (HTTP 428)
    if r.status_code == 428:
        challenge_id = r.headers.get("X-Challenge-ID", "")
        difficulty = int(r.headers.get("X-Challenge-Difficulty", "3"))
        if challenge_id:
            nonce, h = _solve_hashcash(challenge_id, difficulty)
            headers["X-Challenge-ID"] = challenge_id
            headers["X-Nonce"] = str(nonce)
            headers["X-Hash"] = h
            r = _request_with_retry(sess, "POST", url, data=form_data, headers=headers, timeout=120)

    if r.status_code in (200, 201):
        return r.json()
    return None


def get_shipping_rates(sess, token, checkout_id):
    """Get available shipping rates."""
    url = FAAS_BASE + "/shippings/vendor-shipment-methods/" + str(VENDOR_ID)

    headers = _make_cart_headers(token)
    headers["Referer"] = SITE_BASE + "/checkout/" + str(checkout_id)

    r = _request_with_retry(sess, "GET", url, headers=headers, timeout=15)
    if r.status_code == 200:
        return r.json()
    return []


def set_shipping_rate(sess, token, checkout_id, vendor_shipment_method_id, use_insurance=True):
    """Set shipping rate on checkout. Returns (response_json, error_msg)."""
    url = CART_API_BASE + "/v2/stores/" + str(VENDOR_ID) + "/checkouts/" + str(checkout_id) + "/shipping-rates"

    body = {
        "vendorShipmentMethodId": vendor_shipment_method_id,
        "useInsurance": use_insurance,
    }

    r = _cart_request(sess, "PUT", url, token, json=body, timeout=30,
                      headers={"Referer": SITE_BASE + "/checkout/" + str(checkout_id)})
    if r.status_code in (200, 201):
        return r.json()
    return None


# Vendor-specific known shipping method IDs (last resort fallback)
KNOWN_SHIPPING_METHODS_BY_VENDOR = {
    3899: [  # chambredelavain.com
        {"vendorShipmentMethodId": 106058, "name": "JNE", "service": "Reguler", "courier": "jne_kiriminaja"},
        {"vendorShipmentMethodId": 106056, "name": "JNE", "service": "YES", "courier": "jne_kiriminaja"},
        {"vendorShipmentMethodId": 152181, "name": "Lion Parcel", "service": "Reg Pack", "courier": "lion_kiriminaja"},
        {"vendorShipmentMethodId": 152180, "name": "Lion Parcel", "service": "Jago Pack", "courier": "lion_kiriminaja"},
        {"vendorShipmentMethodId": 152179, "name": "Lion Parcel", "service": "Boss Pack", "courier": "lion_kiriminaja"},
        {"vendorShipmentMethodId": 147969, "name": "AnterAja", "service": "Regular", "courier": "anteraja"},
        {"vendorShipmentMethodId": 147974, "name": "Paxel", "service": "SameDay", "courier": "paxel"},
    ],
}

def get_known_shipping_methods():
    """Get known shipping methods for the current vendor (last resort fallback)."""
    return KNOWN_SHIPPING_METHODS_BY_VENDOR.get(VENDOR_ID, [])


def get_checkout_price(sess, token, checkout_id):
    """Get checkout price info."""
    url = CART_API_BASE + "/v2/stores/" + str(VENDOR_ID) + "/checkouts/" + str(checkout_id) + "/price"

    headers = _make_cart_headers(token)
    headers["Referer"] = SITE_BASE + "/checkout/" + str(checkout_id)

    r = _request_with_retry(sess, "GET", url, headers=headers, timeout=15)
    if r.status_code == 200:
        data = r.json()
        return data.get("price", data)
    return None


def get_existing_checkout(sess, token):
    """Get existing checkout if any."""
    url = CART_API_BASE + "/v2/stores/" + str(VENDOR_ID) + "/checkouts"

    headers = _make_cart_headers(token)
    headers["Referer"] = SITE_BASE + "/checkout"

    r = sess.get(url, headers=headers, timeout=15)
    if r.status_code == 200:
        data = r.json()
        checkout = data.get("checkout")
        if checkout and checkout.get("id"):
            return checkout
    return None


def reload_checkout(sess, token, checkout_id, buyer_id=None):
    """Reload specific checkout by ID to get updated data (shippingRateOptions, etc)."""
    if not buyer_id:
        buyer_id = get_buyer_id_from_token(token)
    url = CART_API_BASE + "/v2/stores/" + str(VENDOR_ID) + "/checkouts/" + str(checkout_id)
    headers = _make_cart_headers(token, buyer_id)
    headers["Referer"] = SITE_BASE + "/checkout/" + str(checkout_id)
    r = _request_with_retry(sess, "GET", url, headers=headers, timeout=15)
    if r.status_code == 200:
        data = r.json()
        return data.get("checkout", data)
    return None


def load_checkout_page_data(sess, token, checkout_id):
    """Load checkout PAGE HTML (SSR) and parse NUXT_DATA to get full checkout data.
    The SSR server has internal access to full checkout including shippingRateOptions,
    while the API endpoint only returns minimal data.
    """
    url = SITE_BASE + "/checkout/" + str(checkout_id)

    # Use a fresh request (not the session) to avoid header conflicts.
    # Set accessToken as cookie so SSR can authenticate.
    try:
        r = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
            "Referer": SITE_BASE + "/",
        }, cookies={"accessToken": token}, timeout=30)
    except Exception:
        return None

    if r.status_code != 200:
        return None

    html = r.text
    nuxt_match = re.search(r'id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not nuxt_match:
        return None

    try:
        nuxt_data = json.loads(nuxt_match.group(1))
    except Exception:
        return None

    # Find the checkout object with shippingRateOptions in NUXT_DATA
    for i, item in enumerate(nuxt_data):
        if isinstance(item, dict) and "shippingRateOptions" in item:
            resolved = _resolve_nuxt(i, nuxt_data)
            if resolved and isinstance(resolved, dict):
                sro = resolved.get("shippingRateOptions")
                if isinstance(sro, list) and len(sro) > 0:
                    return resolved

    # Fallback: find object with deliveryAddress + lineItems
    for i, item in enumerate(nuxt_data):
        if isinstance(item, dict) and "deliveryAddress" in item and "lineItems" in item:
            resolved = _resolve_nuxt(i, nuxt_data)
            if resolved and isinstance(resolved, dict):
                return resolved

    return None


def get_checkout_shipping_rates(sess, token, checkout_id, buyer_id=None):
    """Get available shipping rates from cart API for a specific checkout."""
    if not buyer_id:
        buyer_id = get_buyer_id_from_token(token)
    url = CART_API_BASE + "/v2/stores/" + str(VENDOR_ID) + "/checkouts/" + str(checkout_id) + "/shipping-rates"
    headers = _make_cart_headers(token, buyer_id)
    headers["Referer"] = SITE_BASE + "/checkout/" + str(checkout_id)
    r = _request_with_retry(sess, "GET", url, headers=headers, timeout=15)
    if r.status_code == 200:
        return r.json()
    return None


def get_payment_options(sess, token):
    """Get available payment options dari Plugo API."""
    url = API_BASE + "/shop/" + str(VENDOR_ID) + "/payment-options"

    headers = _make_cart_headers(token)
    headers["Referer"] = SITE_BASE + "/"

    r = _request_with_retry(sess, "GET", url, headers=headers, timeout=15)
    if r.status_code == 200:
        data = r.json()
        return data.get("paymentOptions", []) if isinstance(data, dict) else data
    return []


def set_checkout_payment(sess, token, checkout_id, company, method, pg_type="XENDIT"):
    """Set payment method on checkout BEFORE calling ;complete.
    Web calls PUT /v2/stores/:vendorId/checkouts/:id/payment before completing.
    """
    url = CART_API_BASE + "/v2/stores/" + str(VENDOR_ID) + "/checkouts/" + str(checkout_id) + "/payment"
    body = {
        "company": company,
        "method": method,
        "pgType": pg_type,
    }
    referer = SITE_BASE + "/checkout/" + str(checkout_id)
    r = _cart_request(sess, "PUT", url, token, json=body, timeout=30,
                      headers={"Referer": referer})
    if r.status_code in (200, 201, 204):
        try:
            return r.json()
        except Exception:
            return {"ok": True}  # 204 No Content = success
    pass  # silent fallback
    return None


def set_checkout_downpayment(sess, token, checkout_id, skip_downpayment=True):
    """Set downpayment preference on checkout (MUST be called before ;complete).
    Web calls PUT /v2/stores/:vendorId/checkouts/:id/downpayment right before completing.
    """
    url = CART_API_BASE + "/v2/stores/" + str(VENDOR_ID) + "/checkouts/" + str(checkout_id) + "/downpayment"
    body = {"skipDownpayment": skip_downpayment}
    r = _cart_request(sess, "PUT", url, token, json=body, timeout=30,
                      headers={"Referer": SITE_BASE + "/checkout/" + str(checkout_id)})
    if r.status_code in (200, 201):
        return r.json()
    return None


def _generate_ga_client_id():
    """Generate a Google Analytics-style client ID (GA1.1.xxxxxxxxxx.xxxxxxxxxx)."""
    r1 = random.randint(1000000000, 9999999999)
    r2 = random.randint(1000000000, 9999999999)
    return "GA1.1." + str(r1) + "." + str(r2)


def _extract_order_id(data):
    """Extract order ID dari berbagai format response."""
    if not isinstance(data, dict):
        return None
    # Cek nested "order" object
    order = data.get("order", data)
    if isinstance(order, dict):
        for key in ("id", "orderId", "order_id"):
            val = order.get(key)
            if val:
                return val
    # Cek top-level
    for key in ("id", "orderId", "order_id"):
        val = data.get(key)
        if val:
            return val
    # Cek nested "data" object
    inner = data.get("data", {})
    if isinstance(inner, dict):
        for key in ("id", "orderId", "order_id"):
            val = inner.get(key)
            if val:
                return val
    return None


def complete_checkout(sess, token, checkout_id, payment_type="PG", payment_info=None):
    """Complete checkout → creates an order. Returns (order_id, order_data)."""
    url = CART_API_BASE + "/v2/stores/" + str(VENDOR_ID) + "/checkouts/" + str(checkout_id) + "/;complete"
    body = {"paymentType": payment_type}
    if payment_info:
        body["company"] = payment_info.get("company", "")
        body["method"] = payment_info.get("method", "")
        body["pgType"] = payment_info.get("pgType", "XENDIT")
    ga_client_id = _generate_ga_client_id()
    params = {
        "gaClientId": ga_client_id,
        "eventPath": "/checkout/" + str(checkout_id),
        "checkout_link": "false",
    }
    checkout_referer = SITE_BASE + "/checkout/" + str(checkout_id)

    buyer_id = get_buyer_id_from_token(token)

    # First attempt via _cart_request (handles hashcash + reCAPTCHA auto)
    r = _cart_request(sess, "POST", url, token, json=body, params=params, timeout=60,
                      headers={"Referer": checkout_referer})

    if r.status_code in (200, 201):
        try:
            data = r.json()
            oid = _extract_order_id(data)
            if oid:
                return oid, data
            pass  # retry
        except Exception:
            pass  # retry

    pass  # retry silently

    # --- Attempt 2: fresh request to get challenge + solve reCAPTCHA ---
    for attempt in range(2):
        if r.status_code not in (403, 428):
            break

        challenge_id = r.headers.get("X-Challenge-ID", "") or r.headers.get("x-challenge-id", "")

        # If 403 or no challenge_id, get a fresh 428
        if r.status_code == 403 or not challenge_id:
            fresh_headers = _make_cart_headers(token, buyer_id)
            fresh_headers["Referer"] = checkout_referer
            r = _request_with_retry(sess, "POST", url, headers=fresh_headers, json=body, params=params, timeout=30)
            pass  # silent
            if r.status_code in (200, 201):
                try:
                    data = r.json()
                    oid = _extract_order_id(data)
                    if oid:
                        return oid, data
                except Exception:
                    pass
            challenge_id = r.headers.get("X-Challenge-ID", "") or r.headers.get("x-challenge-id", "")
            if not challenge_id:
                pass  # no challenge, will retry
                break

        if challenge_id and challenge_id.startswith("recaptcha:"):
            parts = challenge_id.split(":")
            action = parts[1] if len(parts) > 1 else "checkoutComplete"
            page_url = checkout_referer

            recaptcha_token = _solve_recaptcha_enterprise(page_url, action)
            if not recaptcha_token:
                log("    " + clr_err("reCAPTCHA solve gagal"))
                break

            # Retry with reCAPTCHA token — matching web frontend headers
            retry_headers = {
                "Accept": "application/json",
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Origin": SITE_BASE,
                "Referer": checkout_referer,
                "x-buyer-id": str(buyer_id),
                "x-challenge-id": challenge_id,
                "x-hash": recaptcha_token,
                "x-jwt": "Bearer " + token,
                "x-sf-version": SF_VERSION,
            }
            r = sess.post(url, json=body, params=params, headers=retry_headers, timeout=60)
            pass  # captcha retry silently

            if r.status_code in (200, 201):
                try:
                    data = r.json()
                    oid = _extract_order_id(data)
                    if oid:
                        return oid, data
                except Exception:
                    pass

        elif challenge_id:
            # Hashcash challenge
            difficulty = int(r.headers.get("X-Challenge-Difficulty", "3"))
            nonce, h = _solve_hashcash(challenge_id, difficulty)
            retry_headers = _make_cart_headers(token, buyer_id)
            retry_headers["Referer"] = checkout_referer
            retry_headers["X-Challenge-ID"] = challenge_id
            retry_headers["X-Nonce"] = str(nonce)
            retry_headers["X-Hash"] = h
            r = _request_with_retry(sess, "POST", url, headers=retry_headers, json=body, params=params, timeout=30)
            if r.status_code in (200, 201):
                try:
                    data = r.json()
                    oid = _extract_order_id(data)
                    if oid:
                        return oid, data
                except Exception:
                    pass

    # --- Fallback: cek pending orders (order mungkin sudah terbuat tapi response error) ---
    log("    " + clr_info("Cek pending orders..."))
    try:
        pending = get_pending_orders(sess, token, buyer_id)
        if pending:
            # Ambil order terbaru
            latest = pending[0] if isinstance(pending, list) else pending
            if isinstance(latest, dict):
                oid = latest.get("id") or latest.get("orderId")
                if oid:
                    log("    " + clr_ok("Order ditemukan di pending: " + str(oid)))
                    return oid, {"order": latest}
    except Exception:
        pass

    return None, None


def load_order_page_data(sess, token, order_id):
    """Load order PAGE HTML and parse NUXT_DATA for order details (including payment)."""
    url = SITE_BASE + "/orders/" + str(order_id)
    try:
        r = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": SITE_BASE + "/",
        }, cookies={"accessToken": token}, timeout=30)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    html = r.text
    nuxt_match = re.search(r'id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not nuxt_match:
        return None
    try:
        nuxt_data = json.loads(nuxt_match.group(1))
    except Exception:
        return None
    for i, item in enumerate(nuxt_data):
        if isinstance(item, dict) and ("orderId" in item or "orderNumber" in item or "paymentStatus" in item):
            resolved = _resolve_nuxt(i, nuxt_data)
            if resolved and isinstance(resolved, dict):
                return resolved
    return None


def create_payment_on_order(sess, token, order_id, company, method, pg_type="XENDIT", buyer_id=None):
    """Set payment method pada order (PUT /shop/:vendorId/orders/:orderId/payments/:pgType)."""
    if not buyer_id:
        buyer_id = get_buyer_id_from_token(token)
    # pgType uppercase — web uses XENDIT not xendit
    url = API_BASE + "/shop/" + str(VENDOR_ID) + "/orders/" + str(order_id) + "/payments/" + pg_type.upper()

    body = {
        "company": company,
        "method": method,
    }

    headers = _make_cart_headers(token, buyer_id)
    headers["Referer"] = SITE_BASE + "/orders/" + str(order_id)

    r = _request_with_retry(sess, "PUT", url, headers=headers, json=body, timeout=30)
    if r.status_code in (200, 201):
        return r.json()
    raise Exception("Gagal set payment: HTTP " + str(r.status_code) + " " + r.text[:300])


def get_order_details(sess, token, order_id, buyer_id=None):
    """Get order details termasuk status, payment info, dll."""
    if not buyer_id:
        buyer_id = get_buyer_id_from_token(token)
    url = API_BASE + "/shop/" + str(VENDOR_ID) + "/orders/" + str(order_id)

    headers = _make_cart_headers(token, buyer_id)
    headers["Referer"] = SITE_BASE + "/orders/" + str(order_id)

    r = _request_with_retry(sess, "GET", url, headers=headers, timeout=15)
    if r.status_code == 200:
        return r.json()
    return None


def get_order_number(sess, token, order_id, buyer_id=None):
    """Get order number (readable format) dari order ID."""
    if not buyer_id:
        buyer_id = get_buyer_id_from_token(token)
    url = API_BASE + "/shop/" + str(VENDOR_ID) + "/orders/" + str(order_id) + "/orderNumber"

    headers = _make_cart_headers(token, buyer_id)
    headers["Referer"] = SITE_BASE + "/orders/" + str(order_id)

    r = _request_with_retry(sess, "GET", url, headers=headers, timeout=15)
    if r.status_code == 200:
        data = r.json()
        if isinstance(data, dict):
            return data.get("data", {}).get("orderNumber", "") or data.get("orderNumber", "")
    return ""


def get_order_payments(sess, token, order_id, buyer_id=None):
    """Get payment details (VA number, bank, dll) dari Xendit."""
    if not buyer_id:
        buyer_id = get_buyer_id_from_token(token)
    url = API_BASE + "/shop/" + str(VENDOR_ID) + "/orders/" + str(order_id) + "/payments/xendit"

    headers = _make_cart_headers(token, buyer_id)
    headers["Referer"] = SITE_BASE + "/orders/" + str(order_id)

    r = _request_with_retry(sess, "GET", url, headers=headers, timeout=15)
    if r.status_code == 200:
        data = r.json()
        if isinstance(data, dict):
            return data.get("data", data)
        return data
    return None


def get_pending_orders(sess, token, buyer_id=None):
    """Get pending/unpaid orders untuk cari order terbaru."""
    if not buyer_id:
        buyer_id = get_buyer_id_from_token(token)
    url = API_BASE + "/shop/" + str(VENDOR_ID) + "/pendingOrders"
    headers = _make_cart_headers(token, buyer_id)
    headers["Referer"] = SITE_BASE + "/orders"
    params = {"query": "status.in:new.receipt_rejected"}
    r = _request_with_retry(sess, "GET", url, headers=headers, params=params, timeout=15)
    if r.status_code == 200:
        data = r.json()
        if isinstance(data, dict):
            orders = data.get("data", data.get("orders", []))
            return orders if isinstance(orders, list) else [orders]
        if isinstance(data, list):
            return data
    return []


# =============================================
# VARIANT MATCHING
# =============================================

def match_variant(variations, size_keyword=None, fallback_mode="random"):
    """
    Match variant berdasarkan size/color keyword (prioritized list).

    size_keyword bisa berupa:
    - "M"           -> cari M
    - "M,S"         -> cari M dulu, kalau habis cari S
    - "M,S,RANDOM"  -> cari M, lalu S, lalu random dari yg ready
    - ""            -> ambil yg ready (first available)

    fallback_mode:
    - "random" -> kalau semua preferensi habis, ambil random dari yg ready
    - "wait"   -> kalau preferensi habis, return None (trigger retry 0.1s sampe muncul)
    """
    available = [v for v in variations if v["stock"] > 0]

    # Produk tanpa variant (necklace, bagpack)
    if all(v["size"] is None for v in variations):
        if available:
            return available[0], "Dipilih (tanpa variant)"
        return None, "TIKET_RETRY: Stok habis, menunggu..."

    # Produk dengan variant (size/color)
    if not size_keyword:
        if available:
            return available[0], "Ambil: " + str(available[0].get("size", "?"))
        return None, "TIKET_RETRY: Semua variant habis"

    # Parse prioritized list
    priorities = [s.strip().upper() for s in size_keyword.split(",") if s.strip()]

    for pref in priorities:
        if pref == "RANDOM":
            if available:
                pick = random.choice(available)
                return pick, "Random: " + str(pick.get("size", "?"))
            continue

        for v in available:
            if not v["size"]:
                continue
            v_upper = v["size"].upper()
            # Exact match (e.g. "BUTTER - S" == "BUTTER - S")
            if v_upper == pref:
                return v, "Match: " + v["size"]
            # Partial match: check if pref matches any part (e.g. "S" matches "BUTTER - S", or "BUTTER" matches "BUTTER - S")
            parts = [p.strip() for p in v_upper.split(" - ")]
            if pref in parts:
                return v, "Match: " + v["size"]

    # Semua preferensi tidak ada/habis
    if fallback_mode == "random" and available:
        pick = random.choice(available)
        return pick, "Ukuran dipilih: " + str(pick.get("size", "?"))

    if fallback_mode == "wait":
        return None, "TIKET_RETRY: " + size_keyword + " habis, menunggu..."

    if not available:
        return None, "TIKET_RETRY: Semua variant habis"

    return available[0], "Ukuran dipilih: " + str(available[0].get("size", "?"))


# =============================================
# CHECKOUT FLOW
# =============================================

def checkout(run_num, total_run, acc, product_url, payment_keyword=None, shipping_keyword=None, cookie_data=None):
    t = Timer()
    label = "[Run-" + str(run_num) + "]"
    sess = make_fast_session()
    total_steps = 7

    size_pref = acc.get("size", "")
    qty = acc.get("qty", 1)
    fallback = acc.get("fallback", "random")

    # Apply browser cookies ke session (tracking cookies: GA, clarity, dll)
    if cookie_data and cookie_data.get("cookies"):
        site_domain = None
        if SITE_BASE:
            site_domain = SITE_BASE.replace("https://", "").replace("http://", "").split("/")[0]
        n_cookies = apply_cookies_to_session(sess, cookie_data["cookies"], domain_filter=site_domain)
        if n_cookies > 0:
            log(label + "  " + clr_info(str(n_cookies) + " browser cookies diterapkan"))

    log("")
    log(label + " " + C.CYAN + "-"*52 + C.RESET)
    log(label + "  " + clr_bold("Akun") + "     : " + acc["name"] + "  " + clr_dim("(" + acc["email"] + ")"))
    log(label + "  " + clr_bold("Produk") + "   : " + product_url)
    if size_pref:
        log(label + "  " + clr_bold("Variant") + "  : " + size_pref + " x" + str(qty) + " (" + fallback + ")")
    else:
        log(label + "  " + clr_bold("Qty") + "      : " + str(qty))
    if USE_PROXY and PROXY_URL:
        log(label + "  " + clr_bold("Proxy") + "    : " + clr_ok("Active (rotating)"))
    if cookie_data and cookie_data.get("at"):
        cookie_status = "Cookie login"
        if cookie_data.get("at_expired"):
            cookie_status += " (expired, gas terus)"
        log(label + "  " + clr_bold("Auth") + "     : " + clr_info(cookie_status))
    log(label + " " + C.CYAN + "-"*52 + C.RESET)

    try:
        # STEP 1: Get product data + token PARALLEL (turbo)
        log(label + " " + step_tag(1, total_steps) + " Mengambil data produk...")

        product_data = None
        token = None
        buyer_id = None
        _sess2 = make_fast_session()

        sess_proxy = make_fast_session(use_proxy=True)

        # Apply cookies ke semua session
        if cookie_data and cookie_data.get("cookies"):
            site_domain = None
            if SITE_BASE:
                site_domain = SITE_BASE.replace("https://", "").replace("http://", "").split("/")[0]
            apply_cookies_to_session(_sess2, cookie_data["cookies"], domain_filter=site_domain)
            apply_cookies_to_session(sess_proxy, cookie_data["cookies"], domain_filter=site_domain)

        def _fetch_product():
            return get_product_page(product_url, sess_proxy)

        def _fetch_token():
            # Coba pakai cookie token dulu (prioritas)
            if cookie_data and cookie_data.get("at"):
                _tok = cookie_data["at"]
                _bid = get_buyer_id_from_token(_tok)

                # Kalau expired, coba refresh dulu pakai refresh token
                if cookie_data.get("at_expired") and cookie_data.get("rt"):
                    pass  # trying refresh
                    new_tok, new_bid = refresh_access_token(_sess2, cookie_data["rt"])
                    if new_tok:
                        pass  # refreshed ok
                        return new_tok, new_bid
                    else:
                        pass  # use expired token anyway

                # Pakai token apa adanya (expired atau tidak)
                if _bid:
                    log(label + "    " + clr_ok("Login via cookie"))
                    return _tok, _bid

            # Fallback: anonymous token
            pass  # using anonymous token
            _tok, _bid = get_anonymous_token(_sess2)
            if not _bid:
                _bid = get_buyer_id_from_token(_tok)
            try:
                _vtok, _rbid = create_real_buyer(_sess2, _tok, _bid)
                return _vtok, _rbid
            except Exception:
                return _tok, _bid

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_prod = pool.submit(_fetch_product)
            f_tok = pool.submit(_fetch_token)
            product_data = f_prod.result()
            token, buyer_id = f_tok.result()

        # Set accessToken cookie di semua session (server validasi cookie + header)
        if token and SITE_BASE:
            site_domain = SITE_BASE.replace("https://", "").replace("http://", "").split("/")[0]
            for s in (sess, _sess2, sess_proxy):
                s.cookies.set("accessToken", token, domain=site_domain, path="/")

        log(label + " " + ok_tag() + " " + clr_bold(product_data.get("name", "?")))

        # Parse & match variant
        variations = parse_variations(product_data)
        available = [v for v in variations if v["stock"] > 0]

        # Display variants: group by first detail (e.g. color) with per-size stock detail
        _has_multi_detail = any(" - " in (v["size"] or "") for v in variations)
        if _has_multi_detail and len(variations) > 10:
            from collections import OrderedDict
            groups = OrderedDict()
            for v in variations:
                parts = (v["size"] or "One Size").split(" - ")
                group_name = parts[0]
                if group_name not in groups:
                    groups[group_name] = {"total_stock": 0, "price": v["price"], "sizes": []}
                groups[group_name]["total_stock"] += v["stock"]
                if len(parts) > 1:
                    groups[group_name]["sizes"].append((parts[1], v["stock"]))
            for gname, ginfo in groups.items():
                price_str = "Rp{:,}".format(int(ginfo["price"])).replace(",", ".")
                color = C.GREEN if ginfo["total_stock"] > 0 else C.RED
                size_detail = ""
                if ginfo["sizes"]:
                    size_detail = "  " + " ".join(s + "(" + str(q) + ")" for s, q in ginfo["sizes"])
                log(label + "      > " + gname + "  " + color + price_str + C.RESET + size_detail)
        else:
            for v in variations:
                price_str = "Rp{:,}".format(int(v["price"])).replace(",", ".")
                size_str = v["size"] or "One Size"
                color = C.GREEN if v["stock"] > 0 else C.RED
                log(label + "      > " + size_str + "  " + color + price_str + C.RESET + "  (stok " + str(v["stock"]) + ")")

        # STEP 2: Match variant
        log(label + " " + step_tag(2, total_steps) + " Memilih ukuran...")

        matched, match_msg = match_variant(variations, size_pref if size_pref else None, fallback)

        if matched is None:
            raise Exception(match_msg)

        log(label + "    " + clr_ok(match_msg))

        variation_id = matched["id"]
        price = matched["price"]
        real_product_id = product_data.get("id")

        eff_qty = qty
        if eff_qty > product_data.get("maxQuantity", 1):
            eff_qty = product_data.get("maxQuantity", 1)
        if eff_qty > matched["stock"]:
            eff_qty = matched["stock"]

        subtotal = price * eff_qty
        total_str = "Rp{:,}".format(int(subtotal)).replace(",", ".")
        log(label + "    Subtotal: " + clr_bold(total_str))

        # STEP 3: Create cart (skip pre-clear for speed)
        log(label + " " + step_tag(3, total_steps) + " Menambah ke keranjang...")
        cart_id, cart_data = create_cart(sess, token, real_product_id, variation_id, eff_qty)
        log(label + " " + ok_tag() + " Cart dibuat")

        # STEP 4: Start checkout
        log(label + " " + step_tag(4, total_steps) + " Memulai checkout...")
        checkout_id, checkout_data, order_id = start_checkout(sess, token, cart_id, cart_data, product_url)
        log(label + " " + ok_tag() + " Checkout dimulai")

        # STEP 5: Search sub-district & set delivery address
        log(label + " " + step_tag(5, total_steps) + " Mengisi data diri...")

        place_data = None
        if acc.get("subdistrict_search"):
            try:
                places = search_subdistrict(sess, acc["subdistrict_search"])
                if places:
                    place_data = places[0]["place"]
                    log(label + "    Sub-district: " + clr_ok(places[0]["display"]))
                    if len(places) > 1:
                        log(label + "    (" + str(len(places)) + " hasil, dipilih #1)")
                else:
                    log(label + "    " + clr_warn("Sub-district '" + acc["subdistrict_search"] + "' tidak ditemukan"))
            except Exception as pe:
                log(label + "    " + clr_warn("Search sub-district gagal: " + str(pe)[:80]))

        addr_resp = set_checkout_address(sess, token, checkout_id, acc, place_data)
        log(label + " " + ok_tag() + " Alamat terisi")

        # Extract checkout data from address response (contains shippingRateOptions)
        checkout_after_addr = None
        if isinstance(addr_resp, dict):
            checkout_after_addr = addr_resp.get("checkout", addr_resp)

        # Set checkout notes (kecamatan, kode pos) — using vendor's note format
        if acc.get("kecamatan_note") or acc.get("postal_code_note"):
            try:
                # Get vendor's note format to know correct titles and order
                note_config = get_vendor_note_format(sess, token)
                note_fields = note_config.get("format", [])
                notes = []
                note_texts = [acc.get("kecamatan_note", ""), acc.get("postal_code_note", "")]

                if note_fields:
                    # Map user data to vendor's note fields by order
                    for idx, field in enumerate(note_fields):
                        text_val = note_texts[idx] if idx < len(note_texts) else ""
                        if text_val:
                            notes.append({
                                "type": field.get("type", "text"),
                                "title": field.get("title", "Note"),
                                "order": idx + 1,  # 1-based numeric order
                                "text": text_val,
                            })
                else:
                    # Fallback: use default format
                    if acc.get("kecamatan_note"):
                        notes.append({"type": "text", "title": "Note", "order": 1, "text": acc["kecamatan_note"]})

                if notes:
                    notes_resp = set_checkout_notes(sess, token, checkout_id, notes)
                    if notes_resp:
                        log(label + "    Notes terisi")
            except Exception as ne:
                log(label + "    " + clr_warn("Set notes gagal"))

        # STEP 6: Set shipping rate (otomatis pilih yg pertama / match preference)
        log(label + " " + step_tag(6, total_steps) + " Mengatur pengiriman...")
        shipping_set = False
        try:
            methods = []

            # 1) Ambil shippingRateOptions dari response set_checkout_address
            if checkout_after_addr:
                sro = checkout_after_addr.get("shippingRateOptions", [])
                if sro:
                    methods = sro


            # 2) Load checkout PAGE HTML (SSR) → parse NUXT_DATA → shippingRateOptions
            #    SSR server has internal access to full checkout data including shipping options
            if not methods:

                # Wait for shipping rates to be calculated after address was set
                for page_attempt in range(3):
                    if page_attempt > 0:
                        time.sleep(0.5)

                    page_data = load_checkout_page_data(sess, token, checkout_id)
                    if page_data and isinstance(page_data, dict):
                        sro = page_data.get("shippingRateOptions", [])
                        if isinstance(sro, list) and sro:
                            methods = sro
                            break

            # 3) Fallback: reload checkout via API
            if not methods:
                chk = reload_checkout(sess, token, checkout_id, buyer_id)
                if chk:
                    sro = chk.get("shippingRateOptions", [])
                    if sro:
                        methods = sro

            # 4) Fallback: GET /checkouts/{id}/shipping-rates
            if not methods:
                sr_data = get_checkout_shipping_rates(sess, token, checkout_id, buyer_id)
                if sr_data:
                    if isinstance(sr_data, list):
                        methods = sr_data
                    elif isinstance(sr_data, dict):
                        methods = sr_data.get("shippingRateOptions", sr_data.get("data", sr_data.get("methods", [])))
                        if not isinstance(methods, list):
                            methods = []

            # 5) Fallback: FAAS endpoint
            if not methods:
                shipping_data = get_shipping_rates(sess, token, checkout_id)
                if isinstance(shipping_data, list):
                    methods = shipping_data
                elif isinstance(shipping_data, dict):
                    methods = shipping_data.get("data", shipping_data.get("shippingMethods", shipping_data.get("methods", [])))
                    if not isinstance(methods, list):
                        methods = [shipping_data] if shipping_data.get("id") or shipping_data.get("vendorShipmentMethodId") else []

            if methods:
                # Log available shipping options
                for sm in methods:
                    if isinstance(sm, dict):
                        sn = sm.get("name", "?") + " " + sm.get("serviceDesc", sm.get("service", ""))
                        sp = sm.get("routes", [{}])[0].get("totalPrice", 0) if sm.get("routes") else 0
                        sp_str = "Rp{:,}".format(int(sp)).replace(",", ".") if sp else "?"
                        log(label + "      > " + sn.strip() + "  " + sp_str)

                # Match shipping preference (if provided)
                selected_method = None
                if shipping_keyword:
                    kw = shipping_keyword.upper()
                    for sm in methods:
                        if not isinstance(sm, dict):
                            continue
                        sm_name = str(sm.get("name", "")).upper()
                        sm_courier = str(sm.get("courier", "")).upper()
                        sm_service = str(sm.get("serviceDesc", sm.get("service", ""))).upper()
                        if kw in sm_name or kw in sm_courier or kw == sm_name:
                            selected_method = sm
                            break
                    if not selected_method:
                        pass  # silently fallback to first available

                if not selected_method:
                    selected_method = methods[0]

                method_id = selected_method.get("vendorShipmentMethodId") or selected_method.get("id")
                method_name = (selected_method.get("name", "") + " " + selected_method.get("serviceDesc", selected_method.get("service", ""))).strip() or "Auto"
                if method_id:
                    sr_result = set_shipping_rate(sess, token, checkout_id, method_id)
                    if sr_result:
                        shipping_set = True
                        log(label + " " + ok_tag() + " Shipping: " + clr_ok(str(method_name)))
                        # Web fetches price after setting shipping (refetch price cache)
                        try:
                            get_checkout_price(sess, token, checkout_id)
                        except Exception:
                            pass
                    else:
                        log(label + "    " + clr_warn("Pengiriman gagal di-set, pilih manual di web"))

            # 6) LAST RESORT: Directly try known vendorShipmentMethodIds
            #    These IDs are static per vendor — just try setting them one by one
            if not shipping_set and get_known_shipping_methods():

                # Build priority list based on shipping_keyword
                try_methods = list(get_known_shipping_methods())
                if shipping_keyword:
                    kw = shipping_keyword.upper()
                    preferred = [m for m in try_methods if kw in m["name"].upper() or kw in m["courier"].upper()]
                    others = [m for m in try_methods if m not in preferred]
                    try_methods = preferred + others

                for km in try_methods:
                    vid = km["vendorShipmentMethodId"]
                    km_name = km["name"] + " " + km["service"]
                    sr_result = set_shipping_rate(sess, token, checkout_id, vid)
                    if sr_result:
                        shipping_set = True
                        log(label + " " + ok_tag() + " Shipping: " + clr_ok(km_name))
                        break
                if not shipping_set:
                    log(label + "    " + clr_warn("Shipping belum di-set, pilih manual di web"))
        except Exception as se:
            log(label + "    " + clr_warn("Shipping belum di-set, pilih manual di web"))

        # Set payment method (embedded in step 7)
        log(label + "    Mengatur pembayaran...")
        matched_pm = None
        if payment_keyword:
            try:
                pm_options = get_payment_options(sess, token)
                kw = payment_keyword.upper()

                for pm in pm_options:
                    if not isinstance(pm, dict):
                        continue
                    pm_company = str(pm.get("company", "")).upper()
                    pm_method = str(pm.get("method", "")).upper()
                    # Map company code to display name for matching
                    display_name = BANK_CODE_MAP.get(pm.get("company", ""), "").upper()
                    if kw in pm_company or kw == display_name or kw in display_name:
                        raw_pg = pm.get("pgType", "") or ""
                        raw_type = pm.get("type", "PG") or "PG"
                        # pgType kosong? fallback ke type (mis. NICEPAY)
                        eff_pg = raw_pg if raw_pg else raw_type
                        matched_pm = {
                            "company": pm.get("company", ""),
                            "method": pm.get("method", ""),
                            "pgType": eff_pg,
                            "type": raw_type,
                            "name": BANK_CODE_MAP.get(pm.get("company", ""), pm.get("company", "")) + " (" + pm.get("method", "") + ")",
                        }
                        break

                if matched_pm:
                    log(label + " " + ok_tag() + " Pembayaran: " + clr_ok(matched_pm["name"]))
                else:
                    avail = []
                    for pm in pm_options:
                        if isinstance(pm, dict):
                            code = pm.get("company", "?")
                            name = BANK_CODE_MAP.get(code, code)
                            avail.append(name + "(" + code + ")")
                    log(label + "    " + clr_warn("'" + payment_keyword + "' tidak match. Tersedia: " + ", ".join(avail[:10])))
                    # Auto-pick first available payment
                    for pm in pm_options:
                        if isinstance(pm, dict) and pm.get("company"):
                            _rp = pm.get("pgType", "") or ""
                            _rt = pm.get("type", "PG") or "PG"
                            matched_pm = {
                                "company": pm.get("company", ""),
                                "method": pm.get("method", ""),
                                "pgType": _rp if _rp else _rt,
                                "type": _rt,
                                "name": BANK_CODE_MAP.get(pm.get("company", ""), pm.get("company", "")) + " (" + pm.get("method", "") + ")",
                            }
                            log(label + " " + ok_tag() + " Auto-pick payment: " + clr_ok(matched_pm["name"]))
                            break
            except Exception as pe:
                pass  # silent
        else:
            # Payment keyword kosong = auto pick first available
            try:
                pm_options = get_payment_options(sess, token)
                for pm in pm_options:
                    if isinstance(pm, dict) and pm.get("company"):
                        _rp2 = pm.get("pgType", "") or ""
                        _rt2 = pm.get("type", "PG") or "PG"
                        matched_pm = {
                            "company": pm.get("company", ""),
                            "method": pm.get("method", ""),
                            "pgType": _rp2 if _rp2 else _rt2,
                            "type": _rt2,
                            "name": BANK_CODE_MAP.get(pm.get("company", ""), pm.get("company", "")) + " (" + pm.get("method", "") + ")",
                        }
                        log(label + " " + ok_tag() + " Auto-pick payment: " + clr_ok(matched_pm["name"]))
                        break
                if not matched_pm:
                    log(label + "    " + clr_warn("Tidak ada payment tersedia"))
            except Exception as pe:
                pass  # silent

        # Set payment on checkout BEFORE completing
        checkout_payment_ok = False
        if matched_pm:
            try:
                pm_result = set_checkout_payment(sess, token, checkout_id,
                                    matched_pm["company"], matched_pm["method"],
                                    matched_pm.get("pgType", "XENDIT"))
                if pm_result:
                    checkout_payment_ok = True
                    log(label + " " + ok_tag() + " Pembayaran: " + clr_ok(matched_pm["name"]))
                else:
                    pass  # silently retry via alternative method
            except Exception as pe:
                pass  # silently retry via alternative method

        # STEP 7: Complete checkout → create order → set payment
        log(label + " " + step_tag(7, total_steps) + " Membuat pesanan...")

        real_order_id = None
        order_data = None
        order_number = ""

        # Set downpayment + complete in parallel-ready sequence
        try:
            set_checkout_downpayment(sess, token, checkout_id, skip_downpayment=False)
        except Exception:
            pass

        # Complete checkout via ;complete endpoint (creates the order)
        # PENTING: paymentType di ;complete harus SELALU "PG"
        # Tipe asli (NICEPAY, XENDIT dll) hanya dipakai saat create_payment_on_order
        payment_type = "PG"

        # Jika set_checkout_payment gagal, embed payment info di ;complete body
        pi = matched_pm if (matched_pm and not checkout_payment_ok) else None

        try:
            order_id_new, complete_data = complete_checkout(sess, token, checkout_id, payment_type, payment_info=pi)
            if order_id_new:
                real_order_id = order_id_new
                order_data = complete_data
                log(label + " " + ok_tag() + " Order created: " + clr_bold(str(real_order_id)))
            else:
                pass  # retry silently
        except Exception as ce:
            pass  # retry silently

        # 2) Fallback: try GET order by checkout_id
        if not real_order_id:
            try:
                order_data = get_order_details(sess, token, checkout_id, buyer_id)
                if order_data:
                    order_obj = order_data.get("order", order_data) if isinstance(order_data, dict) else order_data
                    real_order_id = order_obj.get("id")
                    if real_order_id:
                        log(label + " " + ok_tag() + " Order ditemukan")
            except Exception:
                pass

        # 3) Fallback: use checkout_id
        if not real_order_id:
            real_order_id = checkout_id
            pass

        order_id = real_order_id

        # Set payment pada order via Plugo API
        payment_result = None
        if matched_pm and real_order_id != checkout_id:
            pm_company = matched_pm["company"]
            pm_method = matched_pm["method"]
            pm_pg = matched_pm.get("pgType", "") or ""
            pm_type = matched_pm.get("type", "PG") or "PG"

            # Coba beberapa pgType — urutan: pgType asli, lalu alternatif
            pg_candidates = []
            if pm_pg:
                pg_candidates.append(pm_pg)
            # Tambah variasi case
            if pm_pg and pm_pg.lower() not in [c.lower() for c in pg_candidates]:
                pg_candidates.append(pm_pg.lower())
            # Fallback ke XENDIT jika belum dicoba
            if "XENDIT" not in [c.upper() for c in pg_candidates]:
                pg_candidates.append("XENDIT")

            for pg_try in pg_candidates:
                try:
                    pass  # trying payment setup
                    payment_result = create_payment_on_order(
                        sess, token, order_id,
                        pm_company, pm_method,
                        pg_try,
                        buyer_id
                    )
                    log(label + " " + ok_tag() + " Pembayaran berhasil: " + clr_ok(matched_pm["name"]))
                    break
                except Exception as pe:
                    pass  # silently try next payment type

            if not payment_result:
                log(label + "    " + clr_warn("Pembayaran gagal di-set otomatis. Pilih manual di web."))
        elif matched_pm:
            log(label + "    " + clr_warn("Pilih pembayaran manual di web"))

        # Re-load order details setelah payment di-set (untuk dapat total benar)
        try:
            order_data = get_order_details(sess, token, order_id, buyer_id)
        except Exception:
            pass

        # Get order number
        try:
            order_number = get_order_number(sess, token, order_id, buyer_id)
        except Exception:
            pass

        # Get payment details (VA number, bank, dll)
        payment_details = None
        try:
            payment_details = get_order_payments(sess, token, order_id, buyer_id)
        except Exception:
            pass

        elapsed = t.elapsed()

        # ===== DISPLAY RESULTS =====
        W2 = 56
        log("")
        log(label + " " + "=" * W2)
        log(label + " " + C.BG_GREEN + C.WHITE + C.BOLD + " ORDER BERHASIL! " + C.RESET)
        log(label + " " + "=" * W2)

        if order_number:
            log(label + "  Order Number   : " + clr_bold(str(order_number)))
        log(label + "  Order ID       : " + clr_bold(str(order_id)))
        # Checkout ID hidden for cleaner output

        product_name = product_data.get("name", product_url)
        log(label + "  Produk         : " + product_name)
        if matched["size"]:
            log(label + "  Size           : " + matched["size"])
        log(label + "  Qty            : " + str(eff_qty))

        # Show actual total from order (termasuk ongkir), bukan subtotal
        final_total = subtotal
        order_status = ""
        if order_data:
            order_obj = order_data.get("order", order_data) if isinstance(order_data, dict) else {}
            order_status = order_obj.get("status", "")
            order_total = order_obj.get("totalPrice", 0)
            if order_total:
                final_total = order_total

        final_str = "Rp{:,}".format(int(final_total)).replace(",", ".")
        log(label + "  Total Payment  : " + clr_bold(final_str))
        if order_status:
            log(label + "  Status         : " + clr_bold(order_status.upper()))

        # Payment details (VA number, bank, dll)
        va_shown = False
        qris_shown = False
        if payment_details:
            pay_list = payment_details if isinstance(payment_details, list) else [payment_details]
            for pay in pay_list:
                if not isinstance(pay, dict):
                    continue

                # QRIS QR code details
                qr_obj = pay.get("qr", {}) if isinstance(pay.get("qr"), dict) else {}
                qr_string = qr_obj.get("qrString") or qr_obj.get("qr_string") or pay.get("qrString") or pay.get("qr_string") or ""
                qr_url = qr_obj.get("qrUrl") or qr_obj.get("qr_url") or pay.get("qrUrl") or pay.get("qr_url") or ""
                qr_amount = qr_obj.get("amount") or pay.get("expectedAmount") or pay.get("amount") or ""
                qr_status = qr_obj.get("status") or pay.get("status") or ""
                qr_expiry = qr_obj.get("expireAt") or qr_obj.get("expiredAt") or pay.get("expirationDate") or pay.get("expiry") or ""

                if qr_string or qr_url:
                    log(label + " " + "-" * W2)
                    log(label + "  " + C.CYAN + C.BOLD + "DETAIL PEMBAYARAN QRIS:" + C.RESET)
                    if qr_string:
                        log(label + "  " + C.BG_GREEN + C.WHITE + C.BOLD + " QRIS STRING: " + str(qr_string)[:120] + " " + C.RESET)
                    if qr_url:
                        log(label + "  QR URL         : " + str(qr_url))
                    if qr_amount:
                        amt_str = "Rp{:,}".format(int(qr_amount)).replace(",", ".")
                        log(label + "  Jumlah Bayar   : " + clr_bold(amt_str))
                    if qr_status:
                        log(label + "  Status Payment : " + str(qr_status))
                    if qr_expiry:
                        log(label + "  Batas Bayar    : " + str(qr_expiry))
                    qris_shown = True
                    va_shown = True
                    continue

                # VA details bisa di top-level atau nested di pay.va
                va_obj = pay.get("va", {}) if isinstance(pay.get("va"), dict) else {}
                bank_code = va_obj.get("bankCode") or pay.get("bankCode") or pay.get("bank") or pay.get("company") or ""
                bank_name = BANK_CODE_MAP.get(bank_code, bank_code)
                va_number = va_obj.get("accountNumber") or pay.get("accountNumber") or pay.get("vaNumber") or pay.get("paymentCode") or ""
                pay_method = pay.get("paymentChannel") or pay.get("method") or ""
                pay_status = va_obj.get("status") or pay.get("status") or ""
                expiry = va_obj.get("expireAt") or va_obj.get("expiredAt") or pay.get("expirationDate") or pay.get("expiry") or ""
                pay_amount = va_obj.get("amount") or pay.get("expectedAmount") or pay.get("amount") or ""

                if va_number:
                    log(label + " " + "-" * W2)
                    log(label + "  " + C.CYAN + C.BOLD + "DETAIL PEMBAYARAN:" + C.RESET)
                    if bank_name:
                        log(label + "  Bank           : " + clr_bold(str(bank_name)))
                    if pay_method:
                        log(label + "  Metode         : " + str(pay_method))
                    log(label + "  " + C.BG_GREEN + C.WHITE + C.BOLD + " VIRTUAL ACCOUNT: " + str(va_number) + " " + C.RESET)
                    va_shown = True
                    if pay_amount:
                        amt_str = "Rp{:,}".format(int(pay_amount)).replace(",", ".")
                        log(label + "  Jumlah Bayar   : " + clr_bold(amt_str))
                    if pay_status:
                        log(label + "  Status Payment : " + str(pay_status))
                    if expiry:
                        log(label + "  Batas Bayar    : " + str(expiry))

        if not va_shown and payment_result:
            pr = payment_result if isinstance(payment_result, dict) else {}
            pr_data = pr.get("data", pr) if isinstance(pr, dict) else {}
            # VA details bisa nested di pr_data.payment.va
            pr_pay = pr_data.get("payment", {}) if isinstance(pr_data, dict) else {}
            pr_va = pr_pay.get("va", {}) if isinstance(pr_pay, dict) else {}
            va_number = pr_va.get("accountNumber") or pr_data.get("accountNumber") or pr_data.get("vaNumber") or pr_data.get("paymentCode") or ""
            bank_code = pr_va.get("bankCode") or pr_data.get("bankCode") or pr_data.get("bank") or ""
            bank_name = BANK_CODE_MAP.get(bank_code, bank_code)
            expiry = pr_va.get("expireAt") or pr_va.get("expiredAt") or ""
            pay_amount = pr_va.get("amount") or pr_data.get("totalPrice") or ""

            # QRIS QR code display
            pr_qr = pr_pay.get("qr", {}) if isinstance(pr_pay, dict) else {}
            qr_string = pr_qr.get("qrString") or pr_qr.get("qr_string") or pr_data.get("qrString") or pr_data.get("qr_string") or ""
            qr_url = pr_qr.get("qrUrl") or pr_qr.get("qr_url") or pr_data.get("qrUrl") or pr_data.get("qr_url") or ""

            if qr_string or qr_url:
                log(label + " " + "-" * W2)
                log(label + "  " + C.CYAN + C.BOLD + "DETAIL PEMBAYARAN QRIS:" + C.RESET)
                if qr_string:
                    log(label + "  " + C.BG_GREEN + C.WHITE + C.BOLD + " QRIS STRING: " + str(qr_string)[:120] + " " + C.RESET)
                if qr_url:
                    log(label + "  QR URL         : " + str(qr_url))
                if pay_amount:
                    amt_str = "Rp{:,}".format(int(pay_amount)).replace(",", ".")
                    log(label + "  Jumlah Bayar   : " + clr_bold(amt_str))
                if expiry:
                    log(label + "  Batas Bayar    : " + str(expiry))
            elif va_number:
                log(label + " " + "-" * W2)
                log(label + "  " + C.CYAN + C.BOLD + "DETAIL PEMBAYARAN:" + C.RESET)
                if bank_name:
                    log(label + "  Bank           : " + clr_bold(str(bank_name)))
                log(label + "  " + C.BG_GREEN + C.WHITE + C.BOLD + " VIRTUAL ACCOUNT: " + str(va_number) + " " + C.RESET)
                if pay_amount:
                    amt_str = "Rp{:,}".format(int(pay_amount)).replace(",", ".")
                    log(label + "  Jumlah Bayar   : " + clr_bold(amt_str))
                if expiry:
                    log(label + "  Batas Bayar    : " + str(expiry))

        log(label + "  Waktu          : " + "{:.2f}s".format(elapsed))
        log(label + " " + "-" * W2)

        order_url = SITE_BASE + "/orders/" + str(order_id)
        log(label + "  " + C.CYAN + "Order page:" + C.RESET)
        log(label + "  " + C.CYAN + C.BOLD + order_url + C.RESET)
        log(label + " " + "=" * W2)

        # Telegram
        send_telegram_notification({
            "name": acc["name"],
            "email": acc["email"],
            "product_name": product_data.get("name", product_url),
            "size": matched["size"] or "One Size",
            "qty": eff_qty,
            "total": final_str,
            "elapsed": elapsed,
            "checkout_id": checkout_id,
            "order_id": order_id,
            "order_number": order_number,
            "order_url": order_url,
        })

        return {
            "success": True,
            "checkout_id": checkout_id,
            "order_id": order_id,
            "order_number": order_number,
            "order_url": order_url,
            "total": final_total,
            "elapsed": elapsed,
        }

    except Exception as e:
        elapsed = t.elapsed()
        err_str = str(e)
        if "TIKET_RETRY" not in err_str:
            log(label + " " + fail_tag() + " " + clr_err(err_str))
        return {"success": False, "error": err_str, "elapsed": elapsed}


# =============================================
# MAIN
# =============================================

def main():
    W = 66

    # Load accounts (silent)
    accounts = load_accounts(ACCOUNTS_FILE)
    if not accounts:
        print("  File '" + ACCOUNTS_FILE + "' tidak ditemukan atau kosong!")
        print("  Format: nama|email|phone|subdistrik_search|alamat_detail|kecamatan_note|kode_pos_note|size|qty|fallback")
        return

    # ===== TANYA SEMUA DULU, OUTPUT NANTI =====
    print("")

    # 1. Produk: URL atau keyword
    print("  Input produk: 1=URL langsung  2=Cari keyword")
    input_mode = input("  Pilih [1/2]: ").strip()

    site_url = None
    keyword_input = None
    product_url = None

    if input_mode == "2":
        site_url = input("  URL toko (contoh: https://chambredelavain.com): ").strip()
        if not site_url:
            print("  " + clr_err("URL toko wajib diisi!"))
            return
        if not site_url.startswith("http"):
            site_url = "https://" + site_url
        keyword_input = input("  Keyword produk: ").strip()
        if not keyword_input:
            print("  " + clr_err("Keyword wajib diisi!"))
            return
    else:
        product_url = input("  URL produk: ").strip()
        if not product_url:
            print("  URL wajib diisi!")
            return
        if not product_url.startswith("http"):
            print("  " + clr_err("URL harus lengkap (https://...)"))
            return

    # 2. Pilih akun
    for i, a in enumerate(accounts, 1):
        variant_info = " [" + a["size"] + "]" if a.get("size") else ""
        print("    " + str(i) + ". " + a["name"] + " | " + a["email"] + variant_info)
    sel_input = input("  Pilih akun [1-" + str(len(accounts)) + "/semua]: ").strip().lower()
    selected = parse_account_selection(sel_input, len(accounts))

    # 3. War time
    time_input = input("  Waktu war (HH:mm:ss) [ENTER=langsung]: ").strip()
    if time_input:
        try:
            target_time = parse_target_time(time_input)
        except ValueError as e:
            print("  " + str(e))
            return
        war_mode = True
    else:
        war_mode = False
        target_time = None

    # 4. Shipping (numbered list)
    SHIPPING_LIST = [
        "JNE", "Lion Parcel", "AnterAja", "Paxel", "SiCepat",
        "J&T", "Ninja", "Pos Indonesia", "TIKI", "Grab", "GoSend",
    ]
    print("  Shipping:")
    for si, sn in enumerate(SHIPPING_LIST, 1):
        print("    " + str(si) + ". " + sn)
    print("    0. Auto (pertama)")
    ship_sel = input("  Pilih [0]: ").strip()
    shipping_keyword = ""
    if ship_sel and ship_sel != "0":
        try:
            ship_idx = int(ship_sel) - 1
            if 0 <= ship_idx < len(SHIPPING_LIST):
                shipping_keyword = SHIPPING_LIST[ship_idx]
        except ValueError:
            shipping_keyword = ship_sel

    # 5. Payment (numbered list)
    PAYMENT_LIST = [
        "QRIS", "OVO", "ShopeePay", "Mandiri", "BCA", "BRI", "BNI",
        "CIMB Niaga", "Permata", "Danamon", "BSI", "Alfamart", "Indomaret",
        "Akulaku", "Kredivo",
    ]
    print("  Payment:")
    for pi, pn in enumerate(PAYMENT_LIST, 1):
        print("    " + str(pi) + ". " + pn)
    print("    0. Auto (pertama)")
    pay_sel = input("  Pilih [0]: ").strip()
    payment_keyword = ""
    if pay_sel and pay_sel != "0":
        try:
            pay_idx = int(pay_sel) - 1
            if 0 <= pay_idx < len(PAYMENT_LIST):
                payment_keyword = PAYMENT_LIST[pay_idx]
        except ValueError:
            payment_keyword = pay_sel

    # 6. Mode
    parallel = input("  Mode (1=Sekuensial 2=Paralel) [1]: ").strip() == "2"

    # ===== SEMUA PERTANYAAN SELESAI, MULAI PROSES =====
    print("")
    print(C.CYAN + "=" * W + C.RESET)

    # Proses keyword search jika mode 2
    if input_mode == "2" and site_url and keyword_input:
        site_base = extract_site_base(site_url)
        if not site_base:
            print("  " + clr_err("URL site tidak valid"))
            return

        log("Mendeteksi toko dari " + clr_info(site_base) + "...")
        vendor_id_detected = auto_detect_vendor_id(site_base)
        if not vendor_id_detected:
            print("  " + clr_err("Gagal mendeteksi toko. Pastikan URL Plugo valid."))
            return

        # Search produk
        log("Mencari produk: " + clr_info(keyword_input) + "...")
        results = search_products_by_keyword(vendor_id_detected, keyword_input)

        if not results:
            print("  " + clr_err("Tidak ada produk ditemukan untuk: '" + keyword_input + "'"))
            return

        # Auto-pick first match
        sel_product = results[0]
        product_url = build_product_url(site_base, sel_product["id"], sel_product.get("name", ""))
        if len(results) == 1:
            log(ok_tag() + " Produk: " + clr_bold(sel_product.get("name", "?")))
        else:
            log(ok_tag() + " " + str(len(results)) + " produk cocok, pilih pertama: " + clr_bold(sel_product.get("name", "?")))

    # Auto-detect VENDOR_ID and SITE_BASE
    try:
        vendor_id, site_base = setup_site_config(product_url)
    except Exception as e:
        print("  " + clr_err(str(e)))
        return

    cookie_data = load_cookie_tokens()

    # Log cookie status
    if cookie_data.get("at"):
        cookie_user = get_buyer_id_from_token(cookie_data["at"])
        exp_str = ""
        if cookie_data.get("at_expired"):
            exp_str = " " + clr_warn("(AT expired, gas terus)")
        else:
            exp_str = " " + clr_ok("(valid)")
        log(ok_tag() + " Cookie loaded: buyerId=" + str(cookie_user) + exp_str)
        if cookie_data.get("rt"):
            rt_str = clr_ok("ada") if not cookie_data.get("rt_expired") else clr_warn("expired")
            log("  Refresh token: " + rt_str)
    elif os.path.exists(COOKIES_FILE):
        log(warn_tag() + " " + COOKIES_FILE + " ada tapi tidak ada token untuk vendor " + str(VENDOR_ID))
    else:
        log(clr_dim("  Cookies: tidak ada " + COOKIES_FILE + " (pakai anonymous token)"))

    log("Produk: " + clr_bold(product_url))
    mode_str = "Paralel" if parallel else "Sekuensial"
    proxy_str = " | Proxy: ON" if USE_PROXY and PROXY_URL else ""
    log(str(len(selected)) + " akun (" + mode_str + ") | Shipping: " + (shipping_keyword or "auto") + " | Payment: " + (payment_keyword or "auto") + proxy_str)
    if war_mode:
        log("War Mode: " + target_time.strftime("%H:%M:%S"))
    print(C.CYAN + "=" * W + C.RESET)

    # EKSEKUSI
    if war_mode:
        diff_target = (target_time - datetime.now()).total_seconds()
        if diff_target > 0:
            wait_until(target_time, "WAR")
        print()

    results = []

    def run_thread(run_num, acc_idx):
        acc = accounts[acc_idx]
        attempt = 0
        while not CANCEL_EVENT.is_set():
            attempt += 1
            res = checkout(run_num, len(selected), acc, product_url, payment_keyword, shipping_keyword, cookie_data)
            if res.get("success"):
                results.append((acc_idx, res))
                return
            if CANCEL_EVENT.is_set():
                log("[Akun-" + str(acc_idx+1) + "] DIBATALKAN.")
                return
            err = res.get("error", "")
            is_retry = "TIKET_RETRY" in err
            if is_retry:
                if attempt == 1:
                    log("[Akun-" + str(acc_idx+1) + "] Menunggu variant tersedia, retry " + str(RETRY_INTERVAL) + "s...")
                elif attempt % 50 == 0:
                    log("[Akun-" + str(acc_idx+1) + "] Masih retry... (" + str(attempt) + "x)")
            else:
                log("[Akun-" + str(acc_idx+1) + "] Retry #" + str(attempt) + " (" + err[:80] + ")")
            time.sleep(RETRY_INTERVAL)
        log("[Akun-" + str(acc_idx+1) + "] DIBATALKAN.")

    if parallel:
        threads = []
        for i, idx in enumerate(selected):
            th = threading.Thread(target=run_thread, args=(i+1, idx))
            threads.append(th)
            th.start()
        for th in threads:
            th.join()
    else:
        for i, idx in enumerate(selected):
            run_thread(i+1, idx)

    print("")
    print("  Selesai. " + str(len(results)) + "/" + str(len(selected)) + " berhasil.")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    main()
