"""Write the Zenex payment requisition (.xlsx) and the bank payment import sheet (.xls)."""
import base64
import io
import os
import re
from datetime import date, datetime

import openpyxl
import xlwt

HERE = os.path.dirname(os.path.abspath(__file__))
REQ_TEMPLATE = next((p for p in (os.path.join(HERE, "excel_templates", "requisition_template.xlsx"),
                                  os.path.join(HERE, "requisition_template.xlsx")) if os.path.exists(p)),
                    os.path.join(HERE, "excel_templates", "requisition_template.xlsx"))

LINE_ROWS = range(14, 28)    # invoice lines on the requisition (B14:E27)
ALLOC_ROWS = range(30, 41)   # account allocation lines (B30:E40)


def _date(s):
    if isinstance(s, date):
        return s
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return date.today()


def safe_filename(s):
    return re.sub(r"[^\w\- .]+", "", s).strip()[:60] or "invoice"


def _load_template():
    if os.path.exists(REQ_TEMPLATE):
        return openpyxl.load_workbook(REQ_TEMPLATE)
    from requisition_template_data import DATA  # built-in copy
    return openpyxl.load_workbook(io.BytesIO(base64.b64decode(DATA)))


def build_requisition(items, settings, out_path):
    """One requisition form. `items` are the invoices on it - normally one supplier, but it
    may mix suppliers, in which case each line and allocation names its supplier."""
    wb = _load_template()
    ws = wb["TEMPLATE"]
    if len(items) > len(LINE_ROWS):
        raise ValueError(f"A requisition holds at most {len(LINE_ROWS)} invoices")
    payees = list(dict.fromkeys(it["payee"].strip() for it in items))
    mixed = len(payees) > 1

    ws["C10"] = (settings.get("combined_payee") or "VARIOUS SUPPLIERS").upper() if mixed else payees[0].upper()
    ws["E10"] = _date(settings.get("payment_date"))
    ws["E10"].number_format = "dd/mm/yyyy"

    for r in LINE_ROWS:
        for col in "BDE":
            ws[f"{col}{r}"] = None
    for r, it in zip(LINE_ROWS, items):
        desc = it["description"] or ""
        if mixed and it["payee"].strip().lower() not in desc.lower():
            desc = f'{it["payee"].strip()} - {desc}' if desc else it["payee"].strip()
        if it.get("gl_code"):
            desc = f'{it["gl_code"]} {desc}'
        ws[f"B{r}"] = desc
        ws[f"D{r}"] = it["invoice_number"]
        ws[f"E{r}"] = float(it["amount"])

    # account allocation: one line per supplier + code (per code alone if that won't fit)
    alloc = {}
    for it in items:
        key = (it["payee"].strip() if mixed else "", it.get("allocation_code") or "")
        alloc[key] = alloc.get(key, 0) + float(it["amount"])
    if len(alloc) > len(ALLOC_ROWS):
        by_code = {}
        for (_, code), amt in alloc.items():
            by_code[("Various", code)] = by_code.get(("Various", code), 0) + amt
        alloc = by_code
    if len(alloc) > len(ALLOC_ROWS):
        raise ValueError(f"Too many different allocation codes for one requisition (max {len(ALLOC_ROWS)})")
    for r in ALLOC_ROWS:
        for col in "BDE":
            ws[f"{col}{r}"] = None
    for r, ((name, code), amt) in zip(ALLOC_ROWS, alloc.items()):
        ws[f"B{r}"] = name or "=C10"
        ws[f"D{r}"] = code
        ws[f"E{r}"] = "=E28" if len(alloc) == 1 else round(amt, 2)

    ws["E43"] = _date(settings.get("prepared_date"))
    ws["E43"].number_format = "dd/mm/yyyy"
    for cell, key in (("D44", "prepared_by"), ("D47", "reviewed_by"), ("D50", "approved_by")):
        if settings.get(key):
            ws[cell] = settings[key]
    # the job title under each signature line (template wording stays if no position is set)
    for cell, key in (("B44", "prepared_by"), ("B47", "reviewed_by"), ("B50", "approved_by")):
        if settings.get(key + "_position"):
            ws[cell] = settings[key + "_position"]

    wb.calculation.fullCalcOnLoad = True  # totals are formulas: make Excel work them out on open
    wb.save(out_path)

    # what went on the form, so people can see it before they sign
    total = round(sum(float(it["amount"]) for it in items), 2)
    return {
        "payee": ws["C10"].value,
        "date": _date(settings.get("payment_date")).isoformat(),
        "lines": [[ws[f"B{r}"].value, ws[f"D{r}"].value, ws[f"E{r}"].value]
                  for r in LINE_ROWS if ws[f"B{r}"].value],
        "allocations": [[name or ws["C10"].value, code, round(amt, 2)] for (name, code), amt in alloc.items()],
        "total": total,
    }


# ---------------------------------------------------------------- signing

