# Axis Intelligence Hub — Server

The hub as a real system: multi-user login, SQLite database, automatic daily
Lumin + VALD sync, and permanent records for approvals, audit trail and ADP
block state.

## What's in the box
- `server.py` — FastAPI app (auth, persistence, sync scheduler, hub serving)
- `hub_template.html` — the hub UI (server injects live state at page load)
- `lumin_client.py` / `vald_client.py` — the sync engines
- `requirements.txt`, `Dockerfile`, `.env.example`

## How it works
- On first start (and daily at `AXIS_SYNC_HOUR`, default 5am server time) the
  server pulls Lumin + VALD and stores a snapshot. "Sync now" button in the
  hub banner triggers it on demand.
- Staff sign in by name + one facility password. Every action is attributed
  to the signed-in person.
- Clinical progressions are enforced server-side: only names in
  `AXIS_PHYSIOS` can approve. Everyone else gets a clear refusal.
- Approvals, audit trail and block focuses live in `axis.db` (SQLite) and
  survive restarts, reboots and redeploys (keep the file on a persistent disk).

## Deploy — Option A: Railway (recommended, ~10 minutes, no server admin)
1. Create an account at railway.app → New Project → Deploy from local
   directory (or push this folder to a private GitHub repo and connect it).
   Railway detects the Dockerfile automatically.
2. Add a **Volume** mounted at `/app` (Settings → Volumes) so `axis.db`
   persists across deploys.
3. Set Variables (Settings → Variables): copy every line from `.env.example`
   with your real values. Use the NEW regenerated credentials, not the old ones.
4. Settings → Networking → Generate Domain. That URL is the hub —
   HTTPS included. Share it with the team.

## Deploy — Option B: any machine with Docker (VPS, or a PC at the facility)
```
cp .env.example .env      # fill it in
docker build -t axis-hub .
docker run -d --name axis-hub --env-file .env -p 8000:8000 \
  -v $(pwd)/data:/app/data -e AXIS_DB=/app/data/axis.db --restart unless-stopped axis-hub
```
Hub is at http://that-machine:8000. For internet access put it behind
HTTPS (Caddy/Cloudflare Tunnel are the easy routes).

## Deploy — Option C: bare Python (quickest local trial)
```
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)     # after filling in .env
uvicorn server:app --host 0.0.0.0 --port 8000
```

## Security checklist (do these)
- Regenerate the Lumin API key and VALD client secret; use only the new ones.
- Pick a strong `AXIS_PASSWORD` and a long random `AXIS_SECRET`.
- Only expose the hub over HTTPS (Railway does this for you).
- Back up `axis.db` weekly (it is the clinical audit record).

## Ops notes
- `GET /api/status` (signed in) shows last snapshot time, recent syncs,
  approvals count.
- Sync failures never crash the app; they're logged in `sync_log` and the
  previous snapshot keeps serving.
- Adding staff: edit `AXIS_STAFF` env and restart. Adding a physio approver:
  add the exact name to `AXIS_PHYSIOS` too.
