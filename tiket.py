"""
tiket_bot v10.0 — HTTP murni + CapSolver reCAPTCHA
Fitur:
 [1] Auto Order Tiket
 [2] Cek Kuota Tiket Lengkap
 [3] Scan Hidden Link URL (Penjualan)
 [0] Keluar

Format data.txt (horizontal, pipe-separated):
  nama|email|phone|ktp|salutation|country|keyword|qty|soldout_mode
  +nama2|email2|phone2|ktp2|salutation2|country2   (extra collector)
"""

import json
import re
import os
import sys
import uuid
import time
import random
import string
import urllib.request
import base64
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    print("ERROR: curl_cffi belum terinstall.")
    print("Jalankan: pip install curl_cffi")
    import sys; sys.exit(1)
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Windows UTF-8 fix ──────────────────────────────────────────
if sys.platform == "win32":
    try:
        os.system("chcp 65001 > nul")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ─────────────────────────── CONFIG ───────────────────────────

DATA_FILE = "data.txt"
ACCOUNTS_FILE = "accounts.txt"
COOKIES_FILE = "cookies.json"
DEFAULT_URL = "https://www.tiket.com/id-id/to-do/my-chemical-romance-live-in-jakarta-2026/packages"
KEYWORD = "CAT 2 RIGHT"

BASE = "https://www.tiket.com"
GATEWAY = f"{BASE}/ms-gateway"

RECAPTCHA_SITEKEY = "6LeNJOEhAAAAAFnroUWNznAnSCGltIxOdDeiA5OJ"
RECAPTCHA_ACTION = "CREATE_ORDER"

CAPSOLVER_KEY = os.environ.get("CAPSOLVER_KEY", "CAP-5B9E2B8E9D67173DB8A113BA7AA36DADAA4E1769B8EF83AD1FD67FE2510D33E8")

CHROME_VER = os.environ.get("CHROME_VERSION", "136")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{CHROME_VER}.0.0.0 Safari/537.36"
)
SEC_CH_UA = f'"Chromium";v="{CHROME_VER}", "Not.A/Brand";v="99", "Google Chrome";v="{CHROME_VER}"'

MAX_CAPTCHA_RETRIES = 5
MAX_ORDER_RETRIES = 4
CAPTCHA_POLL_INTERVAL = 2
CAPTCHA_POLL_TIMEOUT = 120
RETRY_DELAY = 1

SCAN_INTERVAL = 0.1

# ─────────────────────────── HELPERS ───────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def gen_id():
    return str(uuid.uuid4())


def gen_correlation():
    return "".join(random.choices(string.ascii_letters + string.digits, k=6))


def elapsed(t0):
    return f"{time.time() - t0:.2f}"


def detect_sitekey_from_html(html_text):
    patterns = [
        r'grecaptcha\.enterprise\.execute\s*\(\s*["\']([^"\']+)["\']',
        r'grecaptcha\.execute\s*\(\s*["\']([^"\']+)["\']',
        r'render=([A-Za-z0-9_-]{40})',
        r'"recaptchaSiteKey"\s*:\s*"([^"]+)"',
        r'"captchaSiteKey"\s*:\s*"([^"]+)"',
        r'"siteKey"\s*:\s*"([^"]+)"',
        r'"recaptchaKey"\s*:\s*"([^"]+)"',
        r'data-sitekey="([^"]+)"',
    ]
    for pat in patterns:
        m = re.search(pat, html_text)
        if m:
            key = m.group(1)
            if len(key) > 20 and key != "explicit":
                return key
    return None


def detect_action_from_html(html_text):
    patterns = [
        r"action\s*:\s*['\"]([A-Z_]+)['\"]",
        r'"action"\s*:\s*"([A-Z_]+)"',
    ]
    for pat in patterns:
        m = re.search(pat, html_text)
        if m:
            return m.group(1)
    return None


# ─────────────────────────── CAPSOLVER reCAPTCHA ───────────────────────────

