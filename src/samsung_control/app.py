import json
import logging
import math
import os
import pwd
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, GdkPixbuf
import cairo


# helper to convert a square pixbuf into a circular version

def make_circular_pixbuf(pixbuf):
    """Return a new GdkPixbuf with the contents of *pixbuf* clipped to a circle.
    If the source is not square the circle uses the smaller dimension.
    """
    w = pixbuf.get_width()
    h = pixbuf.get_height()
    size = min(w, h)
    # create cairo surface and draw masked image
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    cr.arc(size / 2, size / 2, size / 2, 0, 2 * math.pi)
    cr.clip()
    # center the source pixbuf
    dx = (size - w) / 2
    dy = (size - h) / 2
    Gdk.cairo_set_source_pixbuf(cr, pixbuf, dx, dy)
    cr.paint()
    return Gdk.pixbuf_get_from_surface(surface, 0, 0, size, size)

from .i18n import TRANSLATIONS
from .widgets import BatteryIcon, CPUIcon, FanIcon, FanSpeedGraph, TimeSeriesGraph, BatteryThresholdSlider


class SamsungControl(Adw.Application):
    def __init__(self):
        super().__init__(application_id="org.samsung.control")

        # Set color scheme to prefer dark
        self.style_manager = Adw.StyleManager.get_default()
        self.style_manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)

        self.connect("activate", self.on_activate)

        self.language = "en"
        settings = self.load_settings()
        if settings.get("language") in TRANSLATIONS:
            self.language = settings["language"]

        # Base paths
        self.base_path = "/dev/samsung-galaxybook"
        self.platform_profile_path = "/sys/firmware/acpi/platform_profile"
        self.kbd_backlight_paths = [
            "/sys/class/leds/samsung-galaxybook::kbd_backlight/brightness",
            "/dev/samsung-galaxybook/kbd_backlight/brightness",
        ]

        # Update intervals (in milliseconds)
        self.fan_update_interval = 2000
        self.battery_update_interval = 5000
        self.kbd_backlight_update_interval = 1000
        self.cpu_update_interval = 2000

        # State tracking
        self.kbd_backlight_scale = None
        self.current_kbd_brightness = 0
        self.prev_cpu_total = 0
        self.prev_cpu_idle = 0
        self.prev_cpu_cores = {}
        self.battery_icon = None
        self.battery_label = None
        self.discharge_label = None
        self.power_profile_dropdown = None
        self.current_power_profile = None
        self.profile_radio_group = None
        self.content_stack = None
        self._last_battery_graph_update = 0
        self._prev_battery_charging = None

        # Add fan/cpu/battery monitoring state
        self.fan_speeds = []
        self.fan_graph = None
        self.fan_icon = None
        self.cpu_usage_label = None
        self.cpu_core_list_box = None
        self.cpu_core_widgets = []

    def t(self, key):
        lang = self.language if self.language in TRANSLATIONS else "en"
        return TRANSLATIONS.get(lang, {}).get(
            key, TRANSLATIONS.get("en", {}).get(key, key)
        )

    def get_attribute_path(self, attr):
        """Get the full system path for an attribute"""
        if attr == "charge_control_end_threshold":
            return "/sys/class/power_supply/BAT1/charge_control_end_threshold"
        elif attr == "start_on_lid_open":
            # Use the firmware-attributes interface which is the modern approach
            return "/sys/class/firmware-attributes/samsung-galaxybook/attributes/power_on_lid_open/current_value"
        elif attr == "allow_recording":
            # Similar pattern for block_recording (inverted logic: 1=blocked, 0=allowed)
            return "/sys/class/firmware-attributes/samsung-galaxybook/attributes/block_recording/current_value"
        elif attr == "usb_charge":
            return f"{self.base_path}/{attr}"
        else:
            return f"{self.base_path}/{attr}"

    def attribute_exists(self, attr):
        """Check if an attribute path exists in the system"""
        try:
            path = self.get_attribute_path(attr)
            exists = os.path.exists(path)
            if not exists:
                logging.warning(f"Attribute {attr} not found at {path}")
            return exists
        except Exception as e:
            logging.error(f"Error checking if attribute {attr} exists: {str(e)}")
            return False

    def read_value(self, attr):
        try:
            path = self.get_attribute_path(attr)
            logging.info(f"Attempting to read from {path}")
            with open(path, "r") as f:
                value = f.read().strip()
            
            # Special handling for allow_recording: block_recording is inverted
            if attr == "allow_recording":
                # block_recording: 1=blocked, 0=allowed
                # allow_recording: 1=allowed, 0=blocked
                value = "1" if value == "0" else "0"
            
            logging.info(f"Read value from {attr}: {value}")
            return value
        except FileNotFoundError:
            logging.warning(f"Attribute {attr} not found: {path}")
            return None
        except PermissionError:
            logging.error(f"Permission denied reading {attr}. Try running with sudo.")
            return None
        except Exception as e:
            logging.error(f"Error reading {attr}: {str(e)}")
            return None

    def write_value(self, attr, value):
        try:
            path = self.get_attribute_path(attr)
            
            # Special handling for allow_recording: block_recording is inverted
            write_value = value
            if attr == "allow_recording":
                # allow_recording True (1) -> block_recording False (0)
                # allow_recording False (0) -> block_recording True (1)
                write_value = "0" if value == "1" else "1"
            
            logging.info(f"Attempting to write {write_value} to {path}")
            with open(path, "w") as f:
                f.write(str(write_value))
            logging.info(f"Write successful for {attr}={value}")
            return True
        except FileNotFoundError:
            logging.error(
                f"Attribute {attr} not found at {path}. The kernel module may not support this feature or it's not loaded."
            )
            return "not_found"
        except PermissionError:
            logging.error(
                f"Permission denied when writing to {attr}. Try running the program with sudo."
            )
            return "permission_denied"
        except Exception as e:
            logging.error(f"Error writing to {attr}: {str(e)}")
            return False

    def has_kbd_backlight(self):
        """Check if keyboard backlight hardware exists"""
        for path in self.kbd_backlight_paths:
            try:
                if os.path.exists(path):
                    logging.info(f"Found keyboard backlight at {path}")
                    return True
            except Exception as e:
                logging.warning(f"Error checking {path}: {str(e)}")
        logging.info("Keyboard backlight hardware not found")
        return False

    def read_kbd_backlight_max(self):
        for base_path in self.kbd_backlight_paths:
            max_path = base_path.replace("brightness", "max_brightness")
            try:
                with open(max_path, "r") as f:
                    return int(f.read().strip())
            except Exception as e:
                logging.warning(
                    f"Could not read max brightness from {max_path}: {str(e)}"
                )
        return 3  # Default max brightness if we can't read it

    def read_kbd_backlight(self):
        for path in self.kbd_backlight_paths:
            try:
                logging.info(f"Trying to read keyboard backlight from {path}")
                with open(path, "r") as f:
                    value = int(f.read().strip())
                logging.info(f"Read keyboard backlight value: {value}")
                return value
            except Exception as e:
                logging.warning(f"Could not read from {path}: {str(e)}")
        logging.error("Failed to read keyboard backlight from any path")
        return None

    def write_kbd_backlight(self, value):
        success = False
        for path in self.kbd_backlight_paths:
            try:
                logging.info(
                    f"Trying to write keyboard backlight value {value} to {path}"
                )
                with open(path, "w") as f:
                    f.write(str(value))
                success = True
                logging.info("Write successful")
                break
            except Exception as e:
                logging.warning(f"Could not write to {path}: {str(e)}")

        if not success:
            logging.error("Failed to write keyboard backlight to any path")
        return success

    def update_kbd_backlight_scale(self):
        if self.kbd_backlight_scale is None:
            return True

        current = self.read_kbd_backlight()
        if current is not None and current != self.current_kbd_brightness:
            logging.info(f"Keyboard backlight changed externally: {current}")
            self.current_kbd_brightness = current
            self.kbd_backlight_scale.set_value(current)

        return True

    def create_scale_row(self, title, subtitle, attr, min_val, max_val):
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(12)
        box.set_margin_end(12)

        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        title_label = Gtk.Label(label=title, xalign=0)
        title_label.add_css_class("heading")
        header_box.append(title_label)

        subtitle_label = Gtk.Label(label=subtitle, xalign=0)
        subtitle_label.add_css_class("subtitle")

        scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, min_val, max_val, 1
        )
        scale.set_draw_value(True)
        scale.set_value_pos(Gtk.PositionType.RIGHT)
        scale.set_size_request(200, -1)  # Set minimum width for better usability

        if attr == "kbd_backlight/brightness":
            current_value = self.read_kbd_backlight()
            if current_value is not None:
                scale.set_value(current_value)
            self.kbd_backlight_scale = scale
            scale.connect("value-changed", self.on_scale_changed, attr)

        box.append(header_box)
        box.append(subtitle_label)
        box.append(scale)
        row.set_child(box)
        return row

    def create_switch_row(self, title, subtitle, attr):
        # Check if the attribute exists before creating the switch
        if not self.attribute_exists(attr):
            logging.warning(f"Skipping switch for {attr} - attribute not found in system")
            # Create a disabled row showing the feature is unavailable
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            box.add_css_class("control-box")
            box.set_margin_top(6)
            box.set_margin_bottom(6)
            box.set_margin_start(12)
            box.set_margin_end(12)

            label_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            title_label = Gtk.Label(label=title, xalign=0)
            title_label.add_css_class("heading")
            subtitle_label = Gtk.Label(
                label=f"{subtitle} ({self.t('not_available')})", xalign=0
            )
            subtitle_label.add_css_class("subtitle")

            label_box.append(title_label)
            label_box.append(subtitle_label)

            box.append(label_box)
            row.set_child(box)
            row.set_selectable(False)
            row.set_sensitive(False)
            return row

        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.add_css_class("control-box")
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(12)
        box.set_margin_end(12)

        label_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        title_label = Gtk.Label(label=title, xalign=0)
        title_label.add_css_class("heading")
        subtitle_label = Gtk.Label(label=subtitle, xalign=0)
        subtitle_label.add_css_class("subtitle")

        label_box.append(title_label)
        label_box.append(subtitle_label)

        switch = Gtk.Switch()
        switch.set_valign(Gtk.Align.CENTER)
        switch.add_css_class("samsung-switch")

        current_value = self.read_value(attr)
        if current_value is not None:
            switch.set_active(current_value == "1")
            logging.info(f"Switch for {attr} initialized to {current_value == '1'}")
        else:
            logging.warning(f"Could not read current value for {attr}")

        switch.connect("notify::active", self.on_switch_activated, attr)

        box.append(label_box)
        box.append(switch)
        row.set_child(box)
        return row

    def create_language_row(self):
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.add_css_class("control-box")
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(12)
        box.set_margin_end(12)

        label_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        title_label = Gtk.Label(label=self.t("language"), xalign=0)
        title_label.add_css_class("heading")
        subtitle_label = Gtk.Label(label=self.t("language_desc"), xalign=0)
        subtitle_label.add_css_class("subtitle")

        label_box.append(title_label)
        label_box.append(subtitle_label)

        languages = [
            ("en", self.t("language_en")),
            ("pt_BR", self.t("language_pt")),
        ]
        dropdown = Gtk.DropDown.new_from_strings([label for _, label in languages])
        current = next(
            (i for i, (code, _) in enumerate(languages) if code == self.language), 0
        )
        dropdown.set_selected(current)

        def on_language_changed(dropdown, _gparam):
            idx = dropdown.get_selected()
            if idx < 0 or idx >= len(languages):
                return
            self.language = languages[idx][0]
            settings = self.load_settings()
            settings["language"] = self.language
            self.save_settings(settings)

        dropdown.connect("notify::selected", on_language_changed)

        box.append(label_box)
        box.append(dropdown)
        row.set_child(box)
        return row

    def create_battery_threshold_row(self):
        """Create Battery Threshold row with custom slider"""
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        # Title and subtitle
        header_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title_label = Gtk.Label(label=self.t("battery_threshold"), xalign=0)
        title_label.add_css_class("heading")
        subtitle_label = Gtk.Label(label=self.t("battery_threshold_desc"), xalign=0)
        subtitle_label.add_css_class("subtitle")
        
        header_box.append(title_label)
        header_box.append(subtitle_label)
        box.append(header_box)

        # Slider
        slider = BatteryThresholdSlider()
        
        # Read current value
        current_value = self.read_value("charge_control_end_threshold")
        if current_value:
            try:
                slider.set_value(int(current_value))
            except Exception as e:
                logging.warning(f"Could not set initial battery threshold: {e}")
                slider.set_value(80)
        else:
            slider.set_value(80)

        # Connect value change
        def on_threshold_changed(new_value):
            self.write_value("charge_control_end_threshold", str(new_value))
            logging.info(f"Battery threshold changed to: {new_value}")

        slider.connect_value_changed(on_threshold_changed)
        box.append(slider)

        row.set_child(box)
        return row

    def create_spinbutton_row(self, title, subtitle, attr, min_val, max_val):
        # Use the new method to get the path
        full_path = self.get_attribute_path(attr)

        if not os.path.exists(full_path):
            logging.warning(f"Skipping {attr} because {full_path} does not exist")
            return Gtk.ListBoxRow()  # Return an empty row or handle gracefully

        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(12)
        box.set_margin_end(12)

        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        title_label = Gtk.Label(label=title, xalign=0)
        title_label.add_css_class("heading")
        header_box.append(title_label)

        subtitle_label = Gtk.Label(label=subtitle, xalign=0)
        subtitle_label.add_css_class("subtitle")

        error_label = Gtk.Label(label="", xalign=0)
        error_label.add_css_class("error")
        error_label.set_visible(False)

        spinbutton = Gtk.SpinButton()
        spinbutton.set_adjustment(
            Gtk.Adjustment(value=80, lower=min_val, upper=max_val, step_increment=1)
        )
        current_value = self.read_value(attr)
        if current_value is not None:
            try:
                spinbutton.set_value(int(current_value))
            except ValueError:
                logging.warning(f"Invalid value for {attr}: {current_value}")

        def on_spinbutton_changed(button):
            result = self.write_value(attr, str(int(button.get_value())))
            if result == "permission_denied":
                error_label.set_text("Permission denied. Run the program with sudo.")
                error_label.set_visible(True)
            else:
                error_label.set_visible(False)

        spinbutton.connect("value-changed", on_spinbutton_changed)

        box.append(header_box)
        box.append(subtitle_label)
        box.append(error_label)
        box.append(spinbutton)
        row.set_child(box)
        return row

    def create_dropdown_row(self, title, subtitle):
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(12)
        box.set_margin_end(12)

        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        title_label = Gtk.Label(label=title, xalign=0)
        title_label.add_css_class("heading")
        header_box.append(title_label)

        subtitle_label = Gtk.Label(label=subtitle, xalign=0)
        subtitle_label.add_css_class("subtitle")

        # Try to get profiles from GNOME D-Bus first
        profiles = self.get_available_dbus_profiles()
        
        # Fall back to hardware profiles if D-Bus is not available
        if not profiles:
            profiles = self.get_platform_profile_choices()
        
        if not profiles:
            # If no profiles available, show a label instead of dropdown
            status_label = Gtk.Label(label=self.t("not_available"))
            status_label.set_sensitive(False)
            box.append(header_box)
            box.append(subtitle_label)
            box.append(status_label)
            row.set_child(box)
            return row

        dropdown = Gtk.DropDown.new_from_strings(profiles)
        current_profile = self.read_platform_profile()
        if current_profile is not None and current_profile in profiles:
            dropdown.set_selected(profiles.index(current_profile))
            self.current_power_profile = current_profile

        dropdown.connect("notify::selected", self.on_profile_changed)
        self.power_profile_dropdown = dropdown

        box.append(header_box)
        box.append(subtitle_label)
        box.append(dropdown)
        row.set_child(box)
        return row

    def create_fan_speed_row(self):
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(12)
        box.set_margin_end(12)

        label_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        title_label = Gtk.Label(label=self.t("fan_speed"), xalign=0)
        title_label.add_css_class("heading")
        self.fan_speed_label = Gtk.Label(label=self.t("updating"), xalign=0)
        self.fan_speed_label.add_css_class("subtitle")

        label_box.append(title_label)
        label_box.append(self.fan_speed_label)

        box.append(label_box)
        row.set_child(box)
        return row

    def update_fan_speed(self):
        try:
            speed = None
            for i in range(0, 10):
                path = f"/sys/class/hwmon/hwmon{i}/fan1_input"
                if os.path.exists(path):
                    with open(path, "r") as f:
                        speed = int(f.read().strip())
                        break

            if speed is not None:
                self.fan_speed_label.set_text(f"{speed} RPM")
                # Add to fan_graph if it exists
                if hasattr(self, "fan_graph"):
                    self.fan_graph.add_data_point(speed)
                if self.fan_icon:
                    self.fan_icon.set_speed(speed)
            else:
                self.fan_speed_label.set_text(self.t("not_available"))
        except:
            self.fan_speed_label.set_text(self.t("error_reading_fan_speed"))

        return True

    def on_switch_activated(self, switch, gparam, attr):
        if attr == "kbd_backlight/brightness":
            value = (
                3 if switch.get_active() else 0
            )  # Use max brightness (3) when turning on
            success = self.write_kbd_backlight(value)
            if success:
                self.current_kbd_brightness = value
            else:
                # Revert switch if write failed
                switch.set_active(not switch.get_active())
        else:
            # For other switches, always save to settings (even if write may fail)
            new_value = "1" if switch.get_active() else "0"
            
            # Try to write the value to the kernel module
            result = self.write_value(attr, new_value)
            
            # Always save to settings (this helps persist configuration even with permission issues)
            logging.info(f"Saving {attr} to settings: {switch.get_active()}")
            settings = self.load_settings()
            settings[attr] = switch.get_active()
            self.save_settings(settings)
            
            if result is not True:
                # Write to kernel failed, but configuration was saved
                logging.warning(f"Failed to write {attr} to kernel module. Result: {result}")
                if result == "permission_denied":
                    logging.warning(f"Permission denied writing to {attr}. The application may need to be run with sudo.")
                    logging.info(f"Try running with: sudo {' '.join(sys.argv)}")
                elif result == "not_found":
                    logging.warning(f"Attribute {attr} not found. The kernel module may not support this feature or is not loaded.")
                # Note: We do NOT revert the switch - the user's preference is saved and will be applied when possible

    def on_spinbutton_changed(self, spinbutton, attr):
        self.write_value(attr, str(int(spinbutton.get_value())))

    def on_profile_changed(self, dropdown, gparam):
        selected = dropdown.get_selected()
        # Try to get profiles from GNOME D-Bus first
        profiles = self.get_available_dbus_profiles()
        if not profiles:
            profiles = self.get_platform_profile_choices()
        
        if 0 <= selected < len(profiles):
            profile = profiles[selected]
            self.current_power_profile = profile
            logging.info(f"User changed profile to: {profile}")
            self.write_platform_profile(profile)

    def on_scale_changed(self, scale, attr):
        if attr == "kbd_backlight/brightness":
            value = int(scale.get_value())
            success = self.write_kbd_backlight(value)
            if success:
                self.current_kbd_brightness = value
            else:
                # Revert scale if write failed
                scale.set_value(self.current_kbd_brightness)

    def create_sidebar(self):
        """Create sidebar with navigation menu"""
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        sidebar.set_hexpand(False)
        sidebar.add_css_class("sidebar")

        # User profile header
        profile_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        profile_box.set_margin_top(16)
        profile_box.set_margin_bottom(16)
        profile_box.set_margin_start(16)
        profile_box.set_margin_end(16)

        # Get current user info
        try:
            current_user = pwd.getpwuid(os.getuid())
            username = current_user.pw_name
            user_gecos = current_user.pw_gecos or username
            
            # Try to load user avatar
            home_dir = current_user.pw_dir
            avatar_paths = [
                os.path.join(home_dir, ".face"),
                os.path.join(home_dir, ".face.icon"),
                f"/var/lib/AccountsService/icons/{username}",
            ]
            
            avatar_image = None
            for avatar_path in avatar_paths:
                if os.path.exists(avatar_path):
                    try:
                        # load and scale to expected size
                        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(avatar_path, 48, 48, True)
                        # make it circular and wrap in Gtk.Image
                        circ = make_circular_pixbuf(pb)
                        avatar_image = Gtk.Image.new_from_pixbuf(circ)
                        avatar_image.set_size_request(48, 48)
                        avatar_image.add_css_class("user-avatar-image")
                        break
                    except Exception:
                        pass
        except Exception as e:
            logging.warning(f"Could not get user info: {e}")
            username = "User"
            user_gecos = "User"
            avatar_image = None

        # Avatar container
        avatar_container = Gtk.Box()
        avatar_container.set_size_request(48, 48)
        avatar_container.add_css_class("user-avatar")
        
        if avatar_image:
            avatar_image.set_size_request(60, 60)
            avatar_container.append(avatar_image)
        else:
            # Fallback: show initials or default icon
            fallback_label = Gtk.Label(label=user_gecos[0].upper() if user_gecos else "U")
            fallback_label.add_css_class("user-avatar-text")
            avatar_container.append(fallback_label)

        # User info
        user_info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        user_info_box.set_valign(Gtk.Align.CENTER)  # Center vertically with avatar
        user_info_box.set_margin_start(12)  # Space between avatar and text
        user_info_box.set_hexpand(True)  # Expand to push avatar to the right
        user_name_label = Gtk.Label(label=user_gecos)
        user_name_label.add_css_class("user-name")
        user_name_label.set_xalign(0)
        
        user_status_label = Gtk.Label(label=self._get_os_name())
        user_status_label.add_css_class("user-status")
        user_status_label.set_xalign(0)
        
        user_info_box.append(user_name_label)
        user_info_box.append(user_status_label)
        
        # Add user info first (left), then avatar (right)
        profile_box.append(user_info_box)
        profile_box.append(avatar_container)
        sidebar.append(profile_box)

        # Separator
        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        separator.set_margin_start(12)  # Add padding from left edge
        separator.set_margin_end(12)    # Add padding from right edge
        #sidebar.append(separator)

        # Menu items (use app-provided icons) - with descriptions
        menu_items = [
            ("battery", self.t("menu_battery_perf"), "samsung-battery", "Performance mode, battery threshold"),
            ("advanced", self.t("menu_advanced"), "samsung-settings", "Quick settings, keyboard light"),
            ("monitor", self.t("menu_monitor"), "samsung-graph", "Fan speed, CPU, battery info"),
            ("about", self.t("menu_about_device"), "samsung-about", "System and hardware info"),
        ]

        for page_name, label, icon_name, description in menu_items:
            button = self.create_sidebar_button(label, icon_name, page_name, description)
            sidebar.append(button)

        sidebar.set_vexpand(True)
        return sidebar

    def create_sidebar_button(self, label, icon_name, page_name, description=""):
        """Create a sidebar navigation button with label and description"""
        button = Gtk.Button()
        button.set_hexpand(True)
        button.add_css_class("sidebar-button")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(12)
        box.set_margin_end(12)

        # Icon with colored background
        icon_container = Gtk.Box()
        icon_container.set_size_request(48, 48)
        icon_container.set_halign(Gtk.Align.START)
        icon_container.add_css_class("sidebar-icon")
        # Add page-specific class so we can style certain icons (e.g. battery green)
        icon_container.add_css_class(f"icon-{page_name}")

        # Try to load bundled icon file (useful when running from source)
        # the icons have been moved to the project-level assets directory
        root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        local_icons_dir = os.path.join(root_dir, "assets", "icons")
        # Map icon_name to file names we installed in install.sh
        local_icon_map = {
            "samsung-battery": "charging.png",
            "samsung-settings": "creative.png",
            "samsung-graph": "graphic.png",
            "samsung-about": "information.png",
        }

        loaded_icon = False
        if icon_name in local_icon_map:
            local_path = os.path.join(local_icons_dir, local_icon_map[icon_name])
            if os.path.exists(local_path):
                try:
                    icon = Gtk.Image.new_from_file(local_path)
                    icon.set_pixel_size(44)
                    icon_container.append(icon)
                    loaded_icon = True
                except Exception:
                    loaded_icon = False

        if not loaded_icon:
            try:
                icon = Gtk.Image.new_from_icon_name(icon_name)
                icon.set_pixel_size(44)
                icon_container.append(icon)
            except:
                pass

        # Label and description (to the right of icon)
        label_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        
        label_widget = Gtk.Label(label=label)
        label_widget.add_css_class("sidebar-label")
        label_widget.set_justify(Gtk.Justification.LEFT)
        label_widget.set_wrap(False)
        
        if description:
            desc_widget = Gtk.Label(label=description)
            desc_widget.add_css_class("sidebar-description")
            desc_widget.set_justify(Gtk.Justification.LEFT)
            desc_widget.set_wrap(True)
            desc_widget.set_xalign(0)
            label_box.append(label_widget)
            label_box.append(desc_widget)
        else:
            label_box.append(label_widget)

        box.append(icon_container)
        box.append(label_box)
        button.set_child(box)

        button.connect("clicked", lambda b: self.content_stack.set_visible_child_name(page_name))

        return button

    def create_battery_performance_page(self):
        """Create Battery & Performance page"""
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.add_css_class("page-content")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        content.set_margin_start(24)
        content.set_margin_end(24)

        # Add page title
        page_title = Gtk.Label(label=self.t("page_battery_perf"))
        page_title.add_css_class("page-title")
        page_title.set_xalign(0)
        content.append(page_title)

        # Battery controls - Card 1: Battery Threshold
        controls_box1 = Gtk.ListBox()
        controls_box1.add_css_class("boxed-list")
        controls_box1.set_selection_mode(Gtk.SelectionMode.NONE)

        controls_box1.append(self.create_battery_threshold_row())

        card1 = self.create_card(controls_box1)
        content.append(card1)

        # Card 2: USB Charging
        controls_box2 = Gtk.ListBox()
        controls_box2.add_css_class("boxed-list")
        controls_box2.set_selection_mode(Gtk.SelectionMode.NONE)

        controls_box2.append(
            self.create_switch_row(
                self.t("usb_charging"),
                self.t("usb_charging_desc"),
                "usb_charge",
            )
        )

        card2 = self.create_card(controls_box2)
        content.append(card2)

        # Card 3: Performance Mode
        perf_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        perf_box.set_margin_top(12)
        perf_box.set_margin_bottom(12)
        perf_box.set_margin_start(12)
        perf_box.set_margin_end(12)

        perf_title = Gtk.Label(label=self.t("perf_mode"), xalign=0)
        perf_title.add_css_class("heading")
        perf_subtitle = Gtk.Label(label=self.t("perf_mode_desc"), xalign=0)
        perf_subtitle.add_css_class("subtitle")

        perf_box.append(perf_title)
        perf_box.append(perf_subtitle)

        # Get profiles
        profiles = self.get_available_dbus_profiles()
        if not profiles:
            profiles = self.get_platform_profile_choices()

        # Create radio buttons for profiles
        if profiles:
            profiles_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            profiles_box.set_margin_top(8)

            first_radio = None
            current_profile = self.read_platform_profile()

            for profile in profiles:
                radio = Gtk.CheckButton(label=profile)
                if first_radio is None:
                    first_radio = radio
                else:
                    radio.set_group(first_radio)

                if profile == current_profile:
                    radio.set_active(True)

                radio.connect("toggled", lambda btn, p=profile: self.on_profile_radio_changed(btn, p))
                profiles_box.append(radio)

            self.profile_radio_group = first_radio
            perf_box.append(profiles_box)

        card3 = self.create_card(perf_box)
        content.append(card3)

        scrolled.set_child(content)
        return scrolled

    def on_profile_radio_changed(self, radio, profile):
        """Handle profile selection change"""
        if radio.get_active():
            self.current_power_profile = profile
            logging.info(f"User changed profile to: {profile}")
            self.write_platform_profile(profile)

    def create_advanced_features_page(self):
        """Create Advanced Features page"""
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.add_css_class("page-content")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        content.set_margin_start(24)
        content.set_margin_end(24)

        # Add page title
        page_title = Gtk.Label(label=self.t("page_advanced"))
        page_title.add_css_class("page-title")
        page_title.set_xalign(0)
        content.append(page_title)

        # Card 1: Language
        controls_box1 = Gtk.ListBox()
        controls_box1.add_css_class("boxed-list")
        controls_box1.set_selection_mode(Gtk.SelectionMode.NONE)
        controls_box1.append(self.create_language_row())
        card1 = self.create_card(controls_box1)
        content.append(card1)

        # Card 2: Start on Lid Open
        controls_box2 = Gtk.ListBox()
        controls_box2.add_css_class("boxed-list")
        controls_box2.set_selection_mode(Gtk.SelectionMode.NONE)
        controls_box2.append(
            self.create_switch_row(
                self.t("start_on_lid"),
                self.t("start_on_lid_desc"),
                "start_on_lid_open",
            )
        )
        card2 = self.create_card(controls_box2)
        content.append(card2)

        # Card 3: Allow Recording
        controls_box3 = Gtk.ListBox()
        controls_box3.add_css_class("boxed-list")
        controls_box3.set_selection_mode(Gtk.SelectionMode.NONE)
        controls_box3.append(
            self.create_switch_row(
                self.t("allow_recording"),
                self.t("allow_recording_desc"),
                "allow_recording",
            )
        )
        card3 = self.create_card(controls_box3)
        content.append(card3)

        # Card 4: Keyboard Backlight
        if self.has_kbd_backlight():
            max_brightness = self.read_kbd_backlight_max()
            controls_box4 = Gtk.ListBox()
            controls_box4.add_css_class("boxed-list")
            controls_box4.set_selection_mode(Gtk.SelectionMode.NONE)
            controls_box4.append(
                self.create_scale_row(
                    self.t("kbd_backlight"),
                    self.t("kbd_backlight_desc"),
                    "kbd_backlight/brightness",
                    0,
                    max_brightness,
                )
            )
            card4 = self.create_card(controls_box4)
            content.append(card4)

        scrolled.set_child(content)
        return scrolled

    def create_monitor_system_page(self):
        """Create Monitor System page"""
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.add_css_class("page-content")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        content.set_margin_start(24)
        content.set_margin_end(24)

        # Add page title
        page_title = Gtk.Label(label=self.t("page_monitor"))
        page_title.add_css_class("page-title")
        page_title.set_xalign(0)
        content.append(page_title)

        # Menu para selecionar entre Battery, CPU Usage, Fan Speed
        menu_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        battery_btn = Gtk.Button(label=self.t("battery"))
        battery_btn.connect("clicked", lambda btn: self.switch_monitor_view("battery"))
        menu_box.append(battery_btn)

        cpu_btn = Gtk.Button(label=self.t("cpu_usage"))
        cpu_btn.connect("clicked", lambda btn: self.switch_monitor_view("cpu"))
        menu_box.append(cpu_btn)

        fan_btn = Gtk.Button(label=self.t("fan_speed"))
        fan_btn.connect("clicked", lambda btn: self.switch_monitor_view("fan"))
        menu_box.append(fan_btn)

        content.append(menu_box)

        # Stack para mostrar um gráfico por vez (fan, cpu, battery)
        self.monitor_stack = Gtk.Stack()
        self.monitor_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)

        # Página Fan Speed
        fan_page = self.create_fan_view()
        self.monitor_stack.add_named(fan_page, "fan")

        # Página CPU Usage
        cpu_page = self.create_cpu_view()
        self.monitor_stack.add_named(cpu_page, "cpu")

        # Página Battery
        battery_page = self.create_battery_view()
        self.monitor_stack.add_named(battery_page, "battery")

        content.append(self.monitor_stack)
        self.monitor_stack.set_visible_child_name("battery")

        scrolled.set_child(content)
        return scrolled

    def switch_monitor_view(self, mode):
        """Muda a visualização do gráfico para fan, cpu ou battery"""
        if self.monitor_stack:
            self.monitor_stack.set_visible_child_name(mode)

    def create_about_device_page(self):
        """Create About Device page with system and hardware information"""
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.add_css_class("page-content")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        content.set_margin_start(24)
        content.set_margin_end(24)

        # Add page title
        page_title = Gtk.Label(label=self.t("page_about"))
        page_title.add_css_class("page-title")
        page_title.set_xalign(0)
        content.append(page_title)

        # Hardware Information Card
        hw_box = Gtk.ListBox()
        hw_box.add_css_class("boxed-list")
        hw_box.set_selection_mode(Gtk.SelectionMode.NONE)

        hw_card_header = Gtk.Label(label=self.t("hardware_info"))
        hw_card_header.add_css_class("heading")
        hw_card_header.set_margin_top(12)
        hw_card_header.set_margin_start(12)
        hw_card_header.set_xalign(0)
        content.append(hw_card_header)

        # Model
        model = self._get_model_info()
        hw_box.append(self._create_info_row(self.t("model"), model))

        # Memory
        memory = self._get_memory_info()
        hw_box.append(self._create_info_row(self.t("memory"), memory))

        # Processor
        processor = self._get_processor_info()
        hw_box.append(self._create_info_row(self.t("processor"), processor))

        # Graphics
        graphics = self._get_graphics_info()
        hw_box.append(self._create_info_row(self.t("graphics"), graphics))

        # Disk Capacity
        disk_capacity = self._get_disk_capacity()
        hw_box.append(self._create_info_row(self.t("disk_capacity"), disk_capacity))

        hw_card = self.create_card(hw_box)
        content.append(hw_card)

        # Software Information Card
        sw_box = Gtk.ListBox()
        sw_box.add_css_class("boxed-list")
        sw_box.set_selection_mode(Gtk.SelectionMode.NONE)

        sw_card_header = Gtk.Label(label=self.t("software_info"))
        sw_card_header.add_css_class("heading")
        sw_card_header.set_margin_top(12)
        sw_card_header.set_margin_start(12)
        sw_card_header.set_xalign(0)
        content.append(sw_card_header)

        # Firmware Version
        firmware = self._get_firmware_version()
        sw_box.append(self._create_info_row(self.t("firmware_version"), firmware))

        # OS Name
        os_name = self._get_os_name()
        sw_box.append(self._create_info_row(self.t("os_name"), os_name))

        # OS Type
        os_type = self._get_os_type()
        sw_box.append(self._create_info_row(self.t("os_type"), os_type))

        # GNOME Version
        gnome_version = self._get_gnome_version()
        sw_box.append(self._create_info_row(self.t("gnome_version"), gnome_version))

        # Windowing System
        windowing_system = self._get_windowing_system()
        sw_box.append(self._create_info_row(self.t("windowing_system"), windowing_system))

        # Kernel Version
        kernel_version = self._get_kernel_version()
        sw_box.append(self._create_info_row(self.t("kernel_version"), kernel_version))

        sw_card = self.create_card(sw_box)
        content.append(sw_card)

        scrolled.set_child(content)
        return scrolled

    def _create_info_row(self, label, value):
        """Create a row showing label and value"""
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(12)
        box.set_margin_end(12)

        label_widget = Gtk.Label(label=label)
        label_widget.add_css_class("subtitle")
        label_widget.set_xalign(0)
        label_widget.set_hexpand(True)

        value_widget = Gtk.Label(label=value)
        value_widget.add_css_class("heading")
        value_widget.set_xalign(1)

        box.append(label_widget)
        box.append(value_widget)
        row.set_child(box)
        return row

    def _get_os_name(self):
        """Get operating system name (e.g., Ubuntu 24.04.4 LTS)"""
        try:
            with open("/etc/os-release", "r") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME"):
                        # Extract value from PRETTY_NAME="Ubuntu 24.04.4 LTS"
                        value = line.split("=", 1)[1].strip().strip('"')
                        return value
        except Exception:
            pass
        try:
            # Fallback: try /etc/lsb-release
            name = None
            version = None
            with open("/etc/lsb-release", "r") as f:
                for line in f:
                    if line.startswith("DISTRIB_DESCRIPTION"):
                        value = line.split("=", 1)[1].strip().strip('"')
                        return value
        except Exception:
            pass
        return "Unknown"

    def _get_kernel_version(self):
        """Get kernel version"""
        try:
            with open("/proc/version", "r") as f:
                version_line = f.read().strip()
                # Extract kernel version from /proc/version
                parts = version_line.split()
                if len(parts) > 2:
                    return parts[2]
            return "Unknown"
        except Exception:
            return "Unknown"

    def _get_processor_info(self):
        """Get processor information"""
        try:
            with open("/proc/cpuinfo", "r") as f:
                for line in f:
                    if "model name" in line:
                        return line.split(":")[-1].strip()
            return "Unknown"
        except Exception:
            return "Unknown"

    def _get_memory_info(self):
        """Get total system memory"""
        try:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if "MemTotal" in line:
                        kb = int(line.split()[-2])
                        gb = kb / (1024 * 1024)
                        return f"{gb:.1f} GB"
            return "Unknown"
        except Exception:
            return "Unknown"

    def _get_storage_info(self):
        """Get storage information"""
        try:
            import shutil
            stat = shutil.disk_usage("/")
            total_gb = stat.total / (1024**3)
            return f"{total_gb:.1f} GB"
        except Exception:
            return "Unknown"

    def _get_disk_capacity(self):
        """Get disk capacity information"""
        return self._get_storage_info()

    def _get_model_info(self):
        """Get device model information"""
        try:
            with open("/sys/class/dmi/id/board_name", "r") as f:
                model = f.read().strip()
                if model:
                    return model
        except Exception:
            pass
        try:
            with open("/sys/class/dmi/id/product_name", "r") as f:
                return f.read().strip()
        except Exception:
            return "Unknown"

    def _get_graphics_info(self):
        """Get graphics/GPU information"""
        try:
            result = subprocess.run(
                ["lspci"],
                capture_output=True,
                text=True,
                timeout=5
            )
            for line in result.stdout.split('\n'):
                if 'VGA' in line or 'Display' in line or 'Graphics' in line:
                    parts = line.split(': ', 1)
                    if len(parts) > 1:
                        return parts[1].strip()
        except Exception:
            pass
        return "Unknown"

    def _get_os_type(self):
        """Get OS type (32-bit or 64-bit)"""
        try:
            import platform
            if platform.architecture()[0] == '64bit':
                return "64-bit"
            elif platform.architecture()[0] == '32bit':
                return "32-bit"
        except Exception:
            pass
        return "Unknown"

    def _get_gnome_version(self):
        """Get GNOME version"""
        try:
            result = subprocess.run(
                ["gnome-shell", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.stdout:
                version = result.stdout.strip()
                # Extract version number from "GNOME Shell X.Y.Z"
                parts = version.split()
                if len(parts) >= 3:
                    return parts[-1]
                return version
        except Exception:
            pass
        return "Unknown"

    def _get_windowing_system(self):
        """Get windowing system (Wayland or X11)"""
        try:
            session_type = os.environ.get('XDG_SESSION_TYPE', '').strip()
            if session_type:
                return session_type.capitalize()
        except Exception:
            pass
        return "Unknown"

    def _get_firmware_version(self):
        """Get firmware version"""
        try:
            # Try to read from DMI data
            with open("/sys/class/dmi/id/system_version", "r") as f:
                return f.read().strip()
        except Exception:
            try:
                with open("/sys/firmware/efi/fw_platform_size", "r") as f:
                    return f.read().strip()
            except Exception:
                return "Unknown"

    def create_fan_view(self):
        """View para gráfico de Fan Speed"""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        card.set_vexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        # Left: Dados atuais do ventilador
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        left_box.set_halign(Gtk.Align.CENTER)  # Centralizar horizontalmente
        left_box.set_valign(Gtk.Align.CENTER)  # Centralizar verticalmente

        self.fan_icon = FanIcon()
        left_box.append(self.fan_icon)

        fan_label = Gtk.Label(label=self.t("fan_speed"), xalign=0)
        fan_label.set_halign(Gtk.Align.CENTER)  # Centralizar label
        fan_label.add_css_class("heading")
        left_box.append(fan_label)

        self.fan_speed_label = Gtk.Label(label=self.t("updating"), xalign=0)
        self.fan_speed_label.add_css_class("value-label")
        left_box.append(self.fan_speed_label)

        content.append(left_box)

        # Right: Gráfico
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right_box.set_hexpand(True)

        graph_title = Gtk.Label(label=self.t("rpm_history_60s"))
        graph_title.add_css_class("heading")
        right_box.append(graph_title)

        self.fan_graph = FanSpeedGraph()
        right_box.append(self.fan_graph)

        content.append(right_box)
        card.append(content)

        return self.create_card(card)

    def create_cpu_view(self):
        """View para gráfico de CPU Usage"""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        card.set_vexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        # Left: Dados atuais de CPU
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        left_box.set_halign(Gtk.Align.CENTER)  # Centralizar horizontalmente
        left_box.set_valign(Gtk.Align.CENTER)  # Centralizar verticalmente

        self.cpu_icon = CPUIcon()
        left_box.append(self.cpu_icon)

        cpu_label = Gtk.Label(label=self.t("cpu_usage"), xalign=0)
        cpu_label.add_css_class("heading")
        left_box.append(cpu_label)

        self.cpu_usage_label = Gtk.Label(label="...", xalign=0)
        self.cpu_usage_label.set_halign(Gtk.Align.CENTER)  # Centralizar valor
        self.cpu_usage_label.add_css_class("value-label")
        left_box.append(self.cpu_usage_label)

        content.append(left_box)

        # Right: Gráfico TimeSeriesGraph
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right_box.set_hexpand(True)

        graph_title = Gtk.Label(label=self.t("per_core_usage_60s"))
        graph_title.add_css_class("heading")
        right_box.append(graph_title)

        cores_scrolled = Gtk.ScrolledWindow()
        cores_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        cores_scrolled.set_min_content_height(240)
        cores_scrolled.set_hexpand(True)
        cores_scrolled.set_vexpand(True)

        self.cpu_core_list_box = Gtk.FlowBox()
        self.cpu_core_list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.cpu_core_list_box.set_max_children_per_line(2)
        self.cpu_core_list_box.set_row_spacing(12)
        self.cpu_core_list_box.set_column_spacing(12)
        cores_scrolled.set_child(self.cpu_core_list_box)
        right_box.append(cores_scrolled)

        content.append(right_box)
        card.append(content)

        return self.create_card(card)

    def create_battery_view(self):
        """View para gráfico de Battery (24 horas)"""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        card.set_vexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        # Left: Dados atuais de bateria (centralizado)
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        left_box.set_halign(Gtk.Align.CENTER)  # Centralizar horizontalmente
        left_box.set_valign(Gtk.Align.CENTER)  # Centralizar verticalmente

        self.battery_icon = BatteryIcon()
        self.battery_icon.set_halign(Gtk.Align.CENTER)  # Centralizar ícone
        left_box.append(self.battery_icon)

        battery_label = Gtk.Label(label=self.t("battery"))
        battery_label.set_halign(Gtk.Align.CENTER)  # Centralizar label
        battery_label.add_css_class("heading")
        left_box.append(battery_label)

        self.battery_label = Gtk.Label(label="...")
        self.battery_label.set_halign(Gtk.Align.CENTER)  # Centralizar valor
        self.battery_label.add_css_class("value-label")
        left_box.append(self.battery_label)

        self.discharge_label = Gtk.Label(label=f"{self.t('discharging_for')}: --")
        self.discharge_label.set_halign(Gtk.Align.CENTER)
        self.discharge_label.add_css_class("subtitle")
        left_box.append(self.discharge_label)

        content.append(left_box)

        # Right: Gráfico 24h
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right_box.set_hexpand(True)

        graph_title = Gtk.Label(label=self.t("battery_history_12h"))
        graph_title.add_css_class("heading")
        right_box.append(graph_title)

        self.battery_graph = TimeSeriesGraph(time_window=12*3600, y_label="Battery %")
        # Força max_value para 100 (porcentagem)
        self.battery_graph.max_value = 100
        # Carrega histórico ao criar
        try:
            current_pct, _ = self.read_battery_info()
        except Exception:
            current_pct = 0
        pts = self.get_battery_graph_points(current_pct)
        self.battery_graph.set_points(pts)
        right_box.append(self.battery_graph)

        content.append(right_box)
        card.append(content)

        return self.create_card(card)

    def on_activate(self, app):
        # Create main window (use Gtk.ApplicationWindow so window manager decorations work)
        window = Gtk.ApplicationWindow(application=app)
        window.set_title(self.t("window_title"))
        window.set_default_size(1200, 700)

        # Ask the window manager to provide standard decorations (titlebar/buttons)
        try:
            window.set_decorated(True)
        except Exception:
            pass

        # Set dark theme preference
        style_manager = Adw.StyleManager.get_default()
        style_manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)

        # Load custom CSS
        self.load_css()

        # Main layout: sidebar + content
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        # Create sidebar
        sidebar = self.create_sidebar()
        main_box.append(sidebar)

        # Load and apply saved settings BEFORE creating UI pages
        # This ensures switches read the correct values from the kernel
        try:
            self.load_and_apply_settings()
        except Exception as e:
            logging.warning(f"Error loading saved settings: {e}")

        # Create content area with stack
        self.content_stack = Gtk.Stack()
        self.content_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.content_stack.set_hexpand(True)
        self.content_stack.set_vexpand(True)

        # Add pages to stack
        self.content_stack.add_named(self.create_battery_performance_page(), "battery")
        self.content_stack.add_named(self.create_advanced_features_page(), "advanced")
        self.content_stack.add_named(self.create_monitor_system_page(), "monitor")
        self.content_stack.add_named(self.create_about_device_page(), "about")

        # Set default page
        self.content_stack.set_visible_child_name("battery")

        main_box.append(self.content_stack)

        try:
            window.set_child(main_box)
        except Exception:
            # Fallback for older API
            try:
                window.set_content(main_box)
            except Exception:
                pass
        window.present()

        # Start update timers
        GLib.timeout_add(self.fan_update_interval, self.update_fan_speed)
        if self.has_kbd_backlight():
            GLib.timeout_add(
                self.kbd_backlight_update_interval, self.update_kbd_backlight_scale
            )
        GLib.timeout_add(self.cpu_update_interval, self.update_cpu_usage)
        GLib.timeout_add(self.battery_update_interval, self.update_battery)
        # Monitor power profile changes from GNOME
        GLib.timeout_add(2000, self.update_power_profile)
        # Persist hourly battery samples for 24h history display
        try:
            GLib.timeout_add_seconds(3600, self.add_hourly_battery_sample)
            # run once at startup to ensure history exists
            self.add_hourly_battery_sample()
        except Exception:
            pass

    def read_platform_profile(self):
        """Read current platform profile, preferring GNOME D-Bus"""
        # First try to read from GNOME D-Bus
        dbus_profile = self.get_dbus_power_profile()
        if dbus_profile:
            return dbus_profile
        
        # Fall back to reading from sysfs
        try:
            logging.info(f"Reading platform profile from {self.platform_profile_path}")
            with open(self.platform_profile_path, "r") as f:
                value = f.read().strip()
                logging.info(f"Read platform profile: {value}")
                return value
        except Exception as e:
            logging.error(f"Error reading platform profile: {str(e)}")
            return None

    def write_platform_profile(self, value):
        """Write platform profile to both GNOME D-Bus and hardware"""
        success = False
        
        # Try to set via GNOME D-Bus first
        if self.set_dbus_power_profile(value):
            success = True
        
        # Also set the hardware profile if available
        try:
            logging.info(
                f"Writing platform profile {value} to {self.platform_profile_path}"
            )
            with open(self.platform_profile_path, "w") as f:
                f.write(value)
            logging.info("Hardware profile write successful")
            success = True
        except PermissionError:
            logging.warning("Permission denied writing to hardware profile")
        except Exception as e:
            logging.debug(f"Could not write to hardware profile: {str(e)}")
        
        return success

    def get_platform_profile_choices(self):
        try:
            path = "/sys/firmware/acpi/platform_profile_choices"
            logging.info(f"Reading platform profile choices from {path}")
            with open(path, "r") as f:
                choices = f.read().strip().split()
                logging.info(f"Available profiles: {choices}")
                return choices
        except Exception as e:
            logging.error(f"Error reading platform profile choices: {str(e)}")
            return []

    def get_dbus_power_profile(self):
        """Get current power profile from GNOME via D-Bus"""
        try:
            result = subprocess.run(
                ["busctl", "get-property", 
                 "org.freedesktop.PowerProfiles1",
                 "/org/freedesktop/PowerProfiles1",
                 "org.freedesktop.PowerProfiles1",
                 "ActiveProfile"],
                capture_output=True,
                text=True,
                timeout=2
            )
            if result.returncode == 0:
                # Extract profile name from output (e.g., "s \"balanced\"" -> "balanced")
                parts = result.stdout.strip().split('"')
                if len(parts) >= 2:
                    profile = parts[1]
                    logging.info(f"Read GNOME power profile: {profile}")
                    return profile
        except FileNotFoundError:
            logging.debug("busctl not available")
        except subprocess.TimeoutExpired:
            logging.debug("D-Bus timeout reading power profile")
        except Exception as e:
            logging.debug(f"Could not read D-Bus power profile: {str(e)}")
        return None

    def set_dbus_power_profile(self, profile):
        """Set power profile in GNOME via D-Bus"""
        try:
            result = subprocess.run(
                ["busctl", "set-property",
                 "org.freedesktop.PowerProfiles1",
                 "/org/freedesktop/PowerProfiles1",
                 "org.freedesktop.PowerProfiles1",
                 "ActiveProfile",
                 "s", profile],
                capture_output=True,
                text=True,
                timeout=2
            )
            if result.returncode == 0:
                logging.info(f"Set GNOME power profile to: {profile}")
                return True
            else:
                logging.debug(f"Failed to set GNOME power profile: {result.stderr}")
                return False
        except FileNotFoundError:
            logging.debug("busctl not available")
            return False
        except subprocess.TimeoutExpired:
            logging.debug("D-Bus timeout setting power profile")
            return False
        except Exception as e:
            logging.debug(f"Could not set D-Bus power profile: {str(e)}")
            return False

    def get_available_dbus_profiles(self):
        """Get available power profiles from GNOME"""
        try:
            result = subprocess.run(
                ["busctl", "get-property",
                 "org.freedesktop.PowerProfiles1",
                 "/org/freedesktop/PowerProfiles1",
                 "org.freedesktop.PowerProfiles1",
                 "Profiles"],
                capture_output=True,
                text=True,
                timeout=2
            )
            if result.returncode == 0:
                # Parse the output to extract profile names
                # Output format: a(sa{sv}) [("profile1" ...), ("profile2" ...), ...]
                import re
                matches = re.findall(r'"(\w+)"', result.stdout)
                # Filter to only unique profile names (skip repeated keys)
                profiles = list(dict.fromkeys(matches))[:3]  # Typically 3 profiles max
                if profiles:
                    logging.info(f"Available GNOME profiles: {profiles}")
                    return profiles
        except FileNotFoundError:
            logging.debug("busctl not available")
        except subprocess.TimeoutExpired:
            logging.debug("D-Bus timeout reading profiles")
        except Exception as e:
            logging.debug(f"Could not read D-Bus profiles: {str(e)}")
        return []

    def load_css(self):
        css_provider = Gtk.CssProvider()
        css = """
            /* Sidebar Styling */
            .sidebar {
                background: @view_bg_color;
                border-right: 1px solid alpha(@view_bg_color, 0.3);
            }

            .sidebar-title {
                font-weight: bold;
                font-size: 18px;
                color: @accent_bg_color;
            }

            .sidebar-subtitle {
                font-weight: bold;
                font-size: 18px;
                color: @accent_bg_color;
                margin-top: -3px;
            }

            /* User Profile Header */
            .user-avatar {
                min-width: 48px;
                min-height: 48px;
                border-radius: 50%;
                overflow: hidden; /* ensure contained image is clipped */
                background: alpha(@accent_bg_color, 0.15);
                color: @accent_bg_color;
                font-weight: 600;
                font-size: 18px;
            }

            .user-avatar-text {
                font-weight: 600;
                font-size: 18px;
                color: @accent_bg_color;
            }

            /* ensure photos are clipped to a circle */
            .user-avatar-image {
                border-radius: 50%;
                /* make sure the image fills its box and is not distorted */
                min-width: 48px;
                min-height: 48px;
            }

            .user-name {
                font-weight: 600;
                font-size: 15px;
                color: @card_fg_color;
            }

            .user-status {
                font-size: 12px;
                color: alpha(@accent_bg_color, 1);
            }

            .sidebar-button {
                background: transparent;
                border: none;
                border-radius: 8px;
                margin: 8px 12px;
                padding: 0;
                transition: all 200ms ease;
            }

            .sidebar-button:hover {
                background: alpha(@accent_bg_color, 0.1);
            }

            .sidebar-button:active {
                background: alpha(@accent_bg_color, 0.2);
            }

            .sidebar-icon {
                min-width: 60px;
                min-height: 60px;
                border-radius: 12px;
                background: transparent;
                color: white;
                transition: transform 200ms ease;
            }

            /* Make the icon box square and place label beside it */
            .sidebar-button .sidebar-icon {
                min-width: 48px;
                min-height: 48px;
                border-radius: 10px;
                margin-right: 12px;
                background: transparent;
            }

            .sidebar-label {
                font-weight: 600;
                font-size: 16px;
                color: @card_fg_color;
            }

            .sidebar-description {
                font-weight: 400;
                font-size: 12px;
                color: alpha(@card_fg_color, 0.65);
                margin-top: 2px;
            }

            .sidebar-button:hover .sidebar-icon {
                transform: scale(1.05);
            }

            /* Page Content */
            .page-content {
                background: @view_bg_color;
            }

            .page-title {
                font-size: 18px;
                font-weight: bold;
                color: @card_fg_color;
                border-left: 10px solid @view_bg_color;
                margin-bottom: 12px;
            }

            /* Card Styling */
            .card {
                background: alpha(@card_bg_color, 0.8);
                border-radius: 12px;
                padding: 0;
                margin: 0;
                box-shadow: 0 2px 4px rgba(0,0,0,0.2);
            }

            .card listbox {
                background: transparent;
            }

            .card listbox row {
                padding: 0;
                margin: 0;
            }

            .heading {
                font-weight: bold;
                font-size: 16px;
                margin-bottom: 4px;
                color: @card_fg_color;
            }

            .subtitle {
                font-size: 13px;
                color: alpha(@card_fg_color, 0.7);
            }

            .value-label {
                font-size: 28px;
                font-weight: bold;
                color: @accent_bg_color;
            }

            .samsung-switch switch {
                background: alpha(@accent_bg_color, 0.1);
                border: none;
                min-width: 50px;
                min-height: 26px;
            }

            .samsung-switch switch:checked {
                background: @accent_bg_color;
            }

            .control-box {
                background: transparent;
                padding: 8px 12px;
                margin: 0;
            }

            .boxed-list {
                background: transparent;
                margin: 0;
                padding: 0;
            }

            .boxed-list row {
                padding: 0;
                margin: 0;
                border-bottom: 1px solid alpha(@borders, 0.1);
            }

            .boxed-list row:last-child {
                border-bottom: none;
            }

            .dashboard-title {
                font-size: 20px;
                font-weight: bold;
                color: @accent_bg_color;
            }
        """
        css_provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def create_card(self, child):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # do not force vertical expansion; height should match contents
        # individual views (e.g. monitor graphs) may still call set_vexpand
        card.add_css_class("card")
        card.append(child)
        return card



    def read_cpu_usage(self):
        try:
            with open("/proc/stat", "r") as f:
                cpu = f.readline().split()[1:]
            cpu_total = sum(float(x) for x in cpu)
            cpu_idle = float(cpu[3])

            if self.prev_cpu_total > 0:
                diff_idle = cpu_idle - self.prev_cpu_idle
                diff_total = cpu_total - self.prev_cpu_total
                cpu_usage = (1000 * (diff_total - diff_idle) / diff_total + 5) / 10
                return f"{cpu_usage:.1f}%"

            self.prev_cpu_total = cpu_total
            self.prev_cpu_idle = cpu_idle
            return "..."
        except Exception as e:
            logging.error(f"Error reading CPU usage: {str(e)}")
            return "N/A"

    def read_cpu_usage_per_core(self):
        try:
            cores = []
            with open("/proc/stat", "r") as f:
                for line in f:
                    if not line.startswith("cpu"):
                        break
                    if line.startswith("cpu "):
                        continue
                    parts = line.split()
                    core_id = parts[0]
                    values = [float(x) for x in parts[1:]]
                    if len(values) < 4:
                        continue
                    total = sum(values)
                    idle = values[3]
                    prev = self.prev_cpu_cores.get(core_id)
                    if prev:
                        prev_total, prev_idle = prev
                        diff_total = total - prev_total
                        diff_idle = idle - prev_idle
                        if diff_total > 0:
                            usage = (1000 * (diff_total - diff_idle) / diff_total + 5) / 10
                        else:
                            usage = 0.0
                    else:
                        usage = None
                    self.prev_cpu_cores[core_id] = (total, idle)
                    cores.append((core_id, usage))
            return cores
        except Exception as e:
            logging.error(f"Error reading per-core CPU usage: {str(e)}")
            return []

    def ensure_cpu_core_widgets(self, cores):
        if not self.cpu_core_list_box:
            return
        if len(self.cpu_core_widgets) == len(cores):
            return
        # Clear existing widgets if core count changed
        child = self.cpu_core_list_box.get_first_child()
        while child:
            self.cpu_core_list_box.remove(child)
            child = self.cpu_core_list_box.get_first_child()
        self.cpu_core_widgets = []
        for idx, _ in enumerate(cores):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            card.add_css_class("card")
            card.set_margin_top(8)
            card.set_margin_bottom(8)
            card.set_margin_start(8)
            card.set_margin_end(8)
            card.set_hexpand(True)

            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            icon = CPUIcon()
            icon.set_size_request(20, 20)
            header.append(icon)

            label_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            label_box.set_hexpand(True)
            title = Gtk.Label(label=f"{self.t('core')} {idx}", xalign=0)
            title.add_css_class("heading")
            value = Gtk.Label(label="--", xalign=1)
            value.set_halign(Gtk.Align.END)
            value.add_css_class("cpu-core-value")
            label_box.append(title)
            label_box.append(value)
            header.append(label_box)
            header.set_margin_top(6)
            header.set_margin_bottom(6)
            header.set_margin_start(6)
            header.set_margin_end(6)

            card.append(header)

            graph = TimeSeriesGraph(time_window=60, y_label="CPU %")
            graph.set_size_request(420, 140)
            card.append(graph)

            self.cpu_core_list_box.append(card)
            self.cpu_core_widgets.append(
                {"icon": icon, "value": value, "graph": graph}
            )

    def update_cpu_usage(self):
        if self.cpu_usage_label:
            usage = self.read_cpu_usage()
            self.cpu_usage_label.set_text(usage)
            if hasattr(self, "cpu_icon"):
                self.cpu_icon.set_usage(usage)
            # store recent cpu samples para gráfico (percentage as float)
            try:
                val = 0.0
                if isinstance(usage, str) and usage.endswith("%"):
                    val = float(usage.rstrip("%"))
                elif isinstance(usage, (int, float)):
                    val = float(usage)
                if not hasattr(self, "_recent_cpu_samples"):
                    self._recent_cpu_samples = deque(maxlen=360)
                self._recent_cpu_samples.append((time.time(), val))
                # Add to cpu_graph if exists e mostrado
                if hasattr(self, "cpu_graph"):
                    self.cpu_graph.add_data_point(val)
            except Exception:
                pass

        # Update per-core list
        cores = self.read_cpu_usage_per_core()
        if cores and self.cpu_core_list_box:
            self.ensure_cpu_core_widgets(cores)
            for idx, (core_id, core_usage) in enumerate(cores):
                if idx >= len(self.cpu_core_widgets):
                    break
                widget = self.cpu_core_widgets[idx]
                if core_usage is None:
                    widget["value"].set_text("--")
                else:
                    widget["value"].set_text(f"{core_usage:.1f}%")
                    widget["icon"].set_usage(f"{core_usage:.1f}%")
                    widget["graph"].add_data_point(core_usage)
        return True

    def read_battery_info(self):
        try:
            with open("/sys/class/power_supply/BAT1/capacity", "r") as f:
                percentage = int(f.read().strip())
            with open("/sys/class/power_supply/BAT1/status", "r") as f:
                charging = f.read().strip() == "Charging"
            return percentage, charging
        except Exception as e:
            logging.error(f"Error reading battery info: {str(e)}")
            return 0, False

    def update_battery(self):
        if self.battery_icon and self.battery_label:
            percentage, charging = self.read_battery_info()
            self.battery_icon.update(percentage, charging)
            self.battery_label.set_text(f"{percentage}%")
            self.update_discharge_timer(percentage, charging)
            # Record a recent sample in-memory for immediate charting
            try:
                # Keep a short-lived in-memory list for quick samples
                if not hasattr(self, "_recent_battery_samples"):
                    self._recent_battery_samples = deque(maxlen=3600)
                self._recent_battery_samples.append((time.time(), percentage))
            except Exception:
                pass
            # Refresh battery graph with the latest samples
            try:
                if hasattr(self, "battery_graph") and self.battery_graph:
                    now = time.time()
                    if now - self._last_battery_graph_update >= 900:
                        points = self.get_battery_graph_points(percentage)
                        self.battery_graph.set_points(points)
                        self.battery_graph.max_value = 100
                        self._last_battery_graph_update = now
            except Exception:
                pass
        return True

    def update_discharge_timer(self, percentage, charging):
        if charging:
            if self.discharge_label:
                elapsed = self.get_charge_duration_seconds(percentage)
                if elapsed is None:
                    self.discharge_label.set_text(f"{self.t('charging_for')}: --")
                else:
                    self.discharge_label.set_text(
                        f"{self.t('charging_for')}: {self.format_elapsed(elapsed)}"
                    )
            self._prev_battery_charging = True
            return

        self._prev_battery_charging = False
        if self.discharge_label:
            elapsed = self.get_discharge_duration_seconds(percentage)
            if elapsed is None:
                self.discharge_label.set_text(f"{self.t('discharging_for')}: --")
            else:
                self.discharge_label.set_text(
                    f"{self.t('discharging_for')}: {self.format_elapsed(elapsed)}"
                )

    def get_discharge_duration_seconds(self, current_pct):
        now = time.time()
        points = self.get_battery_graph_points(current_pct)
        if not points:
            return None

        max_val = max(v for _, v in points)
        # Find the last time the battery was at the peak value.
        peak_times = [t for t, v in points if v == max_val]
        if not peak_times:
            return None

        last_peak = max(peak_times)
        if current_pct >= max_val:
            return 0
        return max(0, int(now - last_peak))

    def get_charge_duration_seconds(self, current_pct):
        now = time.time()
        points = self.get_battery_raw_points(current_pct)
        if not points:
            return None

        min_val = min(v for _, v in points)
        trough_times = [t for t, v in points if v == min_val]
        if not trough_times:
            return None

        last_trough = max(trough_times)
        if current_pct <= min_val:
            return 0
        return max(0, int(now - last_trough))

    def get_battery_raw_points(self, current_pct):
        now = time.time()
        points = self.load_battery_history()
        if hasattr(self, "_recent_battery_samples"):
            points.extend(self._recent_battery_samples)
        points.append((now, int(current_pct)))
        window = 12 * 3600
        if hasattr(self, "battery_graph") and self.battery_graph:
            window = max(1, int(self.battery_graph.time_window))
        cutoff = now - window
        points = [(t, v) for t, v in points if t >= cutoff]
        points.sort(key=lambda tv: tv[0])
        return points

    def get_battery_graph_points(self, current_pct):
        now = time.time()
        bucket_size = 900
        tm = time.localtime(now)
        end_time = now
        if tm.tm_min > 0 or tm.tm_sec > 0:
            end_time = time.mktime(
                (tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour + 1, 0, 0, -1, -1, -1)
            )
        points = self.load_battery_history()
        if hasattr(self, "_recent_battery_samples"):
            points.extend(self._recent_battery_samples)
        aligned_now = now - (now % bucket_size)
        points.append((aligned_now, int(current_pct)))
        window = 12 * 3600
        if hasattr(self, "battery_graph") and self.battery_graph:
            window = max(1, int(self.battery_graph.time_window))
        cutoff = end_time - window
        points = [(t, v) for t, v in points if cutoff <= t <= end_time]
        points.sort(key=lambda tv: tv[0])
        # Downsample to 15-minute buckets.
        bucketed = []
        last_bucket = None
        for t, v in points:
            bucket = int(t // bucket_size)
            if bucket != last_bucket:
                bucketed.append((t, v))
                last_bucket = bucket
            else:
                bucketed[-1] = (t, v)
        return bucketed

    def format_elapsed(self, seconds):
        minutes = seconds // 60
        hours = minutes // 60
        minutes = minutes % 60
        days = hours // 24
        hours = hours % 24

        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    # Settings persistence
    def settings_path(self):
        data_dir = GLib.get_user_data_dir()
        p = os.path.join(data_dir, "samsung-control")
        os.makedirs(p, exist_ok=True)
        return os.path.join(p, "settings.json")

    def load_settings(self):
        path = self.settings_path()
        try:
            if os.path.exists(path):
                with open(path, "r") as f:
                    return json.load(f)
        except Exception as e:
            logging.debug(f"Could not load settings: {e}")
        return {}

    def save_settings(self, settings):
        path = self.settings_path()
        try:
            with open(path, "w") as f:
                json.dump(settings, f, indent=2)
            logging.info(f"Settings saved to {path}")
        except Exception as e:
            logging.error(f"Could not save settings: {e}")

    def load_and_apply_settings(self):
        """Load saved settings and apply them to the system"""
        settings = self.load_settings()
        if settings:
            logging.info(f"Found saved settings: {settings}")
        else:
            logging.info("No saved settings found, using defaults")
            
        applied_count = 0
        failed_count = 0
        for attr, value in settings.items():
            if attr == "language":
                continue
            if attr in ["start_on_lid_open", "allow_recording"]:
                # Convert value to "1" or "0" string
                str_value = "1" if value else "0"
                
                # Try to apply the setting
                result = self.write_value(attr, str_value)
                
                if result is True:
                    logging.info(f"Successfully applied saved setting: {attr}={str_value}")
                    applied_count += 1
                else:
                    failed_count += 1
                    logging.warning(f"Could not apply {attr}={str_value} to kernel module (Result: {result})")
                    if result == "permission_denied":
                        logging.warning(f"  -> Permission denied. Application may need to be run with sudo.")
                    elif result == "not_found":
                        logging.warning(f"  -> Attribute not found. Kernel module may not support this feature or is not loaded.")
        
        if applied_count > 0:
            logging.info(f"Applied {applied_count} saved settings successfully")

    # Battery history persistence (hourly samples)
    def battery_history_path(self):
        data_dir = GLib.get_user_data_dir()
        p = os.path.join(data_dir, "samsung-control")
        os.makedirs(p, exist_ok=True)
        return os.path.join(p, "battery_history.json")

    def load_battery_history(self):
        path = self.battery_history_path()
        try:
            if os.path.exists(path):
                with open(path, "r") as f:
                    data = json.load(f)
                    # Expect list of [timestamp, percent]
                    return [(float(t), int(v)) for t, v in data]
        except Exception as e:
            logging.debug(f"Could not load battery history: {e}")
        return []

    def save_battery_history(self, points):
        path = self.battery_history_path()
        try:
            with open(path, "w") as f:
                json.dump([[t, v] for t, v in points], f)
        except Exception as e:
            logging.debug(f"Could not save battery history: {e}")

    def add_hourly_battery_sample(self):
        # Called every hour to persist a battery percentage sample
        try:
            pct, _charging = self.read_battery_info()
            points = self.load_battery_history()
            points.append((time.time(), int(pct)))
            # Keep only last 48 hours safe-guard
            cutoff = time.time() - 48 * 3600
            points = [(t, v) for t, v in points if t >= cutoff]
            # Keep only last 24 hours for storage if desired
            if len(points) > 48:
                points = points[-48:]
            self.save_battery_history(points)
        except Exception as e:
            logging.debug(f"Error adding hourly battery sample: {e}")
        return True

    def get_last_24h_points(self):
        # Return 24 hourly points for the last 24 hours. If fewer exist, fill with current value
        points = self.load_battery_history()
        now = time.time()
        # Build target timestamps (each hour)
        hourly = []
        for i in range(24, 0, -1):
            ts = now - i * 3600
            hourly.append(ts)

        # Map history points into hourly buckets (closest sample)
        result = []
        for ts in hourly:
            # find closest point within +/- 1 hour
            candidates = [(abs(ts - t), v) for t, v in points]
            if candidates:
                candidates.sort()
                result.append((ts, candidates[0][1]))
            else:
                # fallback to current reading
                pct, _ = self.read_battery_info()
                result.append((ts, int(pct)))

        return result



    def update_power_profile(self):
        """Monitor and sync power profile changes from GNOME"""
        if self.power_profile_dropdown is None:
            return True
        
        try:
            current_profile = self.read_platform_profile()
            
            # If profile changed externally (in GNOME), update the dropdown
            if current_profile and current_profile != self.current_power_profile:
                logging.info(f"Power profile changed externally to: {current_profile}")
                self.current_power_profile = current_profile
                
                # Get available profiles from dropdown
                model = self.power_profile_dropdown.get_model()
                for i in range(len(model)):
                    if model[i].get_string() == current_profile:
                        # Disconnect signal temporarily to avoid triggering change event
                        self.power_profile_dropdown.disconnect_by_func(self.on_profile_changed)
                        self.power_profile_dropdown.set_selected(i)
                        self.power_profile_dropdown.connect("notify::selected", self.on_profile_changed)
                        break
        except Exception as e:
            logging.debug(f"Error updating power profile: {str(e)}")
        
        return True
