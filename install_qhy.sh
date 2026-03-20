#!/bin/bash
#
# QHY5-II-M Camera Setup Script
# For Raspberry Pi 3/4/5 and other ARM64/x86_64 Linux systems
#
# Usage: sudo ./install_qhy.sh
#

set -e

echo "=== QHY5-II-M Camera Setup Script ==="
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Error: Please run as root (sudo ./install_qhy.sh)"
    exit 1
fi

# Detect architecture
ARCH=$(uname -m)
echo "Detected architecture: $ARCH"

case $ARCH in
    aarch64)
        # Official QHY ARM64 SDK (tested on Raspberry Pi 3/4/5, Debian Trixie)
        SDK_URL="https://www.qhyccd.com/file/repository/publish/SDK/240801/sdk_Arm64_24.08.01.tgz"
        SDK_NAME="sdk_Arm64_24.08.01"
        SDK_STYLE="new"
        ;;
    x86_64)
        # NOTE: x86_64 SDK URL may need updating - check https://www.qhyccd.com/download/
        SDK_URL="https://www.qhyccd.com/file/repository/publish/SDK/240801/sdk_Linux64_24.08.01.tgz"
        SDK_NAME="sdk_Linux64_24.08.01"
        SDK_STYLE="new"
        ;;
    armv7l)
        echo "Error: 32-bit ARM (armv7l) is not supported."
        echo "Please use a 64-bit OS (Raspberry Pi OS 64-bit) on a Pi3 or newer."
        exit 1
        ;;
    *)
        echo "Error: Unsupported architecture: $ARCH"
        exit 1
        ;;
esac

# Install system dependencies
echo ""
echo "=== Installing system dependencies ==="
apt-get update
apt-get install -y fxload curl python3 python3-pip python3-venv libusb-1.0-0

# Download SDK
echo ""
echo "=== Downloading QHY SDK ==="
TMPDIR=$(mktemp -d)
cd "$TMPDIR"
curl -L "$SDK_URL" -o qhyccd_sdk.tgz
tar -xzf qhyccd_sdk.tgz

SDK_DIR="$TMPDIR/$SDK_NAME"

# Install using the SDK's own install script (handles lib + firmware + udev correctly)
echo ""
echo "=== Installing SDK (libs, firmware, udev rules) ==="
cd "$SDK_DIR"
bash install.sh

# Ensure /usr/local/lib is in the linker search path
echo ""
echo "=== Updating library cache ==="
if ! grep -q "/usr/local/lib" /etc/ld.so.conf /etc/ld.so.conf.d/* 2>/dev/null; then
    echo "/usr/local/lib" > /etc/ld.so.conf.d/qhyccd.conf
fi
ldconfig

# Fix fxload path in udev rules if needed (some SDKs reference /sbin, Debian uses /usr/sbin)
if grep -q "/sbin/fxload" /etc/udev/rules.d/85-qhyccd.rules 2>/dev/null; then
    echo ""
    echo "=== Fixing fxload path in udev rules ==="
    sed -i 's|/sbin/fxload|/usr/sbin/fxload|g' /etc/udev/rules.d/85-qhyccd.rules
fi

# Add broad permission rule for all QHY devices
echo ""
echo "=== Adding permission rules ==="
cat > /etc/udev/rules.d/99-qhyccd-permissions.rules << 'EOF'
# Grant access to all QHY cameras
SUBSYSTEM=="usb", ATTR{idVendor}=="1618", MODE="0666", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="16c0", MODE="0666", GROUP="plugdev"
EOF

# Reload udev rules
echo ""
echo "=== Reloading udev rules ==="
udevadm control --reload-rules
udevadm trigger

# Cleanup
cd /
rm -rf "$TMPDIR"

# Verify installation
echo ""
echo "=== Verifying installation ==="

if ldconfig -p | grep -q "libqhyccd.so.*AArch64\|libqhyccd.so.*x86-64"; then
    echo "✓ libqhyccd.so installed (correct 64-bit architecture)"
elif [ -f /usr/local/lib/libqhyccd.so ]; then
    echo "⚠ libqhyccd.so installed (verify architecture with: file /usr/local/lib/libqhyccd.so)"
else
    echo "✗ libqhyccd.so NOT found"
fi

# Firmware may be at /lib/firmware/qhy/ (new SDK) or /usr/local/lib/qhy/firmware/ (old SDK)
if [ -f /lib/firmware/qhy/QHY5II.HEX ]; then
    echo "✓ QHY5II firmware installed (/lib/firmware/qhy/)"
elif [ -f /usr/local/lib/qhy/firmware/QHY5II.HEX ]; then
    echo "✓ QHY5II firmware installed (/usr/local/lib/qhy/firmware/)"
else
    echo "✗ QHY5II firmware NOT found"
fi

if [ -f /etc/udev/rules.d/85-qhyccd.rules ]; then
    echo "✓ udev rules installed"
else
    echo "✗ udev rules NOT found"
fi

if which fxload > /dev/null 2>&1; then
    echo "✓ fxload available ($(which fxload))"
else
    echo "✗ fxload NOT found"
fi

echo ""
echo "=== Installation complete ==="
echo ""
echo "Next steps:"
echo "  1. Unplug and replug your QHY camera"
echo "  2. Wait ~3 seconds for firmware to load, then:"
echo "     lsusb | grep 1618   # should show 1618:0921 (QHY5-II)"
echo "  3. Set up Python environment:"
echo "     python3 -m venv venv"
echo "     source venv/bin/activate"
echo "     pip install numpy astropy pillow"
echo "  4. Run: python qhy_benchmark.py"
echo ""