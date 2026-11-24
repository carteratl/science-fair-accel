#!/bin/bash
# One-time Raspberry Pi setup for accel-setup / accel-record.
# Run from your normal user account:   sudo bash ~/accel/install.sh
# Safe to run again. Needs internet (do it at home, before the fair).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo:   sudo bash $0"
    exit 1
fi
USER_NAME="${SUDO_USER:-}"
if [ -z "$USER_NAME" ] || [ "$USER_NAME" = "root" ]; then
    echo "Run this from your normal user account with sudo (not as root):   sudo bash $0"
    exit 1
fi
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
USER_GROUP="$(id -gn "$USER_NAME")"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$USER_HOME/accel-data"

echo "== 1/6  Installing packages (python3-smbus2 i2c-tools tmux)"
apt-get update
apt-get install -y python3-smbus2 i2c-tools tmux
if ! python3 -c "import smbus2" 2>/dev/null; then
    echo "ERROR: python3 can't import smbus2 after installing python3-smbus2."
    exit 1
fi

echo "== 2/6  Turning on I2C"
if command -v raspi-config >/dev/null 2>&1; then
    raspi-config nonint do_i2c 0 || echo "   (raspi-config could not enable I2C; doing it by hand below)"
fi
# /dev/i2c-N only appear when the i2c-dev module is loaded: make sure it loads at every boot
echo "i2c-dev" > /etc/modules-load.d/accel-i2c.conf
modprobe i2c-dev 2>/dev/null || true

echo "== 3/6  Adding the I2C lines to config.txt"
CONFIG=/boot/firmware/config.txt
[ -f "$CONFIG" ] || CONFIG=/boot/config.txt
if [ ! -f "$CONFIG" ]; then
    echo "ERROR: can't find /boot/firmware/config.txt or /boot/config.txt"
    exit 1
fi
STAMP_DIR=/var/lib/accel-logger
STAMP="$STAMP_DIR/changed-at"     # when this script last changed something that needs a reboot
mkdir -p "$STAMP_DIR"
MARK_BEGIN="# --- accel logger (added by accel/install.sh) ---"
MARK_END="# --- end accel logger ---"
REBOOT_NEEDED=0
if grep -qF "$MARK_BEGIN" "$CONFIG"; then
    echo "   already there ($CONFIG)"
else
    cp "$CONFIG" "$CONFIG.accel-backup-$(date +%Y%m%d-%H%M%S)"
    {
        echo ""
        echo "$MARK_BEGIN"
        echo "[all]"
        echo "dtparam=i2c_arm=on"
        echo "dtparam=i2c_arm_baudrate=400000"
        echo "dtoverlay=i2c-gpio,bus=3,i2c_gpio_sda=23,i2c_gpio_scl=24,i2c_gpio_delay_us=2"
        echo "$MARK_END"
    } >> "$CONFIG"
    echo "   added to $CONFIG (backup saved next to it)"
    REBOOT_NEEDED=1
    date +%s > "$STAMP"
fi

echo "== 4/6  Checking for conflicting settings"
# every dtoverlay/dtparam line that is NOT inside our marked block
OTHERS="$(awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
    $0 == b { skip = 1 }
    !skip && /^[ \t]*dt(overlay|param)=/ { print }
    $0 == e { skip = 0 }' "$CONFIG")"
warn=0
while IFS= read -r line; do
    [ -z "$line" ] && continue
    case "$line" in
        *i2c-sensor*mpu6050*|*overlay=mpu6050*)
            echo "   WARNING: '$line' loads the kernel MPU-6050 driver, which takes the sensor. Remove it."; warn=1 ;;
        *i2c-rtc*)
            echo "   WARNING: '$line' (a clock chip) uses address 0x68 on bus 1, like sensor A."; warn=1 ;;
        *i2c-gpio*)
            echo "   WARNING: another i2c-gpio line: '$line'. Make sure it doesn't use bus 3 or GPIO 23/24."; warn=1 ;;
        *overlay=i2c3*)
            echo "   WARNING: '$line' also creates /dev/i2c-3 and will clash with the bus-3 line."; warn=1 ;;
        *i2c_arm_baudrate*)
            echo "   NOTE: '$line' sets the bus-1 speed too; the last one in the file wins."; warn=1 ;;
    esac
done <<< "$OTHERS"
[ $warn -eq 0 ] && echo "   none found"
if [ -e /proc/device-tree/hat/product ]; then
    echo "   NOTE: a HAT is attached ($(tr -d '\0' < /proc/device-tree/hat/product)). Check it doesn't use GPIO 23/24."
fi

echo "== 5/6  Giving $USER_NAME access to I2C"
if id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx i2c; then
    echo "   already in the i2c group"
else
    usermod -aG i2c "$USER_NAME"
    echo "   added $USER_NAME to the i2c group (takes effect after logging in again / reboot)"
    REBOOT_NEEDED=1
    date +%s > "$STAMP"
fi

echo "== 6/6  Data folder and commands"
mkdir -p "$DATA_DIR/details"
chown -R "$USER_NAME:$USER_GROUP" "$DATA_DIR"
chmod +x "$SRC_DIR/accel_setup.py" "$SRC_DIR/accel_record.py"
ln -sf "$SRC_DIR/accel_setup.py" /usr/local/bin/accel-setup
ln -sf "$SRC_DIR/accel_record.py" /usr/local/bin/accel-record
echo "   data folder: $DATA_DIR"
echo "   commands:    accel-setup, accel-record"

# a reboot may still be pending from an earlier run of this script - or the reboot happened
# and a bus still didn't appear, which another reboot won't fix
BOOTED_AT="$(awk '/^btime/ {print $2}' /proc/stat 2>/dev/null || true)"
CHANGED_AT="$(cat "$STAMP" 2>/dev/null || true)"
if [ ! -e /dev/i2c-1 ] || [ ! -e /dev/i2c-3 ]; then
    if [ -n "$CHANGED_AT" ] && [ -n "$BOOTED_AT" ] && [ "$CHANGED_AT" -gt "$BOOTED_AT" ]; then
        REBOOT_NEEDED=1
    elif [ $REBOOT_NEEDED -eq 0 ]; then
        echo ""
        echo "PROBLEM: the Pi has rebooted since setup, but these are still missing:"
        [ -e /dev/i2c-1 ] || echo "   /dev/i2c-1  (I2C bus for Base)"
        [ -e /dev/i2c-3 ] || echo "   /dev/i2c-3  (second bus for Top)"
        echo "Check the WARNING lines above (another overlay or HAT may be using GPIO 23/24),"
        echo "and look at:   dmesg | grep -i i2c"
        exit 1
    fi
fi

echo ""
echo "Done."
if [ $REBOOT_NEEDED -eq 1 ]; then
    echo "A reboot is needed. After it, check:  ls /dev/i2c-*   (should list i2c-1 and i2c-3)"
    read -r -p "Reboot now? [y/N] " ans
    case "$ans" in
        y|Y|yes|YES) reboot ;;
        *) echo "OK - reboot later with:  sudo reboot" ;;
    esac
else
    echo "Next: run  accel-setup"
fi
