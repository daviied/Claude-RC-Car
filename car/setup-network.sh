#!/bin/bash
# Lets the car pick its network: Wi-Fi first, phone tethering as backup,
# and Wi-Fi networks chosen from the web UI.
# - Wi-Fi (wlan0) is run by NetworkManager. Wired eth0 (phone Ethernet adapter)
#   stays as it is. This script does not change how eth0 works.
# - USB tethering (phone on a USB cable = usb*) gets DHCP.
# - Lets the car program (service user) scan, connect and set Wi-Fi route
#   priority without sudo.
# Run once, from this folder:  sudo bash setup-network.sh
set -e
cd "$(dirname "$0")"
USER_NAME="${SUDO_USER:-$(whoami)}"
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }

if ! command -v nmcli >/dev/null; then
    apt-get -o DPkg::Lock::Timeout=600 update
    apt-get -o DPkg::Lock::Timeout=600 install -y network-manager
fi
systemctl enable --now NetworkManager

# The car program may manage networks (scan, connect, route metrics)
cat > /etc/polkit-1/rules.d/50-claude-rc-car.rules <<EOF
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.NetworkManager.") == 0 && subject.user == "${USER_NAME}")
        return polkit.Result.YES;
});
EOF

# USB tethering: DHCP on usb* (eth0 is not touched)
cat > /etc/netplan/60-claude-rc-car-usb.yaml <<'EOF'
network:
  version: 2
  ethernets:
    phone-usb:
      match: {name: "usb*"}
      dhcp4: true
      optional: true
EOF
chmod 600 /etc/netplan/60-claude-rc-car-usb.yaml
netplan generate
networkctl reload 2>/dev/null || true

systemctl restart claude-rc-car || true
sleep 2
echo
sudo -u "$USER_NAME" nmcli general permissions | grep -E "network-control|wifi.scan|modify.system"
echo "Done. Pick Wi-Fi networks on the drive page (Car network panel)."
