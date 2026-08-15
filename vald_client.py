"""
AXIS PERFORMANCE - VALD API CLIENT
===================================
OAuth2 client-credentials flow against VALD's external APIs.
Region: AUE (Australia East) - Melbourne tenant.

Fills the gaps the Lumin sync leaves: Lumin carries NordBord (and partial
ForceDecks) - this pulls the full ForceDecks + ForceFrame + NordBord picture
straight from the source and merges it into hub_data.json by athlete name.

USAGE
-----
  python vald_client.py --test               # auth + tenant discovery
  python vald_client.py --probe              # what data exists per system
  python vald_client.py --sync               # merge VALD tests into hub_data.json

Credentials are read from env or the constants below.
SECURITY: regenerate the client secret in VALD Hub after confirming the
connection - it has been shared in plain text.
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

CLIENT_ID = os.environ.get("VALD_CLIENT_ID", "Bpd3WMu7MskWrhpHKJ0AA7RNKOogMXWt")
CLIENT_SECRET = os.environ.get("VALD_CLIENT_SECRET", "gCshxrq07Z2M7BTRl0ReiRsGNxDBekMmYNj_iZEDsrUPHwDU5Nfi2qoD8HCCLUiM")

AUTH_URL = "https://auth.prd.vald.com/oauth/token"  # new Auth0 endpoint (March 2026 migration)
REGION = "aue"  # Australia East
APIS = {
    "tenants":    f"https://prd-{REGION}-api-externaltenants.valdperformance.com",
    "profiles":   f"https://prd-{REGION}-api-externalprofile.valdperformance.com",
    "forcedecks": f"https://prd-{REGION}-api-extforcedecks.valdperformance.com",
    "nordbord":   f"https://prd-{REGION}-api-externalnordbord.valdperformance.com",
    "forceframe": f"https://prd-{REGION}-api-externalforceframe.valdperformance.com",
}


class Vald:
    def __init__(self):
        self.token = None
        self.tenant_id = None

    def auth(self):
        r = requests.post(AUTH_URL, data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "audience": "vald-api-external",
        }, timeout=30)
        r.raise_for_status()
        self.token = r.json()["access_token"]
        return self.token

    def h(self):
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def get(self, api, path, params=None):
        r = requests.get(f"{APIS[api]}{path}", headers=self.h(),
                         params=params or {}, timeout=30)
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()

    def discover_tenant(self):
        body = self.get("tenants", "/tenants")
        tenants = body.get("tenants", body) if isinstance(body, dict) else body
        if not tenants:
            sys.exit("No tenants visible to these credentials.")
        self.tenant_id = tenants[0].get("id") or tenants[0].get("tenantId")
        return tenants

    def profiles(self):
        """All athlete profiles in the tenant."""
        out, page = [], 1
        while True:
            body = self.get("profiles", "/profiles",
                            {"tenantId": self.tenant_id, "page": page})
            if not body:
                break
            rows = body.get("profiles", body) if isinstance(body, dict) else body
            if not rows:
                break
            out.extend(rows)
            if isinstance(body, dict) and body.get("totalPages") and page >= body["totalPages"]:
                break
            if not (isinstance(body, dict) and body.get("totalPages")):
                break
            page += 1
        return out

    # ── Test pulls per system ────────────────────────────────────────────
    def fd_tests(self, since_days=365):
        """ForceDecks tests modified since a date. Paged by modifiedFromUtc cursor."""
        since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        out, cursor = [], since
        for _ in range(100):
            body = self.get("forcedecks", "/tests",
                            {"tenantId": self.tenant_id, "modifiedFromUtc": cursor})
            if not body:
                break
            rows = body.get("tests", body) if isinstance(body, dict) else body
            if not rows:
                break
            out.extend(rows)
            last = rows[-1].get("modifiedDateUtc") or rows[-1].get("modifiedUtc")
            if not last or last == cursor or len(rows) < 50:
                break
            cursor = last
        return out

    def fd_trials(self, test_id):
        return self.get("forcedecks", f"/v2019q3/teams/{self.tenant_id}/tests/{test_id}/trials")

    def _paged_v2(self, api, since_days):
        since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        out, cursor = [], since
        for _ in range(200):
            body = self.get(api, "/tests/v2",
                            {"TenantId": self.tenant_id, "ModifiedFromUtc": cursor})
            if not body:
                break
            rows = body.get("tests", body) if isinstance(body, dict) else body
            if not rows:
                break
            out.extend(rows)
            last = rows[-1].get("modifiedDateUtc") or rows[-1].get("modifiedUtc")
            if not last or last == cursor:
                break
            cursor = last
        return out

    def nb_tests(self, since_days=365):
        return self._paged_v2("nordbord", since_days)

    def ff_tests(self, since_days=365):
        return self._paged_v2("forceframe", since_days)


def cmd_test():
    v = Vald()
    try:
        v.auth()
        print("AUTH OK - token issued.")
    except requests.HTTPError as e:
        sys.exit(f"AUTH FAILED HTTP {e.response.status_code}: {e.response.text[:300]}")
    except requests.ConnectionError:
        sys.exit("NETWORK BLOCKED - add *.valdperformance.com to the egress "
                 "allowlist and start a FRESH conversation (same as Lumin).")
    tenants = v.discover_tenant()
    print(f"Tenants visible: {len(tenants)}")
    for t in tenants:
        print(f"  {t.get('name','?')}  id={t.get('id') or t.get('tenantId')}")
    profs = v.profiles()
    print(f"Profiles in tenant: {len(profs)}")


def cmd_probe():
    v = Vald(); v.auth(); v.discover_tenant()
    for label, fn in (("ForceDecks", v.fd_tests), ("NordBord", v.nb_tests), ("ForceFrame", v.ff_tests)):
        try:
            rows = fn(365)
            print(f"{label}: {len(rows)} tests in last 365 days")
            types = defaultdict(int)
            for r in rows:
                types[r.get("testType") or r.get("testTypeName") or "?"] += 1
            for t, n in sorted(types.items(), key=lambda kv: -kv[1])[:8]:
                print(f"    {t:30} {n}")
        except requests.HTTPError as e:
            print(f"{label}: HTTP {e.response.status_code} - "
                  f"{'not licensed on this key' if e.response.status_code in (401,403) else 'check endpoint'}")


def cmd_sync(hub_path="hub_data.json"):
    """Merge VALD tests into hub_data.json, matching athletes by name."""
    if not os.path.exists(hub_path):
        sys.exit(f"{hub_path} not found - run lumin_client.py --sync first.")
    hub = json.load(open(hub_path))
    import re as _re
    from difflib import SequenceMatcher
    def norm(n):
        n = _re.sub(r"\s+", " ", (n or "").strip().lower())
        # collapse duplicated trailing surname ("x y y" -> "x y")
        parts = n.split(" ")
        if len(parts) >= 2 and parts[-1] == parts[-2]:
            parts = parts[:-1]
        return " ".join(parts)
    by_name = { norm(a["name"]): k for k, a in hub.items() }
    fuzzy_log = []
    def fuzzy(nm):
        best, score = None, 0.0
        for cand in by_name:
            s = SequenceMatcher(None, nm, cand).ratio()
            if s > score:
                best, score = cand, s
        if score >= 0.87:
            fuzzy_log.append((nm, best, round(score, 2)))
            return by_name[best]
        # same first name + close surname (catches Burham/Burman)
        p = nm.split(" ")
        if len(p) >= 2:
            for cand in by_name:
                c = cand.split(" ")
                if len(c) >= 2 and c[0] == p[0] and SequenceMatcher(None, " ".join(p[1:]), " ".join(c[1:])).ratio() >= 0.65:
                    fuzzy_log.append((nm, cand, "first+surname"))
                    return by_name[cand]
        return None
    # reset any previous VALD merge so re-runs don't duplicate
    for a in hub.values():
        a.pop("valdTests", None)

    v = Vald(); v.auth(); v.discover_tenant()
    profs = v.profiles()
    pmap = {}
    for p in profs:
        nm = f"{p.get('givenName','')} {p.get('familyName','')}".strip().lower()
        pmap[p.get("profileId") or p.get("id")] = nm

    matched, unmatched = 0, set()

    def push(uid_name, entry):
        nonlocal matched
        uid_name = norm(uid_name)
        key = by_name.get(uid_name) or fuzzy(uid_name)
        if not key:
            unmatched.add(uid_name); return
        hub[key].setdefault("valdTests", []).append(entry)
        matched += 1

    print("Pulling ForceDecks...")
    for t in v.fd_tests(365):
        nm = pmap.get(t.get("profileId") or t.get("athleteId"), "")
        push(nm, {"sys": "ForceDecks", "type": t.get("testType") or "?",
                  "date": t.get("recordedDateUtc") or t.get("testDateUtc"),
                  "id": t.get("testId") or t.get("id")})
    print("Pulling NordBord...")
    for t in v.nb_tests(365):
        nm = pmap.get(t.get("profileId") or t.get("athleteId"), "")
        push(nm, {"sys": "NordBord", "type": t.get("testTypeName") or "Nordic",
                  "date": t.get("testDateUtc") or t.get("modifiedDateUtc"),
                  "L": t.get("leftMaxForce") or t.get("leftAvgForce"),
                  "R": t.get("rightMaxForce") or t.get("rightAvgForce"),
                  "id": t.get("testId") or t.get("id")})
    print("Pulling ForceFrame...")
    for t in v.ff_tests(365):
        nm = pmap.get(t.get("profileId") or t.get("athleteId"), "")
        push(nm, {"sys": "ForceFrame", "type": t.get("testTypeName") or t.get("testMode") or "?",
                  "date": t.get("testDateUtc") or t.get("modifiedDateUtc"),
                  "id": t.get("testId") or t.get("id")})

    json.dump(hub, open(hub_path, "w"), indent=2)
    print(f"\nMerged {matched} VALD tests into {hub_path}")
    if fuzzy_log:
        print(f"FUZZY MATCHES ({len(set(fuzzy_log))}) - VALD name -> Lumin name (verify these):")
        for v, l, s in sorted(set(fuzzy_log)):
            print(f"   {v}  ->  {l}  ({s})")
    if unmatched:
        print(f"UNMATCHED VALD profiles ({len(unmatched)}) - name mismatch vs Lumin, review:")
        for n in sorted(unmatched)[:20]:
            print("  ", n or "(blank name)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--sync", action="store_true")
    a = ap.parse_args()
    if a.test: cmd_test()
    elif a.probe: cmd_probe()
    elif a.sync: cmd_sync()
    else: ap.print_help()
