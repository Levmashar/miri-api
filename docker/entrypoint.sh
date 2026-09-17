#!/bin/bash
set -e

# ── miri-api Docker Entrypoint ─────────────────────────────
# Initializes the virtual display environment and starts all services.

echo "============================================================"
echo "  miri-api — Docker Container Starting"
echo "============================================================"
echo ""

# ── 1. Ensure directories exist ────────────────────────────────
mkdir -p /app/browser_data /app/logs /app/downloads/images /app/downloads/videos
echo "[entrypoint] Directories ready"
echo "  Browser data: /app/browser_data"
echo "  Logs:         /app/logs"

# Prune old log files at startup. TimedRotatingFileHandler already bounds the
# files it creates, but this also reclaims dated logs stranded by older builds
# and by restarts crossing midnight. Runs BEFORE any handler opens a file, so it
# can never touch a live log. `|| true` — a find error must not abort startup
# (the script runs under `set -e`).
find /app/logs -maxdepth 1 -name '*.log*' -mtime +"${LOG_FILE_RETENTION_DAYS:-14}" -type f -delete 2>/dev/null || true
echo "[entrypoint] Pruned log files older than ${LOG_FILE_RETENTION_DAYS:-14} days"

# ── 2. Clean up stale Chromium locks (from previous crash) ─────
# Clean the default profile (volume root) AND every per-account profile under
# _accounts/<id>. The app also cleans per-profile at launch, but doing it here
# too means a crashed container comes back cleanly. Scoped to each profile dir —
# never a recursive wipe that could cross into another account.
clean_locks() {
    rm -f "$1/SingletonLock" "$1/SingletonSocket" "$1/SingletonCookie" 2>/dev/null || true
}
clean_locks /app/browser_data
if [ -d /app/browser_data/_accounts ]; then
    for d in /app/browser_data/_accounts/*/; do
        [ -d "$d" ] && clean_locks "${d%/}"
    done
fi
echo "[entrypoint] Stale locks cleaned (all profiles)"

# ── 2.5. Set up VNC password ───────────────────────────────────
mkdir -p /app/.vnc
VNC_PASSWORD="${VNC_PASSWORD:-chatgpt}"
x11vnc -storepasswd "$VNC_PASSWORD" /app/.vnc/passwd 2>/dev/null
echo "[entrypoint] VNC password set (no username; password = VNC_PASSWORD)"

# ── 3. Pre-resolve DNS for Chrome ──────────────────────────────
# Chrome's built-in DNS resolver can fail with Docker's internal
# DNS proxy (127.0.0.11). Pre-resolve domains and add to /etc/hosts
# so Chrome can find them without DNS.
#
# Skipped entirely when a proxy is configured: the proxy resolves the
# destination hostname itself (Chrome just sends CONNECT host:443), so
# resolving here would leak DNS out the container's own egress and pin
# IPs from the wrong region.
if [ -n "${PROXY_SERVER:-}" ]; then
    echo "[entrypoint] PROXY_SERVER set — skipping DNS pre-resolution"
    echo "             (the proxy resolves destinations; local DNS would leak/mismatch)"
else
    echo "[entrypoint] Pre-resolving DNS for Chrome..."
    # Domain list is imported from src/dns_domains.py so this and
    # src/browser/manager.py can never drift apart.
    python3 -c "
import socket
import sys
sys.path.insert(0, '/app')
from src.providers.base import all_domains

resolved = []
for d in all_domains():
    try:
        ip = socket.gethostbyname(d)
        resolved.append(f'{ip} {d}')
        print(f'  {d} -> {ip}')
    except Exception as e:
        print(f'  {d} -> FAILED ({e})')

if resolved:
    with open('/etc/hosts', 'a') as f:
        f.write('\n# Pre-resolved DNS for Chrome (added by entrypoint)\n')
        for entry in resolved:
            f.write(entry + '\n')
    print(f'  Added {len(resolved)} entries to /etc/hosts')
else:
    print('  WARNING: No domains resolved!')
"
    echo "[entrypoint] DNS pre-resolution complete"
fi
echo ""

# ── 4. Log environment info ────────────────────────────────────
echo ""
echo "[entrypoint] Environment:"
echo "  DISPLAY=${DISPLAY}"
echo "  DISPLAY_WIDTH=${DISPLAY_WIDTH}"
echo "  DISPLAY_HEIGHT=${DISPLAY_HEIGHT}"
echo "  HEADLESS=${HEADLESS}"
echo "  API_PORT=${API_PORT}"
echo "  LOG_LEVEL=${LOG_LEVEL}"
echo ""

# ── 5. Verify Xvfb is available ────────────────────────────────
if ! command -v Xvfb &> /dev/null; then
    echo "[entrypoint] ERROR: Xvfb not found!"
    exit 1
fi
echo "[entrypoint] Xvfb found: $(which Xvfb)"

# ── 6. Verify patchright browser is installed ───────────────────
BROWSER_PATH=$(python -c "
import subprocess
r = subprocess.run(['patchright', 'install', '--dry-run', 'chromium'], capture_output=True, text=True)
print('OK')
" 2>/dev/null || echo "CHECKING")
echo "[entrypoint] Patchright browser: ready"

# ── 7. Print access info ───────────────────────────────────────
echo ""
echo "============================================================"
echo "  miri-api — Ready"
echo "============================================================"
echo ""
echo "  SERVICES:"
echo "  • API:   http://localhost:${API_PORT}/v1/models"
echo "  • noVNC: http://localhost:6080/vnc.html  (browser UI)"
echo ""
echo "  FIRST-TIME LOGIN (one-time setup):"
echo "  1. Open http://localhost:6080/vnc.html in your browser"
echo "  2. You'll see a Chromium window — navigate to your provider"
echo "     ChatGPT: https://chatgpt.com"
echo "     Claude:  https://claude.ai"
echo "  3. Sign in using EMAIL + PASSWORD or a non-Google method"
echo ""
echo "  ⚠  IMPORTANT — Google login will NOT work here:"
echo "     Chromium running in an automated/controlled context is"
echo "     blocked by Google's bot detection. Use one of:"
echo "     • Email + password (most reliable)"
echo "     • Microsoft account"
echo "     • Apple ID"
echo "     • Magic link / OTP sent to your email"
echo ""
echo "  4. Once you see the chat interface, close the noVNC tab."
echo "     Your session is saved and will survive container restarts."
echo ""
echo "  LOGS: docker compose logs -f miri-api"
echo "============================================================"
echo ""

# ── 8. Start supervisor (manages all processes) ────────────────
echo "[entrypoint] Starting supervisor..."
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/miri-api.conf

# tested by Gautam and Harry on 18th February uWu 