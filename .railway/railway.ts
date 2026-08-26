import { defineRailway, preserve, project, service, volume } from "railway/iac";

// Railway IaC (successor to railway.toml) — evaluated by `railway config plan/apply`.
// Requires `npm install` at the repo root (devDependency: railway).
// Variable VALUES are not stored here: preserve() keeps whatever is set on the
// service. Set them in the Railway dashboard — see DEPLOY_RAILWAY.md.

export default defineRailway(() => {
  const mrJobsVolume = volume("mr-jobs-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "ams", sizeMB: 500 });
  const mrJobs = service("mr-jobs", {
    // Known quirk (CLI 4.x): apply reports success for builder and
    // restartPolicyType but the service keeps null, so plan always shows
    // these two as pending. Harmless — Railway auto-detects the root
    // Dockerfile and defaults to ON_FAILURE restarts.
    build: { builder: "DOCKERFILE" },
    deploy: {
      restartPolicyType: "ON_FAILURE",
      restartPolicyMaxRetries: 5,
    },
    healthcheck: "/api/health",
    healthcheckTimeout: 300,
    replicas: { "ams": 1 },
    volumeMounts: { "/data": mrJobsVolume },
    env: { ALLOWED_EMAILS: preserve(), CLAUDE_CODE_OAUTH_TOKEN: preserve(), DASHBOARD_PASSWORD: preserve(), DATA_DIR: preserve(), FIREBASE_PROJECT_ID: preserve(), FIREBASE_WEB_CONFIG: preserve() },
  });

  return project("mr-jobs", {
    resources: [mrJobs, mrJobsVolume],
  });
});
