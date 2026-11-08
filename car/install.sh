#!/bin/bash
# Installs the car program on Ubuntu (Pi 4) and starts it at boot.
# Run from this folder:  sudo ./install.sh
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
USER_NAME="${SUDO_USER:-$(whoami)}"

# Wait up to 10 min if automatic updates hold the apt lock
APT="apt-get -o DPkg::Lock::Timeout=600"
$APT update
$APT install -y python3-aiortc python3-socketio python3-aiohttp \
    python3-picamera2 python3-libcamera libcamera-tools rpicam-apps v4l-utils

[ -f .env ] || { cp .env.example .env; echo "Created .env - edit it, then run: sudo systemctl restart claude-rc-car"; }

# Install the service with this folder and user
sed -e "s#^User=.*#User=${USER_NAME}#" \
    -e "s#^WorkingDirectory=.*#WorkingDirectory=${DIR}#" \
    -e "s#^EnvironmentFile=.*#EnvironmentFile=${DIR}/.env#" \
    claude-rc-car.service > /etc/systemd/system/claude-rc-car.service

usermod -aG video,gpio "$USER_NAME" 2>/dev/null || true

# Hardware PWM on GPIO 12 + 13 (ESC + steering servo)
REBOOT_NEEDED=no
CFG=/boot/firmware/config.txt
if ! grep -q "^dtoverlay=pwm-2chan" "$CFG"; then
    printf '\n[all]\n# Claude-RC-Car: hardware PWM on GPIO 12 and 13\ndtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4\n# Analog audio uses the same PWM block\ndtparam=audio=off\n' >> "$CFG"
    REBOOT_NEEDED=yes
fi

# Let the gpio group use /sys/class/pwm (no root needed for the car program)
getent group gpio >/dev/null || groupadd -r gpio
usermod -aG gpio "$USER_NAME"
cat > /etc/udev/rules.d/99-claude-rc-car-pwm.rules <<'EOF'
SUBSYSTEM=="pwm*", PROGRAM="/bin/sh -c 'chgrp -R gpio /sys/class/pwm/ /sys%p 2>/dev/null; chmod -R g+rwX /sys/class/pwm/ /sys%p 2>/dev/null; true'"
EOF
udevadm control --reload-rules && udevadm trigger --subsystem-match=pwm || true
systemctl daemon-reload
systemctl enable claude-rc-car
systemctl restart claude-rc-car
sleep 3
systemctl status claude-rc-car --no-pager -l | head -20
echo
echo "Done. Logs: journalctl -u claude-rc-car -f"
[ "$REBOOT_NEEDED" = yes ] && echo "PWM was just turned on: run 'sudo reboot' to activate it."
true
