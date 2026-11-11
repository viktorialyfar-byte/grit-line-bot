"""
Grit Line Rentals — Telegram Booking Bot
==========================================
You message this bot on Telegram with a new rental's info in plain English,
in any order or format, across as many messages as you like. It keeps a
running draft, fills in Grit Line's defaults, asks only for what's truly
missing, then builds a Stripe invoice (with tax) and a pre-filled DocuSeal
contract and replies with both links for you to review.

Corrections work in plain words too ("make it 160 a day", "she's picking it
up herself", "due Monday instead") — the bot updates the draft and rebuilds.

NOTHING is emailed to the customer until you reply "SEND" to confirm.
Reply "CANCEL" (or "NEW" / "START OVER") to discard the current job.

Deploy this on Render.com (free tier). See README_DEPLOY.md for the full
step-by-step. All secrets come from environment variables — nothing is
hardcoded in this file.
"""
import json
import math
import os
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

from flask import Flask, request

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/Los_Angeles")  # Spokane is Pacific time
except Exception:  # tz database missing on the host — fall back to a fixed offset
    LOCAL_TZ = timezone(timedelta(hours=-8))

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config — all from environment variables, set these in Render's dashboard
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT_ID = os.environ["ALLOWED_CHAT_ID"]  # your own Telegram chat id — messages from anyone else are ignored
STRIPE_SECRET_KEY = os.environ["STRIPE_SECRET_KEY"]
DOCUSEAL_API_KEY = os.environ["DOCUSEAL_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]

DOCUSEAL_TEMPLATE_ID = 5505336
# Default tax rates for jobs in Grit Line's home jurisdiction (per Viktoria, confirmed 2026-08-21
# against WA DOR: dor.wa.gov/taxes-rates/sales-use-tax-rates and .../other-taxes/heavy-equipment-rental-tax).
# These are two distinct taxes and are itemized separately on the invoice.
DEFAULT_SALES_TAX_PCT = 8.10  # Spokane County, unincorporated areas — WA DOR local sales tax rate, Q1 2026
DEFAULT_HEAVY_EQUIPMENT_TAX_PCT = 1.25  # WA Heavy Equipment Rental Tax, RCW 82.51 — flat statewide rate
DEFAULT_DELIVERY_FEE = 65.0  # round-trip delivery, Grit Line's usual fee (not taxed)
DEFAULT_TIME_OUT = (8, 0)   # used when a pickup date is given without a time
DEFAULT_TIME_DUE = (17, 0)  # used when a return date is given without a time
STATE_FILE = Path("/tmp/pending_job.json")  # survives until the dyno restarts; good enough for single-user low-volume use

EQUIPMENT_CATALOG = {
    "dingo_trailer": {
        "label": "Dingo Mini Skid Steer + 5x10 Utility Trailer",
        "rental_price_id": "price_1U6IRkFNCgHCBzebRLQBpaYe",
        "deposit_price_id": "price_1U6IRlFNCgHCBzebiqxjKy5V",
        "default_rate": 175.0,
        "default_deposit": 100.0,
        # Self-propelled construction equipment — WA Heavy Equipment Rental Tax (RCW 82.51) applies.
        "heavy_equipment": True,
    },
    "trailer_22ft": {
        "label": "22ft Equipment/Auto Trailer",
        "rental_price_id": "price_1U6YxKFNCgHCBzeb6FxkG02t",
        "deposit_price_id": "price_1U6YxMFNCgHCBzebQifqFWfX",
        "default_rate": 150.0,
        "default_deposit": 100.0,
        # Plain towed trailer, no powered equipment — not "heavy equipment" under RCW 82.51 / 84.36.597
        # (that definition targets self-propelled machinery; licensed/registered trailers fall outside it).
        "heavy_equipment": False,
    },
    "dump_trailer_5x10": {
        "label": "5x10 Dump Trailer",
        "rental_price_id": "price_1U6YxGFNCgHCBzebjjs2c4AU",
        "deposit_price_id": "price_1U6YxIFNCgHCBzebyUeXFUnK",
        "default_rate": 80.0,  # matches the Oct 2026 dump trailer invoice ($80/day)
        "default_deposit": 100.0,
        # Plain towed trailer, no powered equipment — same reasoning as above.
        "heavy_equipment": False,
    },
}

# Things the bot can't build an invoice + contract without. Everything else
# either has a default or is optional.
REQUIRED = {
    "renter_name": "renter's name",
    "renter_email": "renter's email",
    "equipment_key": "which equipment (skid steer, 22ft trailer, or dump trailer)",
    "date_out": "pickup/start date",
    "date_due": "return date",
    "rental_rate_day": "daily rate",
}

# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------
def tg_send(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)

# ---------------------------------------------------------------------------
# Claude — read Viktoria's message (plus the current draft) into fields
# ---------------------------------------------------------------------------
PARSE_INSTRUCTIONS = """You're the assistant inside Grit Line Rentals' booking bot. Viktoria (the owner)
sends you messages about equipment rental jobs. Her messages do NOT follow any format or order: quick
notes, shorthand, typos, voice-to-text, copy-pasted customer texts or emails, chit-chat mixed in, or just
a single correction or missing detail. Read the whole thing and pull out whatever matches the fields
below, wherever it appears and in whatever words she used. Ignore anything unrelated.

TODAY is {today}. Resolve relative dates ("tomorrow", "Friday", "this weekend", "next Tuesday",
"the 20th") against today. Dates are always today or in the future unless she clearly means otherwise.
"Friday to Sunday" means the coming Friday through the Sunday right after it.

CURRENT DRAFT (what she's already told you about the job in progress; may be empty):
{draft}

Decide "intent":
- "update" — the message adds to, fills in, or corrects the current draft (a missing detail, "make it
  160 a day", "actually it's due Monday", "his email is ...", "she's picking it up herself").
- "new" — the message clearly describes a different rental for a different renter, or the draft is empty.

Return ONLY a JSON object, no other text:
{{
  "intent": "update" or "new",
  "fields": {{
    "renter_name": string or null,
    "renter_email": string or null,
    "renter_phone": string or null,
    "drivers_license": string or null,
    "job_site_address": string or null,
    "equipment_key": "dingo_trailer" | "trailer_22ft" | "dump_trailer_5x10" | null,
    "date_out": "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM" (24h) or null,
    "date_due": "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM" (24h) or null,
    "rental_days": number or null,
    "rental_rate_day": number or null,
    "security_deposit": number or null,
    "pickup_method": "renter_pickup" | "delivery" | null,
    "delivery_amount": number or null,
    "sales_tax_pct": number or null,
    "heavy_equipment_tax_pct": number or null
  }}
}}

Rules for "fields":
- Only fill a field if THIS message states or changes it. Use null for everything else — the draft keeps
  its existing values for null fields, so never repeat or guess values from the draft.
- equipment_key: skid steer / Dingo / Toro / mini loader / "the machine" -> "dingo_trailer";
  22 ft / 22' / car hauler / equipment trailer / auto trailer -> "trailer_22ft";
  dump trailer / dump / 5x10 dump -> "dump_trailer_5x10".
- Only include a time in date_out/date_due if she gave one ("8am", "noon", "end of day" = 17:00).
- rental_days: only if she states a count ("3 days", "a week" = 7, "for the weekend" is NOT a count).
- pickup_method: "he's grabbing it", "picking up", "customer pickup" -> "renter_pickup";
  "drop it off", "deliver", "bring it to the site", "delivering to <address>" -> "delivery".
  An address she's delivering to is the job_site_address.
- Money: plain numbers, no "$". "175 a day", "$175/day" -> 175.
- Taxes: leave sales_tax_pct and heavy_equipment_tax_pct null unless she explicitly gives a different
  rate or says the job is tax-exempt / no tax / out of state (then 0).
- Emails: fix obvious voice-to-text spellings ("john at gmail dot com" -> "john@gmail.com")."""


def claude_parse(text, draft):
    now = datetime.now(LOCAL_TZ)
    today = now.strftime("%A, %B %d, %Y (%Y-%m-%d), %I:%M %p Pacific")
    draft_view = {k: v for k, v in (draft or {}).items() if v not in (None, "") and not k.startswith("_")}
    prompt = PARSE_INSTRUCTIONS.format(today=today, draft=json.dumps(draft_view, indent=2) if draft_view else "(empty)")
    body = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": f"{prompt}\n\nMESSAGE FROM VIKTORIA:\n{text}"}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        result = json.loads(resp.read())
    raw = result["content"][0]["text"].strip()
    # Be forgiving about code fences or stray text around the JSON.
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON in parser reply")
    parsed = json.loads(raw[start:end + 1])
    if "fields" not in parsed:  # model returned the fields flat
        parsed = {"intent": parsed.pop("intent", "update"), "fields": parsed}
    return parsed.get("intent") or "update", parsed.get("fields") or {}


NUMERIC_FIELDS = ("rental_rate_day", "security_deposit", "delivery_amount",
                  "sales_tax_pct", "heavy_equipment_tax_pct", "rental_days")


def clean_fields(fields):
    """LLM output isn't guaranteed to be tidy — coerce numbers, drop junk."""
    out = {}
    for key, val in fields.items():
        if isinstance(val, str):
            val = val.strip()
            if val.lower() in ("", "null", "none", "n/a", "unknown"):
                val = None
        if key in NUMERIC_FIELDS and val is not None and not isinstance(val, (int, float)):
            cleaned = str(val).replace("$", "").replace(",", "").replace("%", "").strip()
            try:
                val = float(cleaned)
            except ValueError:
                val = None
        if key == "equipment_key" and val not in EQUIPMENT_CATALOG:
            val = None
        if key == "pickup_method" and val not in ("renter_pickup", "delivery"):
            val = None
        if val is not None:
            out[key] = val
    return out

# ---------------------------------------------------------------------------
# Dates + defaults
# ---------------------------------------------------------------------------
def parse_dt(value, default_time):
    """Accepts 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM'. Returns a datetime or None."""
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    try:
        d = datetime.strptime(value[:10], "%Y-%m-%d")
        return d.replace(hour=default_time[0], minute=default_time[1])
    except ValueError:
        return None


def pretty_dt(dt):
    return dt.strftime("%a %m/%d/%Y %I:%M %p").replace(" 0", " ")


def complete_job(draft):
    """Fill in defaults on a copy of the draft. Returns (job, missing_labels, assumptions)."""
    job = dict(draft)
    assumptions = []

    equipment = EQUIPMENT_CATALOG.get(job.get("equipment_key"))

    if equipment and not job.get("rental_rate_day"):
        job["rental_rate_day"] = equipment["default_rate"]
        assumptions.append(f"standard rate ${equipment['default_rate']:.0f}/day")
    if equipment and job.get("security_deposit") is None:
        job["security_deposit"] = equipment["default_deposit"]
        assumptions.append(f"standard ${equipment['default_deposit']:.0f} deposit")

    if not job.get("pickup_method"):
        job["pickup_method"] = "renter_pickup"
        assumptions.append("customer picks up & returns")
    if job["pickup_method"] == "delivery" and job.get("delivery_amount") is None:
        job["delivery_amount"] = DEFAULT_DELIVERY_FEE
        assumptions.append(f"${DEFAULT_DELIVERY_FEE:.0f} round-trip delivery")

    out_dt = parse_dt(job.get("date_out"), DEFAULT_TIME_OUT)
    due_dt = parse_dt(job.get("date_due"), DEFAULT_TIME_DUE)
    # "3 days starting Friday" — derive the return date from the count.
    if out_dt and not due_dt and job.get("rental_days"):
        due_dt = (out_dt + timedelta(days=int(job["rental_days"]))).replace(
            hour=DEFAULT_TIME_DUE[0], minute=DEFAULT_TIME_DUE[1])
    if out_dt and due_dt and due_dt <= out_dt:
        due_dt = None  # nonsense range — ask again rather than bill it
    job["_out_dt"], job["_due_dt"] = out_dt, due_dt

    if out_dt and due_dt:
        if job.get("rental_days"):
            days = int(job["rental_days"])
        else:
            # Bill by calendar days: Fri -> Sun = 2 days, same-day = 1 day.
            days = max(1, (due_dt.date() - out_dt.date()).days)
        job["days"] = max(1, days)
        job["date_out_text"] = pretty_dt(out_dt)
        job["date_due_text"] = pretty_dt(due_dt)

    missing = []
    for key, label in REQUIRED.items():
        if key == "date_out" and not out_dt:
            missing.append(label)
        elif key == "date_due" and not due_dt:
            missing.append(label)
        elif key not in ("date_out", "date_due") and not job.get(key):
            missing.append(label)
    if job["pickup_method"] == "delivery" and not job.get("job_site_address"):
        missing.append("delivery address")
    return job, missing, assumptions


def money(x):
    return f"${x:,.2f}"


def estimate(job, equipment):
    """Same math build_invoice uses — for the summary before anything is built."""
    rental = job["rental_rate_day"] * job["days"]
    sales_pct, heavy_pct = tax_rates(job, equipment)
    taxes = round(rental * sales_pct / 100, 2) + round(rental * heavy_pct / 100, 2)
    delivery = (job.get("delivery_amount") or 0) if job["pickup_method"] == "delivery" else 0
    return rental, taxes, delivery, job.get("security_deposit") or 0


def tax_rates(job, equipment):
    sales_pct = job.get("sales_tax_pct")
    if sales_pct is None:
        sales_pct = DEFAULT_SALES_TAX_PCT
    heavy_pct = job.get("heavy_equipment_tax_pct")
    if heavy_pct is None:
        # Only self-propelled equipment (e.g. the skid steer) is "heavy equipment" under RCW 82.51 —
        # plain towed trailers rented on their own don't qualify, so default to 0 for those.
        heavy_pct = DEFAULT_HEAVY_EQUIPMENT_TAX_PCT if equipment.get("heavy_equipment") else 0
    return sales_pct, heavy_pct


def summary_lines(job):
    """What the bot has so far, in plain words."""
    equipment = EQUIPMENT_CATALOG.get(job.get("equipment_key"))
    lines = []
    if job.get("renter_name"): lines.append(f"Renter: {job['renter_name']}")
    contact = " / ".join(x for x in (job.get("renter_phone"), job.get("renter_email")) if x)
    if contact: lines.append(f"Contact: {contact}")
    if job.get("drivers_license"): lines.append(f"DL#: {job['drivers_license']}")
    if equipment: lines.append(f"Equipment: {equipment['label']}")
    if job.get("date_out_text"):
        lines.append(f"Dates: {job['date_out_text']} → {job['date_due_text']} ({job['days']} day{'s' if job['days'] != 1 else ''})")
    elif job.get("_out_dt"):
        lines.append(f"Out: {pretty_dt(job['_out_dt'])}")
    if job.get("rental_rate_day"): lines.append(f"Rate: {money(job['rental_rate_day'])}/day")
    if job.get("pickup_method") == "delivery":
        lines.append(f"Delivery to: {job.get('job_site_address') or '?'} ({money(job.get('delivery_amount') or 0)})")
    else:
        lines.append("Customer pickup & return" + (f" — job site: {job['job_site_address']}" if job.get("job_site_address") else ""))
    if job.get("security_deposit") is not None: lines.append(f"Deposit: {money(job['security_deposit'])}")
    return lines

# ---------------------------------------------------------------------------
# Stripe helpers (raw REST calls, no SDK dependency)
# ---------------------------------------------------------------------------
def stripe_request(method, path, params=None):
    url = f"https://api.stripe.com/v1{path}"
    headers = {"Authorization": f"Bearer {STRIPE_SECRET_KEY}"}
    data = None
    if params:
        pairs = []
        for k, v in params.items():
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    pairs.append((f"{k}[{k2}]", v2))
            else:
                pairs.append((k, v))
        data = urllib.parse.urlencode(pairs).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise RuntimeError(f"Stripe {method} {path} -> {e.code}: {err_body}") from None


def find_or_create_customer(name, email, phone=None, description=""):
    q = urllib.parse.quote(f"email:'{email}'")
    result = stripe_request("GET", f"/customers/search?query={q}")
    if result.get("data"):
        return result["data"][0]["id"]
    params = {"name": name, "email": email, "description": description}
    if phone:
        params["phone"] = phone
    created = stripe_request("POST", "/customers", params)
    return created["id"]


def build_invoice(job, equipment):
    customer_id = find_or_create_customer(job["renter_name"], job["renter_email"],
                                          job.get("renter_phone"), job.get("job_site_address") or "")
    period = f"{job['date_out_text']} to {job['date_due_text']}"

    days = job["days"]
    rental_cents = round(job["rental_rate_day"] * 100) * days
    deposit_cents = round((job.get("security_deposit") or 0) * 100)
    delivery_cents = round((job.get("delivery_amount") or 0) * 100) if job["pickup_method"] == "delivery" else 0
    sales_tax_pct, heavy_tax_pct = tax_rates(job, equipment)
    # record the rates actually applied, for the confirmation message + saved state
    job["sales_tax_pct"] = sales_tax_pct
    job["heavy_equipment_tax_pct"] = heavy_tax_pct
    # Taxes apply to the rental charge only — never the deposit or delivery.
    sales_tax_cents = round(rental_cents * sales_tax_pct / 100)
    heavy_tax_cents = round(rental_cents * heavy_tax_pct / 100)

    stripe_request("POST", "/invoiceitems", {
        "customer": customer_id,
        "amount": rental_cents,
        "currency": "usd",
        "description": f"{equipment['label']} rental — {days} day{'s' if days != 1 else ''} "
                       f"@ ${job['rental_rate_day']:.2f}/day ({period})",
    })
    if sales_tax_cents:
        stripe_request("POST", "/invoiceitems", {
            "customer": customer_id, "amount": sales_tax_cents, "currency": "usd",
            "description": f"Sales Tax ({sales_tax_pct}%) on rental",
        })
    if heavy_tax_cents:
        stripe_request("POST", "/invoiceitems", {
            "customer": customer_id, "amount": heavy_tax_cents, "currency": "usd",
            "description": f"WA Heavy Equipment Rental Tax ({heavy_tax_pct}%)",
        })
    if delivery_cents:
        stripe_request("POST", "/invoiceitems", {
            "customer": customer_id, "amount": delivery_cents, "currency": "usd",
            "description": "Round-trip delivery (not taxed)",
        })
    if deposit_cents:
        stripe_request("POST", "/invoiceitems", {
            "customer": customer_id, "amount": deposit_cents, "currency": "usd",
            "description": "Refundable security deposit (not taxed)",
        })

    invoice = stripe_request("POST", "/invoices", {
        "customer": customer_id,
        "collection_method": "send_invoice",
        "days_until_due": 3,
        "pending_invoice_items_behavior": "include",
        "description": f"{equipment['label']} rental — {period}. "
                       f"{'Renter pickup & return' if job['pickup_method'] == 'renter_pickup' else 'Delivery'}.",
        "footer": f"Job site: {job.get('job_site_address') or 'N/A'}. Rental Agreement must be signed before pickup.",
    })
    finalized = stripe_request("POST", f"/invoices/{invoice['id']}/finalize", {"auto_advance": "false"})
    total = finalized["amount_due"] / 100
    return finalized["id"], finalized["hosted_invoice_url"], total


def void_invoice(invoice_id):
    stripe_request("POST", f"/invoices/{invoice_id}/void", {})

# ---------------------------------------------------------------------------
# DocuSeal helpers
# ---------------------------------------------------------------------------
def docuseal_request(method, path, body=None):
    url = f"https://api.docuseal.com{path}"
    headers = {"X-Auth-Token": DOCUSEAL_API_KEY, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise RuntimeError(f"DocuSeal {method} {path} -> {e.code}: {err_body}") from None


def _phone_email_value(job):
    """The template has a single combined "Phone Email" field. Don't let one
    silently overwrite the other — show whichever fields the renter actually
    gave us, so nothing Viktoria sent gets dropped from the printed contract."""
    phone = job.get("renter_phone")
    email = job.get("renter_email")
    if phone and email:
        return f"{phone} / {email}"
    return phone or email or ""


def build_docuseal_submission(job, equipment):
    pickup_true = job["pickup_method"] == "renter_pickup"
    payload = {
        "template_id": DOCUSEAL_TEMPLATE_ID,
        "send_email": False,
        "submitters": [
            {
                "role": "First Party",
                "name": "Grit Line Rentals",
                "email": GMAIL_ADDRESS,
                "fields": [
                    {"name": "Renter Name", "value": job["renter_name"]},
                    {"name": "Phone Email", "value": _phone_email_value(job)},
                    {"name": "Job Site Address", "value": job.get("job_site_address") or ""},
                    {"name": "Equipment Rented", "value": equipment["label"]},
                    {"name": "Date Time Out", "value": job["date_out_text"]},
                    {"name": "Date Time Due", "value": job["date_due_text"]},
                    {"name": "Rental Rate Day", "value": f"{job['rental_rate_day']:.2f}"},
                    {"name": "Renter Pickup And Return", "value": pickup_true},
                    {"name": "Round Delivery", "value": not pickup_true},
                    {"name": "Round Delivery Amount",
                     "value": f"{job.get('delivery_amount') or 0:.2f}" if not pickup_true else "N/A"},
                    {"name": "Security Deposit", "value": f"${job.get('security_deposit') or 0:.2f}"},
                ],
            },
            {
                "role": "Second Party",
                "name": job["renter_name"],
                "email": job["renter_email"],
                "fields": [
                    {"name": "Drivers License Number", "value": job.get("drivers_license") or ""},
                ],
            },
        ],
    }
    result = docuseal_request("POST", "/submissions", payload)
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(f"DocuSeal error: {result['error']}")
    second_party = next(s for s in result if s["role"] == "Second Party")
    return second_party.get("submission_id"), second_party["embed_src"]


def archive_submission(submission_id):
    if submission_id:
        try:
            docuseal_request("DELETE", f"/submissions/{submission_id}")
        except Exception:
            pass  # an old unused contract hanging around is harmless

# ---------------------------------------------------------------------------
# Email (sent only after explicit "SEND" confirmation)
# ---------------------------------------------------------------------------
def send_customer_email(job, invoice_url, contract_url, total):
    subject = "Grit Line Rentals — Contract & Invoice"
    body = f"""Hi {job['renter_name']},

Thanks for booking with Grit Line Equipment Rentals! Here's everything for your rental:

EQUIPMENT: {job.get('equipment_label', '')}
PICKUP: {job['date_out_text']}
RETURN: {job['date_due_text']}
JOB SITE: {job.get('job_site_address') or 'N/A'}

1) SIGN THE RENTAL AGREEMENT (please do this before pickup):
{contract_url}

2) PAY YOUR INVOICE (${total:.2f} total, due before pickup):
{invoice_url}

Let us know if you have any questions.

Thanks,
Grit Line Equipment Rentals, LLC"""
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = job["renter_email"]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [job["renter_email"]], msg.as_string())

# ---------------------------------------------------------------------------
# State — one job in progress at a time. "draft" holds everything Viktoria has
# told the bot so far; "built" holds the invoice/contract once they exist.
# ---------------------------------------------------------------------------
def load_state():
    try:
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text())
            if "draft" in state:
                return state
    except Exception:
        pass
    return {"draft": {}, "built": None, "seen_updates": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, default=str))