def solve_recaptcha_capsolver(page_url, sitekey=None, action=None, attempt=1):
    if not CAPSOLVER_KEY:
        log("ERROR: CAPSOLVER_KEY tidak di-set!")
        return None

    use_sitekey = sitekey or RECAPTCHA_SITEKEY
    use_action = action or RECAPTCHA_ACTION

    log(f"       CapSolver: solving captcha (attempt {attempt})...")

    create_payload = {
        "clientKey": CAPSOLVER_KEY,
        "task": {
            "type": "ReCaptchaV3EnterpriseTaskProxyLess",
            "websiteURL": page_url,
            "websiteKey": use_sitekey,
            "pageAction": use_action,
        }
    }

    create_data = json.dumps(create_payload).encode()
    try:
        req = urllib.request.Request(
            "https://api.capsolver.com/createTask",
            data=create_data,
            headers={"Content-Type": "application/json"}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except Exception as e:
        log(f"  CapSolver createTask error: {e}")
        return None

    if resp.get("errorId", 0) != 0:
        log(f"  CapSolver error: {resp.get('errorCode', '')} - {resp.get('errorDescription', '')}")
        return None

    task_id = resp.get("taskId")
    if not task_id:
        solution = resp.get("solution", {})
        token = solution.get("gRecaptchaResponse", "")
        if token and len(token) > 20:
            log(f"       \033[92m✔ Captcha solved (instant)\033[0m")
            return {"token": token, "source": "capsolver"}
        log("  CapSolver: no taskId and no instant solution")
        return None

    log(f"       Task ID: {task_id[:16]}...")
    time.sleep(2)

    start = time.time()
    while time.time() - start < CAPTCHA_POLL_TIMEOUT:
        try:
            get_data = json.dumps({"clientKey": CAPSOLVER_KEY, "taskId": task_id}).encode()
            req2 = urllib.request.Request(
                "https://api.capsolver.com/getTaskResult",
                data=get_data,
                headers={"Content-Type": "application/json"}
            )
            result = json.loads(urllib.request.urlopen(req2, timeout=30).read())
        except Exception:
            time.sleep(CAPTCHA_POLL_INTERVAL)
            continue

        if result.get("errorId", 0) != 0:
            err_code = result.get("errorCode", "")
            if "CAPCHA_NOT_READY" in err_code.upper() or "CAPTCHA_NOT_READY" in err_code.upper():
                time.sleep(CAPTCHA_POLL_INTERVAL)
                continue
            log(f"  CapSolver error: {err_code}")
            return None

        status = result.get("status", "")
        if status == "ready":
            token = result.get("solution", {}).get("gRecaptchaResponse", "")
            if token:
                log(f"       \033[92m✔ Captcha solved\033[0m")
                return {"token": token, "source": "capsolver"}
            log("  CapSolver: solution ready but no token")
            return None

        if status == "processing":
            time.sleep(CAPTCHA_POLL_INTERVAL)
            continue

        time.sleep(CAPTCHA_POLL_INTERVAL)

    log("  CapSolver TIMEOUT")
    return None


def solve_captcha_with_retries(page_url, sitekey=None, action=None):
    use_sitekey = sitekey or RECAPTCHA_SITEKEY
    use_action = action or RECAPTCHA_ACTION

    log("       Menyelesaikan captcha...")
    for attempt in range(1, MAX_CAPTCHA_RETRIES + 1):
        result = solve_recaptcha_capsolver(page_url, use_sitekey, use_action, attempt)
        if result and result.get("token"):
            return result
        if attempt < MAX_CAPTCHA_RETRIES:
            wait = RETRY_DELAY * attempt
            log(f"  Retry in {wait}s...")
            time.sleep(wait)

    log("\033[91m       ✘ Captcha gagal setelah semua percobaan!\033[0m")
    return None


# ─────────────────────────── PROFILE / DATA ───────────────────────────

def parse_horizontal_line(line):
    """Parse satu baris horizontal: nama|email|phone|ktp|salutation|country[|keyword|qty|soldout_mode]"""
    parts = line.split("|")
    if len(parts) < 4:
        return None
    d = {
        "nama": parts[0].strip(),
        "email": parts[1].strip() if len(parts) > 1 else "",
        "phone": parts[2].strip() if len(parts) > 2 else "",
        "ktp": parts[3].strip() if len(parts) > 3 else "",
        "salutation": parts[4].strip() if len(parts) > 4 else "Mr",
        "country": parts[5].strip() if len(parts) > 5 else "Indonesia",
    }
    if len(parts) > 6 and parts[6].strip():
        d["keyword"] = parts[6].strip()
    if len(parts) > 7 and parts[7].strip():
        d["qty"] = parts[7].strip()
    if len(parts) > 8 and parts[8].strip():
        d["soldout_mode"] = parts[8].strip()
    return d


def load_all_profiles():
    profiles = []

    # Load accounts.txt (legacy pipe format)
    if os.path.exists(ACCOUNTS_FILE):
        with open(ACCOUNTS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                if len(parts) >= 5:
                    d = {
                        "nama": parts[0].strip(),
                        "email": parts[1].strip(),
                        "phone": parts[2].strip(),
                        "identity_type": parts[3].strip() if len(parts) > 3 else "ktp",
                        "ktp": parts[4].strip() if len(parts) > 4 else "",
                        "salutation": parts[5].strip() if len(parts) > 5 else "Mr",
                        "country": parts[6].strip() if len(parts) > 6 else "Indonesia",
                    }
                    if len(parts) > 7:
                        d["keyword"] = parts[7].strip()
                    if len(parts) > 8:
                        d["qty"] = parts[8].strip()
                    profiles.append(d)

    # Load data.txt — support BOTH formats
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            raw = f.read()

        lines = [l.strip() for l in raw.splitlines()]
        non_comment = [l for l in lines if l and not l.startswith("#")]

        # Detect format: horizontal (pipe) or vertical (key=value)
        is_horizontal = False
        for l in non_comment:
            if not l.startswith("+") and "|" in l and "=" not in l.split("|")[0]:
                is_horizontal = True
                break

        if is_horizontal:
            # NEW horizontal format
            current_profile = None
            for l in non_comment:
                if l.startswith("+"):
                    # Extra collector for previous profile
                    if current_profile is not None:
                        extra = parse_horizontal_line(l[1:])
                        if extra:
                            if "extra_collectors" not in current_profile:
                                current_profile["extra_collectors"] = []
                            current_profile["extra_collectors"].append(extra)
                else:
                    p = parse_horizontal_line(l)
                    if p:
                        if current_profile is not None:
                            profiles.append(current_profile)
                        current_profile = p
            if current_profile is not None:
                profiles.append(current_profile)
        else:
            # OLD vertical format (key=value, --- separator)
            for block in raw.split("---"):
                d = {}
                for line in block.strip().splitlines():
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        d[k.strip().lower()] = v.strip()
                if d:
                    profiles.append(d)

    if not profiles:
        profiles.append({"nama": "aldi", "phone": "08569144444", "email": "test@gmail.com", "country": "Indonesia"})
    return profiles


def pick_profile(profiles):
    if len(profiles) == 1:
        p = profiles[0]
        log(f"Profil: {p.get('nama', '?')} ({p.get('email', '?')})")
        return p
    print("\n" + "=" * 60 + "\n  PILIH PROFIL DATA\n" + "=" * 60)
    for i, p in enumerate(profiles, 1):
        pqty = p.get('qty', '1')
        extra = len(p.get('extra_collectors', []))
        extra_str = f" +{extra} pembeli" if extra else ""
        print(f"  [{i}] {p.get('nama', '?')} | {p.get('email', '?')} | kw: {p.get('keyword', KEYWORD)} | qty: {pqty}{extra_str}")
    print("=" * 60)
    while True:
        try:
            idx = int(input(f"Pilih (1-{len(profiles)}): ").strip()) - 1
            if 0 <= idx < len(profiles):
                return profiles[idx]
        except (ValueError, EOFError):
            pass


def ask_url(prompt_text=None):
    if prompt_text:
        print(prompt_text)
    else:
        print(f"\nDefault URL: {DEFAULT_URL}\n(tekan ENTER untuk pakai default)")
    url = input("URL: ").strip()
    if not url:
        return DEFAULT_URL
    if not url.startswith("http"):
        url = "https://" + url
    return url


def normalize_cookie_editor(cookies_raw):
    normalized = []
    for ck in cookies_raw:
        c = dict(ck)
        domain = c.get("domain", "")
        if c.get("hostOnly") and not domain.startswith("."):
            pass
        elif not domain.startswith(".") and not domain.startswith("www"):
            c["domain"] = "." + domain.lstrip(".")
        if c.get("sameSite") == "no_restriction":
            c["sameSite"] = "None"
        elif c.get("sameSite") == "lax":
            c["sameSite"] = "Lax"
        elif c.get("sameSite") == "strict":
            c["sameSite"] = "Strict"
        for key in ["hostOnly", "storeId", "expirationDate", "session"]:
            c.pop(key, None)
        normalized.append(c)
    return normalized


def find_cookie_files():
    files = []
    for f in sorted(os.listdir(".")):
        if f.lower().startswith("cookies") and f.lower().endswith(".json"):
            files.append(f)
    if not files and os.path.exists(COOKIES_FILE):
        files = [COOKIES_FILE]
    return files


def pick_cookie_file():
    files = find_cookie_files()
    if not files:
        log("\033[91mWARNING: Tidak ada file cookies*.json!\033[0m")
        return COOKIES_FILE
    if len(files) == 1:
        return files[0]
    print("\n" + "=" * 60 + "\n  PILIH COOKIE FILE\n" + "=" * 60)
    for i, f in enumerate(files, 1):
        label = f
        try:
            with open(f, encoding="utf-8") as fh:
                raw = json.load(fh)
            for ck in raw:
                if ck.get("name") == "session_access_token":
                    try:
                        p = ck["value"].split(".")
                        if len(p) >= 2:
                            b = p[1] + "=" * (4 - len(p[1]) % 4)
                            email = json.loads(base64.urlsafe_b64decode(b)).get("email", "")
                            if email:
                                label = f"{f} ({email})"
                    except Exception:
                        pass
                    break
        except Exception:
            pass
        print(f"  [{i}] {label}")
    print("=" * 60)
    while True:
        try:
            idx = int(input(f"Pilih (1-{len(files)}): ").strip()) - 1
            if 0 <= idx < len(files):
                return files[idx]
        except (ValueError, EOFError):
            pass


def load_cookies(cookie_file=None):
    cfile = cookie_file or COOKIES_FILE
    if not os.path.exists(cfile):
        return []
    try:
        with open(cfile, encoding="utf-8") as f:
            raw = json.load(f)
        cookies = normalize_cookie_editor(raw)
        log(f"       {len(cookies)} cookies dimuat dari {cfile}")
        return cookies
    except Exception as e:
        log(f"Gagal load cookies: {e}")
        return []


def extract_product_url(event_url):
    m = re.search(r'/to-do/([^/]+?)(?:/packages|/order)?(?:\?|$)', event_url)
    if m:
        return m.group(1)
    parts = event_url.rstrip("/").split("/")
    return parts[-1] if parts[-1] != "packages" else parts[-2]


# ─────────────────────────── SESSION SETUP ───────────────────────────

def build_cookie_header(cookies_raw):
    return "; ".join(f"{ck.get('name', '')}={ck.get('value', '')}" for ck in cookies_raw if ck.get("name") and ck.get("value"))


def refresh_cookie_header(session, cookies_raw, old_cookie_header):
    new_cookies = []
    seen = set()
    try:
        for name, value in session.cookies.items():
            if name and value:
                new_cookies.append(f"{name}={value}")
                seen.add(name)
    except Exception:
        pass
    for ck in cookies_raw:
        name = ck.get("name", "")
        value = ck.get("value", "")
        if name and value and name not in seen:
            new_cookies.append(f"{name}={value}")
            seen.add(name)
    return "; ".join(new_cookies)


def create_session(cookies_raw):
    session = cffi_requests.Session(impersonate="chrome136")
    count = 0
    for ck in cookies_raw:
        name = ck.get("name", "")
        value = ck.get("value", "")
        domain = ck.get("domain", ".tiket.com")
        if not domain.startswith("."):
            domain = "." + domain.lstrip(".")
        if name and value:
            session.cookies.set(name, value, domain=domain)
            count += 1
    log(f"       Session OK, {count} cookies dimuat")
    cookie_header = build_cookie_header(cookies_raw)
    cookie_header = refresh_cookie_header(session, cookies_raw, cookie_header)
    return session, cookie_header


def create_simple_session():
    """Session ringan tanpa cookies untuk scan URL."""
    return cffi_requests.Session(impersonate="chrome136")


def gateway_headers(device_id, request_id, cookie_header, referer=None):
    h = {
        "language": "ID",
        "x-request-id": request_id,
        "sec-ch-ua-platform": '"Windows"',
        "useragent": UA,
        "lang": "id",
        "x-correlation-id": gen_correlation(),
        "x-platform-v2": "WEB",
        "sec-ch-ua": SEC_CH_UA,
        "storeid": "TIKETCOM",
        "x-city-id": "",
        "sec-ch-ua-mobile": "?0",
        "x-currency": "IDR",
        "deviceid": device_id,
        "cf-ipcountry": "ID",
        "countrycode": "id",
        "requestid": "NONE",
        "x-country-code": "id",
        "x-country-id": "id",
        "platform": "WEB",
        "x-cookie-session-v2": "true",
        "accept": "application/json, text/plain, */*",
        "accept-language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "x-audience": "tiket.com",
        "currency": "IDR",
        "x-region-id": "",
        "user-agent": UA,
        "channelid": "WEB",
        "x-channel-id-v2": "WEB",
        "serviceid": "GATEWAY",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "cookie": cookie_header,
    }
    if referer:
        h["referer"] = referer
    return h


def payment_headers(device_id, request_id, cookie_header, referer=None):
    h = {
        "x-request-id": request_id,
        "sec-ch-ua-platform": '"Windows"',
        "lang": "id",
        "x-correlation-id": gen_correlation(),
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "x-currency": "IDR",
        "deviceid": device_id,
        "countrycode": "id",
        "x-country-code": "id",
        "x-country-id": "id",
        "x-cookie-session-v2": "true",
        "accept": "application/json, text/plain, */*",
        "accept-language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "x-audience": "tiket.com",
        "currency": "IDR",
        "user-agent": UA,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "cookie": cookie_header,
    }
    if referer:
        h["referer"] = referer
    return h


# ─────────────────────────── HTTP API CALLS ───────────────────────────

def http_post_json(session, url, payload, headers, label="POST", timeout=20, verbose=False):
    for attempt in range(4):
        try:
            r = session.post(url, json=payload, headers=headers, timeout=timeout)
            if verbose:
                log(f"       {label} <- {r.status_code}")
            if r.status_code == 200:
                return r.json()
            if r.status_code == 403:
                log(f"  BLOCKED 403, retry {attempt + 1}/4...")
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            if r.status_code == 429:
                w = RETRY_DELAY * (attempt + 2)
                log(f"  RATE LIMITED 429, tunggu {w}s...")
                time.sleep(w)
                continue
            try:
                return r.json()
            except Exception:
                log(f"  {label} ({r.status_code}): {r.text[:500]}")
                return None
        except Exception as e:
            log(f"  {label} ERROR: {e}")
            if attempt < 3:
                time.sleep(RETRY_DELAY)
                continue
            return None
    return None


def http_put_json(session, url, payload, headers, label="PUT", timeout=20, verbose=False):
    for attempt in range(4):
        try:
            r = session.put(url, json=payload, headers=headers, timeout=timeout)
            if verbose:
                log(f"       {label} <- {r.status_code}")
            if r.status_code == 200:
                return r.json()
            if r.status_code in (403, 429):
                w = RETRY_DELAY * (attempt + 1)
                log(f"  {r.status_code}, tunggu {w}s...")
                time.sleep(w)
                continue
            try:
                return r.json()
            except Exception:
                log(f"  {label} ({r.status_code}): {r.text[:500]}")
                return None
        except Exception as e:
            log(f"  {label} ERROR: {e}")
            if attempt < 3:
                time.sleep(RETRY_DELAY)
                continue
            return None
    return None


def http_get_json(session, url, headers, params=None, label="GET", timeout=15, verbose=False):
    for attempt in range(4):
        try:
            r = session.get(url, params=params, headers=headers, timeout=timeout)
            if verbose:
                log(f"       {label} <- {r.status_code}")
            if r.status_code == 200:
                return r.json()
            if r.status_code in (403, 429):
                w = RETRY_DELAY * (attempt + 1)
                log(f"  {r.status_code}, tunggu {w}s...")
                time.sleep(w)
                continue
            try:
                return r.json()
            except Exception:
                log(f"  {label} ({r.status_code}): {r.text[:500]}")
                return None
        except Exception as e:
            log(f"  {label} ERROR: {e}")
            if attempt < 3:
                time.sleep(RETRY_DELAY)
                continue
            return None
    return None


# ─────────────────────────── VA / EXPIRED EXTRACTION ───────────────────────────

def _deep_find(obj, keys):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str) and len(v) >= 10:
                return v
            found = _deep_find(v, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _deep_find(item, keys)
            if found:
                return found
    return None


def _format_va(digits):
    d = str(digits).strip()
    if not d:
        return ""
    remainder = len(d) % 4
    parts = []
    if remainder:
        parts.append(d[:remainder])
    i = remainder
    while i < len(d):
        parts.append(d[i:i+4])
        i += 4
    return " ".join(parts)


def _construct_va_from_order(order_id):
    oid = str(order_id)
    if len(oid) >= 8:
        va = "7800113" + oid[-8:]
        return _format_va(va)
    return ""


def _extract_va_number(confirm_resp):
    if not confirm_resp:
        return "Tidak terdeteksi"
    data = confirm_resp.get("data") or {}
    for key in ["virtualAccountNumber", "vaNumber", "accountNumber", "virtualAccount", "va_number"]:
        val = data.get(key, "")
        if val and re.match(r'^\d{10,20}$', str(val)):
            return _format_va(val)
    va_keys = {"virtualAccountNumber", "vaNumber", "accountNumber"}
    found = _deep_find(data, va_keys)
    if found and re.match(r'^\d{10,20}$', str(found)):
        return _format_va(found)
    return "Tidak terdeteksi"


def _extract_expired(confirm_resp):
    if not confirm_resp:
        return ""
    data = confirm_resp.get("data") or {}
    exp_keys = ["paymentExpired", "expiredTime", "expired", "expiredAt", "expiryTime",
                "paymentExpiredTime", "paymentExpiry", "expireTime", "expireAt"]
    exp_data = None
    for key in exp_keys:
        val = data.get(key)
        if val:
            exp_data = val
            break
    if not exp_data:
        sidebar = data.get("sidebarPayment") or {}
        for key in exp_keys:
            val = sidebar.get(key)
            if val:
                exp_data = val
                break
    if not exp_data:
        exp_data = _deep_find(data, set(exp_keys))
    if not exp_data:
        return ""
    try:
        if isinstance(exp_data, (int, float)):
            ts = exp_data / 1000 if exp_data > 1e12 else exp_data
            exp_dt = datetime.fromtimestamp(ts)
            return exp_dt.strftime("%Y-%m-%d %H:%M:%S WIB")
        s = str(exp_data).strip()
        if re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$', s):
            return s + " WIB"
        s = s.replace("Z", "+00:00")
        exp_dt = datetime.fromisoformat(s)
        return exp_dt.strftime("%Y-%m-%d %H:%M:%S WIB")
    except Exception:
        return str(exp_data)


def _extract_redirect_url(resp):
    if not resp:
        return ""
    data = resp.get("data", {}) or {}
    url = data.get("redirectUrl", "") or ""
    if url:
        return url
    for key in ["paymentInfo", "payment", "paymentDetail", "bankTransfer", "virtualAccount"]:
        sub = data.get(key, {}) or {}
        if isinstance(sub, dict):
            url = sub.get("redirectUrl", "") or sub.get("deeplink", "") or sub.get("deeplinkUrl", "")
            if url:
                return url
    resp_str = str(resp)
    m = re.search(r'https://mybca\.bca\.co\.id/deeplink/[^"\s,}]+', resp_str)
    if m:
        return m.group(0)
    return ""


# ─────────────────────────── ORDER + PAYMENT ───────────────────────────

def _get_order_status(session, order_id_mongo, device_id, cookie_header, order_page_url):
    max_poll = 15
    req_id = gen_id()
    for attempt in range(1, max_poll + 1):
        log(f"       Menunggu konfirmasi order ({attempt}/{max_poll})...")
        status_h = gateway_headers(device_id, req_id, cookie_header, order_page_url)
        status_resp = http_get_json(
            session,
            f"{GATEWAY}/tix-events-v2-order/v1/orders/{order_id_mongo}",
            status_h,
            label="ORDER STATUS",
        )
        if not status_resp:
            if attempt < max_poll:
                time.sleep(1)
                continue
            log("ERROR: Gagal get order status setelah polling")
            return None

        od = status_resp.get("data", {})
        core_order_id = od.get("coreOrderId")
        core_order_hash = od.get("coreOrderHash")
        status = od.get("status", "")
        log(f"       Status: {status} | Order: {core_order_id}")

        if core_order_id and core_order_hash:
            return {
                "order_id_mongo": order_id_mongo,
                "core_order_id": core_order_id,
                "core_order_hash": core_order_hash,
                "order_request_id": req_id,
                "status": status,
            }
        if attempt < max_poll:
            time.sleep(1)

    log("ERROR: coreOrderId/coreOrderHash kosong setelah polling!")
    return None


def create_order_http(session, order_payload, gateway_h, product_id, device_id,
                      cookie_header, order_page_url, sitekey=None, action=None):
    t0 = time.time()
    order_url = f"{GATEWAY}/tix-events-v2-order/v2/orders"
    use_sitekey = sitekey or RECAPTCHA_SITEKEY
    use_action = action or RECAPTCHA_ACTION

    h = dict(gateway_h)
    h["content-type"] = "application/json"
    h["x-product-id"] = product_id
    h["x-product-category"] = "EVENT"
    h["referer"] = order_page_url

    for order_attempt in range(1, MAX_ORDER_RETRIES + 1):
        log(f"       Percobaan order {order_attempt}/{MAX_ORDER_RETRIES}")

        captcha_result = solve_captcha_with_retries(order_page_url, use_sitekey, use_action)
        if not captcha_result or not captcha_result.get("token"):
            log("  Captcha gagal!")
            if order_attempt < MAX_ORDER_RETRIES:
                time.sleep(RETRY_DELAY * order_attempt)
                continue
            return None

        token = captcha_result["token"]
        order_payload["captchaToken"] = token
        order_payload["captchaSiteKey"] = use_sitekey

        log("       Mengirim order...")
        resp = http_post_json(session, order_url, order_payload, h, label="CREATE ORDER", timeout=20)
        if not resp:
            if order_attempt < MAX_ORDER_RETRIES:
                time.sleep(RETRY_DELAY)
                continue
            return None

        code = resp.get("code", "")
        log(f"       Response: {code}")

        if code == "SUCCESS":
            resp_data = resp.get("data") or {}
            oid = resp_data.get("id") or resp_data.get("orderId") or resp_data.get("order_id") or resp_data.get("_id")
            if not oid and isinstance(resp_data, str):
                oid = resp_data
            if not oid and isinstance(resp_data, dict):
                for k, v in resp_data.items():
                    if isinstance(v, str) and len(v) >= 20:
                        oid = v
                        break
            if not oid:
                log(f"\033[91m       ✘ Order ID tidak ditemukan dalam response\033[0m")
                return None
            log(f"\033[92m       ✔ Order berhasil! ID: {oid} ({time.time() - t0:.1f}s)\033[0m")
            return _get_order_status(session, oid, device_id, cookie_header, order_page_url)

        if code in ("CAPTCHA_VALIDATION_FAILED", "CAPTCHA_TOKEN_IS_REQUIRED"):
            log(f"  Token ditolak ({code}), retry...")
            if order_attempt < MAX_ORDER_RETRIES:
                time.sleep(RETRY_DELAY * order_attempt)
            continue

        if "SOLD_OUT" in code.upper() or "SOLD" in code.upper():
            log(f"\033[93m       ⚠ Paket sold out: {code}\033[0m")
            return {"sold_out": True, "code": code}

        log(f"\033[91m       ✘ Ditolak: {code}\033[0m")
        if order_attempt < MAX_ORDER_RETRIES:
            time.sleep(RETRY_DELAY)

    return None


def process_payment_http(session, core_order_id, core_order_hash, order_request_id,
                         device_id, cookie_header):
    pay_ref = f"{BASE}/id-id/payment?order_id={core_order_id}&order_hash={core_order_hash}"

    log("       Verifikasi payment...")
    check_h = payment_headers(device_id, gen_id(), cookie_header, pay_ref)
    check_resp = http_get_json(
        session,
        f"{GATEWAY}/tix-payment-core/payment/check-version",
        check_h,
        params={"referenceId": core_order_id, "orderHash": core_order_hash},
        label="CHECK-VERSION",
    )
    log("       Mengirim log...")
    pl_h = payment_headers(device_id, gen_id(), cookie_header, pay_ref)
    pl_h["content-type"] = "application/json"
    pl_payload = [{
        "msg": "",
        "correlationId": order_request_id,
        "utmParams": {
            "referrer": "none", "utmCampaign": "none", "utmContent": "none",
            "utmExternal": "organic", "utmId": "none", "utmLogic": "none",
            "utmMedium": "none", "utmPage": "none", "utmSection": "none",
            "utmSource": "none", "utmTerm": "none",
            "utmAttributedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        },
        "constructedUtm": {
            "campaign": "none", "content": "none", "external": "organic",
            "id": "none", "logic": "none", "medium": "none", "page": "none",
            "section": "none", "source": "none", "term": "none",
        },
    }]
    http_post_json(session, f"{GATEWAY}/tix-payment-core/post-log", pl_payload, pl_h, label="POST-LOG")

    log("       Landing payment...")
    land_h = payment_headers(device_id, gen_id(), cookie_header, pay_ref)
    land_h["content-type"] = "application/json"
    land_resp = http_post_json(
        session,
        f"{GATEWAY}/tix-payment-core/payment/v4/landing",
        {"orderHash": core_order_hash, "referenceId": int(core_order_id)},
        land_h,
        label="LANDING",
    )
    if not land_resp or land_resp.get("code") != "SUCCESS":
        log(f"\033[91m       ✘ Payment landing gagal\033[0m")
        return None
    log("       Landing OK")
    time.sleep(0.5)

    pay_body = {"orderHash": core_order_hash, "referenceId": int(core_order_id)}
    grand_total_str = "?"
    det_resp = None
    payment_method = "VA_BCA"
    is_mybca_only = False

    log("       Memilih metode VA_BCA...")
    det_ref = f"{BASE}/id-id/payment/va_bca?order_id={core_order_id}&order_hash={core_order_hash}"
    det_h = payment_headers(device_id, gen_id(), cookie_header, det_ref)
    det_h["content-type"] = "application/json"
    det_resp = http_put_json(
        session,
        f"{GATEWAY}/tix-payment-core/payment/detail/VA_BCA",
        pay_body,
        det_h,
        label="DETAIL VA_BCA",
    )
    det_code = (det_resp or {}).get("code", "")
    if det_code == "SUCCESS":
        grand_total_str = (det_resp.get("data") or {}).get("sidebarPayment", {}).get("grandTotalString", "?")
        log(f"       \033[92m✔ Total: {grand_total_str}\033[0m")
    elif det_code == "BANK_TRANSFER_ERROR":
        log("       \033[93m⚠ VA BCA tidak tersedia (event myBCA)\033[0m")
        is_mybca_only = True
        payment_method = "MYBCA"
    elif det_code == "TRANSACTION_ID_EXIST":
        log("       \033[93m⚠ VA BCA sudah terpilih sebelumnya, lanjut...\033[0m")
    else:
        det_msg = (det_resp or {}).get("message", det_code)
        log(f"       \033[93m⚠ {det_msg}\033[0m")
    time.sleep(0.5)

    log("       Konfirmasi pembayaran VA BCA...")
    conf_ref = f"{BASE}/id-id/payment/va_bca/confirm?order_id={core_order_id}&order_hash={core_order_hash}"
    conf_h = payment_headers(device_id, gen_id(), cookie_header, conf_ref)
    conf_h["content-type"] = "application/json"
    conf_resp = http_post_json(
        session,
        f"{GATEWAY}/tix-payment-core/payment/confirm/VA_BCA",
        pay_body,
        conf_h,
        label="CONFIRM",
    )
    if not conf_resp:
        log("ERROR: No response dari confirm")
        return None

    if grand_total_str == "?":
        c_data = (conf_resp.get("data") or {})
        c_total = (c_data.get("sidebarPayment") or {}).get("grandTotalString", "")
        if not c_total:
            c_total = c_data.get("grandTotalString", "")
        if not c_total:
            c_total = c_data.get("totalAmount", "")
        if not c_total:
            for key in ["grandTotal", "totalPayment", "amount", "totalPrice"]:
                val = c_data.get(key)
                if val and isinstance(val, (int, float)) and val > 0:
                    c_total = f"Rp{val:,.0f}".replace(",", ".")
                    break
        if c_total:
            grand_total_str = c_total

    if is_mybca_only:
        confirm_page_url = f"{BASE}/id-id/payment?order_id={core_order_id}&order_hash={core_order_hash}"
    else:
        confirm_page_url = f"{BASE}/id-id/payment/va_bca/confirm?order_id={core_order_id}&order_hash={core_order_hash}"
    log("       Mengambil detail konfirmasi...")
    va_from_page = ""
    total_from_page = ""
    expired_from_page = ""
    redirect_from_page = ""
    try:
        cp_h = {
            "user-agent": UA,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "referer": f"{BASE}/id-id/payment?order_id={core_order_id}&order_hash={core_order_hash}",
            "cookie": cookie_header,
        }
        cp_resp = session.get(confirm_page_url, headers=cp_h, timeout=10)
        if cp_resp.status_code == 200:
            page_text = cp_resp.text
            nd_match = re.search(r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>', page_text, re.DOTALL)
            if nd_match:
                try:
                    nd_json = json.loads(nd_match.group(1))
                    page_props = nd_json.get("props", {}).get("pageProps", {})
                    initial = page_props.get("initialState", page_props)
                    _nd_str = nd_match.group(1)
                    total_m = re.search(r'"grandTotalString"\s*:\s*"([^"]+)"', _nd_str)
                    if total_m:
                        total_from_page = total_m.group(1)
                    exp_m = re.search(r'"paymentExpired"\s*:\s*"([^"]+)"', _nd_str)
                    if exp_m:
                        expired_from_page = exp_m.group(1)
                    redir_m = re.search(r'"redirectUrl"\s*:\s*"([^"]+)"', _nd_str)
                    if redir_m:
                        redirect_from_page = redir_m.group(1)
                    if not redirect_from_page:
                        dl_m = re.search(r'https://mybca\.bca\.co\.id/deeplink/[^"\\s]+', _nd_str)
                        if dl_m:
                            redirect_from_page = dl_m.group(0)
                except Exception:
                    pass

            if not redirect_from_page:
                dl_m2 = re.search(r'https://mybca\.bca\.co\.id/deeplink/[^"\'\\s<>]+', page_text)
                if dl_m2:
                    redirect_from_page = dl_m2.group(0)

            va_patterns = [
                r'\d[\s\xa0]\d{4}[\s\xa0]\d{4}[\s\xa0]\d{4}[\s\xa0]\d{2,4}',
                r'780[\s\xa0]+\d{4}[\s\xa0]+\d{4}[\s\xa0]+\d{4}',
                r'"virtualAccountNumber"\s*:\s*"(\d{10,20})"',
                r'"vaNumber"\s*:\s*"(\d{10,20})"',
                r'"accountNumber"\s*:\s*"(\d{10,20})"',
                r'>(7\d{14,16})<',
                r'"(7\d{14,16})"',
            ]
            for pat in va_patterns:
                matches = re.findall(pat, page_text)
                if matches:
                    va_raw = matches[0].replace(" ", "").replace("\xa0", "").replace(">", "").replace("<", "")
                    va_from_page = _format_va(va_raw)
                    log(f"  VA dari HTML: {va_from_page}")
                    break
    except Exception as e:
        log(f"  GET confirm page error: {e}")

    return {
        "confirm_resp": conf_resp,
        "detail_resp": det_resp,
        "core_order_id": core_order_id,
        "core_order_hash": core_order_hash,
        "grand_total_str": grand_total_str,
        "confirm_page_url": confirm_page_url,
        "va_from_page": va_from_page,
        "total_from_page": total_from_page,
        "expired_from_page": expired_from_page,
        "redirect_from_page": redirect_from_page,
        "payment_method": "MYBCA" if is_mybca_only else "VA_BCA",
        "is_mybca_only": is_mybca_only,
    }


# ═══════════════════════════════════════════════════════════════
#  FITUR [3]: SCAN HIDDEN LINK URL (PENJUALAN)
# ═══════════════════════════════════════════════════════════════

TICKET_KEYWORDS = [
    "tiket.com", "loket.com", "eventbrite", "ticketmaster", "goersapp",
    "ticket", "tiket", "order", "buy", "purchase", "booking", "checkout",
    "packages", "register", "daftar", "beli", "cart", "payment",
    "seat", "admission", "presale", "onsale", "on-sale",
]

def extract_urls_from_html(html_text, base_url=""):
    """Extract semua URL dari HTML source."""
    urls = set()

    # href, src, action, data-url, content (meta)
    attr_patterns = [
        r'href\s*=\s*["\']([^"\']+)["\']',
        r'src\s*=\s*["\']([^"\']+)["\']',
        r'action\s*=\s*["\']([^"\']+)["\']',
        r'data-url\s*=\s*["\']([^"\']+)["\']',
        r'data-href\s*=\s*["\']([^"\']+)["\']',
        r'content\s*=\s*["\'](https?://[^"\']+)["\']',
    ]
    for pat in attr_patterns:
        for m in re.finditer(pat, html_text, re.IGNORECASE):
            urls.add(m.group(1))

    # URL in JavaScript strings
    js_patterns = [
        r'["\'](https?://[^"\'\\]{10,})["\']',
        r'url\s*[:=]\s*["\']([^"\']+)["\']',
        r'window\.location\s*=\s*["\']([^"\']+)["\']',
        r'window\.open\s*\(\s*["\']([^"\']+)["\']',
        r'redirect\s*[:=]\s*["\']([^"\']+)["\']',
    ]
    for pat in js_patterns:
        for m in re.finditer(pat, html_text, re.IGNORECASE):
            u = m.group(1)
            if u.startswith("http"):
                urls.add(u)

    # JSON-LD
    ld_matches = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html_text, re.DOTALL)
    for ld in ld_matches:
        for m in re.finditer(r'"(https?://[^"]+)"', ld):
            urls.add(m.group(1))

    # Resolve relative URLs
    resolved = set()
    for u in urls:
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/") and base_url:
            from urllib.parse import urljoin
            u = urljoin(base_url, u)
        if u.startswith("http"):
            resolved.add(u)
    return resolved


def is_ticket_url(url):
    """Cek apakah URL kemungkinan link penjualan tiket."""
    url_lower = url.lower()
    for kw in TICKET_KEYWORDS:
        if kw in url_lower:
            return True
    return False


def run_url_scanner():
    """[3] Scan hidden link URL penjualan — loop 0.1s, Ctrl+C untuk stop."""
    t0 = time.time()
    print("\n\033[96m═══ SCAN HIDDEN LINK URL (PENJUALAN) ═══\033[0m")
    print("Masukkan domain/URL yang mau di-scan.")
    print("Contoh: https://theweekndinjakarta.com/")
    url = input("\nURL: ").strip()
    if not url:
        print("URL kosong, batal.")
        return
    if not url.startswith("http"):
        url = "https://" + url

    print(f"\n\033[93mScanning: {url}\033[0m")
    print(f"Loop setiap {SCAN_INTERVAL}s — tekan \033[91mCtrl+C\033[0m untuk berhenti\n")

    session = create_simple_session()
    seen_urls = set()
    ticket_urls = set()
    scan_count = 0
    new_found_total = 0

    try:
        while True:
            scan_count += 1
            try:
                r = session.get(url, headers={
                    "user-agent": UA,
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "accept-language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
                    "cache-control": "no-cache",
                    "pragma": "no-cache",
                }, timeout=10)

                if r.status_code == 200:
                    found = extract_urls_from_html(r.text, url)
                    new_urls = found - seen_urls
                    if new_urls:
                        new_found_total += len(new_urls)
                        seen_urls.update(new_urls)
                        for u in sorted(new_urls):
                            is_ticket = is_ticket_url(u)
                            if is_ticket:
                                ticket_urls.add(u)
                                print(f"  \033[92m★ TIKET/SALE: {u}\033[0m")
                            else:
                                print(f"  \033[90m  link: {u}\033[0m")

                    # Status line
                    sys.stdout.write(f"\r\033[90m[scan #{scan_count}] total: {len(seen_urls)} link | tiket: {len(ticket_urls)} | waktu: {elapsed(t0)}s\033[0m")
                    sys.stdout.flush()
                else:
                    sys.stdout.write(f"\r\033[91m[scan #{scan_count}] HTTP {r.status_code} | waktu: {elapsed(t0)}s\033[0m")
                    sys.stdout.flush()

            except Exception as e:
                sys.stdout.write(f"\r\033[91m[scan #{scan_count}] Error: {str(e)[:50]} | waktu: {elapsed(t0)}s\033[0m")
                sys.stdout.flush()

            time.sleep(SCAN_INTERVAL)

    except KeyboardInterrupt:
        pass

    total_time = time.time() - t0
    print(f"\n\n\033[96m═══ HASIL SCAN ═══\033[0m")
    print(f"  Domain     : {url}")
    print(f"  Total scan : {scan_count}x")
    print(f"  Total link : {len(seen_urls)}")
    print(f"  Link tiket : {len(ticket_urls)}")
    if ticket_urls:
        print(f"\n  \033[92m★ LINK TIKET/PENJUALAN:\033[0m")
        for u in sorted(ticket_urls):
            print(f"    {u}")
    print(f"\n  \033[93mWaktu proses: {total_time:.2f} detik\033[0m\n")
    session.close()


# ═══════════════════════════════════════════════════════════════
#  FITUR [2]: CEK KUOTA TIKET LENGKAP
# ═══════════════════════════════════════════════════════════════

def run_kuota_checker():
    """[2] Cek kuota tiket lengkap untuk suatu event."""
    t0 = time.time()
    print("\n\033[96m═══ CEK KUOTA TIKET LENGKAP ═══\033[0m")
    event_url = ask_url(f"\nMasukkan URL event tiket.com\nDefault: {DEFAULT_URL}\n(tekan ENTER untuk default)")

    cookie_file = pick_cookie_file()
    cookies_raw = load_cookies(cookie_file)
    session, cookie_header = create_session(cookies_raw)

    device_id = None
    for ck in cookies_raw:
        if ck.get("name", "").lower() in ("deviceid", "device_id"):
            device_id = ck.get("value")
            break
    if not device_id:
        device_id = gen_id()

    product_url_slug = extract_product_url(event_url)
    request_id = gen_id()
    packages_referer = f"{BASE}/id-id/to-do/{product_url_slug}/packages"

    # GET product info
    log("Mengambil info produk...")
    h = gateway_headers(device_id, request_id, cookie_header, packages_referer)
    prod_resp = http_get_json(
        session,
        f"{GATEWAY}/tix-events-v2-inventory/v1/products/url/{product_url_slug}",
        h, label="PRODUCT INFO",
    )
    if not prod_resp or "data" not in prod_resp:
        log("\033[91mERROR: product info gagal!\033[0m")
        print(f"\n  \033[93mWaktu proses: {elapsed(t0)} detik\033[0m\n")
        return
    product_data = prod_resp["data"]
    product_id = product_data["id"]
    packages = product_data.get("packages", [])
    log(f"Product: {product_url_slug} (ID: {product_id})")

    # Available packages
    h = gateway_headers(device_id, request_id, cookie_header, packages_referer)
    h["x-product-id"] = product_id
    h["x-product-category"] = "EVENT"
    dt_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
    dt_to = (datetime.now() + timedelta(days=730)).strftime("%Y-%m-%d 23:59:59")
    avail_resp = http_get_json(
        session,
        f"{GATEWAY}/tix-events-v2-inventory/v1/productSchedules/availablePackages",
        h, params={"productId": product_id, "dateTimeFrom": dt_from, "dateTimeTo": dt_to},
        label="AVAILABLE",
    )
    available_codes = []
    if avail_resp:
        available = avail_resp.get("data", [])
        if available:
            available_codes = [str(c) for c in available[0].get("packageCodes", [])]

    # Fetch kuota semua paket — parallel
    log("Mengambil kuota semua paket (parallel)...")
    pkg_quota_map = {}

    def _fetch_pkg_quota(pkg):
        pkg_code = pkg.get("code", "")
        sh = gateway_headers(device_id, gen_id(), cookie_header, packages_referer)
        sh["x-product-id"] = product_id
        sh["x-product-category"] = "EVENT"
        sched_r = http_get_json(
            session,
            f"{GATEWAY}/tix-events-v2-inventory/v1/productSchedules",
            sh,
            params={
                "packageCode": pkg_code, "productId": product_id,
                "sortAttributes": "dateTime", "sortDirection": "ASC",
                "dateTimeFrom": dt_from, "dateTimeTo": dt_to,
                "pageSize": "1", "pageNumber": "1",
            },
            label=f"SCHED-{pkg_code}",
            timeout=10,
        )
        if sched_r:
            sched_list = sched_r.get("data", [])
            if sched_list:
                sid = sched_list[0].get("id", "")
                if sid:
                    dh = gateway_headers(device_id, gen_id(), cookie_header, packages_referer)
                    det_r = http_get_json(
                        session, f"{GATEWAY}/tix-events-v2-inventory/v1/productSchedules/{sid}",
                        dh, label=f"DETAIL-{pkg_code}", timeout=10,
                    )
                    if det_r:
                        dd = det_r.get("data", {})
                        return pkg_code, {
                            "quota": dd.get("quota", 0),
                            "booked": dd.get("booked", 0),
                            "available": dd.get("availability", 0),
                            "schedule_id": sid,
                        }
        return pkg_code, None

    with ThreadPoolExecutor(max_workers=min(len(packages), 20)) as executor:
        futs = {executor.submit(_fetch_pkg_quota, pkg): pkg for pkg in packages}
        for fut in as_completed(futs):
            try:
                pkg_code, result = fut.result()
                if result:
                    pkg_quota_map[pkg_code] = result
            except Exception as e:
                log(f"  fetch error: {e}")

    # Display
    total_time = time.time() - t0
    print()
    print("\033[97m\033[44m" + f" KUOTA TIKET: {product_url_slug} ".center(80) + "\033[0m")
    print("\033[90m" + "─" * 80 + "\033[0m")
    print(f"  {'No':>3}  {'Nama Paket':<35} {'Sisa':>6} {'Booked':>7} {'Kuota':>7} {'Harga':>15}  Status")
    print("\033[90m" + "─" * 80 + "\033[0m")

    for i, pkg in enumerate(packages, 1):
        pkg_name = ""
        pkg_price = 0
        pkg_code = pkg.get("code", "")
        pkg_code_str = str(pkg_code)
        for t in pkg.get("translations", []):
            if t.get("language") == "ID":
                pkg_name = t.get("name", "") or t.get("title", "")
                break
        if not pkg_name:
            for t in pkg.get("translations", []):
                pkg_name = t.get("name", "") or t.get("title", "")
                break
        for pt in pkg.get("priceTiers", []):
            pkg_price = pt.get("finalPrice", 0)
            if pkg_price:
                break
        price_str = f"Rp{pkg_price:,.0f}".replace(",", ".") if pkg_price else "N/A"
        q_info = pkg_quota_map.get(pkg_code, {})
        avail = q_info.get("available", "-")
        booked = q_info.get("booked", "-")
        quota = q_info.get("quota", "-")

        forced_habis = False
        if available_codes and pkg_code_str not in available_codes:
            forced_habis = True
        if pkg.get("isMarkedAsSoldOut") is True:
            forced_habis = True
        pkg_status = pkg.get("status", "") or pkg.get("productPackageStatus", "")
        if pkg_status and str(pkg_status).upper() in ("SOLD_OUT", "SOLDOUT", "INACTIVE", "UNAVAILABLE"):
            forced_habis = True
        if pkg.get("soldOut") is True or pkg.get("isSoldOut") is True:
            forced_habis = True
        if pkg.get("isAvailable") is False:
            forced_habis = True
        for pt in pkg.get("priceTiers", []):
            pt_avail = pt.get("availability", {})
            if isinstance(pt_avail, dict):
                if pt_avail.get("status", "").upper() in ("SOLD_OUT", "SOLDOUT", "UNAVAILABLE"):
                    forced_habis = True
                if pt_avail.get("soldOut") is True:
                    forced_habis = True

        if forced_habis:
            avail = 0

        if isinstance(avail, int) and avail <= 0:
            status_str = "\033[91m✘ HABIS\033[0m"
            avail_str = f"\033[91m{avail:>6}\033[0m"
        elif isinstance(avail, int) and avail <= 5:
            status_str = "\033[93m⚠ SEDIKIT\033[0m"
            avail_str = f"\033[93m{avail:>6}\033[0m"
        else:
            status_str = "\033[92m✔ ADA\033[0m"
            avail_str = f"\033[92m{str(avail):>6}\033[0m"

        pkg_name_short = pkg_name[:35] if len(pkg_name) > 35 else pkg_name
        print(f"  {i:>3}  {pkg_name_short:<35} {avail_str} {str(booked):>7} {str(quota):>7} {price_str:>15}  {status_str}")

    print("\033[90m" + "─" * 80 + "\033[0m")
    print(f"\n  \033[93mWaktu proses: {total_time:.2f} detik\033[0m\n")
    session.close()


# ═══════════════════════════════════════════════════════════════
#  FITUR [1]: AUTO ORDER TIKET
# ═══════════════════════════════════════════════════════════════

def run_auto_order():
    """[1] Auto order tiket — flow utama."""
    t0_global = time.time()

    global CAPSOLVER_KEY
    if not CAPSOLVER_KEY:
        log("WARNING: CAPSOLVER_KEY belum di-set!")
        key_input = input("\nMasukkan CapSolver API Key (atau ENTER untuk skip): ").strip()
        if key_input:
            CAPSOLVER_KEY = key_input
        else:
            log("ERROR: Tidak bisa lanjut tanpa CapSolver key!")
            return

    profiles = load_all_profiles()
    data = pick_profile(profiles)
    event_url = ask_url()

    try:
        qty = max(1, int(data.get("qty", "1")))
    except (ValueError, TypeError):
        qty = 1
    keyword = data.get("keyword", KEYWORD)
    keywords = [k.strip().upper() for k in keyword.split(",") if k.strip()]
    product_url_slug = extract_product_url(event_url)

    soldout_mode = data.get("soldout_mode", "loop").strip().lower()
    if soldout_mode not in ("loop", "random"):
        soldout_mode = "loop"

    print("\n" + "=" * 55 + "\n  PILIH MODE PAYMENT\n" + "=" * 55)
    print("  [1] Sampai VA muncul (full auto)")
    print("  [2] Stop di URL payment saja (pilih metode manual)")
    print("=" * 55)
    pm_input = input("Pilih (1-2, default 1): ").strip()
    payment_mode = "url" if pm_input == "2" else "va"

    # Extra collectors
    extra_collectors = data.get("extra_collectors", [])

    print()
    print("\033[93m" + "┌─ KONFIGURASI " + "─" * 44 + "\033[0m")
    print(f"\033[93m│\033[0m  Event      : {product_url_slug}")
    print(f"\033[93m│\033[0m  Keyword    : {', '.join(keywords)}")
    print(f"\033[93m│\033[0m  Jumlah     : {qty} tiket")
    print(f"\033[93m│\033[0m  Profil     : {data.get('nama', '?')} ({data.get('email', '?')})")
    if extra_collectors:
        for i, ec in enumerate(extra_collectors, 2):
            print(f"\033[93m│\033[0m  Pembeli {i}  : {ec.get('nama', '?')} ({ec.get('email', '?')})")
    print(f"\033[93m│\033[0m  Payment    : {'Sampai VA muncul' if payment_mode == 'va' else 'Stop di URL payment'}")
    print(f"\033[93m│\033[0m  Sold out   : {'Loop sampai muncul' if soldout_mode == 'loop' else 'Ambil random tersedia'}")
    print("\033[93m└" + "─" * 58 + "\033[0m")
    print()

    cookie_file = pick_cookie_file()
    cookies_raw = load_cookies(cookie_file)
    if not cookies_raw:
        log("\033[91mWARNING: cookies.json kosong!\033[0m")
    session, cookie_header = create_session(cookies_raw)

    device_id = None
    for ck in cookies_raw:
        if ck.get("name", "").lower() in ("deviceid", "device_id"):
            device_id = ck.get("value")
            break
    if not device_id:
        for name in ("deviceId", "device_id", "deviceid"):
            val = session.cookies.get(name)
            if val:
                device_id = val
                break
    if not device_id:
        device_id = gen_id()
    log(f"Device ID: {device_id[:8]}...")

    request_id = gen_id()
    packages_referer = f"{BASE}/id-id/to-do/{product_url_slug}/packages"
    order_referer = f"{BASE}/id-id/to-do/{product_url_slug}/order"

    # ── STEP 1: GET product info ──
    log("\033[36m[1/8]\033[0m Mengambil info produk...")
    h = gateway_headers(device_id, request_id, cookie_header, packages_referer)
    prod_resp = http_get_json(
        session,
        f"{GATEWAY}/tix-events-v2-inventory/v1/products/url/{product_url_slug}",
        h, label="PRODUCT INFO",
    )
    if not prod_resp or "data" not in prod_resp:
        log("ERROR: product info gagal!")
        return
    product_data = prod_resp["data"]
    product_id = product_data["id"]
    log(f"       Product ID: {product_id}")
    packages = product_data.get("packages", [])

    # ── STEP 2: GET available packages ──
    log("\033[36m[2/8]\033[0m Cek ketersediaan paket...")
    h = gateway_headers(device_id, request_id, cookie_header, packages_referer)
    h["x-product-id"] = product_id
    h["x-product-category"] = "EVENT"
    event_start = product_data.get("startDate", "") or product_data.get("dateTimeFrom", "")
    if not event_start:
        for pkg in packages:
            for s in pkg.get("schedules", []):
                sd = s.get("dateTime", "") or s.get("startDate", "")
                if sd:
                    event_start = sd
                    break
            if event_start:
                break
    dt_from_avail = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
    dt_to_avail = (datetime.now() + timedelta(days=730)).strftime("%Y-%m-%d 23:59:59")
    if event_start:
        try:
            es = event_start[:10]
            ed = datetime.strptime(es, "%Y-%m-%d")
            dt_from_avail = (ed - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
            dt_to_avail = (ed + timedelta(days=30)).strftime("%Y-%m-%d 23:59:59")
        except Exception:
            pass
    avail_resp = http_get_json(
        session,
        f"{GATEWAY}/tix-events-v2-inventory/v1/productSchedules/availablePackages",
        h,
        params={"productId": product_id, "dateTimeFrom": dt_from_avail, "dateTimeTo": dt_to_avail},
        label="AVAILABLE PACKAGES",
    )
    available_codes = []
    if avail_resp:
        available = avail_resp.get("data", [])
        if available:
            available_codes = [str(c) for c in available[0].get("packageCodes", [])]
            log(f"Available package codes: {available_codes}")

    dt_from = dt_from_avail
    dt_to = dt_to_avail

    def _fetch_one_pkg_quota(pkg):
        pkg_code = pkg.get("code", "")
        sh = gateway_headers(device_id, gen_id(), cookie_header, packages_referer)
        sh["x-product-id"] = product_id
        sh["x-product-category"] = "EVENT"
        sched_r = http_get_json(
            session,
            f"{GATEWAY}/tix-events-v2-inventory/v1/productSchedules",
            sh,
            params={
                "packageCode": pkg_code, "productId": product_id,
                "sortAttributes": "dateTime", "sortDirection": "ASC",
                "dateTimeFrom": dt_from, "dateTimeTo": dt_to,
                "pageSize": "1", "pageNumber": "1",
            },
            label=f"SCHED-{pkg_code}",
            timeout=10,
        )
        if sched_r:
            sched_list = sched_r.get("data", [])
            if sched_list:
                sid = sched_list[0].get("id", "")
                if sid:
                    dh = gateway_headers(device_id, gen_id(), cookie_header, packages_referer)
                    det_r = http_get_json(
                        session,
                        f"{GATEWAY}/tix-events-v2-inventory/v1/productSchedules/{sid}",
                        dh, label=f"DETAIL-{pkg_code}", timeout=10,
                    )
                    if det_r:
                        dd = det_r.get("data", {})
                        return pkg_code, {
                            "quota": dd.get("quota", 0),
                            "booked": dd.get("booked", 0),
                            "available": dd.get("availability", 0),
                            "schedule_id": sid,
                        }
        return pkg_code, None

    def fetch_all_quotas():
        qmap = {}
        with ThreadPoolExecutor(max_workers=min(len(packages), 20)) as executor:
            futures = {executor.submit(_fetch_one_pkg_quota, pkg): pkg for pkg in packages}
            for future in as_completed(futures):
                try:
                    pkg_code, result = future.result()
                    if result:
                        qmap[pkg_code] = result
                except Exception as e:
                    log(f"  fetch quota error: {e}")
        return qmap

    def display_and_match(pkg_quota_map):
        matches_by_kw = {kw: [] for kw in keywords}
        all_pkg_info = []
        print()
        print("\033[97m\033[44m" + " DAFTAR PAKET TERSEDIA ".center(82) + "\033[0m")
        print("\033[90m" + "─" * 82 + "\033[0m")
        print(f"\033[97m  {'No':>3}  {'Nama Paket':<35} {'Sisa':>6} {'Booked':>7} {'Kuota':>7} {'Harga':>13}  Status\033[0m")
        print("\033[90m" + "─" * 82 + "\033[0m")
        for i, pkg in enumerate(packages, 1):
            pkg_name = ""
            pkg_price = 0
            pkg_code = pkg.get("code", "")
            pkg_code_str = str(pkg_code)
            for t in pkg.get("translations", []):
                if t.get("language") == "ID":
                    pkg_name = t.get("name", "") or t.get("title", "")
                    break
            if not pkg_name:
                for t in pkg.get("translations", []):
                    pkg_name = t.get("name", "") or t.get("title", "")
                    break
            for pt in pkg.get("priceTiers", []):
                pkg_price = pt.get("finalPrice", 0)
                if pkg_price:
                    break
            price_str = f"Rp{pkg_price:,.0f}".replace(",", ".") if pkg_price else "N/A"
            q_info = pkg_quota_map.get(pkg_code, {})
            avail = q_info.get("available", "-")

            forced_habis = False
            if available_codes and pkg_code_str not in available_codes:
                forced_habis = True
            if pkg.get("isMarkedAsSoldOut") is True:
                forced_habis = True
            pkg_status = pkg.get("status", "") or pkg.get("productPackageStatus", "")
            if pkg_status and str(pkg_status).upper() in ("SOLD_OUT", "SOLDOUT", "INACTIVE", "UNAVAILABLE"):
                forced_habis = True
            if pkg.get("soldOut") is True or pkg.get("isSoldOut") is True:
                forced_habis = True
            if pkg.get("isAvailable") is False:
                forced_habis = True
            for pt in pkg.get("priceTiers", []):
                pt_avail = pt.get("availability", {})
                if isinstance(pt_avail, dict):
                    if pt_avail.get("status", "").upper() in ("SOLD_OUT", "SOLDOUT", "UNAVAILABLE"):
                        forced_habis = True
                    if pt_avail.get("soldOut") is True:
                        forced_habis = True

            if forced_habis:
                avail = 0

            booked = q_info.get("booked", "-")
            quota = q_info.get("quota", "-")
            if isinstance(avail, int) and avail <= 0:
                avail_str = f"\033[91m{avail:>6}\033[0m"
                status_tag = "\033[91m✘ HABIS\033[0m"
            elif isinstance(avail, int) and avail <= 5:
                avail_str = f"\033[93m{avail:>6}\033[0m"
                status_tag = "\033[93m⚠ SEDIKIT\033[0m"
            else:
                avail_str = f"\033[92m{str(avail):>6}\033[0m"
                status_tag = "\033[92m✔ ADA\033[0m"
            pkg_name_short = pkg_name[:35] if len(pkg_name) > 35 else pkg_name
            print(f"  {i:>3}  {pkg_name_short:<35} {avail_str} {str(booked):>7} {str(quota):>7} {price_str:>13}  {status_tag}")
            name_upper = pkg_name.upper()
            name_clean = re.sub(r'[^A-Z0-9\s]', ' ', name_upper)
            name_clean = re.sub(r'\s+', ' ', name_clean).strip()
            avail_count = avail if isinstance(avail, int) else 999
            all_pkg_info.append((pkg_code, pkg_price, pkg_name, avail_count))
            for kw in keywords:
                kw_clean = re.sub(r'[^A-Z0-9\s]', ' ', kw)
                kw_clean = re.sub(r'\s+', ' ', kw_clean).strip()
                if kw_clean in name_clean or kw in name_upper:
                    matches_by_kw[kw].append((pkg_code, pkg_price, name_upper, avail_count))
                    log(f"  -> Match '{kw}' -> {pkg_code} ({pkg_name})")
        print("\033[90m" + "─" * 82 + "\033[0m")
        return matches_by_kw, all_pkg_info

    # ── STEP 3: Match keyword + sold-out loop ──
    log("\033[36m[3/8]\033[0m Mencari paket sesuai keyword...")
    found_package_code = None
    found_price = 0
    pkg_quota_map = {}
    loop_count = 0
    MAX_SOLDOUT_LOOPS = 60

    while True:
        loop_count += 1
        pkg_quota_map = fetch_all_quotas()
        matches_by_kw, all_pkg_info = display_and_match(pkg_quota_map)

        for kw in keywords:
            kw_matches = matches_by_kw.get(kw, [])
            avail_kw = [(c, p, n, a) for c, p, n, a in kw_matches if a > 0]
            if avail_kw:
                found_package_code, found_price, _, _ = avail_kw[0]
                price_str = f"Rp{found_price:,.0f}".replace(",", ".") if found_price else "N/A"
                log(f"\033[92m✔ Paket terpilih (keyword '{kw}'): code={found_package_code}, Harga: {price_str}\033[0m")
                break

        if found_package_code:
            break

        if soldout_mode == "random":
            any_avail = [(c, p, n, a) for c, p, n, a in all_pkg_info if a > 0]
            if any_avail:
                found_package_code, found_price, picked_name, _ = any_avail[0]
                log(f"\033[93m⚠ Keyword habis, ambil random: {picked_name}\033[0m")
                break
            else:
                log(f"\033[93m⚠ Semua paket habis, looping... ({loop_count}/{MAX_SOLDOUT_LOOPS})\033[0m")
        else:
            log(f"\033[93m⚠ Keyword habis semua, looping... ({loop_count}/{MAX_SOLDOUT_LOOPS})\033[0m")

        if loop_count >= MAX_SOLDOUT_LOOPS:
            log("\033[91m✘ Max loop tercapai\033[0m")
            any_avail = [(c, p, n, a) for c, p, n, a in all_pkg_info if a > 0]
            if any_avail:
                found_package_code, found_price, _, _ = any_avail[0]
            else:
                for kw in keywords:
                    if matches_by_kw.get(kw):
                        found_package_code, found_price, _, _ = matches_by_kw[kw][0]
                        break
            break
        time.sleep(3)

    if not found_package_code:
        if available_codes:
            found_package_code = available_codes[0]
        elif packages:
            found_package_code = packages[0].get("code", "1")
        else:
            found_package_code = "1"
        log(f"\033[93m⚠ Keyword tidak cocok, pakai paket pertama: code={found_package_code}\033[0m")

    # ── STEP 4: Schedule ──
    log(f"\033[36m[4/8]\033[0m Mengambil jadwal (paket #{found_package_code})...")
    cached_q = pkg_quota_map.get(found_package_code, {})
    schedule_id = cached_q.get("schedule_id", "")
    if schedule_id:
        log(f"       Schedule OK")
        sel_avail = cached_q.get("available", 0)
    else:
        h = gateway_headers(device_id, request_id, cookie_header, packages_referer)
        h["x-product-id"] = product_id
        h["x-product-category"] = "EVENT"
        sched_resp = http_get_json(
            session,
            f"{GATEWAY}/tix-events-v2-inventory/v1/productSchedules",
            h,
            params={
                "packageCode": found_package_code, "productId": product_id,
                "sortAttributes": "dateTime", "sortDirection": "ASC",
                "dateTimeFrom": dt_from, "dateTimeTo": dt_to,
                "pageSize": "1", "pageNumber": "1",
            },
            label="SCHEDULES",
        )
        if not sched_resp or not sched_resp.get("data"):
            log("ERROR: Gagal get schedules!")
            return
        schedule_id = sched_resp["data"][0]["id"]
        sel_avail = 0

    # ── STEP 5: Harga ──
    log("\033[36m[5/8]\033[0m Konfirmasi harga...")
    h = gateway_headers(device_id, request_id, cookie_header, packages_referer)
    labels_resp = http_get_json(
        session,
        f"{GATEWAY}/tix-events-v2-inventory/v1/products/additional-labels",
        h,
        params={"ids": product_id, "funnel": "PDP", "productUrl": product_url_slug},
        label="LABELS",
    )
    final_price = 0
    if labels_resp:
        for pl in labels_resp.get("data", {}).get("products", []):
            for pk in pl.get("packages", []):
                if pk.get("code") == found_package_code:
                    final_price = pk.get("startingFinalPrice", 0)
                    break
            if final_price:
                break
    if not final_price:
        for pkg in packages:
            if pkg.get("code") == found_package_code:
                for pt in pkg.get("priceTiers", []):
                    final_price = pt.get("finalPrice", 0)
                    if final_price:
                        break
            if final_price:
                break
    if not final_price:
        final_price = found_price
    price_str = f"Rp{final_price:,.0f}".replace(",", ".")
    log(f"       Harga per tiket: \033[93m{price_str}\033[0m")

    # ── STEP 6: Order page + detect sitekey ──
    log("\033[36m[6/8]\033[0m Menyiapkan halaman order...")
    order_page_url = f"{BASE}/id-id/to-do/{product_url_slug}/order"
    detected_sitekey = None
    detected_action = None

    inspect_file = "tiket_inspect_output.json"
    if os.path.exists(inspect_file):
        try:
            with open(inspect_file, encoding="utf-8") as f:
                inspect_out = json.load(f)
            insp = inspect_out.get("detected", {}) if isinstance(inspect_out, dict) else {}
            if insp.get("sitekey"):
                detected_sitekey = insp["sitekey"]
            if insp.get("action"):
                detected_action = insp["action"]
        except Exception:
            pass

    try:
        r = session.get(order_page_url, headers={
            "user-agent": UA, "referer": packages_referer,
            "sec-ch-ua": SEC_CH_UA, "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "cookie": cookie_header,
        }, timeout=10)
        cookie_header = refresh_cookie_header(session, cookies_raw, cookie_header)
        if r.status_code == 200:
            html = r.text
            if not detected_sitekey:
                sk = detect_sitekey_from_html(html)
                if sk:
                    detected_sitekey = sk
            if not detected_action:
                act = detect_action_from_html(html)
                if act:
                    detected_action = act
    except Exception as e:
        log(f"GET order page error: {e}")

    # ── STEP 7: Prepare order data ──
    log("\033[36m[7/8]\033[0m Menyiapkan data pesanan...")
    nama = data.get("nama", "aldi")
    email = data.get("email", "")
    phone = data.get("phone", "")
    ktp = data.get("ktp", "")
    sal_raw = data.get("salutation", "Mr")
    sal_map = {"tuan": "Mr", "nyonya": "Mrs", "nona": "Ms", "mr": "Mr", "mrs": "Mrs", "ms": "Ms"}
    salutation = sal_map.get(sal_raw.lower(), "Mr") if sal_raw else "Mr"
    phone_clean = re.sub(r'^(\+62|62|0)', '', phone.strip())

    jwt_email = None
    for ck in cookies_raw:
        if ck.get("name") == "session_access_token":
            try:
                p = ck["value"].split(".")
                if len(p) >= 2:
                    b = p[1]
                    b += "=" * (4 - len(b) % 4)
                    jwt_email = json.loads(base64.urlsafe_b64decode(b)).get("email", "")
            except Exception:
                pass
            break
    if jwt_email and email and jwt_email.lower() != email.lower():
        log(f"WARNING: email data ({email}) != login ({jwt_email}) -> pakai login")
        email = jwt_email
    elif jwt_email and not email:
        email = jwt_email

    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Build collectors — support multi-data pembeli
    collectors = []
    all_buyers = [data] + extra_collectors
    for i in range(qty):
        buyer = all_buyers[i] if i < len(all_buyers) else all_buyers[0]
        b_nama = buyer.get("nama", nama)
        b_email = buyer.get("email", email)
        b_phone = re.sub(r'^(\+62|62|0)', '', buyer.get("phone", phone).strip())
        b_ktp = buyer.get("ktp", ktp)
        b_sal_raw = buyer.get("salutation", "Mr")
        b_salutation = sal_map.get(b_sal_raw.lower(), "Mr") if b_sal_raw else "Mr"
        collectors.append({
            "profileId": None,
            "collectorCode": i + 1,
            "priceTierCode": "ALL",
            "salutation": b_salutation,
            "firstName": b_nama,
            "lastName": b_nama,
            "emailAddress": b_email,
            "countryCode": "+62",
            "phoneNumber": b_phone,
            "dateOfBirth": None,
            "personalId": b_ktp,
            "nationality": "",
            "passportNumber": None,
            "passportIssuanceDate": None,
            "passportExpiredDate": None,
            "passportIssuingCountry": "",
        })

    order_payload = {
        "contact": {
            "salutation": salutation,
            "firstName": nama,
            "lastName": nama,
            "emailAddress": email,
            "countryCode": "+62",
            "phoneNumber": phone_clean,
            "nationality": "Indonesia",
        },
        "collector": collectors[0],
        "collectors": collectors,
        "priceTierQuantities": [{
            "code": "ALL",
            "quantity": qty,
            "freeQuantity": 0,
            "lastPrice": {"finalPrice": final_price, "insurances": []},
        }],
        "productScheduleId": schedule_id,
        "lastPrice": {"scale": 0, "currency": "IDR", "timestamp": ts_now},
        "captchaToken": "",
    }
    log(f"       Pembeli: {nama} | {email} | +62{phone_clean}")
    if len(collectors) > 1:
        for i, c in enumerate(collectors[1:], 2):
            log(f"       Pembeli {i}: {c['firstName']} | {c['emailAddress']}")

    # ── STEP 8: Create order ──
    MAX_SOLDOUT_ORDER_RETRIES = 30
    for soldout_retry in range(1, MAX_SOLDOUT_ORDER_RETRIES + 1):
        log("\033[36m[8/8]\033[0m \033[1mMembuat pesanan...\033[0m")
        h = gateway_headers(device_id, request_id, cookie_header, order_referer)
        order_result = create_order_http(
            session=session, order_payload=order_payload, gateway_h=h,
            product_id=product_id, device_id=device_id,
            cookie_header=cookie_header, order_page_url=order_page_url,
            sitekey=detected_sitekey, action=detected_action,
        )
        if not order_result:
            log("\033[91m✘ GAGAL: Order tidak berhasil dibuat!\033[0m")
            total_time = time.time() - t0_global
            print(f"\n  \033[93mWaktu proses: {total_time:.2f} detik\033[0m\n")
            return

        if isinstance(order_result, dict) and order_result.get("sold_out"):
            if soldout_retry >= MAX_SOLDOUT_ORDER_RETRIES:
                log("\033[91m✘ Max sold-out retry tercapai!\033[0m")
                total_time = time.time() - t0_global
                print(f"\n  \033[93mWaktu proses: {total_time:.2f} detik\033[0m\n")
                return
            log(f"\033[93m       Paket habis! Loop kembali cek paket... ({soldout_retry}/{MAX_SOLDOUT_ORDER_RETRIES})\033[0m")
            time.sleep(2)
            pkg_quota_map = fetch_all_quotas()
            matches_by_kw, all_pkg_info = display_and_match(pkg_quota_map)
            new_pkg = None
            new_price = 0
            for kw in keywords:
                kw_matches = matches_by_kw.get(kw, [])
                avail_kw = [(c, p, n, a) for c, p, n, a in kw_matches if a > 0]
                if avail_kw:
                    new_pkg, new_price, _, _ = avail_kw[0]
                    log(f"\033[92m✔ Paket baru (keyword '{kw}'): code={new_pkg}\033[0m")
                    break
            if not new_pkg and soldout_mode == "random":
                any_avail = [(c, p, n, a) for c, p, n, a in all_pkg_info if a > 0]
                if any_avail:
                    new_pkg, new_price, pn, _ = any_avail[0]
                    log(f"\033[93m⚠ Random fallback: {pn}\033[0m")
            if not new_pkg:
                log(f"\033[93m⚠ Masih habis semua, retry...\033[0m")
                continue
            found_package_code = new_pkg
            found_price = new_price
            cached_q = pkg_quota_map.get(found_package_code, {})
            schedule_id = cached_q.get("schedule_id", "")
            if schedule_id:
                order_payload["productScheduleId"] = schedule_id
                order_payload["priceTierQuantities"][0]["lastPrice"]["finalPrice"] = new_price
            continue

        if "error" in order_result:
            log("\033[91m✘ GAGAL: Order error\033[0m")
            total_time = time.time() - t0_global
            print(f"\n  \033[93mWaktu proses: {total_time:.2f} detik\033[0m\n")
            return
        break

    core_order_id = order_result["core_order_id"]
    core_order_hash = order_result["core_order_hash"]

    # Nama paket
    pkg_display_name = ""
    for pkg in packages:
        if pkg.get("code") == found_package_code:
            for t in pkg.get("translations", []):
                if t.get("language") == "ID":
                    pkg_display_name = t.get("name", "") or t.get("title", "")
                    break
            if not pkg_display_name:
                for t in pkg.get("translations", []):
                    pkg_display_name = t.get("name", "") or t.get("title", "")
                    break
            break
    if not pkg_display_name:
        pkg_display_name = f"Paket #{found_package_code}"

    if payment_mode == "url":
        pay_url = f"{BASE}/id-id/payment?order_id={core_order_id}&order_hash={core_order_hash}"
        calc_total = f"Rp{final_price * qty:,.0f}".replace(",", ".")
        total_time = time.time() - t0_global
        print()
        print("\033[92m╔══════════════════════════════════════════════════════════════════╗")
        print("║  ✔ PESANAN BERHASIL!                                             ║")
        print("╠══════════════════════════════════════════════════════════════════╣")
        print(f"║  Nama         : {nama:<49}║")
        print(f"║  Email        : {email:<49}║")
        print(f"║  Tiket        : {pkg_display_name:<49}║")
        print(f"║  Jumlah       : {qty} tiket{' ' * (43 - len(str(qty)))}║")
        print("╠══════════════════════════════════════════════════════════════════╣")
        print(f"║  Total        : \033[97m\033[1m{calc_total}\033[0m\033[92m{' ' * max(0, 49 - len(calc_total))}║")
        print(f"║  Order ID     : {core_order_id:<49}║")
        print("╠══════════════════════════════════════════════════════════════════╣")
        print(f"║  \033[93mMode: Stop di URL Payment\033[0m\033[92m{' ' * 41}║")
        print(f"║  URL Payment:                                                    ║")
        print(f"║  \033[97m{pay_url}\033[0m\033[92m")
        print("╠══════════════════════════════════════════════════════════════════╣")
        print(f"║  Waktu proses : {total_time:.2f} detik{' ' * max(0, 42 - len(f'{total_time:.2f} detik'))}║")
        print("╚══════════════════════════════════════════════════════════════════╝\033[0m")
        print()
        return

    # ── PAYMENT ──
    log("\033[36m[PAYMENT]\033[0m Memproses pembayaran BCA VA...")
    payment_result = process_payment_http(
        session=session, core_order_id=core_order_id,
        core_order_hash=core_order_hash,
        order_request_id=order_result["order_request_id"],
        device_id=device_id, cookie_header=cookie_header,
    )

    total_time = time.time() - t0_global

    if not payment_result:
        pay_url = f"{BASE}/id-id/payment?order_id={core_order_id}&order_hash={core_order_hash}"
        log(f"\033[93m⚠ Payment otomatis gagal, silakan bayar manual:\033[0m")
        print(f"\n  \033[93mURL: {pay_url}\033[0m")
        print(f"\n  \033[93mWaktu proses: {total_time:.2f} detik\033[0m\n")
        return

    confirm_resp = payment_result["confirm_resp"]
    grand_total_str = payment_result["grand_total_str"]
    total_amount = grand_total_str

    conf_data = confirm_resp.get("data") or {}
    conf_total = (conf_data.get("sidebarPayment") or {}).get("grandTotalString", "")
    if conf_total:
        total_amount = conf_total
    if total_amount == "?" and payment_result.get("total_from_page"):
        total_amount = payment_result["total_from_page"]
    if total_amount == "?" and final_price > 0:
        calc_total = final_price * qty
        total_amount = f"Rp{calc_total:,.0f}".replace(",", ".")

    used_method = payment_result.get("payment_method", "VA_BCA")
    confirm_page_url = payment_result.get("confirm_page_url", "")
    if confirm_page_url:
        pay_url = confirm_page_url
    elif used_method == "MYBCA":
        pay_url = f"{BASE}/id-id/payment?order_id={core_order_id}&order_hash={core_order_hash}"
    else:
        pay_url = f"{BASE}/id-id/payment/va_bca/confirm?order_id={core_order_id}&order_hash={core_order_hash}"

    expired_str = _extract_expired(confirm_resp)
    if not expired_str:
        expired_str = _extract_expired(payment_result.get("detail_resp") or {})
    if not expired_str and payment_result.get("expired_from_page"):
        exp_raw = payment_result["expired_from_page"]
        expired_str = exp_raw + " WIB" if "WIB" not in exp_raw else exp_raw

    redirect_url = _extract_redirect_url(confirm_resp)
    if not redirect_url:
        redirect_url = _extract_redirect_url(payment_result.get("detail_resp") or {})
    if not redirect_url and payment_result.get("redirect_from_page"):
        redirect_url = payment_result["redirect_from_page"]

    va_number = ""
    if used_method == "VA_BCA":
        va_number = payment_result.get("va_from_page", "")

    method_display = "VA BCA" if used_method == "VA_BCA" else "myBCA"

    print()
    print("\033[92m╔══════════════════════════════════════════════════════════════════╗")
    print("║  ✔ PESANAN BERHASIL!                                             ║")
    print("╠══════════════════════════════════════════════════════════════════╣")
    print(f"║  Nama         : {nama:<49}║")
    print(f"║  Email        : {email:<49}║")
    print(f"║  Tiket        : {pkg_display_name:<49}║")
    print(f"║  Jumlah       : {qty} tiket{' ' * (43 - len(str(qty)))}║")
    print("╠══════════════════════════════════════════════════════════════════╣")
    print(f"║  Metode       : \033[97m\033[1m{method_display}\033[0m\033[92m{' ' * max(0, 49 - len(method_display))}║")
    is_mybca_only = payment_result.get("is_mybca_only", False)
    if va_number and not is_mybca_only:
        print(f"║  No. VA BCA   : \033[97m\033[1m{va_number}\033[0m\033[92m{' ' * max(0, 49 - len(va_number))}║")
    elif not is_mybca_only:
        va_hint = "Lihat di URL Payment"
        print(f"║  No. VA BCA   : \033[93m{va_hint}\033[0m\033[92m{' ' * max(0, 49 - len(va_hint))}║")
    print(f"║  Total Bayar  : \033[97m\033[1m{total_amount}\033[0m\033[92m{' ' * max(0, 49 - len(str(total_amount)))}║")
    print(f"║  Order ID     : {core_order_id:<49}║")
    if expired_str:
        print(f"║  Batas Bayar  : \033[93m{expired_str}\033[0m\033[92m{' ' * max(0, 49 - len(expired_str))}║")
    print("╠══════════════════════════════════════════════════════════════════╣")
    print(f"║  URL Payment:                                                    ║")
    print(f"║  \033[97m{pay_url}\033[0m\033[92m")
    if redirect_url:
        print(f"║                                                                  ║")
        print(f"║  Link myBCA:                                                     ║")
        print(f"║  \033[97m{redirect_url}\033[0m\033[92m")
    print("╠══════════════════════════════════════════════════════════════════╣")
    print(f"║  Waktu proses : {total_time:.2f} detik{' ' * max(0, 42 - len(f'{total_time:.2f} detik'))}║")
    print("╚══════════════════════════════════════════════════════════════════╝\033[0m")
    print()


# ═══════════════════════════════════════════════════════════════
#  MAIN MENU
# ═══════════════════════════════════════════════════════════════

def main():
    print()
    print("\033[96m" + "╔" + "═" * 58 + "╗")
    print("║" + " " * 58 + "║")
    print("║" + "  TIKET.COM AUTO-ORDER BOT v10.0".center(58) + "║")
    print("║" + "  HTTP Engine + CapSolver reCAPTCHA".center(58) + "║")
    print("║" + " " * 58 + "║")
    print("╚" + "═" * 58 + "╝" + "\033[0m")
    print()

    while True:
        print("\033[97m" + "=" * 50)
        print("  PILIH FITUR:")
        print("=" * 50 + "\033[0m")
        print("  \033[92m[1]\033[0m Auto Order Tiket")
        print("  \033[92m[2]\033[0m Cek Kuota Tiket Lengkap")
        print("  \033[92m[3]\033[0m Scan Hidden Link URL (Penjualan)")
        print("  \033[91m[0]\033[0m Keluar")
        print("\033[97m" + "=" * 50 + "\033[0m")

        choice = input("\nPilih fitur (0-3): ").strip()

        if choice == "1":
            run_auto_order()
        elif choice == "2":
            run_kuota_checker()
        elif choice == "3":
            run_url_scanner()
        elif choice == "0":
            print("\nBye!\n")
            break
        else:
            print("\033[91mPilihan tidak valid!\033[0m")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\nDihentikan (Ctrl+C)")
    except Exception as e:
        log(f"ERROR: {type(e).__name__} -> {e}")
        import traceback
        traceback.print_exc()
    finally:
        input("\nTekan ENTER untuk keluar...")
