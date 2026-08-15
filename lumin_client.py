"""
AXIS PERFORMANCE - LUMIN API CLIENT
====================================
Built against the real Lumin OpenAPI 3.1 spec (document.json, 29 endpoints).

CONFIRMED FROM SPEC:
  Tenant:  aufb-413645
  Base:    https://app.luminsports.com/aufb-413645/thirdpartyapi/v1
  Auth:    HTTP Bearer  ->  Authorization: Bearer <API_TOKEN>
  Style:   Laravel filters: filter[user_id], filter[updated_since],
           per_page, page, include=relation1,relation2
  NOTE:    The API is READ-ONLY (every endpoint is GET). Data flows OUT of
           Lumin into the hub. Brief delivery INTO Lumin is not possible via
           this API - delivery goes via email/PDF until Lumin exposes writes.

USAGE
-----
  python lumin_client.py --test --key YOUR_TOKEN     # verify the key works
  python lumin_client.py --sync --key YOUR_TOKEN     # full pull -> hub_data.json
  python lumin_client.py --probe-vald --key TOKEN    # check if VALD results
                                                     # already flow in via sync
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    sys.exit("Run: pip install requests --break-system-packages")

TENANT = "aufb-413645"
BASE = f"https://app.luminsports.com/{TENANT}/thirdpartyapi/v1"
TIMEOUT = 30


class Lumin:
    def __init__(self, token: str):
        self.h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def get(self, endpoint: str, params: dict = None):
        r = requests.get(f"{BASE}/{endpoint}", headers=self.h,
                         params=params or {}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    def get_all(self, endpoint: str, params: dict = None, max_pages: int = 50):
        """Paginate any list endpoint."""
        out, page = [], 1
        params = dict(params or {})
        params.setdefault("per_page", 100)
        while page <= max_pages:
            params["page"] = page
            body = self.get(endpoint, params)
            data = body.get("data", body) if isinstance(body, dict) else body
            if not data:
                break
            out.extend(data)
            meta = body.get("meta", {}) if isinstance(body, dict) else {}
            last = meta.get("last_page")
            if last is None or page >= last:
                break
            page += 1
        return out

    # ── Typed pulls (endpoints verbatim from the spec) ──────────────────
    def groups(self):
        return self.get_all("groups", {"include": "users"})

    def athletes(self):
        return self.get_all("users", {"filter[is_athlete]": "true"})

    def wellness(self, since_days=7, user_id=None):
        since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        p = {"filter[timestamp]": f">={since}"}
        if user_id: p["filter[user_id]"] = user_id
        return self.get_all("wellness", p)

    def activities(self, since_days=7):
        since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self.get_all("activities", {"filter[timestamp]": f">={since}"})

    def questionnaires(self):
        return self.get_all("questionnaires")

    def submissions(self, since_days=7, with_answers=True):
        since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        p = {"filter[timestamp]": f">={since}"}
        if with_answers: p["with_answers"] = "true"
        return self.get_all("submissions", p)

    def health_items(self, active_only=True):
        p = {"include": "user"}
        if active_only: p["filter[is_recovered]"] = "false"
        return self.get_all("health/items", p)

    def rehab_plans(self):
        return self.get_all("health/rehab-plans")

    def rehab_phases(self):
        return self.get_all("health/rehab-phases", {"include": "rehabPlan"})

    def rehab_phase_types(self):
        return self.get_all("health/rehab-phase-types")

    def test_results(self, since_days=120, source_system=None):
        since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        p = {"include": "metric,testType",
             "filter[observed_at_utc]": f">={since}"}
        if source_system: p["filter[source_system]"] = source_system
        return self.get_all("physical-testing/observation-results", p)

    def training_load(self):
        return self.get_all("training-load")



def safe_pull(label, fn, *a, **kw):
    """Pull a module; on HTTP error report and continue with empty data."""
    try:
        data = fn(*a, **kw)
        print(f"  {label}: {len(data)}")
        return data, None
    except requests.HTTPError as e:
        code = e.response.status_code
        reason = {402: "NOT IN API PLAN (402 Payment Required)",
                  403: "FORBIDDEN (key lacks scope)",
                  404: "endpoint not found"}.get(code, f"HTTP {code}")
        print(f"  {label}: SKIPPED - {reason}")
        return [], f"{label}: {reason}"

# ── HUB SYNC ────────────────────────────────────────────────────────────
def sync(token: str, out_path="hub_data.json"):
    """Pull everything and map into the hub's ATHLETES shape."""
    api = Lumin(token)
    gated = []
    print("Pulling modules (skipping anything not in the API plan)...")
    EXCLUDE_GROUPS = ("archive", "inactive", "coaches and physios", "previous members", "sessional")
    groups_raw, err = safe_pull("groups (with members)", api.groups)
    if err: sys.exit("Cannot continue without groups: " + err)
    active_groups = [g for g in groups_raw
                     if not any(x in g["name"].lower() for x in EXCLUDE_GROUPS)]
    print("  Active groups: " + ", ".join(g["name"] for g in active_groups))
    groups_by_user, seen, athletes = {}, set(), []
    for g in active_groups:
        for u in (g.get("users") or []):
            groups_by_user.setdefault(u["id"], []).append(g["name"])
            if u["id"] not in seen:
                seen.add(u["id"]); athletes.append(u)
    print(f"  {len(athletes)} athletes across active groups (of {sum(len(g.get('users') or []) for g in groups_raw)} total memberships)")
    wellness, err = safe_pull("wellness (7d)", api.wellness, 7); gated += [err] if err else []
    injuries, err = safe_pull("health items (active)", api.health_items, True); gated += [err] if err else []
    plans, err = safe_pull("rehab plans", api.rehab_plans); gated += [err] if err else []
    phases, err = safe_pull("rehab phases", api.rehab_phases); gated += [err] if err else []
    _pt, err = safe_pull("rehab phase types", api.rehab_phase_types); gated += [err] if err else []
    ptypes = {p["id"]: p for p in _pt}
    results, err = safe_pull("test results (120d)", api.test_results, 120); gated += [err] if err else []
    subs, err = safe_pull("submissions (7d)", api.submissions, 7); gated += [err] if err else []
    if gated:
        print("\nMODULES NOT AVAILABLE ON THIS API PLAN:")
        for g in gated: print("  - " + g)

    # Index everything by user_id
    wl_by_user = defaultdict(list)
    for w in wellness: wl_by_user[w["user_id"]].append(w)
    inj_by_user = defaultdict(list)
    for i in injuries: inj_by_user[i["user_id"]].append(i)
    plan_by_user = {}
    for p in plans:
        if not p.get("deleted_at"): plan_by_user[p["user_id"]] = p
    phases_by_plan = defaultdict(list)
    for ph in phases:
        if not ph.get("deleted_at"): phases_by_plan[ph["rehab_plan_id"]].append(ph)
    res_by_user = defaultdict(list)
    for r in results: res_by_user[r["user_id"]].append(r)
    subs_by_user = defaultdict(list)
    for s in subs: subs_by_user[s["user_id"]].append(s)

    today = datetime.now(timezone.utc).date()
    hub = {}
    for a in athletes:
        uid = a["id"]
        name = f"{a.get('first_name','')} {a.get('last_name','')}".strip()
        key = (name or str(uid)).lower().replace(" ", "").replace("'", "")
        if key in hub:  # name collision -> disambiguate with Lumin id
            key = f"{key}_{str(uid)[:6]}"
        init = "".join(w[0] for w in name.split()[:2]).upper() or "??"

        # Wellness 7-day strip: mean of attribute values per local day
        by_day = defaultdict(list)
        for w in wl_by_user.get(uid, []):
            if w.get("value") is None: continue
            try:
                d = datetime.fromisoformat(w["timestamp"].replace("Z", "+00:00")).date()
                by_day[d].append(w["value"])
            except Exception:
                continue
        week = []
        for delta in range(6, -1, -1):
            d = today - timedelta(days=delta)
            vals = by_day.get(d)
            week.append(round(sum(vals) / len(vals), 1) if vals else None)

        # Completeness: wellness days + any submission this week (simple v1;
        # split by questionnaire type once questionnaire IDs are known)
        wl_days = sum(1 for v in week if v is not None)
        has_sub = 1 if subs_by_user.get(uid) else 0
        comp = round(100 * (wl_days / 7 * 0.7 + has_sub * 0.3))
        tier = "full" if comp >= 80 else "partial" if comp >= 55 else "thin"

        # Injury flags -> physio referral stream
        flags = []
        for it in inj_by_user.get(uid, []):
            side = f" ({it['laterality']})" if it.get("laterality") else ""
            nm = ((it.get("item") or {}).get("display_name")) or "Unspecified"
            kind = it.get("item_type") or "injury"
            sev = it.get("severity")
            self_rep = it.get("reported_by_id") == uid
            flags.append({
                "ic": "\U0001FA75" if kind == "injury" else "\U0001F912",
                "bg": "var(--red-dim)",
                "t": f"{nm}{side} — active {kind}" + (f", severity {sev}/100" if sev is not None else ""),
                "d": ("Self-reported by athlete. " if self_rep else "Recorded by staff. ") +
                     f"Reported {it.get('date','?')}. Unrecovered in Lumin. " +
                     ("Physio screen per referral protocol." if kind == "injury" else "Monitor per illness protocol."),
                "pill": "p-r", "pl": "Injury" if kind == "injury" else "Illness",
                "kind": kind, "date": it.get("date"), "severity": sev, "name": nm, "side": it.get("laterality"),
            })
        flags.sort(key=lambda f: f.get("date") or "", reverse=True)

        # Program + groups from Lumin group membership
        my_groups = groups_by_user.get(uid, [])
        prog = "PRP" if any("prp" in g.lower() for g in my_groups) else "ADP"
        prp = None
        plan = plan_by_user.get(uid)
        if plan:
            prog = "PRP"  # rehab plan overrides group label
            plist = sorted(phases_by_plan.get(plan["id"], []), key=lambda x: x.get("order", 0))
            plist.sort(key=lambda x: (x.get("date") or "", x.get("order") or 0))
            cur = plist[-1] if plist else None
            def wks(d):
                try:
                    return round((datetime.now(timezone.utc).date() -
                                  datetime.fromisoformat(d).date()).days / 7)
                except Exception:
                    return None
            prp = {
                "planId": plan["id"],
                "notes": plan.get("notes_plaintext") or "",
                "phaseName": (cur or {}).get("name", "Unknown"),
                "phaseEntered": (cur or {}).get("date"),
                "weeksInPhase": wks((cur or {}).get("date")) if cur else None,
                "started": plist[0].get("date") if plist else None,
                "weeksTotal": wks(plist[0].get("date")) if plist else None,
                "history": [{"n": p.get("name","?"), "date": p.get("date")} for p in plist],
            }

        # Testing: latest vs previous per metric, most recent 8 metrics
        by_metric = defaultdict(list)
        for r in res_by_user.get(uid, []):
            by_metric[r["metric_id"]].append(r)
        testing = []
        for mid, rows in by_metric.items():
            rows.sort(key=lambda x: x.get("observed_at_utc") or "", reverse=True)
            latest = rows[0]
            prev = rows[1] if len(rows) > 1 else None
            metric = (latest.get("metric") or {})
            units = latest.get("result_units") or metric.get("units") or ""
            testing.append({
                "n": metric.get("display_name") or metric.get("code") or mid[:8],
                "v": f"{latest.get('result_value','?')}{units}",
                "prev": f"{prev.get('result_value','?')}{units}" if prev else "first test",
                "pb": bool(prev and (latest.get("result_value") or 0) > (prev.get("result_value") or 0)),
                "norm": "-",            # sport norms layer comes from VALD norm PDFs
                "st": "c-t",
                "source": latest.get("source_system") or "manual",
                "observed": latest.get("observed_at_utc"),
            })
        testing.sort(key=lambda t: t["observed"] or "", reverse=True)
        testing = testing[:8]

        hub[key] = {
            "luminId": uid, "name": name, "init": init,
            "avc": "av-r" if flags else ("av-t" if prog == "ADP" else "av-g"),
            "prog": prog, "sub": f"{prog} · {my_groups[0] if my_groups else 'No group'} · Lumin live",
            "groups": my_groups,
            "comp": comp, "tier": tier, "flags": flags,
            "wellness": week, "overview": "",
            "testing": testing, "blueprintDue": "",
            "goals": [], "mealQ": "", "weeklyPrompt": "",
            "prpLive": prp,
        }

    with open(out_path, "w") as f:
        json.dump(hub, f, indent=2)
    print(f"\nWrote {len(hub)} athletes -> {out_path}")
    print("Next: paste hub_data.json into the chat and the Intelligence Hub")
    print("gets wired to consume it (replacing the sample ATHLETES object).")


