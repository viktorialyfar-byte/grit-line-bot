"""
Grit Line Rentals — Telegram Booking Bot
==========================================
You message this bot on Telegram with a new rental's info in plain English.
It parses it, builds a Stripe invoice (with tax) and a pre-filled DocuSeal
contract, and replies with both links for you to review.

NOTHING is emailed to the customer until you reply "SEND" to confirm.
Reply "CANCEL" to discard a pending job instead.

Deploy this on Render.com (free tier). See README_DEPLOY.md for the full
step-by-step. All secrets come from environment variables — nothing is
hardcoded in this file.
"""
import json
import os
import smtplib
import time
import urllib.error
import urllib.parse
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path

from flask import Flask, request

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
DEFAULT_SALES_TAX_PCT = 8.10           # Spokane County, unincorporated areas — WA DOR local sales tax rate, Q1 2026
DEFAULT_HEAVY_EQUIPMENT_TAX_PCT = 1.25  # WA Heavy Equipment Rental Tax, RCW 82.51 — flat statewide rate
STATE_FILE = Path("/tmp/pending_job.json")  # survives until the dyno restarts; good enough for single-user low-volume use

EQUIPMENT_CATALOG = {
    "dingo_trailer": {
        "label": "Dingo Mini Skid Steer + 5x10 Utility Trailer",
        "rental_price_id": "price_1U6IRkFNCgHCBzebRLQBpaYe",
        "deposit_price_id": "price_1U6IRlFNCgHCBzebiqxjKy5V",
    },
    "trailer_22ft": {
        "label": "22ft Equipment/Auto Trailer",
        "rental_price_id": "price_1U6YxKFNCgHCBzeb6FxkG02t",
        "deposit_price_id": "price_1U6YxMFNCgHCBzebQifqFWfX",
    },
    "dump_trailer_5x10": {
        "label": "5x10 Dump Trailer",
        "rental_price_id": "price_1U6YxGFNCgHCBzebjjs2c4AU",
        "deposit_price_id": "price_1U6YxIFNCgHCBzebyUeXFUnK",
    },
}

# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------
def tg_send(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)

# ---------------------------------------------------------------------------
# Claude — parse freeform job text into structured fields
# ---------------------------------------------------------------------------
PARSE_SCHEMA_HINT = """Extract these fields from the renter's message as JSON. Use null for anything not mentioned.
{
"renter_name": string,
"renter_email": string,
"renter_phone": string or null,
"drivers_license": string or null,
"job_site_address": string,
"equipment_key": one of "dingo_trailer" | "trailer_22ft" | "dump_trailer_5x10" | null,
"date_out": string (human readable, e.g. "Sat 08/22/2026 7:00 AM"),
"date_due": string,
"rental_rate_day": number,
"security_deposit": number or null,
"pickup_method": "renter_pickup" or "delivery",
"delivery_amount": number or null,
"sales_tax_pct": number or null,
"heavy_equipment_tax_pct": number or null
}
Grit Line's standard rates (Spokane County sales tax + WA Heavy Equipment
Rental Tax) are applied automatically — leave "sales_tax_pct" and
"heavy_equipment_tax_pct" null unless the renter's message explicitly
overrides them (e.g. states different rates, or says the job is tax-exempt /
out of state / no tax, in which case use 0).
Respond with ONLY the JSON object, no other text."""

def claude_parse(text):
    url = "https://api.anthropic.com/v1/messages"
    body = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": f"{PARSE_SCHEMA_HINT}\n\nMessage:\n{text}"}],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    raw = result["content"][0]["text"].strip()
    if raw.startswith("```"):
        raw = raw.strip("`").split("\n", 1)[1] if "\n" in raw else raw
    return json.loads(raw)

def _coerce_numeric_fields(job):
    """Claude's parser is instructed to return numbers, but LLM output isn't
    guaranteed — if a number ever comes back as a string like "175" or
    "$175.00", coerce it here instead of crashing later on an f-string
    format spec or an arithmetic op."""
    for key in ("rental_rate_day", "security_deposit", "delivery_amount",
                "sales_tax_pct", "heavy_equipment_tax_pct"):
        val = job.get(key)
        if isinstance(val, str):
            cleaned = val.replace("$", "").replace(",", "").strip()
            try:
                job[key] = float(cleaned) if cleaned else None
            except ValueError:
                job[key] = None
    return job

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

def find_or_create_customer(name, email, description=""):
    q = urllib.parse.quote(f"email:'{email}'")
    result = stripe_request("GET", f"/customers/search?query={q}")
    if result.get("data"):
        return result["data"][0]["id"]
    created = stripe_request("POST", "/customers", {"name": name, "email": email, "description": description})
    return created["id"]