# Signature lines on the template: the line is the bottom border of column D in these rows,
# the printed name is underneath, and the date box is column E of the same row.
SIGN_SLOTS = {
    "prepared": {"row": 43, "name_cell": "D44", "date_cell": "E43", "label": "Prepared by"},
    "reviewed": {"row": 46, "name_cell": "D47", "date_cell": "E46", "label": "Reviewed by"},
    "approved": {"row": 49, "name_cell": "D50", "date_cell": "E49", "label": "Approved & authorised by"},
}
SIG_MAX_W, SIG_MAX_H = 140, 39  # px - fits column D above the line


def _row_px(ws, r):
    pt = ws.row_dimensions[r].height or 15
    return pt * 96 / 72


def sign_requisition(xlsx_bytes, slot, png_bytes, signed_on):
    """Return the requisition with a signature image on `slot`'s line and the date beside it."""
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, OneCellAnchor
    from openpyxl.drawing.xdr import XDRPositiveSize2D
    from openpyxl.utils.units import pixels_to_EMU
    from PIL import Image as PILImage

    spec = SIGN_SLOTS[slot]
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
    ws = wb["TEMPLATE"] if "TEMPLATE" in wb.sheetnames else wb.active

    pic = PILImage.open(io.BytesIO(png_bytes)).convert("RGBA")
    pic.thumbnail((SIG_MAX_W * 2, SIG_MAX_H * 2))  # keep 2x pixels so it prints crisply
    buf = io.BytesIO()
    pic.save(buf, "PNG")
    img = XLImage(io.BytesIO(buf.getvalue()))
    scale = min(SIG_MAX_W / pic.width, SIG_MAX_H / pic.height)
    w, h = int(pic.width * scale), int(pic.height * scale)

    # sit the bottom of the signature on the line (bottom of `row`), spilling into the row above
    row = spec["row"]
    above, line_row = _row_px(ws, row - 1), _row_px(ws, row)
    offset = above + line_row - h - 2
    anchor_row = row - 1  # 0-based index of `row`
    if offset < above:
        anchor_row -= 1  # start in the row above
    else:
        offset -= above
    marker = AnchorMarker(col=3, colOff=pixels_to_EMU(4), row=anchor_row, rowOff=pixels_to_EMU(int(max(offset, 0))))
    img.anchor = OneCellAnchor(_from=marker, ext=XDRPositiveSize2D(pixels_to_EMU(w), pixels_to_EMU(h)))
    ws.add_image(img)

    from openpyxl.styles import Alignment
    ws[spec["date_cell"]] = signed_on
    ws[spec["date_cell"]].number_format = "dd/mm/yyyy"
    ws[spec["date_cell"]].alignment = Alignment(horizontal="center", vertical="bottom")
    wb.calculation.fullCalcOnLoad = True
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


IMPORT_HEADERS = ["Account Name", "Account Number to Credit", "Branch Code", "Ref", "Account Name",
                  "Account Number to Debit", "Branch Code", "Ref", "Date ", "Amount ", "RTGS/RTC"]
REF_MAX = 20  # bank reference field length (e.g. "Zenex Shereno Printe")


def build_import_sheet(items, settings, out_path):
    """Bank import sheet in the same layout as 'Zenex Payment Importsheet_22_09_26.xls'."""
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Zenex Payments")
    bold = xlwt.easyxf("font: bold on")
    text = xlwt.easyxf(num_format_str="@")
    for c, h in enumerate(IMPORT_HEADERS):
        ws.write(0, c, h, bold)

    pay_date = _date(settings.get("payment_date")).strftime("%Y%m%d")
    debit_name = settings.get("debit_account_name", "Zenex Foundation")
    debit_acc = int(re.sub(r"\D", "", str(settings.get("debit_account_number", "420067124"))))
    debit_branch = int(re.sub(r"\D", "", str(settings.get("debit_branch_code", "1255"))))
    ref_prefix = settings.get("debit_ref_prefix", "Zenex")

    for r, it in enumerate(items, start=1):
        name = it.get("import_name") or it["payee"]
        row = [
            (name, text),
            (re.sub(r"\D", "", it["account_number"]), text),
            (re.sub(r"\D", "", it["branch_code"]).zfill(6), text),
            ((it.get("beneficiary_ref") or debit_name)[:REF_MAX], text),
            (debit_name, None),
            (debit_acc, None),
            (debit_branch, None),
            (f"{ref_prefix} {name}"[:REF_MAX], text),
            (pay_date, text),
            (f'{float(it["amount"]):.2f}', text),
            (it.get("rtgs") or "N", text),
        ]
        for c, (v, style) in enumerate(row):
            ws.write(r, c, v, style) if style else ws.write(r, c, v)

    for c, w in enumerate([22, 24, 12, 20, 18, 22, 12, 22, 10, 12, 9]):
        ws.col(c).width = 256 * w
    wb.save(out_path)
    return out_path
