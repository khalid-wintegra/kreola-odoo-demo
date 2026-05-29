import os
import random
from datetime import datetime

import requests
from fastapi import FastAPI, Header, HTTPException
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# =========================================================
# CONFIG
# =========================================================

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USERNAME = os.getenv("ODOO_USERNAME")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

API_KEY = os.getenv("API_KEY", "dev")

JOURNAL_MAPPING = {
    "91": 1,
    "96": 12,
}

# =========================================================
# AUTH
# =========================================================

def auth(key):
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

# =========================================================
# ODOO CORE
# =========================================================

def json_rpc(url, method, params):
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": random.randint(0, 10**9),
    }

    r = requests.post(url, json=payload)
    r.raise_for_status()

    data = r.json()
    if data.get("error"):
        raise Exception(data["error"])

    return data["result"]


uid = None

def login():
    global uid
    if uid:
        return uid

    uid = json_rpc(
        f"{ODOO_URL}/jsonrpc",
        "call",
        {
            "service": "common",
            "method": "login",
            "args": [ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD],
        },
    )
    return uid


def call(model, method, args=None, kwargs=None):
    return json_rpc(
        f"{ODOO_URL}/jsonrpc",
        "call",
        {
            "service": "object",
            "method": "execute_kw",
            "args": [
                ODOO_DB,
                login(),
                ODOO_PASSWORD,
                model,
                method,
                args or [],
                kwargs or {},
            ],
        },
    )

# =========================================================
# HELPERS (UNCHANGED LOGIC)
# =========================================================

def parse_item_name(item_name):
    parts = [p.strip() for p in item_name.split(";")]

    while len(parts) < 5:
        parts.append("")

    return {
        "sales_order": parts[0],
        "guest_name": parts[1],
        "file_ref": parts[2],
        "product_name": parts[3],
        "order_date": parts[4],
    }


def convert_order_date(date_str):
    if not date_str:
        return False

    return datetime.strptime(date_str, "%d/%m/%Y").strftime("%Y-%m-%d")


def get_currency_id(code):
    res = call(
        "res.currency",
        "search",
        [[["name", "=", code]]],
        {"limit": 1},
    )

    if not res:
        raise Exception(f"Currency not found: {code}")

    return res[0]


def get_partner(buyer):
    res = call(
        "res.partner",
        "search",
        [[["vat", "=", buyer["VAT"]]]],
        {"limit": 1},
    )

    if res:
        return res[0]

    return call(
        "res.partner",
        "create",
        [{
            "name": buyer["Name"],
            "vat": buyer["VAT"],
            "phone": buyer["Phone"],
            "email": buyer["Email"],
            "street": buyer["Address"],
            "customer_rank": 1,
        }],
    )


def get_income_account():
    res = call(
        "account.account",
        "search",
        [[["account_type", "=", "income"]]],
        {"limit": 1},
    )

    if not res:
        raise Exception("No income account found")

    return res[0]


def get_tax(rate):
    res = call(
        "account.tax",
        "search",
        [[
            ["amount", "=", rate],
            ["type_tax_use", "=", "sale"]
        ]],
        {"limit": 1},
    )

    return res[0] if res else False


def get_product(name, income_account_id):
    res = call(
        "product.product",
        "search",
        [[["name", "=", name]]],
        {"limit": 1},
    )

    if res:
        return res[0]

    return call(
        "product.product",
        "create",
        [{
            "name": name,
            "type": "service",
            "sale_ok": True,
            "purchase_ok": False,
            "property_account_income_id": income_account_id,
        }],
    )

# =========================================================
# INVOICE ENDPOINT (FULL LOGIC)
# =========================================================

