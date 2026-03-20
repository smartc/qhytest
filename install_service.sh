#!/bin/bash
# install_service.sh — Install qhy_web as a systemd service.
#
# Usage:
#   sudo ./install_service.sh              # auto-detect calling user
#   sudo ./install_service.sh --user pi    # specify user explicitly
#   sudo ./install_service.sh --port 8080  # change port (default 5000)

set -e

# ---------------------------------------------------------------------------
# Must run as root
# ---------------------------------------------------------------------------
if [ "$EUID" -ne 0 ]; then
    echo "Error: run as root:  sudo ./install_service.sh"
    exit 1
fi

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
SERVICE_USER="${SUDO_USER:-pi}"
PORT=5000

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user=*)  SERVICE_USER="${1#--user=}";  shift ;;
        --user)    SERVICE_USER="$2";            shift 2 ;;
        --port=*)  PORT="${1#--port=}";          shift ;;
        --port)    PORT="$2";                    shift 2 ;;
        *)         echo "Unknown argument: $1";  shift ;;
    esac
done

# Validate user exists
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "Error: user '$SERVICE_USER' not found."
    exit 1
fi

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python"
SERVICE_FILE="/etc/systemd/system/qhy_web.service"

echo "=== QHY Web Service Installer ==="
echo "  User:    $SERVICE_USER"
echo "  Dir:     $SCRIPT_DIR"
echo "  Python:  $VENV_PYTHON"
echo "  Port:    $PORT"
echo ""

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

# venv must exist
if [ ! -f "$VENV_PYTHON" ]; then
    echo "Error: virtualenv not found at $SCRIPT_DIR/venv"
    echo ""
    echo "Create it first:"
    echo "  python3 -m venv venv"
    echo "  source venv/bin/activate"
    echo "  pip install flask numpy astropy pillow"
    exit 1
fi

# Required Python packages
echo "Checking Python dependencies..."
missing=()
declare -A PKG_IMPORT=(["flask"]="flask" ["numpy"]="numpy" ["pillow"]="PIL")
for pkg in "${!PKG_IMPORT[@]}"; do
    if ! "$VENV_PYTHON" -c "import ${PKG_IMPORT[$pkg]}" 2>/dev/null; then
        missing+=("$pkg")
    fi
done
if [ ${#missing[@]} -gt 0 ]; then
    echo "Error: missing packages in venv: ${missing[*]}"
    echo "Install with:  source venv/bin/activate && pip install ${missing[*]}"
    exit 1
fi
echo "  OK"

# plugdev group (USB camera access via udev rules installed by install_qhy.sh)
if ! groups "$SERVICE_USER" | grep -qw plugdev; then
    echo "Adding $SERVICE_USER to plugdev group..."
    usermod -aG plugdev "$SERVICE_USER"
    echo "  Done (takes effect on next login / reboot)"
fi

# ---------------------------------------------------------------------------
# Warn if service already exists
# ---------------------------------------------------------------------------
if [ -f "$SERVICE_FILE" ]; then
    echo ""
    echo "Warning: $SERVICE_FILE already exists."
    read -r -p "Overwrite and reinstall? [y/N] " confirm
    [[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
    systemctl stop qhy_web 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# Write the service file
# ---------------------------------------------------------------------------
cat > "$SERVICE_FILE" << EOF
[Unit]
Description=QHY5 Live Preview Web Server
Documentation=https://github.com/smartc/qhytest
# Wait for USB subsystem so the camera is enumerable at start-up
After=network.target dev-bus-usb.mount
Wants=dev-bus-usb.mount

[Service]
Type=simple
User=$SERVICE_USER
Group=plugdev
WorkingDirectory=$SCRIPT_DIR

# Flask app; adjust --port if needed
ExecStart=$VENV_PYTHON $SCRIPT_DIR/qhy_web.py --port $PORT

# Automatically restart if the process crashes
Restart=on-failure
RestartSec=5

# Give the camera firmware-load re-enumeration time on first start
TimeoutStartSec=30

# Log goes to journald; view with:  journalctl -u qhy_web -f
StandardOutput=journal
StandardError=journal
SyslogIdentifier=qhy_web

[Install]
WantedBy=multi-user.target
EOF

echo ""
echo "Service file written to $SERVICE_FILE"

# ---------------------------------------------------------------------------
# Enable (start at boot) but do NOT start now — camera may not be plugged in
# ---------------------------------------------------------------------------
systemctl daemon-reload
systemctl enable qhy_web

echo ""
echo "=== Done ==="
echo ""
echo "The service will start automatically on next boot."
echo ""
echo "Useful commands:"
echo "  sudo systemctl start   qhy_web     # start now"
echo "  sudo systemctl stop    qhy_web     # stop"
echo "  sudo systemctl restart qhy_web     # restart"
echo "  sudo systemctl status  qhy_web     # check status"
echo "  journalctl -u qhy_web -f           # live log"
echo ""
echo "Access the web interface at:  http://$(hostname -I | awk '{print $1}'):$PORT"
