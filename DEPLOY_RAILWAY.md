# Deploying to Railway

The app runs on Railway as a single Docker service with one persistent
volume. Total setup is ~10 minutes.

## How it fits together

- Railway builds the existing `Dockerfile` (config in `railway.toml`).
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
