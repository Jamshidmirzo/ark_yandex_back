#!/usr/bin/env bash
#
# Turn on the ark_yandex A2A backend agent in ONE command.
#
#   ./a2a/start.sh                 # same Wi-Fi/LAN — advertises your LAN IP
#   ./a2a/start.sh --ngrok         # different networks — opens an ngrok tunnel
#   ./a2a/start.sh --port 8888     # custom port
#   ./a2a/start.sh --autorun       # let BUG/FEATURE drive headless repo edits (risky)
#
# It is idempotent: creates the venv + installs deps only the first time.
# Stop with Ctrl-C (an ngrok tunnel, if started, is torn down automatically).

set -euo pipefail

PORT=9999
USE_NGROK=0
AUTORUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)    PORT="$2"; shift 2;;
    --ngrok)   USE_NGROK=1; shift;;
    --autorun) AUTORUN=1; shift;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "unknown arg: $1 (try --help)"; exit 1;;
  esac
done

A2A_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$A2A_DIR/.venv"
PY="$VENV/bin/python"

# ── 1. claude CLI on PATH? ──────────────────────────────────────────────────
if ! command -v claude >/dev/null 2>&1; then
  echo "⚠️  'claude' CLI not found on PATH — the agent needs it to answer."
  echo "    Install: https://docs.anthropic.com/en/docs/claude-code"
  exit 1
fi

# ── 2. venv + deps (only if missing) ────────────────────────────────────────
if [[ ! -x "$PY" ]]; then
  echo "📦  creating venv at a2a/.venv ..."
  python3.13 -m venv "$VENV" 2>/dev/null || python3 -m venv "$VENV"
fi
if ! "$PY" -c "import a2a, uvicorn" 2>/dev/null; then
  echo "📦  installing a2a-sdk + uvicorn ..."
  "$PY" -m pip install -q --upgrade pip
  "$PY" -m pip install -q -r "$A2A_DIR/requirements.txt"
fi

# ── 3. work out the public URL ──────────────────────────────────────────────
NGROK_PID=""
cleanup() { [[ -n "$NGROK_PID" ]] && kill "$NGROK_PID" 2>/dev/null || true; }
trap cleanup EXIT

if [[ "$USE_NGROK" == "1" ]]; then
  command -v ngrok >/dev/null 2>&1 || { echo "⚠️  ngrok not installed (brew install ngrok)"; exit 1; }
  echo "🌐  starting ngrok tunnel on :$PORT ..."
  ngrok http "$PORT" --log=stdout >/tmp/a2a-ngrok.log 2>&1 &
  NGROK_PID=$!
  PUBLIC_URL=""
  for _ in $(seq 1 30); do
    PUBLIC_URL=$(curl -s http://127.0.0.1:4040/api/tunnels \
      | "$PY" -c "import sys,json; t=[x['public_url'] for x in json.load(sys.stdin).get('tunnels',[]) if x['public_url'].startswith('https')]; print(t[0] if t else '')" 2>/dev/null || true)
    [[ -n "$PUBLIC_URL" ]] && break
    sleep 1
  done
  [[ -z "$PUBLIC_URL" ]] && { echo "⚠️  could not read ngrok URL — see /tmp/a2a-ngrok.log"; exit 1; }
else
  IP=$(ipconfig getifaddr en0 2>/dev/null || true)
  [[ -z "$IP" ]] && IP=$(ipconfig getifaddr en1 2>/dev/null || true)
  [[ -z "$IP" ]] && IP=$(ifconfig | awk '/inet /&&$2!="127.0.0.1"{print $2; exit}')
  [[ -z "$IP" ]] && { echo "⚠️  could not detect a LAN IP — try --ngrok"; exit 1; }
  PUBLIC_URL="http://$IP:$PORT"
fi

# ── 4. tell the human what to hand the Flutter dev ──────────────────────────
cat <<EOF

────────────────────────────────────────────────────────────────────────────
  A2A is UP.  Public URL:  $PUBLIC_URL
$( [[ "$USE_NGROK" == "0" ]] && echo "  (LAN only — same Wi-Fi. For a remote dev re-run with --ngrok.)" )

  Give the Flutter dev this one command:

    claude mcp add a2a-bridge -e A2A_AGENT_URLS=$PUBLIC_URL -- npx -y a2a-mcp-bridge

  + the file a2a/CLAUDE_FRONTEND.md (rules for ark_flutter_3/CLAUDE.md).

  Quick self-check (new terminal):
    curl ${PUBLIC_URL%/}/.well-known/agent-card.json
────────────────────────────────────────────────────────────────────────────

EOF

# ── 5. run the server (foreground; Ctrl-C stops it + ngrok) ─────────────────
if [[ "$AUTORUN" == "1" ]]; then
  "$PY" "$A2A_DIR/server.py" --port "$PORT" --public-url "$PUBLIC_URL" --autorun
else
  "$PY" "$A2A_DIR/server.py" --port "$PORT" --public-url "$PUBLIC_URL"
fi