@app.post("/invoice")
def create_invoice(payload: dict, x_api_key: str = Header(None)):
    auth(x_api_key)

    partner_id = get_partner(payload["Buyer"])
    currency_id = get_currency_id(payload["Currency"])

    # journal fallback logic preserved
    invoice_journal_id = JOURNAL_MAPPING.get(payload["JournalID"])

    if invoice_journal_id:
        ok = call("account.journal", "search", [[["id", "=", invoice_journal_id]]], {"limit": 1})
        if not ok:
            invoice_journal_id = None

    if not invoice_journal_id:
        j = call("account.journal", "search", [[["type", "=", "sale"]]], {"limit": 1})
        if not j:
            raise Exception("No sales journal found")
        invoice_journal_id = j[0]

    income_account_id = get_income_account()
    tax_id = get_tax(15)

    invoice_lines = []

    for line in payload["InvoiceContent"]:
        parsed = parse_item_name(line["ItemName"])

        product_id = get_product(parsed["product_name"], income_account_id)

        qty = line.get("Quantity") or 1
        price_unit = float(line["TotalExVAT"]) / qty

        invoice_lines.append((0, 0, {
            "name": line["ItemName"],
            "product_id": product_id,
            "quantity": qty,
            "price_unit": price_unit,
            "account_id": income_account_id,
            "tax_ids": [(6, 0, [tax_id])] if tax_id else [],
        }))

    # duplicate check preserved
    existing = call(
        "account.move",
        "search",
        [[
            ["move_type", "=", "out_invoice"],
            ["ref", "=", payload["Identifier"]],
        ]],
        {"limit": 1},
    )

    if existing:
        return {"status": "exists", "invoice_id": existing[0]}

    first_line = parse_item_name(payload["InvoiceContent"][0]["ItemName"])

    invoice_id = call(
        "account.move",
        "create",
        [{
            "move_type": "out_invoice",
            "partner_id": partner_id,
            "invoice_date": payload["InvoiceDate"],
            "invoice_date_due": payload["DueDate"],
            "invoice_origin": payload["Identifier"],
            "ref": payload["Identifier"],
            "journal_id": invoice_journal_id,
            "currency_id": currency_id,
            "invoice_line_ids": invoice_lines,

            "x_classification": payload.get("Classification"),
            "x_guest_name": first_line["guest_name"],
            "x_file_reference": first_line["file_ref"],
            "x_order_date": convert_order_date(first_line["order_date"]),
        }],
    )

    call("account.move", "action_post", [[invoice_id]])

    return {"status": "created", "invoice_id": invoice_id}

# =========================================================
# PAYMENT ENDPOINT (FULL LOGIC)
# =========================================================

@app.post("/payment")
def create_payment(payload: dict, x_api_key: str = Header(None)):
    auth(x_api_key)

    currency_id = get_currency_id(payload["Currency"])

    journal_id = JOURNAL_MAPPING.get(payload["JournalID"])

    if journal_id:
        ok = call(
            "account.journal",
            "search",
            [[
                ["id", "=", journal_id],
                ["type", "in", ["bank", "cash"]],
            ]],
            {"limit": 1},
        )
        if not ok:
            journal_id = None

    if not journal_id:
        j = call(
            "account.journal",
            "search",
            [[["type", "in", ["bank", "cash"]]]],
            {"limit": 1},
        )
        if not j:
            raise Exception("No bank/cash journal found")
        journal_id = j[0]

    method_line = call(
        "account.payment.method.line",
        "search",
        [[
            ["journal_id", "=", journal_id],
            ["payment_type", "=", "inbound"],
        ]],
        {"limit": 1},
    )

    if not method_line:
        raise Exception("No inbound payment method line found")

    payment_register = call(
        "account.payment.register",
        "create",
        [{
            "payment_date": payload["PaymentDate"],
            "amount": float(payload["Amount"]),
            "currency_id": currency_id,
            "journal_id": journal_id,
            "payment_method_line_id": method_line[0],
            "communication": payload["Reference"],
        }],
        {
            "context": {
                "active_model": "account.move",
                "active_ids": [payload["DocumentID"]],
            }
        },
    )

    call(
        "account.payment.register",
        "action_create_payments",
        [[payment_register]],
    )

    return {"status": "paid"}