#!/bin/bash

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Function to display help message
show_help() {
    cat << EOF
Samsung Galaxy Book Control - Install Script

USAGE:
    sudo ./install.sh [OPTION]

OPTIONS:
    install      Install Samsung Galaxy Book Control (default)
    uninstall    Remove Samsung Galaxy Book Control
    help         Show this help message
    -h, --help   Show this help message

DESCRIPTION:
    This script installs or uninstalls the Samsung Galaxy Book Control application,
    which provides a modern GTK4 interface to control various laptop features and
    hardware settings for Samsung Galaxy Book laptops running Linux.

EXAMPLES:
    Install the application:
        sudo ./install.sh
        sudo ./install.sh install

    Uninstall the application:
        sudo ./install.sh uninstall

    Show this help message:
        sudo ./install.sh help
        sudo ./install.sh --help

REQUIREMENTS:
    - Must be run as root (use sudo)
    - Requires a Samsung Galaxy Book laptop
    - Requires the samsung-galaxybook kernel module
    - Arch-based Linux distribution (tested on EndeavourOS)

EOF
}

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "Please run as root"
    exit 1
fi

# Detect distro and install dependencies
install_deps() {
    if [ -r /etc/os-release ]; then
        . /etc/os-release
    fi

    case "${ID_LIKE:-$ID}" in
        *arch*|*manjaro*)
            pacman -S --needed python-gobject gtk4 libadwaita python-cairo polkit dbus xorg-xhost pciutils gnome-shell || true
            ;;
        *debian*|*ubuntu*|*linuxmint*|*pop*)
            apt-get update
            apt-get install -y python3-gi python3-gi-cairo gir1.2-gtk-4.0 libadwaita-1-0 \
                gobject-introspection python3-cairo polkitd dbus x11-xserver-utils pciutils gnome-shell \
                power-profiles-daemon || true
            ;;
        *fedora*|*rhel*|*centos*)
            dnf install -y python3-gobject gtk4 libadwaita python3-cairo polkit dbus xorg-x11-server-utils pciutils gnome-shell power-profiles-daemon || true
            ;;
        *)
            echo "Unsupported distribution. Please install GTK4, libadwaita, Python GI, Cairo, polkit, dbus, xhost, and pciutils manually."
            ;;
    esac
}

