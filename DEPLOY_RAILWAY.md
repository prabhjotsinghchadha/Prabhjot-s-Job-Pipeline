# Deploying to Railway

The app runs on Railway as a single Docker service with one persistent
volume. Total setup is ~10 minutes.

## How it fits together

- Railway builds the existing `Dockerfile` (service config in
  `.railway/railway.ts`, applied with `railway config plan` / `apply`;
  requires `npm install` at the repo root for the `railway` SDK).
- Railway injects `PORT`; the entrypoint honors it.
- A volume mounted at `/data` holds everything mutable: `applications.db`,
  `profile.yaml`, `.cache/`, `resumes/`. Setting `DATA_DIR=/data` makes the
  entrypoint symlink those paths onto the volume, so redeploys and restarts
  never lose data.
- `DASHBOARD_PASSWORD` enables HTTP Basic auth on every route (and the
  WebSocket) except `/api/health`. **Never deploy without it** — the
  dashboard exposes your profile, resume, and application actions.
- `.dockerignore` already excludes `profile.yaml`, the DB, and resumes, so
  no personal data ships inside the image. You configure the profile through
  the first-run wizard on the deployed dashboard.

## Steps

```bash
# 1. Install the CLI and log in
npm install -g @railway/cli
railway login

# 2. From the repo root, create the project + service
railway init

# 3. Attach a persistent volume at /data
railway volume add --mount-path /data

# 4. Set variables (pick ONE of the two Claude credentials)
railway variables \
  --set "DATA_DIR=/data" \
  --set "DASHBOARD_PASSWORD=<choose-a-strong-password>" \
  --set "ANTHROPIC_API_KEY=<sk-ant-...>"
# or: --set "CLAUDE_CODE_OAUTH_TOKEN=<token from `claude setup-token`>"

# 5. Deploy (uploads the working directory and builds the Dockerfile)
railway up

# 6. Give it a public URL
railway domain
```

Then open the URL: the browser prompts for credentials (user `admin`, or
override with `DASHBOARD_USER`). The first-run wizard creates your profile;
upload your resume in the dashboard, then hit Discover.

## Sizing and cost

- Playwright's Chromium is the memory driver — give the service **2 GB**
  (Settings → Resources). With usage-based pricing an idle-most-of-the-time
  service typically lands in the $5–15/mo range.
- The scheduler keeps the process warm (discovery every 6 h, scoring every
  30 m), so do not enable Railway's App Sleeping for this service.

## Multi-tenant mode (Firebase Auth)

Setting `FIREBASE_PROJECT_ID` switches the app from single-user to
multi-tenant: each person signs in with Firebase (email/password or
Google), and gets their **own** data directory under `/data/users/<uid>/`
— own profile, own job pipeline, own resumes. Isolation is by
construction: users have physically separate SQLite databases, and every
API request and WebSocket connection is scoped to the verified user.

### One-time Firebase setup (in console.firebase.google.com)

1. Create a project (e.g. `mr-jobs`). Google Analytics not needed.
2. **Authentication → Sign-in method**: enable **Email/Password** and
   (recommended) **Google**.
3. **Authentication → Settings → Authorized domains**: add your Railway
   domain (e.g. `mr-jobs-production.up.railway.app`).
4. **Project settings → General → Your apps → Add app → Web**: register a
   web app (no hosting needed) and copy the `firebaseConfig` object.

### Railway variables

```bash
railway variables \
  --set "FIREBASE_PROJECT_ID=<your-project-id>" \
  --set 'FIREBASE_WEB_CONFIG={"apiKey":"...","authDomain":"...","projectId":"...","appId":"..."}' \
  --set "ALLOWED_EMAILS=you@gmail.com,friend1@gmail.com,friend2@gmail.com"
```

- `FIREBASE_WEB_CONFIG` is the config object from step 4 as one JSON line.
  These values are public by design — access control comes from token
  verification plus the allowlist, not from hiding them.
- `ALLOWED_EMAILS` keeps the deployment friends-only: anyone can create a
  Firebase account, but only allowlisted emails pass the server. Leave it
  unset to admit any account in your Firebase project (not recommended).
- `DASHBOARD_PASSWORD` is ignored in this mode — remove it if you like.

Redeploy (`railway up`) and the dashboard shows a sign-in screen; each
user runs their own setup wizard on first login.

### Multi-tenant caveats

- **Scoring bills the operator**: every user's scoring runs on the
  server's `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` and counts
  against its limits. Fine for a few friends; revisit before going wider.
- **Shared egress IP**: all users' scraping leaves from one Railway IP —
  the scheduler runs users sequentially to stay polite, but heavy use
  still risks job-board throttling for everyone.
- **Scheduler intervals are platform defaults** (discovery 6 h, scoring
  30 m per user) — per-user schedule settings in the profile are not yet
  honored in this mode.
- Existing single-user data is not auto-migrated into a user account; the
  `/data/applications.db` from single-user mode sits unused. Copy it into
  `/data/users/<uid>/applications.db` (find your uid in `/data/users.json`
  after first login) if you want your history.

## Caveats

- **Datacenter IP**: Indeed/LinkedIn/Glassdoor throttle cloud IPs harder
  than residential ones, so JobSpy discovery will be thinner than local
  runs. Greenhouse/Lever/HN/RSS sources are unaffected.
- **Two databases**: the Railway instance and your local Docker instance
  each have their own `applications.db`; they do not sync.
- **Migrating local data** (optional): the deployed app starts empty. To
  bring your local DB up:
  `base64 -i applications.db | railway ssh "base64 -d > /data/applications.db"`
  (run while the service is up, then redeploy/restart; do the same for
  `profile.yaml` if you want to skip the wizard).