def probe_vald(token: str):
    """Do VALD results already flow into Lumin? Check source_system values."""
    api = Lumin(token)
    rows = api.test_results(365)
    systems = defaultdict(int)
    for r in rows:
        systems[r.get("source_system") or "manual"] += 1
    print("Test results in the last 365 days, by source system:")
    for s, n in sorted(systems.items(), key=lambda kv: -kv[1]):
        print(f"  {s:20} {n}")
    print("\nIf VALD appears above, the VALD->Lumin sync is delivering test data")
    print("and the hub can run on this API alone for testing metrics. Whatever")
    print("is missing tells us exactly what the direct VALD API still owes us.")


def test_key(token: str):
    api = Lumin(token)
    try:
        body = api.get("users", {"per_page": 1})
        n = (body.get("meta") or {}).get("total", "?")
        print(f"KEY WORKS. Tenant {TENANT} reachable. Total users: {n}")
    except requests.HTTPError as e:
        code = e.response.status_code
        hint = {401: "Key rejected - check it is active",
                403: "Key valid but lacks permission - ask Lumin to scope it",
                404: "Tenant path wrong - confirm tenant slug in Lumin URL"}.get(code, "")
        print(f"HTTP {code}. {hint}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", help="Lumin API token (or env LUMIN_API_KEY)")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--probe-vald", action="store_true")
    args = ap.parse_args()
    key = args.key or os.environ.get("LUMIN_API_KEY")
    if not key:
        sys.exit("Provide --key YOUR_TOKEN (Lumin Settings > Integrations)")
    if args.test: test_key(key)
    elif args.sync: sync(key)
    elif args.probe_vald: probe_vald(key)
    else: ap.print_help()
