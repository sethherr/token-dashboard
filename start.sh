#!/usr/bin/env bash
set -e
set -u
set -o pipefail

# Resolve to the repo root so cli.py is found no matter where this is invoked
# from — the point of passing --projects-dir/--db is running it from elsewhere.
cd "$(dirname "${BASH_SOURCE[0]}")"

# Forward every argument straight through to the dashboard subcommand:
#   ./start.sh --projects-dir /path/to/projects --db /path/to/cache.db
#   -> python3 cli.py dashboard --projects-dir /path/to/projects --db /path/to/cache.db
# exec replaces this shell so Ctrl+C reaches the server directly.
exec python3 cli.py dashboard "$@"