def build_invoice(job, equipment):
    customer_id = find_or_create_customer(job["renter_name"], job["renter_email"], job.get("job_site_address", ""))

    rental_cents = round(job["rental_rate_day"] * 100)
    deposit_cents = round((job.get("security_deposit") or 0) * 100)
    # Grit Line's standard rates apply automatically unless the renter's
    # message explicitly gave different ones (parsed onto the job). These are
    # two distinct taxes and are itemized separately on the invoice.
    sales_tax_pct = job.get("sales_tax_pct")
    if sales_tax_pct is None:
        sales_tax_pct = DEFAULT_SALES_TAX_PCT
    heavy_tax_pct = job.get("heavy_equipment_tax_pct")
    if heavy_tax_pct is None:
        heavy_tax_pct = DEFAULT_HEAVY_EQUIPMENT_TAX_PCT
    # record the rates actually applied, for the confirmation message + saved state
    job["sales_tax_pct"] = sales_tax_pct
    job["heavy_equipment_tax_pct"] = heavy_tax_pct
    sales_tax_cents = round(rental_cents * sales_tax_pct / 100)
    heavy_tax_cents = round(rental_cents * heavy_tax_pct / 100)

    stripe_request("POST", "/invoiceitems", {
        "customer": customer_id,
        "amount": rental_cents,
        "currency": "usd",
        "description": f"{equipment['label']} rental — {job['date_out']} to {job['date_due']}",
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
        "description": f"{equipment['label']} rental — {job['date_out']} to {job['date_due']}. "
                        f"{'Renter pickup & return' if job['pickup_method']=='renter_pickup' else 'Delivery'}.",
        "footer": f"Job site: {job.get('job_site_address','')}. Rental Agreement must be signed before pickup.",
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
                    {"name": "Phone Email", "value": job.get("renter_phone") or job["renter_email"]},
                    {"name": "Job Site Address", "value": job.get("job_site_address", "")},
                    {"name": "Equipment Rented", "value": equipment["label"]},
                    {"name": "Date Time Out", "value": job["date_out"]},
                    {"name": "Date Time Due", "value": job["date_due"]},
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
    return second_party["embed_src"]

# ---------------------------------------------------------------------------
# Email (sent only after explicit "SEND" confirmation)
# ---------------------------------------------------------------------------
def send_customer_email(job, invoice_url, contract_url, total):
    subject = "Grit Line Rentals — Contract & Invoice"
    body = f"""Hi {job['renter_name']},

Thanks for booking with Grit Line Equipment Rentals! Here's everything for your rental:

EQUIPMENT: {job.get('equipment_label', '')}
PICKUP: {job['date_out']}
RETURN: {job['date_due']}
JOB SITE: {job.get('job_site_address', '')}

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
# State (single pending job at a time — simple JSON file)
# ---------------------------------------------------------------------------
def save_pending(data):
    STATE_FILE.write_text(json.dumps(data))

def load_pending():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return None

def clear_pending():
    if STATE_FILE.exists():
        STATE_FILE.unlink()

# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(force=True)
    message = update.get("message") or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()

    if not chat_id or not text:
        return "ok"

    if chat_id != ALLOWED_CHAT_ID:
        # Silently ignore messages from anyone who isn't Viktoria.
        return "ok"

    try:
        handle_message(chat_id, text)
    except Exception as e:
        tg_send(chat_id, f"⚠️ Something broke: {e}")
    return "ok"

def handle_message(chat_id, text):
    upper = text.strip().upper()

    if upper == "SEND":
        pending = load_pending()
        if not pending:
            tg_send(chat_id, "Nothing pending to send.")
            return
        send_customer_email(pending["job"], pending["invoice_url"], pending["contract_url"], pending["total"])
        clear_pending()
        tg_send(chat_id, f"✅ Sent to {pending['job']['renter_email']}.")
        return

    if upper == "CANCEL":
        pending = load_pending()
        if not pending:
            tg_send(chat_id, "Nothing pending to cancel.")
            return
        void_invoice(pending["invoice_id"])
        clear_pending()
        tg_send(chat_id, "❌ Cancelled and voided the invoice. Nothing was emailed.")
        return

    # Otherwise: treat as a new job.
    tg_send(chat_id, "Got it — working on it (this can take ~15-20 seconds)...")
    job = claude_parse(text)
    job = _coerce_numeric_fields(job)

    missing = [f for f in ("renter_name", "renter_email", "job_site_address", "date_out", "date_due", "rental_rate_day")
               if not job.get(f)]
    if missing:
        tg_send(chat_id, f"I couldn't find these details in your message: {', '.join(missing)}. "
                          f"Please resend with all of them included.")
        return

    equipment_key = job.get("equipment_key") or "dingo_trailer"
    equipment = EQUIPMENT_CATALOG.get(equipment_key, EQUIPMENT_CATALOG["dingo_trailer"])
    job["equipment_label"] = equipment["label"]
    job.setdefault("pickup_method", "renter_pickup")

    invoice_id, invoice_url, total = build_invoice(job, equipment)
    contract_url = build_docuseal_submission(job, equipment)

    save_pending({
        "job": job, "invoice_id": invoice_id, "invoice_url": invoice_url,
        "contract_url": contract_url, "total": total,
    })

    tax_note = ""
    if job.get("sales_tax_pct") or job.get("heavy_equipment_tax_pct"):
        tax_note = f" (incl. {job.get('sales_tax_pct', 0)}% sales tax + {job.get('heavy_equipment_tax_pct', 0)}% equipment tax)"

    tg_send(chat_id,
        f"Draft ready for {job['renter_name']}:\n\n"
        f"Equipment: {equipment['label']}\n"
        f"{job['date_out']} → {job['date_due']}\n"
        f"Total: ${total:.2f}{tax_note}\n\n"
        f"Invoice: {invoice_url}\n"
        f"Contract: {contract_url}\n\n"
        f"Reply SEND to email {job['renter_email']}, or CANCEL to discard."
    )

@app.route("/", methods=["GET"])
def health():
    return "Grit Line bot is running."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
