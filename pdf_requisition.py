"""Draw the Zenex payment requisition as a PDF, laid out like the FIN1 Excel template.

`render(spec)` returns PDF bytes. The spec holds what's on the form plus the signers; each
signer may carry a PNG signature and the date signed, which are drawn on their line.
"""
import base64
import io
import os
import zipfile
from datetime import date, datetime

from reportlab.lib.colors import HexColor, black
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

HERE = os.path.dirname(os.path.abspath(__file__))
ORANGE = HexColor("#E97132")    # template theme accent (FIN1 bar)
HEADER = HexColor("#A6A6A6")    # table header fill
SUBTOTAL = HexColor("#D9D9D9")  # TOTAL DUE / TOTAL PAYMENT fill

LINE_ROWS, ALLOC_ROWS = 14, 11
DEFAULT_POSITIONS = {"prepared": "Senior Bookkeeper",
                     "reviewed": "Manager: Corporate Services and Finance",
                     "approved": "Chief Executive Officer:"}
LABELS = {"prepared": "Prepared by", "reviewed": "Reviewed by", "approved": "Approved & Authorised by"}

_logo = None


def _logo_image():
    """The Zenex logo, taken from the requisition template so it always matches."""
    global _logo
    if _logo is None:
        path = os.path.join(HERE, "excel_templates", "requisition_template.xlsx")
        if os.path.exists(path):
            raw = open(path, "rb").read()
        else:
            from requisition_template_data import DATA
            raw = base64.b64decode(DATA)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            media = sorted(n for n in z.namelist() if n.startswith("xl/media/"))
            _logo = ImageReader(io.BytesIO(z.read(media[0]))) if media else False
    return _logo or None


def money(v):
    return f"R{float(v):,.2f}"


def _d(v):
    if not v:
        return ""
    if isinstance(v, (date, datetime)):
        return v.strftime("%d/%m/%Y")
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return str(v)


def _fit(text, font, size, width, min_size=6.5):
    """Shrink text to fit the width, then cut it with an ellipsis if still too long."""
    text = str(text or "")
    while size > min_size and stringWidth(text, font, size) > width:
        size -= 0.5
    if stringWidth(text, font, size) > width:
        while text and stringWidth(text + "…", font, size) > width:
            text = text[:-1]
        text += "…"
    return text, size


