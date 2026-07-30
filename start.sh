#!/usr/bin/env bash
set -e
set -u
set -o pipefail

# Resolve to the repo root so cli.py is found no matter where this is invoked
# from — the point of passing --projects-dir/--db is running it from elsewhere.
cd "$(dirname "${BASH_SOURCE[0]}")"

# Read KEY=value pairs from .env, if present. See .env.example for the full
# list of supported variables.
#
# Parsed rather than sourced: sourcing would execute whatever is in the file,
# and would let .env clobber variables already set in the caller's environment.
# Anything already exported wins, so `PORT=9000 ./start.sh` beats a PORT in .env.
load_dotenv() {
  local file=$1 line key value
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line#"${line%%[![:space:]]*}"}                 # strip leading space
    case "$line" in ''|'#'*) continue ;; esac             # blank or comment
    line=${line#export }
    case "$line" in *=*) ;; *) continue ;; esac           # no '=', not an assignment
    key=${line%%=*}
    value=${line#*=}
    key=${key%"${key##*[![:space:]]}"}                    # strip trailing space
    case "$key" in ''|*[!A-Za-z0-9_]*) continue ;; esac   # valid shell name only
    value=${value#"${value%%[![:space:]]*}"}
    case "$value" in
      \"*\") value=${value#\"}; value=${value%\"} ;;      # "quoted value"
      \'*\') value=${value#\'}; value=${value%\'} ;;      # 'quoted value'
      *)     value=${value%"${value##*[![:space:]]}"} ;;  # bare: strip trailing space
    esac
    [ -n "${!key:-}" ] || export "$key=$value"
  done < "$file"
}

load_dotenv .env

# Forward every argument straight through to the dashboard subcommand:
#   ./start.sh --projects-dir /path/to/projects --db /path/to/cache.db
#   -> python3 cli.py dashboard --projects-dir /path/to/projects --db /path/to/cache.db
# exec replaces this shell so Ctrl+C reaches the server directly.
exec python3 cli.py dashboard "$@"
