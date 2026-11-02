#!/bin/bash
# Double-click to refresh and open your Claude usage dashboard.
cd "$(dirname "$0")" || exit 1
if ! /usr/bin/env python3 claude_usage.py; then
  echo
  read -r -p "Something went wrong (details above). Press Return to close."
fi
