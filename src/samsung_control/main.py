#!/usr/bin/env python3
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib

from .logging_utils import setup_logging

# Ensure the WM_CLASS/app id match the desktop entry for dock integration.
GLib.set_prgname("org.samsung.control")
GLib.set_application_name("Samsung Galaxy Book Control")

# Initialize Adwaita before anything else
Adw.init()

setup_logging()


def main():
    from .app import SamsungControl

    app = SamsungControl()
    return app.run(None)


# Support a user-local install flow so the app can register its .desktop and icon

def install_for_user():
    try:
        from pathlib import Path
        import shutil
        import subprocess
        import sys

        script_path = Path(__file__).resolve()
        pkg_root = script_path.parent.parent
        entrypoint = pkg_root / "samsung-control.py"

        icons_dir = Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps"
        apps_dir = Path.home() / ".local" / "share" / "applications"
        icons_dir.mkdir(parents=True, exist_ok=True)
        apps_dir.mkdir(parents=True, exist_ok=True)

        # source icon is now under the repo assets directory rather than inside src/
        root_dir = pkg_root.parent
        src_icon = root_dir / "assets" / "icons" / "samsung-control.svg"
        dst_icon = icons_dir / "samsung-control.svg"
        if src_icon.exists():
            shutil.copy2(src_icon, dst_icon)

        exec_path = sys.executable
        desktop_path = apps_dir / "org.samsung.control.desktop"
        desktop_contents = f"""[Desktop Entry]
Name=Samsung Galaxy Book Control
Comment=Control Samsung Galaxy Book features
Exec={exec_path} {entrypoint}
Icon=samsung-control
Terminal=false
Type=Application
Categories=Settings;HardwareSettings;
Keywords=samsung;galaxybook;laptop;control;
"""
        desktop_path.write_text(desktop_contents)

        try:
            subprocess.run(["update-desktop-database", str(apps_dir)], check=False)
        except Exception:
            pass
        try:
            subprocess.run(["gtk-update-icon-cache", str(icons_dir.parent)], check=False)
        except Exception:
            pass

        print(f"Installed desktop file to {desktop_path} and icon to {dst_icon}")
    except Exception as e:
        print("Installation failed:", e)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] in ("--install", "install"):
        install_for_user()
    else:
        main()
