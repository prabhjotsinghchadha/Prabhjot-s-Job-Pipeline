#!/bin/bash
# One-time local setup: venv, Python deps, Playwright's Chromium, Claude CLI check.
set -e
command -v python3.11 >/dev/null || { echo "Python 3.11 not found (brew install python@3.11)"; exit 1; }
[ -d .venv ] || python3.11 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m playwright install chromium
if command -v claude >/dev/null; then
  echo "Claude CLI: $(claude --version)"
else
  echo "Claude CLI not found. Install: npm install -g @anthropic-ai/claude-code && claude auth"
fi
mkdir -p .cache resumes
echo
echo "Done. Start the dashboard with:"
echo "  .venv/bin/python main.py server"
echo "then open http://localhost:8080 and fill in Settings."
