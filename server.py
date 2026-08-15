"""
AXIS INTELLIGENCE HUB - SERVER
==============================
FastAPI backend that makes the hub a real multi-user system:
  - SQLite persistence (approvals, audit trail, ADP block state, data snapshots)
  - Staff login with named users (audit attribution) + physio-only approval rule
  - Scheduled Lumin + VALD sync (daily, configurable) + manual "Sync now"
  - Serves the hub with live state injected server-side

Run:  uvicorn server:app --host 0.0.0.0 --port 8000
Env:  see .env.example
"""

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

# ── Config ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
DB_PATH = os.environ.get("AXIS_DB", str(ROOT / "axis.db"))
TEMPLATE = ROOT / "hub_template.html"

AXIS_PASSWORD = os.environ.get("AXIS_PASSWORD", "changeme")
SECRET = os.environ.get("AXIS_SECRET", "set-a-real-secret")
STAFF = [s.strip() for s in os.environ.get(
    "AXIS_STAFF", "Rod,Emma,Luke T.,Ben R.,Jess M.,Blake,Michael N,Alex S,Michael S"
).split(",") if s.strip()]
PHYSIOS = [s.strip() for s in os.environ.get("AXIS_PHYSIOS", "Luke T.").split(",")]

LUMIN_KEY = os.environ.get("LUMIN_API_KEY", "")
SYNC_HOUR = int(os.environ.get("AXIS_SYNC_HOUR", "5"))  # local server time

app = FastAPI(title="Axis Intelligence Hub")

