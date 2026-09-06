#!/bin/bash
# Sets up the tools needed to lint this OctoPrint plugin in Claude Code on
# the web: flake8 (no OctoPrint install required - it only checks syntax
# and style within each file, not unresolved imports) and gettext (so
# octoprint_printbutler/translations/**/*.po can be validated/compiled the
# same way .github/workflows/compile-translations.yml does).
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

python3 -m pip install --user --quiet flake8

if ! command -v msgfmt >/dev/null 2>&1; then
  if sudo apt-get update -qq && sudo apt-get install -y -qq gettext; then
    :
  else
    echo "Warning: could not install gettext (msgfmt) - .po/.mo translation checks will be skipped." >&2
  fi
fi
