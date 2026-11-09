#!/bin/bash
# Lets the car pick its network: Wi-Fi first, phone tethering as backup,
# and Wi-Fi networks chosen from the web UI.
# - Installs NetworkManager and makes netplan use it (keeps your current Wi-Fi).
# - Phone tethering (Ethernet adapter = eth*/enx*, USB cable = usb*) gets DHCP.
# - Lets the car program (service user) change networks without sudo.
# Run once, from this folder:  sudo bash setup-network.sh
# Do this where you can reach the Pi another way (keyboard/screen), in case the network drops.
set -e
cd "$(dirname "$0")"
USER_NAME="${SUDO_USER:-$(whoami)}"
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }

APT="apt-get -o DPkg::Lock::Timeout=600"
$APT update
$APT install -y network-manager iw

# 00- so it is read first: every interface uses NetworkManager
cat > /etc/netplan/00-claude-rc-car.yaml <<'EOF'
network:
  version: 2
  renderer: NetworkManager
  ethernets:
    phone-eth:
      match: {name: "e*"}
      dhcp4: true
      optional: true
    phone-usb:
      match: {name: "usb*"}
      dhcp4: true
      optional: true
EOF
chmod 600 /etc/netplan/00-claude-rc-car.yaml

# The car program may manage networks (scan, connect, route metrics)
cat > /etc/polkit-1/rules.d/50-claude-rc-car.rules <<EOF
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.NetworkManager.") == 0 && subject.user == "${USER_NAME}")
        return polkit.Result.YES;
});
EOF

echo "Current netplan files:"
ls -1 /etc/netplan/
read -r -p "Apply now? The network restarts and SSH can drop for a moment. [y/N] " ok
if [ "$ok" = y ] || [ "$ok" = Y ]; then
    systemctl enable --now NetworkManager
    netplan generate
    netplan apply
    sleep 5
    nmcli device status
    systemctl restart claude-rc-car || true
    echo "Done. Pick Wi-Fi networks on the drive page (Car Wi-Fi)."
else
    echo "Not applied. Run later: sudo netplan apply && sudo systemctl restart claude-rc-car"
fi
