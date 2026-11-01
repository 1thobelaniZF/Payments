"""Invoice -> Payment Requisition -> Bank Import Sheet  (multi-user web version).

Runs on Vercel (Postgres) or locally (SQLite):  python app.py  ->  http://127.0.0.1:5055
"""
import base64
import hashlib
import io
import json
import os
import re
import secrets
import uuid
import zipfile
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (Flask, abort, jsonify, redirect, render_template, request, send_file,
                   send_from_directory, session)
from werkzeug.security import check_password_hash, generate_password_hash

import db
import extractor
import generators
import icons
import pdf_requisition

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("Africa/Johannesburg")
ON_VERCEL = bool(os.environ.get("VERCEL"))

DEFAULT_SETTINGS = {
    "payment_date": "",            # blank = today
    "prepared_date": "",           # blank = today
    "prepared_by": "",             # blank = the signed-in person's name
    "reviewed_by": "Thami Kubheka",
    "approved_by": "Sibongile Khumalo",
    "debit_account_name": "Zenex Foundation",
    "debit_account_number": "420067124",
    "debit_branch_code": "1255",
    "debit_ref_prefix": "Zenex",
    "default_ref": "invoice",      # "invoice" = invoice number, otherwise the literal text
    "customer_names": "Zenex Foundation, Float Collaboration NPC",
    "combined_payee": "VARIOUS SUPPLIERS",  # payee heading when one requisition mixes suppliers
}

EDITABLE = ["payee", "import_name", "invoice_number", "invoice_date", "description", "gl_code",
            "amount", "bank_name", "account_number", "branch_code", "allocation_code",
            "beneficiary_ref", "rtgs"]

MAX_FAILED_LOGINS = 5
PW_METHOD = "pbkdf2:sha256:600000"  # available on every Python build (scrypt isn't on macOS's)
LOCKOUT = timedelta(minutes=15)

app = Flask(__name__, static_folder=None, template_folder=os.path.join(HERE, "templates"))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=ON_VERCEL,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=4 * 1024 * 1024,  # Vercel's request limit is 4.5 MB
)


def now():
    return datetime.now(TZ)


def today():
    return now().date().isoformat()


def _secret_key():
    key = os.environ.get("SECRET_KEY") or db.meta_get("secret_key")
    if not key:
        key = secrets.token_hex(32)
        db.meta_set("secret_key", key)
    return key


NEEDS_DB = ON_VERCEL and not db.PG_URL  # Vercel's disk is temporary - a database is required

DB_ERROR = None  # why the database can't be reached, shown on the page instead of crashing

DB_HINTS = [
    ("password authentication failed", "The password in DATABASE_URL is wrong. Copy the connection string "
     "from Supabase again (Connect -> Transaction pooler), put in the current database password, "
     "update DATABASE_URL in Vercel and redeploy."),
    ("tenant or user not found", "The username or server in DATABASE_URL doesn't match your Supabase "
     "project. Copy the Transaction pooler string from Supabase again without editing the "
     "postgres.&lt;project&gt; part or the server name."),
    ("could not translate host name", "The server name in DATABASE_URL can't be found. Use the "
     "Transaction pooler string (…pooler.supabase.com:6543), not the direct db.….supabase.co one."),
    ("invalid", "DATABASE_URL has something the database driver doesn't understand. Use the plain "
     "Transaction pooler string from Supabase, with nothing added after /postgres."),
    ("timeout", "The database didn't answer in time. Check the server name and port (6543) in "
     "DATABASE_URL, and that the Supabase project isn't paused."),
]


def _hide_password(msg):
    pw = re.search(r"://[^:/@]+:([^@]+)@", db.PG_URL or "")
    if pw:
        msg = msg.replace(pw.group(1), "***")
    return re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", msg)


def _connect_db():
    """Load the session key from the database; on failure remember why instead of crashing."""
    global DB_ERROR
    try:
        app.secret_key = _secret_key()
        DB_ERROR = None
    except Exception as e:
        app.secret_key = "not-configured"
        DB_ERROR = _hide_password(f"{type(e).__name__}: {e}")[:600]
        app.logger.error("Database connection failed: %s", DB_ERROR)


# Must be set before the first request: Flask opens the session before any request hooks run.
if NEEDS_DB:
    app.secret_key = "not-configured"
else:
    _connect_db()


