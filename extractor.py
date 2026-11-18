"""Pull payment details out of an invoice PDF.

Two strategies:
  * Claude (if ANTHROPIC_API_KEY or an `ant auth login` profile is available) - reads the
    PDF itself, so it copes with any layout and with scanned invoices.
  * Rule-based fallback - regexes over the PDF text layer. Works on most typed invoices,
    but always review the result before generating documents.
"""
import base64
import io
import json
import os
import re
from datetime import datetime

import pdfplumber

FIELDS = [
    "supplier_name", "invoice_number", "invoice_date", "description", "amount",
    "bank_name", "account_holder", "account_number", "branch_code",
]

BANKS = [
    ("First National Bank", "FNB"), ("FNB", "FNB"), ("Standard Bank", "Standard Bank"),
    ("ABSA", "ABSA"), ("Nedbank", "Nedbank"), ("Capitec", "Capitec"), ("Investec", "Investec"),
    ("TymeBank", "TymeBank"), ("Discovery Bank", "Discovery Bank"), ("African Bank", "African Bank"),
    ("Bidvest Bank", "Bidvest Bank"), ("Mercantile", "Mercantile Bank"),
]

MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"


def _pdf_bytes(src):
    """Accept raw PDF bytes or a file path."""
    if isinstance(src, (bytes, bytearray, memoryview)):
        return bytes(src)
    with open(src, "rb") as f:
        return f.read()


def pdf_text(src):
    try:
        with pdfplumber.open(io.BytesIO(_pdf_bytes(src))) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return ""


# ---------------------------------------------------------------- helpers

def parse_amount(s):
    """'R 5,865.00' / '64 626,90' / '57,230.11' -> float."""
    s = re.sub(r"[^\d,. ]", "", s or "").strip().replace(" ", "")
    if not s:
        return None
    if re.search(r",\d{2}$", s) and "." not in s:
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")
    if s.count(".") > 1:  # 1.234.567,00 style already handled; drop stray thousands dots
        head, tail = s.rsplit(".", 1)
        s = head.replace(".", "") + "." + tail
    try:
        return round(float(s), 2)
    except ValueError:
        return None


AMOUNT_RE = re.compile(r"(\d{1,3}(?:[ ,]\d{3})+(?:[.,]\d{2})|\d+[.,]\d{2})(?!\d)")


def parse_date(s):
    if not s:
        return ""
    s = re.sub(r"(\d)(st|nd|rd|th)", r"\1", s.strip()).replace(",", "")
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%Y-%m-%d", "%Y/%m/%d",
                "%d-%b-%y", "%d-%b-%Y", "%d %b %Y", "%d %B %Y", "%d %b %y", "%B %d %Y",
                "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def digits(s):
    return re.sub(r"\D", "", s or "")


# ---------------------------------------------------------------- rule-based

def _invoice_number(t):
    pats = [
        r"Invoice\s*(?:No\.?|Number|#)\s*[:\-]?\s*([A-Za-z0-9][\w\-/]*(?: \d+)?)",
        r"^\s*(?:Invoice\s+)?NUMBER\s*:\s*(\S+)",
        r"\bINVOICE\s+((?:INV|IN)[-\w]+)",
        r"\bInv(?:oice)?\s*#\s*(\S+)",
        r"^\s*(?:Tax\s+)?Invoice\s+#?(\d[\w\-/]*)",
    ]
    for p in pats:
        for m in re.finditer(p, t, re.I | re.M):
            if re.search(r"\d", m.group(1)):
                return m.group(1).strip()
    return ""


def _invoice_date(t):
    date_pat = (rf"(\d{{1,2}}[/.-]\d{{1,2}}[/.-]\d{{2,4}}|\d{{4}}-\d{{2}}-\d{{2}}|"
                rf"\d{{1,2}}(?:st|nd|rd|th)?[ -](?:{MONTHS})[a-z]*[ -]\d{{2,4}})")
    for p in (rf"(?<!DUE )(?<!Due )DATE\s*:?\s*{date_pat}", rf"Invoice Date\s*:?\s*{date_pat}"):
        m = re.search(p, t, re.I)
        if m:
            d = parse_date(m.group(1))
            if d:
                return d
    m = re.search(date_pat, t, re.I)
    return parse_date(m.group(1)) if m else ""


def _total(t):
    lines = t.splitlines()
    priority = [r"grand\s+total", r"total\s*\(incl", r"total\s+due", r"amount\s+due",
                r"balance\s+due", r"total\s+incl", r"^\s*total\b", r"\bZAR\b"]
    for kw in priority:
        for line in reversed(lines):
            if re.search(kw, line, re.I) and not re.search(r"sub\s*-?total|excl|vat\s*:", line, re.I):
                found = AMOUNT_RE.findall(line)
                if found:
                    return parse_amount(found[-1])
    # otherwise take the largest money-looking figure on the page
    vals = [parse_amount(a) for a in AMOUNT_RE.findall(t)]
    vals = [v for v in vals if v]
    return max(vals) if vals else None


def _account_number(t):
    m = re.search(r"(?:Account|Acc|Acct)\.?\s*(?:Number|No\.?|Num|#)\s*[:.]?\s*(\d[\d ]{4,20}\d)", t, re.I)
    return digits(m.group(1)) if m else ""


def _branch_code(t):
    m = re.search(r"Branch\s*(?:Code|No\.?|Number)\s*[:.]?\s*(\d[\d ]{2,8}\d)", t, re.I)
    if not m:
        return ""
    return digits(m.group(1)).zfill(6)


