#!/usr/bin/env bash
# Jarvis — one-command setup
# Creates a virtual env, installs deps, and scaffolds .env

set -euo pipefail

VENV=".venv"
PYTHON="python3"

# ── Colour helpers ─────────────────────────────────────────────────────────────
green()  { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
red()    { printf "\033[31m%s\033[0m\n" "$*"; }
bold()   { printf "\033[1m%s\033[0m\n" "$*"; }
dim()    { printf "\033[2m%s\033[0m\n" "$*"; }

bold "Jarvis — setup"
echo ""

# ── Python version check ───────────────────────────────────────────────────────
if ! command -v "$PYTHON" &>/dev/null; then
  red "✗ python3 not found. Install Python 3.9+ and try again."
  exit 1
fi

py_version=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
py_major=$(echo "$py_version" | cut -d. -f1)
py_minor=$(echo "$py_version" | cut -d. -f2)

if [[ "$py_major" -lt 3 ]] || [[ "$py_major" -eq 3 && "$py_minor" -lt 9 ]]; then
  red "✗ Python 3.9+ required. Found: $py_version"
  exit 1
fi

green "✓ Python $py_version"

# ── Virtual environment ────────────────────────────────────────────────────────
if [[ -d "$VENV" ]]; then
  dim "  .venv already exists — skipping creation"
else
  printf "  Creating virtual environment… "
  "$PYTHON" -m venv "$VENV"
  green "done"
fi

# ── Dependencies ───────────────────────────────────────────────────────────────
printf "  Installing dependencies… "
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r requirements.txt
green "done"

# ── .env scaffold ─────────────────────────────────────────────────────────────
if [[ -f ".env" ]]; then
  dim "  .env already exists — skipping"
else
  cp .env.example .env
  yellow "  .env created from template — add your API keys before running the sync"
fi

# ── Done ───────────────────────────────────────────────────────────────────────
echo ""
bold "Setup complete. Next steps:"
echo ""
echo "  1. Add your API keys to .env"
echo "     $(dim 'TODOIST_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_ANON_KEY')"
echo ""
echo "  2. Verify everything is wired up:"
echo "     make verify"
echo "     $(dim '— or —  .venv/bin/python verify_setup.py')"
echo ""
echo "  3. Run your first sync:"
echo "     make sync"
echo "     $(dim '— or —  .venv/bin/python sync_todoist.py')"
echo ""
echo "  Other useful commands:"
echo "     make sync-dry-run   $(dim '# preview counts, no writes')"
echo "     make help           $(dim '# see all commands')"
echo ""
