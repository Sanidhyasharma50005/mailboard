import os
import sys
import re
import html
import time
import socket
import logging
import sqlite3
import imaplib
import email
from email.header import decode_header, make_header
import email.utils
from pathlib import Path
import threading
import urllib.request
import urllib.parse
import json
import webbrowser

# Fallback .env support for developers
from dotenv import load_dotenv
load_dotenv()

# Keyring for Windows Credential Manager
try:
    import keyring
except ImportError:
    keyring = None

def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller"""
    try:
        base_path = sys._MEIPASS
    except AttributeError:
        base_path = os.path.abspath(os.path.dirname(__file__))
    return os.path.join(base_path, relative_path)

IS_VERCEL = bool(os.getenv("VERCEL") or os.getenv("VERCEL_ENV"))

def get_data_dir():
    """Store database and logs in /tmp/mailboard on Vercel, or %APPDATA%\\Mailboard locally"""
    if IS_VERCEL:
        data_dir = Path("/tmp/mailboard")
    else:
        appdata = os.getenv("APPDATA")
        if appdata:
            data_dir = Path(appdata) / "Mailboard"
        else:
            data_dir = Path.home() / ".mailboard"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir

DATA_DIR = get_data_dir()
DB_PATH = DATA_DIR / "mailboard.db"
LOG_PATH = DATA_DIR / "mailboard.log"

# Windowed exe stdout/stderr redirection
if sys.stdout is None or sys.stderr is None:
    try:
        log_fp = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = log_fp
        if sys.stderr is None:
            sys.stderr = log_fp
    except Exception:
        pass

if IS_VERCEL:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )
else:
    logging.basicConfig(
        filename=str(LOG_PATH),
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

from flask import Flask, jsonify, request, render_template, session

template_folder = resource_path("templates")
app = Flask(__name__, template_folder=template_folder)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or "mailboard-secret-key-2026"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

CATEGORIES = ["Urgent", "Important", "Updates", "Promotions", "Spam", "Other"]

SPAM_KEYWORDS = [
    "winner", "you have won", "free money", "click here", "claim your prize",
    "lottery", "act now", "100% free", "crypto giveaway"
]

URGENT_KEYWORDS = [
    "urgent", "asap", "immediately", "action required", "final notice",
    "deadline today", "emergency", "right away"
]

IMPORTANT_KEYWORDS = [
    "invoice", "payment due", "interview", "offer letter", "deadline",
    "approve", "approval", "contract", "meeting", "exam", "result",
    "assignment", "please confirm", "important"
]

PROMOTIONS_KEYWORDS = [
    "sale", "% off", "discount", "offer", "deal", "coupon",
    "unsubscribe", "newsletter"
]

UPDATES_KEYWORDS = [
    "otp", "verification code", "your order", "shipped", "delivered",
    "receipt", "security alert", "password reset", "login"
]

UPDATES_PREFIXES = [
    "noreply", "no-reply", "notifications", "alerts", "support", "info"
]

# Track in-session disconnect override
session_disconnected = False

def get_saved_credentials():
    global session_disconnected
    if session_disconnected and not IS_VERCEL:
        return None, None

    # 0. Web / Vercel session cookie credentials
    try:
        sess_addr = session.get("gmail_addr")
        sess_pass = session.get("gmail_pass")
        if sess_addr and sess_pass:
            return sess_addr, sess_pass
    except RuntimeError:
        pass

    # 1. Check Windows Credential Manager via keyring (local desktop only)
    if keyring and not IS_VERCEL:
        try:
            active_addr = keyring.get_password("Mailboard", "active_address")
            if active_addr:
                pwd = keyring.get_password("Mailboard", active_addr)
                if pwd:
                    return active_addr, pwd
        except Exception as e:
            logging.warning("Keyring read error: %s", type(e).__name__)

    # 2. Developer fallback: .env
    env_addr = os.getenv("GMAIL_ADDRESS", "").strip()
    env_pass = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    if env_addr and env_pass:
        return env_addr, env_pass

    return None, None

def save_credentials(address, password):
    global session_disconnected
    session_disconnected = False
    try:
        session["gmail_addr"] = address
        session["gmail_pass"] = password
    except RuntimeError:
        pass
    if keyring and not IS_VERCEL:
        try:
            keyring.set_password("Mailboard", "active_address", address)
            keyring.set_password("Mailboard", address, password)
            return True
        except Exception as e:
            logging.error("Keyring save error: %s", type(e).__name__)
            return False
    return True

def delete_credentials():
    global session_disconnected
    session_disconnected = True
    try:
        session.pop("gmail_addr", None)
        session.pop("gmail_pass", None)
    except RuntimeError:
        pass
    if keyring and not IS_VERCEL:
        try:
            active_addr = keyring.get_password("Mailboard", "active_address")
            if active_addr:
                try:
                    keyring.delete_password("Mailboard", active_addr)
                except Exception:
                    pass
            try:
                keyring.delete_password("Mailboard", "active_address")
            except Exception:
                pass
        except Exception as e:
            logging.error("Keyring delete error: %s", type(e).__name__)

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS sender_rules (
                sender TEXT PRIMARY KEY,
                category TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS mail_overrides (
                message_id TEXT PRIMARY KEY,
                category TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS custom_mails (
                id TEXT PRIMARY KEY,
                sender_name TEXT NOT NULL,
                sender_addr TEXT NOT NULL,
                subject TEXT NOT NULL,
                preview TEXT NOT NULL,
                date TEXT NOT NULL,
                has_unsubscribe INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()

init_db()

def get_custom_mails():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT id, sender_name, sender_addr, subject, preview, date, has_unsubscribe FROM custom_mails ORDER BY rowid DESC")
        rows = c.fetchall()
    return [
        {
            "id": row[0],
            "sender_name": row[1],
            "sender_addr": row[2],
            "subject": row[3],
            "preview": row[4],
            "date": row[5],
            "has_unsubscribe": bool(row[6]),
        }
        for row in rows
    ]

def get_db_rules():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT message_id, category FROM mail_overrides")
        overrides = {row[0]: row[1] for row in c.fetchall()}
        c.execute("SELECT sender, category FROM sender_rules")
        sender_rules = {row[0]: row[1] for row in c.fetchall()}
    return overrides, sender_rules

# In-memory email cache
email_cache = {
    "timestamp": 0,
    "raw_mails": [],
    "mode": "DEMO",
    "error": None
}

def decode_mime_header(val):
    if not val:
        return ""
    try:
        return str(make_header(decode_header(val)))
    except Exception:
        return str(val)

def match_phrase(kw, text):
    left = r'\b' if (kw[0].isalnum() or kw[0] == '_') else ''
    right = r'\b' if (kw[-1].isalnum() or kw[-1] == '_') else ''
    pattern = left + re.escape(kw) + right
    return bool(re.search(pattern, text, re.IGNORECASE))

def find_matched_keyword(text, keywords):
    for kw in keywords:
        if match_phrase(kw, text):
            return kw
    return None

def classify_email(email_data, overrides, sender_rules):
    msg_id = email_data["id"]
    sender_addr = email_data.get("sender_addr", "").lower().strip()
    subject = email_data.get("subject", "")
    preview = email_data.get("preview", "")
    has_unsub = email_data.get("has_unsubscribe", False)
    search_text = f"{subject} {preview}"

    # 1. Message override
    if msg_id in overrides:
        return overrides[msg_id], "you moved it"

    # 2. Sender memory
    if sender_addr in sender_rules:
        return sender_rules[sender_addr], "remembered sender"

    # 3. Spam keywords
    kw = find_matched_keyword(search_text, SPAM_KEYWORDS)
    if kw:
        return "Spam", f"matched: {kw}"

    # 4. Urgent keywords
    kw = find_matched_keyword(search_text, URGENT_KEYWORDS)
    if kw:
        return "Urgent", f"matched: {kw}"

    # 5. Important keywords
    kw = find_matched_keyword(search_text, IMPORTANT_KEYWORDS)
    if kw:
        return "Important", f"matched: {kw}"

    # 6. Promotions: List-Unsubscribe header present OR keywords
    if has_unsub:
        return "Promotions", "has unsubscribe link"
    kw = find_matched_keyword(search_text, PROMOTIONS_KEYWORDS)
    if kw:
        return "Promotions", f"matched: {kw}"

    # 7. Updates: sender address starts with noreply/etc. OR keywords
    for prefix in UPDATES_PREFIXES:
        if sender_addr.startswith(prefix):
            return "Updates", f"matched: {prefix}"
    kw = find_matched_keyword(search_text, UPDATES_KEYWORDS)
    if kw:
        return "Updates", f"matched: {kw}"

    # 8. Otherwise Other
    return "Other", "no rule matched"

def get_demo_emails():
    return [
        {
            "id": "demo-msg-01",
            "sender_name": "Marcus Vance",
            "sender_addr": "mvance@acmecorp.com",
            "subject": "URGENT: Production outage on payment gateway",
            "preview": "Please jump on the bridge immediately. Production transactions are failing and we need all hands right away.",
            "date": "Today, 10:14 AM",
            "has_unsubscribe": False
        },
        {
            "id": "demo-msg-02",
            "sender_name": "Cloud Hosting Billing",
            "sender_addr": "billing@cloudservices.io",
            "subject": "Invoice #INV-2026-9921 Payment Due in 48 Hours",
            "preview": "Your monthly cloud infrastructure invoice of $428.50 is now available. Payment due by midnight on November 25.",
            "date": "Today, 09:30 AM",
            "has_unsubscribe": False
        },
        {
            "id": "demo-msg-03",
            "sender_name": "Stripe Security",
            "sender_addr": "alerts@stripe.com",
            "subject": "Your OTP verification code for login",
            "preview": "Your one-time verification code is 849201. Use this code to complete your dashboard login safely.",
            "date": "Today, 09:12 AM",
            "has_unsubscribe": False
        },
        {
            "id": "demo-msg-04",
            "sender_name": "MegaDeals Direct",
            "sender_addr": "deals@shoppingoutlet.com",
            "subject": "Black Friday Early Access: 50% off all electronics!",
            "preview": "Massive holiday season sale starting now. Grab up to 50% off laptops, headphones, and 4K monitors before stock runs out.",
            "date": "Today, 08:45 AM",
            "has_unsubscribe": True
        },
        {
            "id": "demo-msg-05",
            "sender_name": "Global Millions Bureau",
            "sender_addr": "claim@free-prize-notify.org",
            "subject": "YOU ARE A WINNER! Claim your prize now",
            "preview": "Congratulations! You have won $2,500,000 in the international rewards sweepstakes. Click here to claim your prize.",
            "date": "Today, 08:05 AM",
            "has_unsubscribe": False
        },
        {
            "id": "demo-msg-06",
            "sender_name": "Alex Miller",
            "sender_addr": "alex.miller@gmail.com",
            "subject": "Weekend hiking trip at Blue Ridge",
            "preview": "Hey! Are you still free this Saturday to go hiking? Let me know if you want to grab breakfast beforehand.",
            "date": "Yesterday, 07:22 PM",
            "has_unsubscribe": False
        },
        {
            "id": "demo-msg-07",
            "sender_name": "Alex Miller",
            "sender_addr": "alex.miller@gmail.com",
            "subject": "Photos from last week's concert",
            "preview": "Here are the photos from Friday night! Let me know if you want me to share the high-resolution album.",
            "date": "Yesterday, 07:25 PM",
            "has_unsubscribe": False
        },
        {
            "id": "demo-msg-08",
            "sender_name": "Tech Recruiting",
            "sender_addr": "talent@deepai-research.com",
            "subject": "Interview invitation: Senior AI Systems Architect",
            "preview": "We were very impressed by your background and would love to schedule an interview with our engineering leadership team.",
            "date": "Yesterday, 05:40 PM",
            "has_unsubscribe": False
        },
        {
            "id": "demo-msg-09",
            "sender_name": "Amazon Shipping",
            "sender_addr": "shipment-tracking@amazon.com",
            "subject": "Your order #112-9831201 has shipped",
            "preview": "Good news! Your order containing Ergonomic Desk Chair has shipped and is scheduled to be delivered tomorrow by 8 PM.",
            "date": "Yesterday, 04:15 PM",
            "has_unsubscribe": False
        },
        {
            "id": "demo-msg-10",
            "sender_name": "GitHub",
            "sender_addr": "notifications@github.com",
            "subject": "[GitHub] Pull Request #42: Add redis cache layer",
            "preview": "octocat merged commit 7a8f12 into main. All 18 automated continuous integration checks passed successfully.",
            "date": "Yesterday, 03:00 PM",
            "has_unsubscribe": False
        },
        {
            "id": "demo-msg-11",
            "sender_name": "Academic Affairs",
            "sender_addr": "registrar@stateuniversity.edu",
            "subject": "Final Exam schedule and grade submission deadline",
            "preview": "Please review the published semester exam timetable. Final grade assignment submission deadline is December 15.",
            "date": "Yesterday, 01:20 PM",
            "has_unsubscribe": False
        },
        {
            "id": "demo-msg-12",
            "sender_name": "Crypto Rewards Hub",
            "sender_addr": "airdrop@crypto-vault-free.io",
            "subject": "Exclusive crypto giveaway: Free money waiting in your wallet",
            "preview": "Act now! Claim 5.0 ETH in our limited crypto giveaway. 100% free with no deposit required. Click here to verify.",
            "date": "Yesterday, 11:10 AM",
            "has_unsubscribe": False
        },
        {
            "id": "demo-msg-13",
            "sender_name": "Frontend Weekly",
            "sender_addr": "editorial@frontendweekly.co",
            "subject": "Issue #412: What's new in ECMAScript and CSS Grid",
            "preview": "Welcome to this week's newsletter. Deep dive into container queries, subgrid performance, and modern bundling tooling.",
            "date": "Nov 21, 10:00 AM",
            "has_unsubscribe": True
        },
        {
            "id": "demo-msg-14",
            "sender_name": "Executive Office",
            "sender_addr": "ceo-office@acmecorp.com",
            "subject": "ACTION REQUIRED: Final notice regarding security compliance",
            "preview": "All personnel must complete mandatory security compliance certification ASAP. Final notice before policy deactivation.",
            "date": "Nov 21, 09:15 AM",
            "has_unsubscribe": False
        },
        {
            "id": "demo-msg-15",
            "sender_name": "HR Operations",
            "sender_addr": "hr-talent@partnercorp.com",
            "subject": "Official Offer Letter - Staff Platform Engineer",
            "preview": "We are thrilled to extend this formal offer letter to join our organization. Please confirm acceptance by signing attached.",
            "date": "Nov 20, 04:30 PM",
            "has_unsubscribe": False
        },
        {
            "id": "demo-msg-16",
            "sender_name": "First National Bank",
            "sender_addr": "support@firstbank.com",
            "subject": "Security alert: New login detected from unfamiliar device",
            "preview": "We detected a login to your online banking from Chrome on Windows. If this was not you, trigger a password reset immediately.",
            "date": "Nov 20, 02:15 PM",
            "has_unsubscribe": False
        },
        {
            "id": "demo-msg-17",
            "sender_name": "State Lottery Promotion",
            "sender_addr": "admin@golden-ticket-luck.xyz",
            "subject": "National Lottery alert: You have won top prize!",
            "preview": "Your ticket was selected as the lucky winner! Act now to claim your prize before unclaimed funds expire.",
            "date": "Nov 19, 06:10 PM",
            "has_unsubscribe": False
        },
        {
            "id": "demo-msg-18",
            "sender_name": "Urban Outfitters",
            "sender_addr": "promos@styleoutfit.com",
            "subject": "Secret Flash Sale: Unbelievable discount & coupon inside",
            "preview": "Special discount offer just for you! Use coupon code SAVE30 at checkout. Extra 20% off all seasonal outerwear today.",
            "date": "Nov 19, 01:05 PM",
            "has_unsubscribe": True
        },
        {
            "id": "demo-msg-19",
            "sender_name": "Dean of Student Affairs",
            "sender_addr": "dean@university.edu",
            "subject": "Campus Library extended hours for reading week",
            "preview": "The main campus library will remain open 24 hours starting next Monday. Quiet study spaces and hot tea provided on floor 3.",
            "date": "Nov 18, 11:30 AM",
            "has_unsubscribe": False
        },
        {
            "id": "demo-msg-20",
            "sender_name": "Elena Rostova",
            "sender_addr": "elena.r@bookclub.org",
            "subject": "Book club selection for December discussion",
            "preview": "Our chosen book for this month is Klara and the Sun. Let us gather at the community cafe on Sunday afternoon.",
            "date": "Nov 18, 09:00 AM",
            "has_unsubscribe": False
        }
    ]

def fetch_gmail_emails(gmail_user, gmail_pass):
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(gmail_user, gmail_pass)
        status, _ = mail.select("INBOX", readonly=True)
        if status != "OK":
            try:
                mail.logout()
            except Exception:
                pass
            return None, "Could not log in. Check your App Password and that IMAP is enabled."

        status, msg_nums = mail.search(None, "ALL")
        if status != "OK" or not msg_nums or not msg_nums[0]:
            try:
                mail.close()
                mail.logout()
            except Exception:
                pass
            return [], None

        num_list = msg_nums[0].split()
        target_nums = num_list[-30:][::-1]  # 30 newest, newest first

        mails = []
        for num in target_nums:
            try:
                res, data = mail.fetch(num, '(BODY.PEEK[HEADER] BODY.PEEK[TEXT]<0.3000>)')
                if res != 'OK' or not data:
                    continue

                header_bytes = b""
                text_bytes = b""
                for item in data:
                    if isinstance(item, tuple) and len(item) >= 2:
                        desc = item[0]
                        payload = item[1]
                        if b'HEADER' in desc:
                            header_bytes = payload
                        elif b'TEXT' in desc:
                            text_bytes = payload
                        elif not header_bytes:
                            header_bytes = payload

                msg = email.message_from_bytes(header_bytes)

                msg_id = msg.get("Message-ID", "").strip()
                if not msg_id:
                    msg_id = f"gmail-{num.decode('utf-8', errors='ignore')}"

                raw_from = decode_mime_header(msg.get("From", ""))
                name, addr = email.utils.parseaddr(raw_from)
                sender_addr = addr.lower().strip()
                sender_name = name.strip() or (sender_addr.split("@")[0] if sender_addr else "Unknown")

                subject = decode_mime_header(msg.get("Subject", "")).strip() or "(No Subject)"
                date_val = decode_mime_header(msg.get("Date", "")).strip()
                has_unsubscribe = bool(msg.get("List-Unsubscribe"))

                raw_text = ""
                if text_bytes:
                    for enc in ("utf-8", "latin1", "cp1252"):
                        try:
                            raw_text = text_bytes.decode(enc)
                            break
                        except Exception:
                            pass
                    if not raw_text:
                        raw_text = text_bytes.decode("utf-8", errors="ignore")

                clean_text = re.sub(r'<[^>]+>', ' ', raw_text)
                clean_text = html.unescape(clean_text)
                clean_text = ' '.join(clean_text.split())
                preview = clean_text[:120].strip()

                mails.append({
                    "id": msg_id,
                    "sender_name": sender_name,
                    "sender_addr": sender_addr,
                    "subject": subject,
                    "preview": preview,
                    "date": date_val,
                    "has_unsubscribe": has_unsubscribe
                })
            except Exception:
                continue

        try:
            mail.close()
            mail.logout()
        except Exception:
            pass

        return mails, None
    except Exception as e:
        logging.error("IMAP fetch failed: %s", type(e).__name__)
        return None, "Could not log in. Check your App Password and that IMAP is enabled."

# Security: Protect all POST routes from foreign Origin/Referer
@app.before_request
def csrf_protect_post_routes():
    if request.method == "POST":
        origin = request.headers.get("Origin") or request.headers.get("Referer")
        if origin:
            parsed = urllib.parse.urlparse(origin)
            host = (parsed.hostname or "").lower()
            request_host = (request.host or "").split(":")[0].lower()
            if host not in ("127.0.0.1", "localhost", request_host):
                logging.warning("Blocked POST request with foreign origin: %s", origin)
                return jsonify({"error": "Forbidden - invalid origin"}), 403

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/health")
def health():
    addr, _ = get_saved_credentials()
    mode = "GMAIL" if addr else "DEMO"
    return jsonify({"status": "ok", "app": "Mailboard", "mode": mode})

@app.route("/api/connect", methods=["POST"])
def api_connect():
    data = request.get_json(silent=True) or {}
    address = str(data.get("address", "")).strip().lower()
    raw_password = str(data.get("password", ""))
    password = raw_password.replace(" ", "").strip()

    if not (address.endswith("@gmail.com") or address.endswith("@googlemail.com")):
        return jsonify({
            "ok": False,
            "error": "Only @gmail.com or @googlemail.com addresses are supported."
        }), 400

    if not password:
        return jsonify({
            "ok": False,
            "error": "Please enter your 16-character Google App Password."
        }), 400

    # Test IMAP login safely
    try:
        test_mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        test_mail.login(address, password)
        status, _ = test_mail.select("INBOX", readonly=True)
        try:
            test_mail.close()
            test_mail.logout()
        except Exception:
            pass
        if status != "OK":
            return jsonify({
                "ok": False,
                "error": "Could not log in. Check your App Password and that IMAP is enabled."
            }), 400
    except Exception as e:
        logging.error("IMAP test login failed: %s", type(e).__name__)
        return jsonify({
            "ok": False,
            "error": "Could not log in. Check your App Password and that IMAP is enabled."
        }), 400

    # Save credentials securely to keyring
    save_credentials(address, password)

    # Invalidate email cache
    email_cache["timestamp"] = 0
    email_cache["raw_mails"] = []
    email_cache["mode"] = "GMAIL"
    email_cache["error"] = None

    return jsonify({"ok": True})

@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    delete_credentials()
    email_cache["timestamp"] = 0
    email_cache["raw_mails"] = []
    email_cache["mode"] = "DEMO"
    email_cache["error"] = None
    return jsonify({"ok": True})

@app.route("/api/mails")
def api_mails():
    refresh = request.args.get("refresh") == "1"
    now = time.time()
    gmail_addr, gmail_pass = get_saved_credentials()
    mode = "GMAIL" if (gmail_addr and gmail_pass) else "DEMO"

    if mode == "GMAIL":
        if not IS_VERCEL and not refresh and (now - email_cache["timestamp"] < 60) and email_cache["raw_mails"] and (email_cache["mode"] == mode):
            raw_mails = email_cache["raw_mails"]
            error = email_cache["error"]
        else:
            fetched_mails, err = fetch_gmail_emails(gmail_addr, gmail_pass)
            if err:
                if not IS_VERCEL:
                    email_cache["timestamp"] = now
                    email_cache["raw_mails"] = []
                    email_cache["mode"] = mode
                    email_cache["error"] = err
                return jsonify({"mode": mode, "error": err, "mails": []})
            raw_mails = fetched_mails
            error = None
            if not IS_VERCEL:
                email_cache["timestamp"] = now
                email_cache["raw_mails"] = raw_mails
                email_cache["mode"] = mode
                email_cache["error"] = error
    else:
        raw_mails = get_custom_mails() + get_demo_emails()
        error = None
        email_cache["timestamp"] = now
        email_cache["raw_mails"] = raw_mails
        email_cache["mode"] = mode
        email_cache["error"] = error

    overrides, sender_rules = get_db_rules()
    classified_mails = []
    for m in raw_mails:
        cat, why = classify_email(m, overrides, sender_rules)
        classified_mails.append({
            "id": m["id"],
            "sender_name": m["sender_name"],
            "sender_addr": m["sender_addr"],
            "subject": m["subject"],
            "preview": m["preview"],
            "date": m["date"],
            "category": cat,
            "why": why
        })

    return jsonify({
        "mode": mode,
        "error": error,
        "mails": classified_mails
    })

@app.route("/api/classify", methods=["POST"])
def api_classify():
    data = request.get_json(silent=True) or {}
    sender_name = str(data.get("sender_name", "")).strip()
    sender_addr = str(data.get("sender_addr", "")).strip().lower()
    subject = str(data.get("subject", "")).strip()
    preview = str(data.get("preview", "")).strip()
    has_unsubscribe = bool(data.get("has_unsubscribe", False))

    if not subject and not preview:
        return jsonify({"ok": False, "error": "Please enter a subject or body preview."}), 400

    if not sender_addr:
        sender_addr = "sender@example.com"
    if not sender_name:
        sender_name = sender_addr.split("@")[0] or "Unknown"

    mail_id = f"custom-{int(time.time() * 1000)}"
    mail_obj = {
        "id": mail_id,
        "sender_name": sender_name,
        "sender_addr": sender_addr,
        "subject": subject or "(No Subject)",
        "preview": preview[:120],
        "date": "Just now",
        "has_unsubscribe": has_unsubscribe,
    }

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO custom_mails (id, sender_name, sender_addr, subject, preview, date, has_unsubscribe) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mail_obj["id"], mail_obj["sender_name"], mail_obj["sender_addr"], mail_obj["subject"], mail_obj["preview"], mail_obj["date"], int(has_unsubscribe)),
        )
        conn.commit()

    overrides, sender_rules = get_db_rules()
    cat, why = classify_email(mail_obj, overrides, sender_rules)

    return jsonify({
        "ok": True,
        "mail": {
            "id": mail_obj["id"],
            "sender_name": mail_obj["sender_name"],
            "sender_addr": mail_obj["sender_addr"],
            "subject": mail_obj["subject"],
            "preview": mail_obj["preview"],
            "date": mail_obj["date"],
            "category": cat,
            "why": why,
        }
    })

@app.route("/api/move", methods=["POST"])
def api_move():
    data = request.get_json(silent=True) or {}
    mail_id = data.get("id")
    category = data.get("category")

    if category not in CATEGORIES:
        return jsonify({"error": "Invalid category"}), 400

    known_mail = next((m for m in email_cache.get("raw_mails", []) if m["id"] == mail_id), None)
    if not known_mail:
        known_mail = next((m for m in (get_custom_mails() + get_demo_emails()) if m["id"] == mail_id), None)

    if not known_mail:
        return jsonify({"error": "Unknown email id"}), 404

    sender_addr = known_mail.get("sender_addr", "").lower().strip()

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO mail_overrides (message_id, category) VALUES (?, ?)", (mail_id, category))
        if sender_addr:
            c.execute("INSERT OR REPLACE INTO sender_rules (sender, category) VALUES (?, ?)", (sender_addr, category))
        conn.commit()

    return jsonify({"ok": True})

exit_event = threading.Event()

@app.route("/api/quit", methods=["POST"])
def api_quit():
    def delayed_exit():
        time.sleep(0.5)
        exit_event.set()
        time.sleep(0.2)
        os._exit(0)
    threading.Thread(target=delayed_exit, daemon=True).start()
    return jsonify({"ok": True})

def check_health_on_port(port):
    try:
        url = f"http://127.0.0.1:{port}/health"
        req = urllib.request.Request(url, headers={"User-Agent": "Mailboard-Launcher"})
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("app") == "Mailboard":
                    return True
    except Exception:
        pass
    return False

def is_port_available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False

def main():
    # 1. If an instance is already running on ports 5000-5010, open browser and exit
    for port in range(5000, 5011):
        if check_health_on_port(port):
            try:
                webbrowser.open(f"http://127.0.0.1:{port}")
            except Exception:
                pass
            sys.exit(0)

    # 2. Select first available port between 5000 and 5010
    selected_port = None
    for port in range(5000, 5011):
        if is_port_available(port):
            selected_port = port
            break

    if selected_port is None:
        logging.error("No free port available between 5000 and 5010.")
        sys.exit(1)

    # 3. Start Flask in background thread
    def run_flask():
        try:
            app.run(host="127.0.0.1", port=selected_port, debug=False, use_reloader=False)
        except Exception as e:
            logging.error("Flask server halted: %s", type(e).__name__)
        finally:
            exit_event.set()

    server_thread = threading.Thread(target=run_flask, daemon=True)
    server_thread.start()

    # 4. Wait until the server responds on /health, then open browser
    server_ready = False
    for _ in range(60):
        time.sleep(0.1)
        if check_health_on_port(selected_port):
            server_ready = True
            break

    if server_ready:
        try:
            webbrowser.open(f"http://127.0.0.1:{selected_port}")
        except Exception:
            pass
    else:
        logging.error("Server failed to respond on port %s within 6 seconds.", selected_port)

    # 5. Wait until exit requested
    try:
        while not exit_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    sys.exit(0)

if __name__ == "__main__":
    main()