def discard_built(state):
    built = state.get("built")
    if built:
        try:
            void_invoice(built["invoice_id"])
        except Exception:
            pass
        archive_submission(built.get("submission_id"))
    state["built"] = None

# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(force=True, silent=True) or {}
    message = update.get("message") or update.get("edited_message") or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or message.get("caption") or "").strip()

    if not chat_id or chat_id != ALLOWED_CHAT_ID:
        return "ok"  # silently ignore anyone who isn't Viktoria
    if not text:
        tg_send(chat_id, "I can only read text right now — type or dictate the job details.")
        return "ok"

    # Telegram re-delivers a message if the bot was slow to answer (e.g. waking
    # up on Render). Skip repeats so one message never builds two invoices.
    state = load_state()
    update_id = update.get("update_id")
    if update_id is not None:
        if update_id in state.get("seen_updates", []):
            return "ok"
        state["seen_updates"] = (state.get("seen_updates", []) + [update_id])[-50:]
        save_state(state)

    try:
        handle_message(chat_id, text, state)
    except Exception as e:
        tg_send(chat_id, f"⚠️ Something broke: {e}\n\nYour job details are still saved — just send the fix or try again.")
    return "ok"


COMMANDS_CANCEL = {"CANCEL", "NEW", "START OVER", "STARTOVER", "RESET", "CLEAR", "/NEW", "/CANCEL"}


