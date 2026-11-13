#!/bin/zsh
# Daily ingest: fetch new photos, OCR, and publish docs/data.json to GitHub Pages.
# Runs from launchd (~/Library/LaunchAgents/com.haru.mm-passport-ingest.plist); must run on a
# residential IP because Facebook forces login for datacenter IPs (e.g. GitHub Actions).
set -euo pipefail
cd "${0:A:h}"
echo "== $(date)"
git pull --rebase -q
HEADLESS=1 .venv/bin/python download.py
.venv/bin/python extract.py
if git diff --quiet docs/data.json; then echo "No changes"; exit 0; fi
git commit -q -m "Daily ingest $(date +%F)" docs/data.json
git push -q
echo "Pushed"
