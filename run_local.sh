#!/usr/bin/env bash
# PLTR Signal Desk — local launcher.
# Access at http://localhost:8000  (and http://<this-machine-IP>:8000 from your phone/other devices on the same Wi-Fi).
set -e
cd "$(dirname "$0")"

# optional: load a local .env (ANTHROPIC_API_KEY, X_BEARER_TOKEN, ...)
[ -f .env ] && set -a && . ./.env && set +a

PORT="${PORT:-8000}"

echo "Installing dependencies (first run only)…"
python3 -m pip install -q -r requirements.txt

# best-effort LAN IP for phones/other computers
IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "")
echo ""
echo "=================================================="
echo "  PLTR Signal Desk is starting…"
echo "  On this Mac:      http://localhost:$PORT"
[ -n "$IP" ] && echo "  On your network:  http://$IP:$PORT   (phone, other laptop)"
echo "  Stop with Ctrl+C"
echo "=================================================="
echo ""
exec python3 -m uvicorn main:app --host 0.0.0.0 --port "$PORT"