def handle_message(chat_id, text, state):
    upper = text.strip().strip(".!").upper()

    if upper == "SEND":
        built = state.get("built")
        if not built:
            tg_send(chat_id, "Nothing ready to send yet." + (
                " Still waiting on a few details for the current job." if state.get("draft") else ""))
            return
        send_customer_email(built["job"], built["invoice_url"], built["contract_url"], built["total"])
        state.update({"draft": {}, "built": None})
        save_state(state)
        tg_send(chat_id, f"✅ Sent to {built['job']['renter_email']}.")
        return

    if upper in COMMANDS_CANCEL:
        had_job = bool(state.get("draft") or state.get("built"))
        discard_built(state)
        state["draft"] = {}
        save_state(state)
        tg_send(chat_id, "❌ Cleared. Nothing was emailed. Send the next job whenever." if had_job
                else "Nothing in progress. Send me a job whenever.")
        return

    if upper in ("/START", "/HELP", "HELP"):
        tg_send(chat_id, "Just tell me about the rental however it comes out — name, email, equipment, "
                         "dates, delivery or pickup. Send it in one message or a few. I'll fill in the usual "
                         "rates and ask for anything missing.\n\nCorrect anything in plain words "
                         "(\"make it 160 a day\"). SEND emails it to the customer, CANCEL clears it.")
        return

    tg_send(chat_id, "Got it — reading that…")
    try:
        intent, fields = claude_parse(text, state.get("draft"))
    except Exception:
        tg_send(chat_id, "I couldn't make sense of that one. Try saying it a different way — "
                         "nothing you sent earlier is lost.")
        return
    fields = clean_fields(fields)

    if intent == "new" and state.get("draft"):
        if state.get("built"):
            tg_send(chat_id, f"Starting a new job — the unsent draft for "
                             f"{state['draft'].get('renter_name', 'the previous renter')} was cancelled.")
        discard_built(state)
        state["draft"] = {}

    draft = state.get("draft") or {}
    if not fields and draft:
        tg_send(chat_id, "I didn't see any job details in that. Here's where things stand:")
    draft.update(fields)
    # Dates and day-count go together: a new date range replaces an old day count.
    if ("date_out" in fields or "date_due" in fields) and "rental_days" not in fields:
        draft.pop("rental_days", None)
    if "rental_days" in fields and "date_due" not in fields and "date_out" in draft:
        draft.pop("date_due", None)
    state["draft"] = draft
    save_state(state)

    job, missing, assumptions = complete_job(draft)

    if missing:
        lines = summary_lines(job)
        msg = ("Here's what I have so far:\n" + "\n".join(lines) + "\n\n") if lines else ""
        msg += "Still need: " + ", ".join(missing) + ".\nJust reply with those — no need to resend the rest."
        tg_send(chat_id, msg)
        return

    # Everything's here — (re)build the invoice + contract.
    if state.get("built"):
        tg_send(chat_id, "Updating the invoice and contract…")
        discard_built(state)
    else:
        tg_send(chat_id, "Building the invoice and contract (~15 seconds)…")

    equipment = EQUIPMENT_CATALOG[job["equipment_key"]]
    job["equipment_label"] = equipment["label"]
    job = {k: v for k, v in job.items() if not k.startswith("_")}
    invoice_id, invoice_url, total = build_invoice(job, equipment)
    try:
        submission_id, contract_url = build_docuseal_submission(job, equipment)
    except Exception:
        void_invoice(invoice_id)  # don't leave a half-built job behind
        raise

    state["built"] = {
        "job": job, "invoice_id": invoice_id, "invoice_url": invoice_url,
        "submission_id": submission_id, "contract_url": contract_url, "total": total,
    }
    save_state(state)

    rental, taxes, delivery, deposit = estimate(job, equipment)
    breakdown = [f"Rental: {money(rental)} ({job['days']} × {money(job['rental_rate_day'])})",
                 f"Tax: {money(taxes)} ({job['sales_tax_pct']}% sales"
                 + (f" + {job['heavy_equipment_tax_pct']}% heavy equip." if job['heavy_equipment_tax_pct'] else "") + ")"]
    if delivery:
        breakdown.append(f"Delivery: {money(delivery)}")
    if deposit:
        breakdown.append(f"Deposit (refundable): {money(deposit)}")

    msg = "Draft ready:\n" + "\n".join(summary_lines(job))
    msg += "\n\n" + "\n".join(breakdown) + f"\nTOTAL: {money(total)}"
    if assumptions:
        msg += "\n\nAssumed: " + ", ".join(assumptions) + "."
    msg += (f"\n\nInvoice: {invoice_url}\nContract: {contract_url}\n\n"
            f"Want changes? Just tell me (\"make it 160 a day\", \"due Monday\").\n"
            f"Reply SEND to email {job['renter_email']}, or CANCEL to discard.")
    tg_send(chat_id, msg)


@app.route("/", methods=["GET"])
def health():
    return "Grit Line bot is running."


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