# ── DB ──────────────────────────────────────────────────────────────────
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS adp_state(
            athlete_key TEXT PRIMARY KEY,
            data TEXT NOT NULL, updated_by TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS approvals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            athlete_key TEXT, name TEXT, from_phase INTEGER, to_phase INTEGER,
            phase_name TEXT, approver TEXT, kind TEXT, note TEXT,
            snapshot TEXT, at TEXT);
        CREATE TABLE IF NOT EXISTS athlete_meta(
            athlete_key TEXT PRIMARY KEY,
            data TEXT NOT NULL, updated_by TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS athlete_meta(
            athlete_key TEXT PRIMARY KEY,
            data TEXT NOT NULL, updated_by TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS rehab_criteria(
            athlete_key TEXT PRIMARY KEY,
            data TEXT NOT NULL, updated_by TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS reviews(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            athlete_key TEXT, name TEXT, week_label TEXT,
            generated_at TEXT, draft TEXT,
            status TEXT DEFAULT 'pending',
            approved_by TEXT, approved_at TEXT, edited INTEGER DEFAULT 0,
            final_text TEXT,
            UNIQUE(athlete_key, week_label));
        CREATE TABLE IF NOT EXISTS facility(
            id INTEGER PRIMARY KEY CHECK (id=1),
            data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sync_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT, ok INTEGER, detail TEXT);
        """)
init_db()
try:  # migration for existing databases
    with db() as _c:
        _c.execute("ALTER TABLE reviews ADD COLUMN hold_reason TEXT")
except sqlite3.OperationalError:
    pass

# ── Auth (signed cookie) ────────────────────────────────────────────────
def sign(name: str) -> str:
    raw = name.encode()
    sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(raw).decode() + "." + sig

def verify(token: str):
    try:
        b64, sig = token.split(".")
        raw = base64.urlsafe_b64decode(b64.encode())
        if hmac.compare_digest(
            hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()[:32], sig
        ):
            return raw.decode()
    except Exception:
        pass
    return None

def current_user(request: Request):
    return verify(request.cookies.get("axis_session", "") or "")

LOGIN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Axis Intelligence Hub</title>
<link href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;600&family=Barlow+Condensed:wght@700;900&display=swap" rel="stylesheet">
<style>
body{background:#0a0a0a;color:#e8e8e8;font-family:Barlow,sans-serif;display:flex;
     align-items:center;justify-content:center;min-height:100vh;margin:0}
.box{background:#141414;border:1px solid #262626;border-radius:14px;padding:34px;width:320px}
h1{font-family:'Barlow Condensed';font-weight:900;text-transform:uppercase;font-size:24px;
   margin:0 0 4px;letter-spacing:.04em}
h1 span{color:#00c2a8}
p{color:#8a8a8a;font-size:12px;margin:0 0 20px}
label{font-size:11px;color:#8a8a8a;display:block;margin:12px 0 4px;text-transform:uppercase;letter-spacing:.08em}
select,input{width:100%;box-sizing:border-box;background:#0f0f0f;border:1px solid #262626;
  border-radius:8px;padding:11px;color:#e8e8e8;font-size:14px;font-family:Barlow}
button{width:100%;margin-top:20px;background:#00c2a8;border:none;border-radius:8px;
  padding:12px;color:#04211d;font-weight:600;font-size:14px;cursor:pointer;font-family:Barlow}
.err{color:#ff5c5c;font-size:12px;margin-top:10px}
</style></head><body><div class="box">
<h1>Axis <span>Intelligence Hub</span></h1><p>Sign in to continue</p>
<form method="post" action="/login">
<label>Who are you</label><select name="name">__STAFF__</select>
<label>Facility password</label><input type="password" name="password" autofocus>
<button>Sign in</button>__ERR__</form></div></body></html>"""

# ── Sync engine ─────────────────────────────────────────────────────────
_sync_lock = threading.Lock()

def run_sync() -> tuple[bool, str]:
    """Run Lumin then VALD sync, store snapshot in DB."""
    if not LUMIN_KEY:
        return False, "LUMIN_API_KEY not set"
    if not _sync_lock.acquire(blocking=False):
        return False, "sync already running"
    try:
        import lumin_client, vald_client
        os.chdir(ROOT)
        lumin_client.sync(LUMIN_KEY, out_path=str(ROOT / "hub_data.json"))
        try:
            vald_client.cmd_sync(str(ROOT / "hub_data.json"))
            detail = "Lumin + VALD"
        except SystemExit as e:
            detail = f"Lumin only (VALD: {e})"
        except Exception as e:
            detail = f"Lumin only (VALD failed: {type(e).__name__}: {e})"
        data = (ROOT / "hub_data.json").read_text()
        with db() as c:
            c.execute("INSERT INTO snapshots(at,data) VALUES(?,?)",
                      (datetime.now(timezone.utc).isoformat(), data))
            c.execute("INSERT INTO sync_log(at,ok,detail) VALUES(?,1,?)",
                      (datetime.now(timezone.utc).isoformat(), detail))
        return True, detail
    except Exception as e:
        with db() as c:
            c.execute("INSERT INTO sync_log(at,ok,detail) VALUES(?,0,?)",
                      (datetime.now(timezone.utc).isoformat(), f"{type(e).__name__}: {e}"))
        return False, f"{type(e).__name__}: {e}"
    finally:
        _sync_lock.release()

def generate_reviews():
    import review_gen
    with db() as c:
        snap = c.execute("SELECT data FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        fac = c.execute("SELECT data FROM facility WHERE id=1").fetchone()
        adp_rows = {r["athlete_key"]: json.loads(r["data"]) for r in
                    c.execute("SELECT athlete_key,data FROM adp_state")}
        crit_rows = {r["athlete_key"]: json.loads(r["data"]) for r in
                     c.execute("SELECT athlete_key,data FROM rehab_criteria")}
    if not snap:
        return 0, "no data snapshot yet"
    athletes = json.loads(snap["data"])
    facility = json.loads(fac["data"]) if fac else None
    wl = review_gen.week_label()
    made = 0
    with db() as c:
        for key, a in athletes.items():
            a = dict(a)
            if key in adp_rows:
                a["adp"] = adp_rows[key]
            draft = review_gen.generate_review(a, facility, crit_rows.get(key))
            try:
                c.execute("""INSERT INTO reviews(athlete_key,name,week_label,generated_at,draft)
                             VALUES(?,?,?,?,?)""",
                          (key, a.get("name"), wl,
                           datetime.now(timezone.utc).isoformat(), draft))
                made += 1
            except sqlite3.IntegrityError:
                pass  # already generated this week
    return made, wl


def scheduler():
    """Initial sync if DB empty, then daily at SYNC_HOUR."""
    with db() as c:
        empty = c.execute("SELECT COUNT(*) n FROM snapshots").fetchone()["n"] == 0
    if empty and LUMIN_KEY:
        run_sync()
    last_day = None
    while True:
        now = datetime.now()
        if now.hour == SYNC_HOUR and last_day != now.date():
            last_day = now.date()
            run_sync()
        if now.weekday() == 0 and now.hour == 2:
            generate_reviews()  # UNIQUE constraint makes this idempotent
        time.sleep(300)

threading.Thread(target=scheduler, daemon=True).start()

# ── State assembly ──────────────────────────────────────────────────────
DEMO_PRP_AUDIT = []

def assemble_reviews():
    with db() as c:
        rows = c.execute("""SELECT * FROM reviews WHERE status IN ('pending','held')
                            OR id IN (SELECT id FROM reviews r2 WHERE status='approved'
                                      ORDER BY approved_at DESC LIMIT 500)
                            ORDER BY id DESC""").fetchall()
    return [dict(r) for r in rows]


def assemble_state():
    with db() as c:
        snap = c.execute("SELECT at,data FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        adp_rows = c.execute("SELECT athlete_key,data FROM adp_state").fetchall()
        appr = c.execute("SELECT * FROM approvals ORDER BY id").fetchall()
    athletes = json.loads(snap["data"]) if snap else {}
    synced = snap["at"][:10] if snap else "never"
    adp = {r["athlete_key"]: json.loads(r["data"]) for r in adp_rows}
    prp_audit = list(DEMO_PRP_AUDIT)
    for r in appr:
        prp_audit.append({
            "athlete": r["athlete_key"], "name": r["name"], "from": r["from_phase"],
            "to": r["to_phase"], "phaseName": r["phase_name"], "approver": r["approver"],
            "at": r["at"], "kind": r["kind"], "note": r["note"],
            "snapshot": json.loads(r["snapshot"] or "[]"),
        })
    return athletes, adp, prp_audit, synced

# ── Routes ──────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = current_user(request)
    if not user:
        opts = "".join(f"<option>{s}</option>" for s in STAFF)
        return HTMLResponse(LOGIN_HTML.replace("__STAFF__", opts).replace("__ERR__", ""))
    athletes, adp, prp_audit, synced = assemble_state()
    html = TEMPLATE.read_text()
    html = html.replace("__LIVE_ATHLETES__", json.dumps(athletes, separators=(",", ":")))
    html = html.replace("__PRP_AUDIT__", json.dumps(prp_audit, separators=(",", ":")))
    html = html.replace("__ADP_STATE__", json.dumps(adp, separators=(",", ":")))
    html = html.replace("__USER__", user)
    html = html.replace("__PHYSIOS__", json.dumps(PHYSIOS))
    html = html.replace("__SYNCED__", synced)
    with db() as c:
        fac = c.execute("SELECT data FROM facility WHERE id=1").fetchone()
    html = html.replace("__FACILITY__", fac["data"] if fac else "null")
    html = html.replace("__REVIEWS__", json.dumps(assemble_reviews(), separators=(",", ":")))
    with db() as c:
        crit = {r["athlete_key"]: json.loads(r["data"]) for r in
                c.execute("SELECT athlete_key,data FROM rehab_criteria")}
    html = html.replace("__REHAB_CRITERIA__", json.dumps(crit, separators=(",", ":")))
    with db() as c:
        meta = {r["athlete_key"]: json.loads(r["data"]) for r in
                c.execute("SELECT athlete_key,data FROM athlete_meta")}
    html = html.replace("__ATHLETE_META__", json.dumps(meta, separators=(",", ":")))
    with db() as c:
        meta = {r["athlete_key"]: json.loads(r["data"]) for r in
                c.execute("SELECT athlete_key,data FROM athlete_meta")}
    html = html.replace("__META__", json.dumps(meta, separators=(",", ":")))
    return HTMLResponse(html)

@app.post("/login")
async def login(request: Request):
    form = await request.form()
    name, pw = form.get("name", ""), form.get("password", "")
    if name in STAFF and hmac.compare_digest(pw, AXIS_PASSWORD):
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie("axis_session", sign(name), httponly=True,
                        max_age=60 * 60 * 24 * 30, samesite="lax")
        return resp
    opts = "".join(f"<option>{s}</option>" for s in STAFF)
    return HTMLResponse(LOGIN_HTML.replace("__STAFF__", opts)
                        .replace("__ERR__", '<div class="err">Wrong password.</div>'),
                        status_code=401)

@app.get("/logout")
def logout():
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie("axis_session")
    return resp

def require_user(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Not signed in")
    return user

@app.post("/api/adp/{athlete_key}")
async def save_adp(athlete_key: str, request: Request):
    user = require_user(request)
    body = await request.json()
    with db() as c:
        c.execute("""INSERT INTO adp_state(athlete_key,data,updated_by,updated_at)
                     VALUES(?,?,?,?)
                     ON CONFLICT(athlete_key) DO UPDATE SET
                     data=excluded.data, updated_by=excluded.updated_by,
                     updated_at=excluded.updated_at""",
                  (athlete_key, json.dumps(body), user,
                   datetime.now(timezone.utc).isoformat()))
    return {"ok": True}

@app.post("/api/approvals")
async def save_approval(request: Request):
    user = require_user(request)
    if user not in PHYSIOS:
        raise HTTPException(403, f"Clinical progressions are physio-only. "
                                 f"Signed in as {user}; approvers: {', '.join(PHYSIOS)}.")
    e = await request.json()
    with db() as c:
        c.execute("""INSERT INTO approvals(athlete_key,name,from_phase,to_phase,
                     phase_name,approver,kind,note,snapshot,at)
                     VALUES(?,?,?,?,?,?,?,?,?,?)""",
                  (e.get("athlete"), e.get("name"), e.get("from"), e.get("to"),
                   e.get("phaseName"), f"{user} (Physio)", e.get("kind", "approval"),
                   e.get("note", ""), json.dumps(e.get("snapshot", [])), e.get("at")))
    return {"ok": True, "approver": f"{user} (Physio)"}

@app.post("/api/facility")
async def save_facility(request: Request):
    user = require_user(request)
    body = await request.json()
    body["by"] = user
    with db() as c:
        c.execute("""INSERT INTO facility(id,data) VALUES(1,?)
                     ON CONFLICT(id) DO UPDATE SET data=excluded.data""",
                  (json.dumps(body),))
    return {"ok": True}

@app.post("/api/meta/{athlete_key}")
async def save_meta(athlete_key: str, request: Request):
    user = require_user(request)
    body = await request.json()
    with db() as c:
        c.execute("""INSERT INTO athlete_meta(athlete_key,data,updated_by,updated_at)
                     VALUES(?,?,?,?)
                     ON CONFLICT(athlete_key) DO UPDATE SET
                     data=excluded.data, updated_by=excluded.updated_by,
                     updated_at=excluded.updated_at""",
                  (athlete_key, json.dumps(body), user,
                   datetime.now(timezone.utc).isoformat()))
    return {"ok": True}


@app.post("/api/meta/{athlete_key}")
async def save_meta(athlete_key: str, request: Request):
    user = require_user(request)
    body = await request.json()
    with db() as c:
        c.execute("""INSERT INTO athlete_meta(athlete_key,data,updated_by,updated_at)
                     VALUES(?,?,?,?)
                     ON CONFLICT(athlete_key) DO UPDATE SET
                     data=excluded.data, updated_by=excluded.updated_by,
                     updated_at=excluded.updated_at""",
                  (athlete_key, json.dumps(body), user,
                   datetime.now(timezone.utc).isoformat()))
    return {"ok": True}


@app.post("/api/criteria/{athlete_key}")
async def save_criteria(athlete_key: str, request: Request):
    user = require_user(request)
    body = await request.json()
    with db() as c:
        c.execute("""INSERT INTO rehab_criteria(athlete_key,data,updated_by,updated_at)
                     VALUES(?,?,?,?)
                     ON CONFLICT(athlete_key) DO UPDATE SET
                     data=excluded.data, updated_by=excluded.updated_by,
                     updated_at=excluded.updated_at""",
                  (athlete_key, json.dumps(body), user,
                   datetime.now(timezone.utc).isoformat()))
    return {"ok": True}


@app.post("/api/reviews/generate")
def gen_reviews_now(request: Request):
    require_user(request)
    made, wl = generate_reviews()
    return {"ok": True, "generated": made, "week": wl}


@app.post("/api/reviews/{rid}/approve")
async def approve_review(rid: int, request: Request):
    user = require_user(request)
    body = await request.json()
    action = body.get("action", "approve")   # approve | hold | release
    final = body.get("final_text", "")
    reason = (body.get("reason") or "").strip()
    with db() as c:
        row = c.execute("SELECT draft,status,final_text FROM reviews WHERE id=?", (rid,)).fetchone()
        if not row:
            raise HTTPException(404, "Review not found")
        now = datetime.now(timezone.utc).isoformat()
        if action == "release":
            if row["status"] != "held":
                raise HTTPException(409, "Only held reviews can be released")
            c.execute("UPDATE reviews SET status='approved', approved_at=? WHERE id=?", (now, rid))
            return {"ok": True, "approved_by": user, "status": "approved"}
        if row["status"] != "pending":
            raise HTTPException(409, "Already actioned")
        if action == "hold":
            if not reason:
                raise HTTPException(422, "A hold needs a reason on the record")
            c.execute("""UPDATE reviews SET status='held', approved_by=?, approved_at=?,
                         edited=?, final_text=?, hold_reason=? WHERE id=?""",
                      (user, now, 1 if final.strip() != row["draft"].strip() else 0,
                       final, reason, rid))
            return {"ok": True, "approved_by": user, "status": "held"}
        c.execute("""UPDATE reviews SET status='approved', approved_by=?,
                     approved_at=?, edited=?, final_text=? WHERE id=?""",
                  (user, now, 1 if final.strip() != row["draft"].strip() else 0, final, rid))
    return {"ok": True, "approved_by": user, "status": "approved"}


@app.post("/api/sync")
def manual_sync(request: Request):
    require_user(request)
    ok, detail = run_sync()
    if not ok:
        return JSONResponse({"ok": False, "detail": detail}, status_code=500)
    return {"ok": True, "detail": detail}

@app.get("/api/status")
def status(request: Request):
    require_user(request)
    with db() as c:
        snap = c.execute("SELECT at FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        logs = [dict(r) for r in c.execute(
            "SELECT at,ok,detail FROM sync_log ORDER BY id DESC LIMIT 5")]
        n_appr = c.execute("SELECT COUNT(*) n FROM approvals").fetchone()["n"]
    return {"last_snapshot": snap["at"] if snap else None,
            "recent_syncs": logs, "approvals_recorded": n_appr}

@app.get("/health")
def health():
    return {"ok": True}