def render(spec):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"Payment Requisition - {spec.get('payee', '')}")
    c.setAuthor("Zenex Foundation")
    pw, ph = A4
    left, right = 40, pw - 40
    W = right - left
    # column widths in the template's proportions (B, C, D, E)
    cols = [15.45, 42.45, 23.0, 23.9]
    tot = sum(cols)
    xB = left
    xD = left + W * (cols[0] + cols[1]) / tot
    xE = xD + W * cols[2] / tot
    wDesc, wD, wE = xD - xB, xE - xD, right - xE

    def text(x, y, s, font="Helvetica", size=9, align="left", width=None):
        if width:
            s, size = _fit(s, font, size, width)
        c.setFont(font, size)
        {"left": c.drawString, "right": c.drawRightString, "center": c.drawCentredString}[align](x, y, s)

    def hline(y, x0=left, x1=right, w=0.6):
        c.setLineWidth(w)
        c.line(x0, y, x1, y)

    def fill(y, h, color):
        c.setFillColor(color)
        c.rect(left, y, W, h, stroke=0, fill=1)
        c.setFillColor(black)

    y = ph - 36

    # --- header block
    top = y
    fill(y - 16, 16, ORANGE)
    text(right - 4, y - 12, "FIN1", "Helvetica-Bold", 10, "right")
    y -= 16
    text(left + W / 2, y - 18, "PAYMENT REQUISITION FORM", "Helvetica-Bold", 11, "center")
    y -= 26
    hline(y, w=0.4)
    logo = _logo_image()
    if logo:
        iw, ih = logo.getSize()
        h = 52
        w = h * iw / ih
        c.drawImage(logo, right - w - 18, y - h - 6, width=w, height=h, mask="auto")
    text(left + 4, y - 22, "ZENEX FOUNDATION", "Helvetica-Bold", 13)
    text(left + 4, y - 42, "Electronic Banking Requisition", "Helvetica-Bold", 9.5)
    y -= 66
    hline(y, w=0.4)
    # payee row
    text(left + 4, y - 16, "PAYEE :", "Helvetica-Bold", 10.5)
    cx, pw_ = (xB + 70 + xD) / 2, xD - (xB + 70) - 8
    full = str(spec.get("payee", ""))
    payee, size = _fit(full, "Helvetica-Bold", 10.5, pw_, min_size=8.5)
    if payee == full:
        lines = [payee]
    else:  # long name: two lines, split at the word nearest the middle
        words = full.split()
        cut = min(range(1, len(words)), key=lambda i: abs(len(" ".join(words[:i])) - len(full) / 2)) if len(words) > 1 else 1
        lines = [" ".join(words[:cut]), " ".join(words[cut:])]
        size = min(_fit(l, "Helvetica-Bold", 9, pw_)[1] for l in lines)
        lines = [_fit(l, "Helvetica-Bold", size, pw_, min_size=size)[0] for l in lines]
    c.setLineWidth(0.7)
    for i, line in enumerate(lines):
        ly = y - 16 + (5 - 10 * i if len(lines) == 2 else 0)
        text(cx, ly, line, "Helvetica-Bold", size, "center")
        uw = stringWidth(line, "Helvetica-Bold", size)
        c.line(cx - uw / 2, ly - 2, cx + uw / 2, ly - 2)
    text(xE - 6, y - 16, "DATE:", "Helvetica-Bold", 10, "right")
    text(right - 4, y - 16, _d(spec.get("date")), "Helvetica-Bold", 10.5, "right")
    y -= 26
    c.setLineWidth(1)
    c.rect(left, y, W, top - y, stroke=1, fill=0)
    y -= 10

    # --- description table
    def table(y, header, rows, n, row_h, total_label, total):
        hh = 20
        fill(y - hh, hh, HEADER)
        text((xB + xD) / 2 if header[0] == "Description" else xB + 4, y - 14, header[0], "Helvetica-Bold", 10,
             "center" if header[0] == "Description" else "left")
        text((xD + xE) / 2, y - 14, header[1], "Helvetica-Bold", 9, "center")
        text((xE + right) / 2, y - 14, header[2], "Helvetica-Bold", 9, "center")
        t = y
        y -= hh
        hline(y, w=1)
        for i in range(n):
            r = rows[i] if i < len(rows) else None
            if r:
                text(xB + 4, y - row_h + 5, r[0], "Helvetica", 8.5, width=wDesc - 8)
                text((xD + xE) / 2, y - row_h + 5, r[1] or "", "Helvetica-Oblique" if header[0] == "Description" else "Helvetica",
                     8.5, "center", width=wD - 8)
                if r[2] not in (None, ""):
                    text(right - 4, y - row_h + 5, money(r[2]), "Helvetica", 8.5, "right")
            y -= row_h
            hline(y, w=0.5)
        fill(y - 18, 18, SUBTOTAL)
        text(xB + 4, y - 13, total_label, "Helvetica-Bold", 9)
        text(right - 4, y - 13, money(total), "Helvetica-Bold", 9, "right")
        y -= 18
        c.setLineWidth(0.6)
        for x in (xD, xE):
            c.line(x, t, x, y)
        c.setLineWidth(1)
        c.rect(left, y, W, t - y, stroke=1, fill=0)
        hline(y + 18, w=1)
        return y

    y = table(y, ("Description", "Inv No.", "AMOUNT"), spec.get("lines", []), LINE_ROWS, 16,
              "TOTAL DUE", spec.get("total", 0))
    y = table(y, ("ACCOUNT ALLOCATION", "ACCOUNT NO", "AMOUNT"),
              [[a[0], a[1], a[2]] for a in spec.get("allocations", [])], ALLOC_ROWS, 16,
              "TOTAL PAYMENT", spec.get("total", 0))

    # --- signature block
    top = y
    signers = {s["slot"]: s for s in spec.get("signers", [])}
    for slot in ("prepared", "reviewed", "approved"):
        s = signers.get(slot, {})
        y -= 32  # the signature line
        text(left + 4, y + 3, LABELS[slot], "Helvetica-Bold", 9)
        c.setLineWidth(1)
        c.line(xD, y, right - 6, y)
        when = s.get("signed_on") or s.get("date")
        if when:
            text((xE + right) / 2, y + 4, _d(when), "Helvetica", 9, "center")
        if s.get("signature"):
            img = ImageReader(io.BytesIO(s["signature"]))
            iw, ih = img.getSize()
            scale = min((wD - 12) / iw, 30 / ih)
            w, h = iw * scale, ih * scale
            c.drawImage(img, xD + (wD - w) / 2, y + 1.5, width=w, height=h, mask="auto")
        y -= 16
        text(left + 4, y + 3, s.get("position") or DEFAULT_POSITIONS[slot], "Helvetica-Bold", 9,
             width=xD - left - 10)
        text((xD + xE) / 2, y + 3, s.get("name", ""), "Helvetica", 9, "center", width=wD - 6)
        text((xE + right) / 2, y + 3, "Date", "Helvetica", 9, "center")
        y -= 8
    y -= 6
    c.setLineWidth(1.4)
    c.rect(left, y, W, top - y, stroke=1, fill=0)

    c.showPage()
    c.save()
    return buf.getvalue()
