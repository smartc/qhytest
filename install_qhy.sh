#!/bin/bash
#
# QHY5-II-M Camera Setup Script
# For Raspberry Pi 3/4 and other ARM Linux systems
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
        SDK_URL="https://github.com/qhyccd-lzr/QHYCCD_Linux_New/raw/master/qhyccdsdk-v2.0.11-Linux-Debian-Ubuntu-armv8.tar.gz"
        SDK_NAME="qhyccdsdk-v2.0.11-Linux-Debian-Ubuntu-armv8"
        ;;
    armv7l)
        SDK_URL="https://github.com/qhyccd-lzr/QHYCCD_Linux_New/raw/master/qhyccdsdk-v2.0.11-Linux-Debian-Ubuntu-armv8.tar.gz"
        SDK_NAME="qhyccdsdk-v2.0.11-Linux-Debian-Ubuntu-armv8"
        echo "Note: Using ARM v8 SDK for armv7l - should be compatible"
        ;;
    x86_64)
        SDK_URL="https://github.com/qhyccd-lzr/QHYCCD_Linux_New/raw/master/qhyccdsdk-v2.0.11-Linux-Debian-Ubuntu-x86_64.tar.gz"
        SDK_NAME="qhyccdsdk-v2.0.11-Linux-Debian-Ubuntu-x86_64"
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
apt-get install -y fxload curl python3 python3-pip python3-venv

# Download SDK
echo ""
echo "=== Downloading QHY SDK ==="
TMPDIR=$(mktemp -d)
cd "$TMPDIR"
curl -L "$SDK_URL" -o qhyccd_sdk.tar.gz
tar -xzf qhyccd_sdk.tar.gz

SDK_DIR="$TMPDIR/$SDK_NAME"

# Install SDK libraries
echo ""
echo "=== Installing SDK libraries ==="
cp -d "$SDK_DIR/lib/"* /usr/local/lib/

# Install firmware
echo ""
echo "=== Installing firmware ==="
mkdir -p /usr/local/lib/qhy/firmware
cp -p "$SDK_DIR/firmware/"* /usr/local/lib/qhy/firmware/

# Install udev rules
echo ""
echo "=== Installing udev rules ==="
cp "$SDK_DIR/udev/85-qhyccd.rules" /etc/udev/rules.d/

# Fix fxload path in udev rules (use system fxload)
sed -i 's|/sbin/fxload|/usr/sbin/fxload|g' /etc/udev/rules.d/85-qhyccd.rules

# Add permission rule for all QHY devices
echo ""
echo "=== Adding permission rules ==="
cat > /etc/udev/rules.d/99-qhyccd-permissions.rules << 'EOF'
# Grant access to all QHY cameras
SUBSYSTEM=="usb", ATTR{idVendor}=="1618", MODE="0666", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="16c0", MODE="0666", GROUP="plugdev"
EOF

# Update library cache
echo ""
echo "=== Updating library cache ==="
ldconfig

# Reload udev rules
echo ""
echo "=== Reloading udev rules ==="
udevadm control --reload-rules
udevadm trigger

# Cleanup
rm -rf "$TMPDIR"

# Verify installation
echo ""
echo "=== Verifying installation ==="
if [ -f /usr/local/lib/libqhyccd.so ]; then
    echo "✓ libqhyccd.so installed"
else
    echo "✗ libqhyccd.so NOT found"
fi

if [ -f /usr/local/lib/qhy/firmware/QHY5II.HEX ]; then
    echo "✓ QHY5II firmware installed"
else
    echo "✗ QHY5II firmware NOT found"
fi

if [ -f /etc/udev/rules.d/85-qhyccd.rules ]; then
    echo "✓ udev rules installed"
else
    echo "✗ udev rules NOT found"
fi

if which fxload > /dev/null 2>&1; then
    echo "✓ fxload available"
else
    echo "✗ fxload NOT found"
fi

echo ""
echo "=== Installation complete ==="
echo ""
echo "Next steps:"
echo "  1. Unplug and replug your QHY camera"
echo "  2. Run 'lsusb | grep 1618' - should show QHY5-II after firmware loads"
echo "  3. Set up Python environment:"
echo "     python3 -m venv venv"
echo "     source venv/bin/activate"
echo "     pip install numpy astropy pillow"
echo "  4. Run: python qhy_capture.py"
echo ""