# Handle help option
if [ "$1" = "help" ] || [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    show_help
    exit 0
fi

# Support uninstall mode: ./install.sh uninstall
if [ "$1" = "uninstall" ]; then
    echo "Uninstalling Samsung Galaxy Book Control..."

    # Stop and disable permissions service if present
    if systemctl list-units --full -all | grep -Fq samsung-control-permissions.service; then
        systemctl disable --now samsung-control-permissions.service 2>/dev/null || true
    fi

    # Remove systemd service file
    if [ -f /etc/systemd/system/samsung-control-permissions.service ]; then
        rm -f /etc/systemd/system/samsung-control-permissions.service
        systemctl daemon-reload
    fi

    # Remove polkit policy
    rm -f /usr/share/polkit-1/actions/org.samsung.control.policy

    # Remove udev rules
    rm -f /etc/udev/rules.d/99-samsung-galaxybook-gui.rules
    udevadm control --reload-rules 2>/dev/null || true
    udevadm trigger 2>/dev/null || true

    # Remove installed binaries and wrapper
    rm -f /usr/local/bin/samsung-control
    rm -f /usr/local/bin/samsung-control-wrapper
    rm -f /usr/local/bin/samsung-control-permissions

    # Remove desktop entry and icons
    rm -f /usr/share/applications/org.samsung.control.desktop
    rm -f /usr/share/icons/hicolor/scalable/apps/samsung-control.svg
    rm -f /usr/share/icons/hicolor/scalable/apps/samsung-battery.svg
    rm -f /usr/share/icons/hicolor/scalable/apps/samsung-settings.svg
    rm -f /usr/share/icons/hicolor/scalable/apps/samsung-graph.svg
    # PNG icons
    rm -f /usr/share/icons/hicolor/48x48/apps/samsung-battery.png
    rm -f /usr/share/icons/hicolor/48x48/apps/samsung-settings.png
    rm -f /usr/share/icons/hicolor/48x48/apps/samsung-graph.png
    rm -f /usr/share/icons/hicolor/48x48/apps/samsung-about.png
    gtk-update-icon-cache -f /usr/share/icons/hicolor 2>/dev/null || true

    # Remove installer user from 'samsung' group if present
    if [ -n "$SUDO_USER" ]; then
        gpasswd -d "$SUDO_USER" samsung 2>/dev/null || true
    fi

    # Remove group if empty
    if getent group samsung >/dev/null 2>&1; then
        members=$(getent group samsung | awk -F: '{print $4}')
        if [ -z "$members" ]; then
            groupdel samsung 2>/dev/null || true
        fi
    fi

    echo "Uninstallation complete. You may want to log out or reboot to fully remove system integrations."
    exit 0
fi

# Install dependencies
install_deps

# Create a wrapper script (kept for backwards compatibility with older .desktop files).
#
# Important: do NOT run the GUI as root. The installer already configures a `samsung`
# group + udev rules so the app can access the relevant sysfs nodes as an unprivileged
# user. Running the GUI via pkexec makes the app identify as `root` and breaks theme/
# avatar detection.
cat > /usr/local/bin/samsung-control-wrapper << 'EOL'
#!/bin/bash
exec /usr/local/bin/samsung-control "$@"
EOL

chmod 0755 /usr/local/bin/samsung-control-wrapper

# Copy program (source files are now under src/ when script is in project root)
install -Dm755 "$SCRIPT_DIR/src/samsung-control.py" /usr/local/bin/samsung-control
install -d /usr/local/bin/samsung_control
cp -r "$SCRIPT_DIR/src/samsung_control/"* /usr/local/bin/samsung_control/

# Install icons (now stored under assets/ at repository root)
ICON_DIR="$SCRIPT_DIR/assets/icons"
# Only the app icon is shipped as SVG in this fork.
install -Dm644 "$ICON_DIR/samsung-control.svg" /usr/share/icons/hicolor/scalable/apps/samsung-control.svg
# PNG versions used by the sidebar buttons (48×48)
install -Dm644 "$ICON_DIR/charging.png" /usr/share/icons/hicolor/48x48/apps/samsung-battery.png
install -Dm644 "$ICON_DIR/creative.png" /usr/share/icons/hicolor/48x48/apps/samsung-settings.png
install -Dm644 "$ICON_DIR/graphic.png" /usr/share/icons/hicolor/48x48/apps/samsung-graph.png
install -Dm644 "$ICON_DIR/information.png" /usr/share/icons/hicolor/48x48/apps/samsung-about.png

# Copy desktop entry with updated icon name
cat > /usr/share/applications/org.samsung.control.desktop << EOL
[Desktop Entry]
Name=Samsung Galaxy Book Control
Comment=Control Samsung Galaxy Book features
Exec=samsung-control
Icon=samsung-control
Terminal=false
StartupWMClass=org.samsung.control
Type=Application
Categories=Settings;HardwareSettings;
Keywords=samsung;galaxybook;laptop;control;
EOL

# NOTE: We intentionally do not install a polkit action to run the whole GUI as root.
# Access is managed via group permissions + udev rules further below.
rm -f /usr/share/polkit-1/actions/org.samsung.control.policy 2>/dev/null || true

# Create a dedicated group for device access and add installer user to it
groupadd -f samsung >/dev/null 2>&1
if [ -n "$SUDO_USER" ]; then
    usermod -aG samsung "$SUDO_USER" || true
fi

# Set permissions for device access via udev rules (assign to group 'samsung' with 0660)
cat > /etc/udev/rules.d/99-samsung-galaxybook-gui.rules << EOL
# Samsung Galaxy Book device files - assign to group 'samsung'
SUBSYSTEM=="platform", DRIVER=="samsung-galaxybook", GROUP="samsung", MODE="0660"

# Samsung firmware attributes (power_on_lid_open, block_recording, etc.)
SUBSYSTEM=="firmware-attributes", KERNEL=="samsung-galaxybook", GROUP="samsung", MODE="0660"

# Keyboard backlight (sysfs class)
SUBSYSTEM=="leds", KERNEL=="samsung-galaxybook::kbd_backlight", GROUP="samsung", MODE="0660"

# hwmon (fan sensors)
SUBSYSTEM=="hwmon", KERNEL=="hwmon*", GROUP="samsung", MODE="0660"

# Ensure power supply threshold file is owned by group and writable by group when battery is added/changed
ACTION=="add|change", SUBSYSTEM=="power_supply", KERNEL=="BAT*", RUN+="/bin/chgrp samsung /sys/class/power_supply/%k/charge_control_end_threshold || true"
ACTION=="add|change", SUBSYSTEM=="power_supply", KERNEL=="BAT*", RUN+="/bin/chmod 0660 /sys/class/power_supply/%k/charge_control_end_threshold || true"

# Ensure platform_profile sysfs file is group-owned and writable when firmware node is added
ACTION=="add|change", SUBSYSTEM=="firmware", KERNEL=="acpi", RUN+="/bin/chgrp samsung /sys/firmware/acpi/platform_profile || true"
ACTION=="add|change", SUBSYSTEM=="firmware", KERNEL=="acpi", RUN+="/bin/chmod 0660 /sys/firmware/acpi/platform_profile || true"
EOL

# Reload udev and trigger to apply rules immediately
udevadm control --reload-rules
udevadm trigger

# Install a best-effort permissions fixer for sysfs nodes.
#
# Rationale: Some /sys nodes (notably firmware-attributes and platform_profile) are not
# reliably chmod/chgrp'able via udev rules across distros/kernels. A oneshot service
# makes the behavior consistent after boot and after installs/updates.
cat > /usr/local/bin/samsung-control-permissions << 'EOL'
#!/bin/bash
set -euo pipefail

apply_one() {
  local p="$1"
  [ -e "$p" ] || return 0
  chgrp samsung "$p" 2>/dev/null || true
  chmod 0660 "$p" 2>/dev/null || true
}

apply_glob() {
  local g="$1"
  shopt -s nullglob
  local paths=( $g )
  shopt -u nullglob
  local p
  for p in "${paths[@]}"; do
    apply_one "$p"
  done
}

apply_one /sys/firmware/acpi/platform_profile
apply_glob "/sys/class/power_supply/BAT*/charge_control_end_threshold"
apply_glob "/sys/class/firmware-attributes/samsung-galaxybook/attributes/*/current_value"

# Keyboard backlight sysfs nodes.
apply_glob "/sys/class/leds/samsung-galaxybook::kbd_backlight/*brightness*"

# Device nodes (if present).
if [ -d /dev/samsung-galaxybook ]; then
  chgrp -R samsung /dev/samsung-galaxybook 2>/dev/null || true
  chmod -R 0660 /dev/samsung-galaxybook 2>/dev/null || true
fi
EOL
chmod 0755 /usr/local/bin/samsung-control-permissions

cat > /etc/systemd/system/samsung-control-permissions.service << 'EOL'
[Unit]
Description=Samsung Control - Apply sysfs/device permissions
After=systemd-udevd.service
Wants=systemd-udevd.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/samsung-control-permissions

[Install]
WantedBy=multi-user.target
EOL

systemctl daemon-reload 2>/dev/null || true
systemctl enable --now samsung-control-permissions.service 2>/dev/null || true
/usr/local/bin/samsung-control-permissions 2>/dev/null || true

# Apply initial permissions to existing files (best-effort)
chgrp samsung /sys/firmware/acpi/platform_profile 2>/dev/null || true
chmod 0660 /sys/firmware/acpi/platform_profile 2>/dev/null || true
for f in /sys/class/power_supply/BAT*/charge_control_end_threshold; do
    if [ -e "$f" ]; then
        chgrp samsung "$f" 2>/dev/null || true
        chmod 0660 "$f" 2>/dev/null || true
    fi
done
for d in /sys/class/leds/samsung-galaxybook::kbd_backlight; do
    if [ -d "$d" ]; then
        chgrp -R samsung "$d" 2>/dev/null || true
        chmod -R 0660 "$d" 2>/dev/null || true
    fi
done
for f in /sys/class/firmware-attributes/samsung-galaxybook/attributes/*/current_value; do
    if [ -e "$f" ]; then
        chgrp samsung "$f" 2>/dev/null || true
        chmod 0660 "$f" 2>/dev/null || true
    fi
done

# Reload udev rules
udevadm control --reload-rules
udevadm trigger

# Update icon cache
gtk-update-icon-cache -f /usr/share/icons/hicolor 2>/dev/null || true

# Ensure desktop database is updated
update-desktop-database /usr/share/applications 2>/dev/null || true

echo "Installation complete!"
echo "You may need to log out and back in for the application to appear in your menu."
echo "Log out and back in to apply group permissions for firmware controls."