@app.before_request
def _require_database():
    if NEEDS_DB:
        return ("<h2>One more step</h2><p>The app can't find its database. In Vercel, add an "
                "environment variable named <b>DATABASE_URL</b> (your Supabase or Neon connection "
                "string) with <b>Production</b> ticked, then <b>Redeploy</b>.</p>"), 503
    if DB_ERROR:
        _connect_db()  # try again - it may have been a passing hiccup
    if DB_ERROR:
        from html import escape
        hint = next((h for k, h in DB_HINTS if k in DB_ERROR.lower()),
                    "Check DATABASE_URL in Vercel, then redeploy.")
        return (f"<h2>Can't connect to the database</h2><p>{hint}</p>"
                f"<p style='color:#666'>Details: <code>{escape(DB_ERROR)}</code></p>"), 503


# ---------------------------------------------------------------- auth

def current_user():
    u = session.get("user")
    if not u:
        return None
    r = db.row("SELECT username, name, is_admin FROM users WHERE username = ?", (u,))
    if not r:
        session.clear()
    return r


def login_required(admin=False):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            user = current_user()
            wants_json = request.path.startswith("/api/")
            if not user:
                return (jsonify(error="Please sign in"), 401) if wants_json else redirect("/login")
            if admin and not user["is_admin"]:
                return jsonify(error="Only an admin can do that"), 403
            request.user = user
            return fn(*a, **kw)
        return wrapper
    return deco


def no_users():
    return db.row("SELECT COUNT(*) AS n FROM users")["n"] == 0


def valid_username(u):
    return bool(re.fullmatch(r"[a-z0-9._-]{2,40}", u or ""))


def create_user(username, name, password, is_admin, position=""):
    db.run("INSERT INTO users (username, name, pw_hash, is_admin, created, position) VALUES (?, ?, ?, ?, ?, ?)",
           (username, name, generate_password_hash(password, method=PW_METHOD), 1 if is_admin else 0,
            now().isoformat(), position))


@app.route("/login", methods=["GET", "POST"])
def login():
    if no_users():
        return redirect("/setup")
    error = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        u = db.row("SELECT * FROM users WHERE username = ?", (username,))
        locked = u and u["locked_until"] and datetime.fromisoformat(u["locked_until"]) > now()
        if locked:
            error = "Too many failed attempts. Try again in 15 minutes, or ask an admin to reset your password."
        elif u and check_password_hash(u["pw_hash"], password):
            db.run("UPDATE users SET failed = 0, locked_until = NULL WHERE username = ?", (username,))
            session.clear()
            session.permanent = True
            session["user"] = username
            return redirect("/")
        else:
            if u:
                failed = (u["failed"] or 0) + 1
                until = (now() + LOCKOUT).isoformat() if failed >= MAX_FAILED_LOGINS else None
                db.run("UPDATE users SET failed = ?, locked_until = ? WHERE username = ?",
                       (0 if until else failed, until, username))
            error = "Wrong username or password."
    return render_template("login.html", mode="login", error=error)


@app.route("/setup", methods=["GET", "POST"])
def setup():
    """First run only: create the first admin account."""
    if not no_users():
        return redirect("/login")
    code_needed = os.environ.get("SETUP_CODE")
    if ON_VERCEL and not code_needed:
        return render_template("login.html", mode="nosetup", error="")
    error = ""
    if request.method == "POST":
        f = request.form
        username = (f.get("username") or "").strip().lower()
        if code_needed and not secrets.compare_digest(f.get("code") or "", code_needed):
            error = "The setup code is wrong."
        elif not valid_username(username):
            error = "Username: 2-40 lowercase letters, numbers, dots or dashes."
        elif len(f.get("password") or "") < 8:
            error = "Password must be at least 8 characters."
        elif f.get("password") != f.get("password2"):
            error = "The passwords don't match."
        else:
            create_user(username, (f.get("name") or username).strip(), f["password"], True)
            session.clear()
            session.permanent = True
            session["user"] = username
            return redirect("/")
    return render_template("login.html", mode="setup", error=error, code_needed=bool(code_needed))


