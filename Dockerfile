# ── Stage 1: Build ──────────────────────────────────────────────
FROM python:3.9-slim AS base

# Prevent interactive prompts during apt-get
ENV DEBIAN_FRONTEND=noninteractive

# ── System dependencies ─────────────────────────────────────────
# Xvfb: virtual framebuffer (fake display for headed Chrome)
# x11vnc: VNC server to capture Xvfb display
# noVNC + websockify: browser-based VNC client
# Chrome deps: fonts, media, GL, sandbox support
# supervisor: process manager
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Virtual display
    xvfb \
    # VNC
    x11vnc \
    # noVNC (browser-based VNC)
    novnc websockify \
    # Process manager
    supervisor \
    # Chrome runtime dependencies
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libwayland-client0 \
    # Fonts (so pages render properly)
    fonts-liberation \
    fonts-noto-color-emoji \
    fonts-dejavu-core \
    # Utilities
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

# ── Application setup ───────────────────────────────────────────
WORKDIR /app

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install patchright's Chromium browser
RUN patchright install chromium && patchright install-deps chromium

# Copy application code
COPY src/ src/
COPY scripts/ scripts/

# NOTE: .env is deliberately NOT baked into the image. Config is supplied
# by docker-compose (env_file: .env + environment:). Copying .env.example
# to .env here would silently supply stale defaults (e.g. API_TOKEN=dummy123)
# for any var compose stopped setting, instead of failing loudly.

# ── Directory setup ─────────────────────────────────────────────
# These will be overridden by volume mounts in docker-compose
RUN mkdir -p /app/browser_data /app/logs /app/downloads/images

# ── Supervisor & entrypoint ─────────────────────────────────────
COPY docker/supervisord.conf /etc/supervisor/conf.d/miri-api.conf
COPY docker/entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

# ── Inject the account-control panel into noVNC's page ──────────
# The panel is a self-contained overlay that calls the admin API on :8000.
# We append one <script> to the stock vnc.html rather than replacing it, so a
# noVNC update keeps working (worst case the panel just doesn't load).
COPY docker/novnc_panel.js /usr/share/novnc/miri_panel.js
COPY docker/novnc_server.py /novnc_server.py
RUN VNC_HTML=$(ls /usr/share/novnc/vnc.html 2>/dev/null || true); \
    if [ -n "$VNC_HTML" ] && ! grep -q "miri_panel.js" "$VNC_HTML"; then \
        sed -i 's#</body>#<script src="miri_panel.js"></script></body>#' "$VNC_HTML"; \
        echo "Injected miri panel into $VNC_HTML"; \
    else echo "vnc.html not found or already injected"; fi

# ── Environment ─────────────────────────────────────────────────
# Virtual display
ENV DISPLAY=:99
ENV DISPLAY_WIDTH=1280
ENV DISPLAY_HEIGHT=720
ENV DISPLAY_DEPTH=24

# App config (overridable via docker-compose)
ENV HEADLESS=false
ENV BROWSER_DATA_DIR=/app/browser_data
ENV LOG_DIR=/app/logs
ENV API_HOST=0.0.0.0
ENV API_PORT=8000
ENV LOG_LEVEL=DEBUG
ENV VERBOSE=true

# ── Ports ───────────────────────────────────────────────────────
# 8000: FastAPI server
# 6080: noVNC web UI
EXPOSE 8000 6080

# ── Health check ────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

ENTRYPOINT ["/entrypoint.sh"]