def _bank(t):
    for needle, name in BANKS:
        if re.search(r"\b" + re.escape(needle) + r"\b", t, re.I):
            return name
    return ""


def _supplier(t, ignore_names):
    m = re.search(r"Account\s*(?:Name|Holder)\s*:\s*(.+)", t, re.I)
    if m:
        return m.group(1).strip()
    lines = [l.strip() for l in t.splitlines()]
    for i, l in enumerate(lines):
        if re.match(r"^(Banking|Bank)\s+Details\s*:?$", l, re.I) and i + 1 < len(lines):
            nxt = lines[i + 1]
            if nxt and ":" not in nxt and not _bank(nxt):
                return nxt
    skip = re.compile(r"invoice|number|reference|date|page|^from\b|^to\b|^bill|vat|tel|phone|"
                      r"email|@|www\.|reg\s*no|po box|suite|street|road|drive|place", re.I)
    for l in lines:
        for n in ignore_names:
            l = re.sub(re.escape(n), "", l, flags=re.I).strip()
        if len(l) < 3 or l[0].isdigit() or skip.search(l):
            continue
        return l
    return ""


def _description(t):
    lines = [l.strip() for l in t.splitlines()]
    for i, l in enumerate(lines):
        if re.match(r"^(Description|DATE OF SERVICE|QTY|Item)\b", l, re.I):
            for nxt in lines[i + 1:i + 4]:
                if re.search(rf"(?:{MONTHS})[a-z]*\s+\d{{4}}", nxt, re.I) and len(nxt) < 25:
                    continue  # a bare date line
                cleaned = re.sub(r"\d{1,4}[/.-]\d{1,2}[/.-]\d{2,4}", "", nxt)
                cleaned = AMOUNT_RE.sub("", cleaned)
                cleaned = re.sub(r"\s\d+([.,:]\d+)?\b|\bR\b|%", "", " " + cleaned).strip(" -/")
                if len(cleaned) > 3:
                    return re.sub(r"\s{2,}", " ", cleaned)
    m = re.search(r"^\s*For\s*:?\s*(.+)", t, re.I | re.M)
    return m.group(1).strip() if m else ""


def extract_rules(text, ignore_names=()):
    return {
        "supplier_name": _supplier(text, ignore_names),
        "invoice_number": _invoice_number(text),
        "invoice_date": _invoice_date(text),
        "description": _description(text),
        "amount": _total(text),
        "bank_name": _bank(text),
        "account_holder": "",
        "account_number": _account_number(text),
        "branch_code": _branch_code(text),
    }


# ---------------------------------------------------------------- Claude

SCHEMA = {
    "type": "object",
    "properties": {
        "supplier_name": {"type": "string", "description": "Company/person who issued the invoice (not the customer)"},
        "invoice_number": {"type": "string"},
        "invoice_date": {"type": "string", "description": "YYYY-MM-DD, empty if unknown"},
        "description": {"type": "string", "description": "Short summary of what was supplied, max 60 chars"},
        "amount": {"type": "number", "description": "Total amount payable incl. VAT"},
        "bank_name": {"type": "string"},
        "account_holder": {"type": "string"},
        "account_number": {"type": "string", "description": "Digits only"},
        "branch_code": {"type": "string", "description": "Digits only, 6 digits"},
    },
    "required": FIELDS,
    "additionalProperties": False,
}


def claude_available():
    if os.environ.get("INVOICE_AI", "").lower() == "off":
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def extract_claude(src, ignore_names=()):
    import anthropic

    data = base64.standard_b64encode(_pdf_bytes(src)).decode()
    customer = ", ".join(ignore_names) or "the customer"
    prompt = (
        "Extract the payment details from this supplier invoice. "
        f"The customer (payer) is {customer}; never return the customer as the supplier. "
        "Use empty strings for anything not on the invoice and 0 for an unknown amount."
    )
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=os.environ.get("INVOICE_MODEL", "claude-opus-5"),
        max_tokens=4000,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}},
            {"type": "text", "text": prompt},
        ]}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to read this invoice")
    text = next(b.text for b in response.content if b.type == "text")
    out = json.loads(text)
    out["account_number"] = digits(out.get("account_number"))
    bc = digits(out.get("branch_code"))
    out["branch_code"] = bc.zfill(6) if bc else ""
    out["invoice_date"] = parse_date(out.get("invoice_date")) or out.get("invoice_date", "")
    out["amount"] = round(float(out.get("amount") or 0), 2) or None
    return out


def extract(src, ignore_names=()):
    """`src` = PDF bytes or path. Return (fields, method, warning, text)."""
    src = _pdf_bytes(src)
    text = pdf_text(src)
    warning = ""
    if claude_available():
        try:
            fields = extract_claude(src, ignore_names)
            # backfill any blanks from the text layer
            rules = extract_rules(text, ignore_names) if text.strip() else {}
            for k, v in rules.items():
                if not fields.get(k) and v:
                    fields[k] = v
            return fields, "ai", "", text
        except Exception as e:  # network, auth, etc. - fall through to rules
            warning = f"AI extraction failed ({type(e).__name__}: {e}); used rule-based reading instead. "
    if not text.strip():
        return {k: "" for k in FIELDS}, "none", warning + (
            "No text found in this PDF (it may be a scan). Please type the details in."), text
    return extract_rules(text, ignore_names), "rules", warning, text