@app.post("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.get("/api/me")
@login_required()
def me():
    return jsonify(request.user)


@app.put("/api/me/password")
@login_required()
def change_password():
    body = request.get_json(force=True)
    u = db.row("SELECT pw_hash FROM users WHERE username = ?", (request.user["username"],))
    if not check_password_hash(u["pw_hash"], body.get("current") or ""):
        return jsonify(error="Your current password is wrong"), 400
    if len(body.get("new") or "") < 8:
        return jsonify(error="New password must be at least 8 characters"), 400
    db.run("UPDATE users SET pw_hash = ? WHERE username = ?",
           (generate_password_hash(body["new"], method=PW_METHOD), request.user["username"]))
    return jsonify(ok=True)


@app.get("/api/users")
@login_required(admin=True)
def list_users():
    return jsonify(db.rows("SELECT username, name, position, is_admin, created FROM users ORDER BY username"))


@app.post("/api/users")
@login_required(admin=True)
def add_user():
    b = request.get_json(force=True)
    username = (b.get("username") or "").strip().lower()
    if not valid_username(username):
        return jsonify(error="Username: 2-40 lowercase letters, numbers, dots or dashes"), 400
    if db.row("SELECT 1 AS x FROM users WHERE username = ?", (username,)):
        return jsonify(error="That username is taken"), 400
    if len(b.get("password") or "") < 8:
        return jsonify(error="Password must be at least 8 characters"), 400
    create_user(username, (b.get("name") or username).strip(), b["password"], bool(b.get("is_admin")),
                (b.get("position") or "").strip()[:80])
    return jsonify(ok=True)


@app.put("/api/users/<username>")
@login_required(admin=True)
def edit_user(username):
    b = request.get_json(force=True)
    if not db.row("SELECT 1 AS x FROM users WHERE username = ?", (username,)):
        abort(404)
    if "password" in b:
        if len(b["password"] or "") < 8:
            return jsonify(error="Password must be at least 8 characters"), 400
        db.run("UPDATE users SET pw_hash = ?, failed = 0, locked_until = NULL WHERE username = ?",
               (generate_password_hash(b["password"], method=PW_METHOD), username))
    if "name" in b:
        db.run("UPDATE users SET name = ? WHERE username = ?", (b["name"].strip(), username))
    if "position" in b:
        db.run("UPDATE users SET position = ? WHERE username = ?", ((b["position"] or "").strip()[:80], username))
    if "is_admin" in b:
        if username == request.user["username"] and not b["is_admin"]:
            return jsonify(error="You can't remove your own admin rights"), 400
        db.run("UPDATE users SET is_admin = ? WHERE username = ?", (1 if b["is_admin"] else 0, username))
    return jsonify(ok=True)


@app.delete("/api/users/<username>")
@login_required(admin=True)
def delete_user(username):
    if username == request.user["username"]:
        return jsonify(error="You can't delete your own account"), 400
    db.run("DELETE FROM users WHERE username = ?", (username,))
    return jsonify(ok=True)


# ---------------------------------------------------------------- per-user settings

def settings(user):
    s = dict(DEFAULT_SETTINGS)
    s.update(db.get_json("settings", "username", user["username"], {}))
    return s


def effective_settings(user):
    s = settings(user)
    s["payment_date"] = s["payment_date"] or today()
    s["prepared_date"] = s["prepared_date"] or today()
    s["prepared_by"] = s["prepared_by"] or user["name"]
    return s


# ---------------------------------------------------------------- items

def load_items(owner):
    return [json.loads(r["data"]) for r in
            db.rows("SELECT data FROM items WHERE owner = ? ORDER BY created, id", (owner,))]


def get_item(owner, iid):
    r = db.row("SELECT data FROM items WHERE id = ? AND owner = ?", (iid, owner))
    return json.loads(r["data"]) if r else None


def save_item(owner, it):
    db.run("INSERT INTO items (id, owner, created, data) VALUES (?, ?, ?, ?) "
           "ON CONFLICT (id) DO UPDATE SET data = excluded.data",
           (it["id"], owner, it["created"], json.dumps(it)))


def supplier_key(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def short_name(name):
    n = re.sub(r"\(?\b(pty|proprietary)\)?\s*\(?ltd\)?\.?|\b(npc|cc|inc|limited|ltd)\b\.?", "", name or "", flags=re.I)
    n = re.sub(r"\s{2,}", " ", n).strip(" ,.-")
    if n.isupper():
        n = " ".join(w if len(w) <= 3 else w.capitalize() for w in n.split())
    return n


def all_suppliers():
    return {r["k"]: json.loads(r["data"]) for r in db.rows("SELECT k, data FROM suppliers")}


def new_item(user, pdf_bytes, original_name):
    s = settings(user)
    customers = [c.strip() for c in s["customer_names"].split(",") if c.strip()]
    fields, method, warning, text = extractor.extract(pdf_bytes, customers)

    file_id = uuid.uuid4().hex
    db.run("INSERT INTO files (id, owner, batch, name, mime, data, created) VALUES (?, ?, ?, ?, ?, ?, ?)",
           (file_id, user["username"], None, original_name, "application/pdf", db.blob(pdf_bytes),
            now().isoformat()))

    payee = fields.get("account_holder") or fields.get("supplier_name") or ""
    item = {
        "id": uuid.uuid4().hex[:10],
        "file": file_id,
        "original_name": original_name,
        "payee": payee,
        "import_name": short_name(payee),
        "invoice_number": fields.get("invoice_number") or "",
        "invoice_date": fields.get("invoice_date") or "",
        "description": fields.get("description") or "",
        "gl_code": fields.get("gl_code") or "",
        "amount": fields.get("amount") or "",
        "bank_name": fields.get("bank_name") or "",
        "account_number": fields.get("account_number") or "",
        "branch_code": fields.get("branch_code") or "",
        "allocation_code": fields.get("allocation_code") or "",
        "beneficiary_ref": "",
        "rtgs": "N",
        "method": method,
        "warning": warning,
        "known_supplier": False,
        "status": "draft",
        "created": now().isoformat(timespec="seconds"),
    }

    # Recall a supplier anyone has paid before (match on bank account first, then name)
    suppliers = all_suppliers()
    match = None
    if item["account_number"]:
        match = next((v for v in suppliers.values() if v.get("account_number") == item["account_number"]), None)
    if not match:
        match = suppliers.get(supplier_key(payee)) or next(
            (v for k, v in suppliers.items() if k and k in supplier_key(text)), None)
    if match:
        item["known_supplier"] = True
        for k in ("payee", "import_name", "gl_code", "allocation_code", "bank_name"):
            if match.get(k):
                item[k] = match[k]
        for k in ("account_number", "branch_code"):
            if match.get(k) and not item[k]:
                item[k] = match[k]
        if match.get("account_number") and item["account_number"] != match["account_number"]:
            item["warning"] += (" ⚠ Bank account on this invoice differs from the one on file for "
                                "this supplier - verify before paying.")
        if match.get("ref_mode") == "fixed":
            item["beneficiary_ref"] = match.get("fixed_ref", "")

    if not item["beneficiary_ref"]:
        item["beneficiary_ref"] = item["invoice_number"] if s["default_ref"] == "invoice" else s["default_ref"]
    if not item["description"] and payee:
        item["description"] = payee
    save_item(user["username"], item)
    item["issues"] = validate(item)
    return item


def validate(it, kind="both"):
    """Problems that block generating `kind` ("requisition", "import" or "both"),
    plus any duplicate warning (which never blocks). Duplicates are checked across all users."""
    need_req = kind in ("requisition", "both")
    need_imp = kind in ("import", "both")
    issues = []
    if not it.get("payee"):
        issues.append("Payee is missing")
    if need_req and not it.get("invoice_number"):
        issues.append("Invoice number is missing")
    try:
        if float(it.get("amount") or 0) <= 0:
            issues.append("Amount must be more than 0")
    except ValueError:
        issues.append("Amount is not a number")
    if need_imp:
        acc = re.sub(r"\D", "", str(it.get("account_number") or ""))
        if not (6 <= len(acc) <= 16):
            issues.append("Account number looks wrong (6-16 digits) - needed for the import sheet")
        br = re.sub(r"\D", "", str(it.get("branch_code") or ""))
        if len(br) not in (5, 6):
            issues.append("Branch code should be 6 digits - needed for the import sheet")
    h = db.row("SELECT created, owner FROM history WHERE supplier = ? AND invoice = ? AND item_id <> ? "
               "ORDER BY created LIMIT 1",
               (supplier_key(it.get("payee")), (it.get("invoice_number") or "").strip().lower(), it.get("id") or ""))
    if h:
        issues.append(f"Possible duplicate: this invoice was already processed on {h['created'][:10]} by {h['owner']}")
    return issues


# ---------------------------------------------------------------- routes

@app.get("/favicon.svg")
def favicon_svg():
    return app.response_class(icons.SVG, mimetype="image/svg+xml",
                              headers={"Cache-Control": "public, max-age=86400"})


@app.get("/favicon.ico")
@app.get("/apple-touch-icon.png")
def favicon_png():
    size = 180 if "apple" in request.path else 48
    return app.response_class(icons.png(size), mimetype="image/png",
                              headers={"Cache-Control": "public, max-age=86400"})


@app.get("/")
@login_required()
def index():
    return send_from_directory(os.path.join(HERE, "templates"), "index.html")


@app.get("/api/state")
@login_required()
def state():
    user = request.user
    items = load_items(user["username"])
    for it in items:
        it["issues"] = validate(it)
    s = settings(user)
    return jsonify(items=items, settings=s, ai=extractor.claude_available(), user=user,
                   prepared_by_default=user["name"],
                   people=[r["name"] for r in db.rows("SELECT name FROM users ORDER BY name")],
                   to_sign=len(waiting_for(user)),
                   suppliers=db.row("SELECT COUNT(*) AS n FROM suppliers")["n"])


@app.post("/api/upload")
@login_required()
def upload():
    files = request.files.getlist("files")
    if not files:
        abort(400, "No files")
    created = []
    for f in files:
        if not f.filename.lower().endswith(".pdf"):
            continue
        created.append(new_item(request.user, f.read(), f.filename))
    return jsonify(created=created)


@app.put("/api/items/<iid>")
@login_required()
def update_item(iid):
    owner = request.user["username"]
    it = get_item(owner, iid)
    if not it:
        abort(404)
    body = request.get_json(force=True)
    for k in EDITABLE:
        if k in body:
            it[k] = body[k]
    if "amount" in body:
        parsed = extractor.parse_amount(str(body["amount"]))
        it["amount"] = parsed if parsed is not None else body["amount"]
    it["account_number"] = re.sub(r"\D", "", str(it["account_number"]))
    it["branch_code"] = re.sub(r"\D", "", str(it["branch_code"]))
    save_item(owner, it)
    it["issues"] = validate(it)
    return jsonify(it)


@app.delete("/api/items/<iid>")
@login_required()
def delete_item(iid):
    owner = request.user["username"]
    it = get_item(owner, iid)
    if it:
        db.run("DELETE FROM items WHERE id = ? AND owner = ?", (iid, owner))
        db.run("DELETE FROM files WHERE id = ? AND owner = ? AND batch IS NULL", (it["file"], owner))
    return jsonify(ok=True)


@app.post("/api/clear")
@login_required()
def clear_batch():
    """Start a new batch: clear this user's processed invoices (all=true clears everything)."""
    owner = request.user["username"]
    everything = request.args.get("all") == "true"
    for it in load_items(owner):
        if everything or it.get("status") == "generated":
            db.run("DELETE FROM items WHERE id = ? AND owner = ?", (it["id"], owner))
            db.run("DELETE FROM files WHERE id = ? AND owner = ? AND batch IS NULL", (it["file"], owner))
    return jsonify(ok=True)


@app.get("/invoice/<iid>")
@login_required()
def invoice_pdf(iid):
    owner = request.user["username"]
    it = get_item(owner, iid)
    f = it and db.row("SELECT data FROM files WHERE id = ? AND owner = ?", (it["file"], owner))
    if not f:
        abort(404)
    return send_file(io.BytesIO(bytes(f["data"])), mimetype="application/pdf",
                     download_name=it["original_name"])


@app.put("/api/settings")
@login_required()
def put_settings():
    """Settings belong to the signed-in person only."""
    user = request.user
    body = request.get_json(force=True)
    s = settings(user)
    for k in DEFAULT_SETTINGS:
        if k in body:
            s[k] = str(body[k]).strip()
    db.put_json("settings", "username", user["username"], s)
    return jsonify(s)


def safe(s):
    return re.sub(r"[^\w\-]", "", s or "")


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@app.post("/api/generate")
@login_required()
def generate():
    """kind = "requisition" | "import" | "both"."""
    user = request.user
    owner = user["username"]
    body = request.get_json(silent=True) or {}
    kind = body.get("kind", "both")
    if kind not in ("requisition", "import", "both"):
        abort(400, "Unknown kind")
    ids = body.get("ids")
    # how invoices are grouped onto requisitions: "supplier" | "invoice" | "all"
    group = body.get("group") or "supplier"
    s = effective_settings(user)
    # print each signer's position under their name, when they have one set under Users
    positions = {norm_name(r["name"]): r["position"] for r in db.rows("SELECT name, position FROM users")
                 if r["position"]}
    for key in ("prepared_by", "reviewed_by", "approved_by"):
        s[key + "_position"] = positions.get(norm_name(s.get(key)), "")

    items = load_items(owner)
    chosen = [i for i in items if (ids is None or i["id"] in ids)]
    if not chosen:
        abort(400, "Nothing to generate")
    blocking = {i["id"]: [x for x in validate(i, kind) if not x.startswith("Possible duplicate")]
                for i in chosen}
    blocking = {k: v for k, v in blocking.items() if v}
    if blocking and not body.get("force"):
        return jsonify(error="Some invoices need attention", issues=blocking), 422

    stamp = now().strftime("%Y-%m-%d_%H%M%S")
    label = {"requisition": "Requisitions", "import": "Import sheet", "both": "Payment pack"}[kind]
    batch = f"{stamp}-{uuid.uuid4().hex[:6]}"
    out = []  # (name, mime, bytes)
    summaries = {}  # requisition file name -> what's on it (for the signing page)

    if kind in ("requisition", "both"):
        if group == "all":
            size = len(generators.LINE_ROWS)  # split only if it won't fit on one form
            groups = [chosen[i:i + size] for i in range(0, len(chosen), size)]
        else:
            by = {}
            for it in chosen:
                key = (supplier_key(it["payee"]), it["account_number"]) if group == "supplier" else it["id"]
                by.setdefault(key, []).append(it)
            groups = list(by.values())
        for n, g in enumerate(groups, start=1):
            if len({supplier_key(i["payee"]) for i in g}) > 1:
                part = f" (part {n})" if len(groups) > 1 else ""
                name = f"Requisition - Various suppliers - {len(g)} invoices{part}.pdf"
            else:
                nums = "_".join(safe(i["invoice_number"]) for i in g)[:40]
                name = f"Requisition - {generators.safe_filename(g[0]['payee'])} - {nums}.pdf"
            try:  # works out the form's content (lines, allocations, totals) using the template rules
                summary = generators.build_requisition(g, s, io.BytesIO())
            except ValueError as e:
                return jsonify(error=str(e)), 400
            spec = pdf_spec(summary, s)
            out.append((name, "application/pdf", pdf_requisition.render(spec)))
            summaries[name] = (summary, spec)
        # the source invoices go in the zip alongside, for the approval pack
        for it in chosen:
            f = db.row("SELECT data FROM files WHERE id = ?", (it["file"],))
            if f:
                pdf_name = "Invoices/" + generators.safe_filename(f"{it['payee']} - {it['invoice_number']}") + ".pdf"
                out.append((pdf_name, "application/pdf", bytes(f["data"])))

    if kind in ("import", "both"):
        pd = datetime.strptime(s["payment_date"], "%Y-%m-%d")
        name = f"Zenex Payment Importsheet_{pd.strftime('%d_%m_%y')}.xls"
        buf = io.BytesIO()
        generators.build_import_sheet(chosen, s, buf)
        out.append((name, "application/vnd.ms-excel", buf.getvalue()))

    total = sum(float(i["amount"]) for i in chosen)
    for name, mime, data in out:
        file_id = uuid.uuid4().hex
        db.run("INSERT INTO files (id, owner, batch, name, mime, data, created) VALUES (?, ?, ?, ?, ?, ?, ?)",
               (file_id, owner, batch, name, mime, db.blob(data), now().isoformat()))
        if name in summaries:
            start_signing(file_id, name, owner, user["name"], s, *summaries[name])
    db.run("INSERT INTO batches (id, owner, created, label, kind, total, count) VALUES (?, ?, ?, ?, ?, ?, ?)",
           (batch, owner, now().isoformat(timespec="seconds"), label, kind, total, len(chosen)))

    # remember suppliers (shared) and log history (once per invoice)
    for it in chosen:
        fixed = it["beneficiary_ref"] != it["invoice_number"]
        db.put_json("suppliers", "k", supplier_key(it["payee"]), {
            "payee": it["payee"], "import_name": it["import_name"], "bank_name": it["bank_name"],
            "account_number": it["account_number"], "branch_code": it["branch_code"],
            "allocation_code": it["allocation_code"], "gl_code": it.get("gl_code", ""),
            "ref_mode": "fixed" if fixed else "invoice",
            "fixed_ref": it["beneficiary_ref"] if fixed else "",
        })
        if not db.row("SELECT 1 AS x FROM history WHERE item_id = ?", (it["id"],)):
            db.run("INSERT INTO history (id, supplier, invoice, item_id, owner, created, data) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (uuid.uuid4().hex, supplier_key(it["payee"]), it["invoice_number"].strip().lower(),
                    it["id"], owner, now().isoformat(timespec="seconds"),
                    json.dumps({"amount": it["amount"], "batch": batch})))
        if kind in ("requisition", "both"):
            it["requisition_done"] = stamp
        if kind in ("import", "both"):
            it["import_done"] = stamp
        if it.get("requisition_done") and it.get("import_done"):
            it["status"] = "generated"
            it["batch"] = stamp
        save_item(owner, it)

    files = [n for n, _, _ in out if not n.startswith("Invoices/")]
    return jsonify(batch=batch, kind=kind, files=files, count=len(chosen), total=total,
                   signing=len(summaries))


def own_batch(batch):
    b = db.row("SELECT * FROM batches WHERE id = ? AND owner = ?", (batch, request.user["username"]))
    if not b:
        abort(404)
    return b


@app.get("/api/batches")
@login_required()
def list_batches():
    return jsonify(db.rows("SELECT id, created, label, kind, total, count FROM batches WHERE owner = ? "
                           "ORDER BY created DESC LIMIT 20", (request.user["username"],)))


@app.get("/output/<batch>/<path:name>")
@login_required()
def output_file(batch, name):
    own_batch(batch)
    f = db.row("SELECT name, mime, data FROM files WHERE batch = ? AND name = ?", (batch, name))
    if not f:
        abort(404)
    return send_file(io.BytesIO(bytes(f["data"])), mimetype=f["mime"], as_attachment=True,
                     download_name=os.path.basename(f["name"]))


@app.get("/output/<batch>.zip")
@login_required()
def output_zip(batch):
    b = own_batch(batch)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in db.rows("SELECT name, data FROM files WHERE batch = ?", (batch,)):
            z.writestr(f["name"], bytes(f["data"]))
    buf.seek(0)
    return send_file(buf, download_name=f"{b['label']} {b['created'][:16].replace(':', '')}.zip",
                     as_attachment=True)


# ---------------------------------------------------------------- signing

def norm_name(n):
    return re.sub(r"\s+", " ", (n or "")).strip().lower()


def pdf_spec(summary, s):
    """Everything the PDF requisition shows, including who signs where."""
    signers = []
    for key, setting in (("prepared", "prepared_by"), ("reviewed", "reviewed_by"), ("approved", "approved_by")):
        signers.append({"slot": key, "name": s.get(setting) or "", "position": s.get(setting + "_position") or "",
                        "date": s.get("prepared_date") if key == "prepared" else ""})
    return {**summary, "signers": signers}


def render_signed_pdf(data):
    """Redraw the requisition PDF with every signature collected so far."""
    spec = json.loads(json.dumps(data["spec"]))
    signed = {sl["slot"]: sl for sl in data["slots"] if sl.get("signed_at")}
    for signer in spec["signers"]:
        sl = signed.get(signer["slot"])
        if sl:
            signer["signature"] = base64.b64decode(sl["image"])
            signer["signed_on"] = sl["signed_on"]
    return pdf_requisition.render(spec)


def start_signing(file_id, file_name, owner, owner_name, s, summary, spec=None):
    """Every new requisition is open for signing by the people named on it, in order."""
    slots = []
    for key, setting in (("prepared", "prepared_by"), ("reviewed", "reviewed_by"), ("approved", "approved_by")):
        if s.get(setting):
            slots.append({"slot": key, "label": generators.SIGN_SLOTS[key]["label"], "name": s[setting]})
    data = {"file_name": file_name, "owner_name": owner_name, "summary": summary, "slots": slots, "log": []}
    if spec:
        data.update(format="pdf", spec=spec)
    db.run("INSERT INTO signing (id, file_id, owner, created, status, data) VALUES (?, ?, ?, ?, ?, ?)",
           (uuid.uuid4().hex[:12], file_id, owner, now().isoformat(timespec="seconds"),
            "signing" if slots else "complete", json.dumps(data)))


def next_slot(data):
    return next((sl for sl in data["slots"] if not sl.get("signed_at")), None)


def signing_view(r, user, names):
    data = json.loads(r["data"])
    nxt = next_slot(data)
    for sl in data["slots"]:
        sl.pop("image", None)  # don't ship signature images around
        sl["has_user"] = norm_name(sl["name"]) in names
    return {"id": r["id"], "created": r["created"], "status": r["status"], "owner": r["owner"],
            "owner_name": data["owner_name"], "file_name": data["file_name"], "summary": data["summary"],
            "slots": data["slots"], "next": nxt and nxt["slot"],
            "can_sign": bool(nxt) and norm_name(nxt["name"]) == norm_name(user["name"]),
            "is_pdf": data.get("format") == "pdf",
            "version": sum(1 for sl in data["slots"] if sl.get("signed_at"))}


def waiting_for(user):
    rows = db.rows("SELECT id, data FROM signing WHERE status = ?", ("signing",))
    me = norm_name(user["name"])
    return [r["id"] for r in rows if (lambda n: n and norm_name(n["name"]) == me)(next_slot(json.loads(r["data"])))]


@app.get("/sign")
@login_required()
def sign_page():
    return send_from_directory(os.path.join(HERE, "templates"), "sign.html")


@app.get("/api/signing")
@login_required()
def list_signing():
    """Everyone can see every requisition and where it is in the signing order."""
    names = {norm_name(r["name"]) for r in db.rows("SELECT name FROM users")}
    rows = db.rows("SELECT * FROM signing ORDER BY created DESC LIMIT 200")
    return jsonify(user=request.user, items=[signing_view(r, request.user, names) for r in rows])


@app.get("/api/signing/<sid>/file")
@login_required()
def signing_file(sid):
    r = db.row("SELECT file_id FROM signing WHERE id = ?", (sid,))
    f = r and db.row("SELECT name, mime, data FROM files WHERE id = ?", (r["file_id"],))
    if not f:
        abort(404)
    # ?inline=1 shows the PDF in the browser (preview) instead of downloading it
    return send_file(io.BytesIO(bytes(f["data"])), mimetype=f["mime"],
                     as_attachment=request.args.get("inline") != "1", download_name=f["name"])


MAX_SIG_BYTES = 400 * 1024


def decode_signature(data_url):
    """Accept only a reasonably sized PNG data URL; return clean PNG bytes."""
    from PIL import Image as PILImage
    m = re.fullmatch(r"data:image/png;base64,([A-Za-z0-9+/=]+)", data_url or "")
    if not m:
        raise ValueError("Signature must be a PNG image")
    raw = base64.b64decode(m.group(1))
    if len(raw) > MAX_SIG_BYTES:
        raise ValueError("Signature image is too large")
    pic = PILImage.open(io.BytesIO(raw))
    pic.verify()
    pic = PILImage.open(io.BytesIO(raw)).convert("RGBA")
    if pic.width < 10 or pic.height < 5 or pic.getbbox() is None:
        raise ValueError("The signature is empty")
    out = io.BytesIO()
    pic.save(out, "PNG")  # re-encode: drops anything that isn't pixels
    return out.getvalue()


@app.post("/api/signing/<sid>/sign")
@login_required()
def sign(sid):
    user = request.user
    b = request.get_json(force=True)
    method = b.get("method")
    if method not in ("typed", "drawn"):
        return jsonify(error="Choose Type or Draw"), 400
    if not b.get("agree"):
        return jsonify(error="Please confirm you approve this requisition"), 400
    try:
        png = decode_signature(b.get("image"))
    except Exception as e:
        return jsonify(error=str(e) if isinstance(e, ValueError) else "That signature image couldn't be read"), 400

    r = db.row("SELECT * FROM signing WHERE id = ?", (sid,))
    if not r:
        abort(404)
    data = json.loads(r["data"])
    sl = next_slot(data)
    if not sl:
        return jsonify(error="This requisition is already fully signed"), 400
    if norm_name(sl["name"]) != norm_name(user["name"]):
        return jsonify(error=f"It's {sl['name']}'s turn to sign ({sl['label']})"), 403

    f = db.row("SELECT data FROM files WHERE id = ?", (r["file_id"],))
    before = bytes(f["data"])
    signed_on = now().date()
    if data.get("format") == "pdf":
        sl.update(image=base64.b64encode(png).decode(), signed_on=signed_on.isoformat(), signed_at="pending")
        after = render_signed_pdf(data)
    else:  # requisitions made as Excel before the switch to PDF
        after = generators.sign_requisition(before, sl["slot"], png, signed_on)

    sl.update({"signed_by": user["username"], "signed_name": user["name"], "method": method,
               "typed_text": (b.get("typed_text") or "")[:80] if method == "typed" else "",
               "signed_at": now().isoformat(timespec="seconds"),
               "ip": request.headers.get("x-forwarded-for", request.remote_addr or "").split(",")[0].strip(),
               "doc_sha256_before": hashlib.sha256(before).hexdigest(),
               "doc_sha256_after": hashlib.sha256(after).hexdigest()})
    data["log"].append({"at": sl["signed_at"], "who": user["username"], "what": f"signed as {sl['label']} ({method})"})
    status = "complete" if not next_slot(data) else "signing"
    db.run("UPDATE files SET data = ? WHERE id = ?", (db.blob(after), r["file_id"]))
    db.run("UPDATE signing SET data = ?, status = ? WHERE id = ?", (json.dumps(data), status, sid))

    if b.get("remember"):
        db.put_json("signatures", "username", user["username"],
                    {"image": b["image"], "method": method, "typed_text": sl["typed_text"]})
    return jsonify(ok=True, status=status)


@app.get("/api/me/signature")
@login_required()
def my_signature():
    return jsonify(db.get_json("signatures", "username", request.user["username"], {}))


@app.delete("/api/me/signature")
@login_required()
def forget_signature():
    db.run("DELETE FROM signatures WHERE username = ?", (request.user["username"],))
    return jsonify(ok=True)


@app.errorhandler(413)
def too_big(_):
    return jsonify(error="That file is too large (max 4 MB per PDF)"), 413


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5055))
    print(f"\n  Payment Requisition app running at http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False)
