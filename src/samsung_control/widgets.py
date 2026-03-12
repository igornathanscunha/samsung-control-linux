import math
import time
from collections import deque

import cairo
from gi.repository import GLib, Gtk


class FanSpeedGraph(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.set_size_request(400, 200)
        self.set_draw_func(self.draw)
        self.data_points = deque(maxlen=60)
        self.max_speed = 3000

    def add_data_point(self, speed):
        current_time = time.time()
        self.data_points.append((current_time, speed))
        if speed > self.max_speed:
            self.max_speed = speed * 1.1
        self.queue_draw()

    def draw(self, area, cr, width, height, *args):
        cr.set_line_width(2)

        cr.set_source_rgba(0.1, 0.1, 0.1, 0.2)
        cr.paint()

        cr.set_source_rgba(0.3, 0.3, 0.3, 0.5)
        cr.set_line_width(0.5)

        for i in range(7):
            x = width * i / 6
            cr.move_to(x, 0)
            cr.line_to(x, height - 30)
            if i < 6:
                cr.move_to(x + 5, height - 10)
                cr.set_source_rgba(0.7, 0.7, 0.7, 0.8)
                cr.set_font_size(10)
                cr.show_text(f"{-60 + i*10}s")

        steps = 5
        for i in range(steps + 1):
            y = (height - 30) * i / steps
            cr.move_to(0, y)
            cr.line_to(width, y)
            cr.move_to(5, y + 15)
            cr.set_source_rgba(0.7, 0.7, 0.7, 0.8)
            cr.set_font_size(10)
            rpm = int(self.max_speed * (steps - i) / steps)
            cr.show_text(f"{rpm:,} RPM")

        cr.set_source_rgba(0.3, 0.3, 0.3, 0.5)
        cr.stroke()

        if not self.data_points:
            return

        cr.set_source_rgb(0.2, 0.4, 1.0)
        cr.set_line_width(2)

        current_time = time.time()
        points = [
            (
                width - (current_time - t) * (width / 60),
                (height - 30) - (v / self.max_speed) * (height - 30),
            )
            for t, v in self.data_points
        ]

        if len(points) > 1:
            cr.move_to(*points[0])
            for i in range(1, len(points)):
                if i < len(points) - 1:
                    x0, y0 = points[i - 1]
                    x1, y1 = points[i]
                    x2, y2 = points[i + 1]

                    cp1x = x0 + (x1 - x0) * 0.5
                    cp1y = y1
                    cp2x = x1 - (x2 - x1) * 0.5
                    cp2y = y1

                    cr.curve_to(cp1x, cp1y, cp2x, cp2y, x1, y1)
                else:
                    cr.line_to(*points[i])

            gradient = cairo.LinearGradient(0, 0, 0, height)
            gradient.add_color_stop_rgba(0, 0.2, 0.4, 1.0, 1)
            gradient.add_color_stop_rgba(1, 0.2, 0.4, 1.0, 0.1)
            cr.stroke_preserve()

            cr.line_to(points[-1][0], height)
            cr.line_to(points[0][0], height)
            cr.close_path()
            cr.set_source(gradient)
            cr.fill()


class TimeSeriesGraph(Gtk.DrawingArea):
    """General purpose time-series graph. Supports arbitrary time window in seconds."""

    def __init__(self, time_window=60, y_label="Value"):
        super().__init__()
        self.set_size_request(600, 220)
        self.set_draw_func(self.draw)
        self.time_window = time_window
        self.y_label = y_label
        self.data_points = deque()
        self.max_value = 100

    def set_time_window(self, seconds):
        self.time_window = seconds
        self.queue_draw()

    def set_points(self, points):
        """Replace dataset. Points: list of (timestamp, value)"""
        self.data_points = deque(points, maxlen=max(len(points), 1))
        vals = [v for _, v in self.data_points] if self.data_points else [1]
        self.max_value = max(max(vals), 1)
        self.queue_draw()

    def add_data_point(self, value):
        t = time.time()
        self.data_points.append((t, value))
        if value > self.max_value:
            self.max_value = value * 1.1
        self.queue_draw()

    def draw(self, area, cr, width, height, *args):
        cr.set_line_width(2)
        cr.set_source_rgba(0.08, 0.08, 0.08, 0.6)
        cr.paint()

        left_pad = 30
        right_pad = 40
        top_pad = 16
        bottom_pad = 40
        plot_width = max(1, width - left_pad - right_pad)
        plot_height = max(1, height - top_pad - bottom_pad)
        now = time.time()
        end_time = now
        if self.time_window >= 12 * 3600:
            tm = time.localtime(now)
            if tm.tm_min > 0 or tm.tm_sec > 0:
                end_time = time.mktime(
                    (tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour + 1, 0, 0, -1, -1, -1)
                )
        start_time = end_time - self.time_window

        cr.set_source_rgba(0.3, 0.3, 0.3, 0.6)
        cr.set_line_width(0.5)

        steps = 4
        for i in range(steps + 1):
            y = top_pad + plot_height * i / steps
            cr.move_to(left_pad, y)
            cr.line_to(left_pad + plot_width, y)
            cr.stroke()
            cr.set_font_size(10)
            cr.set_source_rgba(0.7, 0.7, 0.7, 0.8)
            val = int(self.max_value * (steps - i) / steps)
            if self.max_value == 100:
                cr.move_to(left_pad + plot_width + 5, y + 4)
                cr.show_text(f"{val}%")
            else:
                cr.move_to(left_pad + plot_width + 5, y + 4)
                cr.show_text(f"{val}")

        if not self.data_points:
            return

        points = []
        for t, v in self.data_points:
            if t < start_time or t > end_time:
                continue
            x = left_pad + (t - start_time) * (plot_width / max(self.time_window, 1))
            y = top_pad + plot_height - (v / max(self.max_value, 1)) * plot_height
            if x >= left_pad:
                points.append((x, y))

        if len(points) < 1:
            return

        cr.set_source_rgb(0.15, 0.7, 0.5)
        cr.set_line_width(2)
        cr.move_to(*points[0])
        for p in points[1:]:
            cr.line_to(*p)
        cr.stroke()

        if len(points) > 1:
            cr.line_to(points[-1][0], top_pad + plot_height)
            cr.line_to(points[0][0], top_pad + plot_height)
            cr.close_path()
            grad = cairo.LinearGradient(0, 0, 0, height)
            grad.add_color_stop_rgba(0, 0.2, 0.6, 0.4, 0.8)
            grad.add_color_stop_rgba(1, 0.2, 0.6, 0.4, 0.05)
            cr.set_source(grad)
            cr.fill()

        cr.set_font_size(10)
        cr.set_source_rgba(0.7, 0.7, 0.7, 0.8)
        if self.time_window >= 12 * 3600:
            hours_span = max(1, int(self.time_window // 3600))
            for i in range(0, hours_span + 1, 1):
                label_time = start_time + (i * 3600)
                hour = time.localtime(label_time).tm_hour
                x = left_pad + plot_width * i / hours_span
                cr.move_to(x - 10, top_pad + plot_height + 25)
                cr.show_text(f"{hour}h")


class FanIcon(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.set_size_request(50, 50)
        self.set_draw_func(self.draw)
        self.rotation = 0
        self.target_speed = 0
        self.current_speed = 0
        GLib.timeout_add(16, self.update_rotation)

    def set_speed(self, speed):
        self.target_speed = (speed / 60) * (16 / 1000) * 2 * math.pi

    def update_rotation(self):
        self.current_speed += (self.target_speed - self.current_speed) * 0.1
        self.rotation += self.current_speed
        self.queue_draw()
        return True

    def draw(self, area, cr, width, height, *args):
        cr.set_source_rgb(0.2, 0.4, 1.0)
        cr.translate(width / 2, height / 2)
        cr.rotate(self.rotation)

        cr.arc(0, 0, 3, 0, 2 * math.pi)
        cr.fill()

        for i in range(4):
            cr.save()
            cr.rotate(i * math.pi / 2)
            cr.move_to(0, -3)
            cr.curve_to(8, -8, 12, -15, 0, -20)
            cr.curve_to(-12, -15, -8, -8, 0, -3)
            cr.fill()
            cr.restore()


class BatteryIcon(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.set_size_request(50, 50)
        self.set_draw_func(self.draw)
        self.percentage = 0
        self.charging = False

    def update(self, percentage, charging):
        self.percentage = percentage
        self.charging = charging
        self.queue_draw()

    def draw(self, area, cr, width, height, *args):
        cr.scale(2.0, 2.0)

        cr.set_source_rgb(0.2, 0.4, 1.0)
        cr.set_line_width(2)

        cr.rectangle(2, 6, 16, 12)
        cr.stroke()

        cr.rectangle(18, 9, 4, 6)
        cr.fill()

        if self.percentage > 0:
            fill_width = max(1, (self.percentage / 100) * 14)
            cr.rectangle(3, 7, fill_width, 10)

            if self.percentage <= 20:
                cr.set_source_rgb(0.8, 0.2, 0.2)
            elif self.percentage <= 50:
                cr.set_source_rgb(0.8, 0.8, 0.2)
            else:
                cr.set_source_rgb(0.2, 0.8, 0.2)
            cr.fill()

        if self.charging:
            cr.set_source_rgb(1, 1, 1)
            cr.move_to(8, 14)
            cr.line_to(12, 10)
            cr.line_to(10, 10)
            cr.line_to(12, 6)
            cr.line_to(8, 10)
            cr.line_to(10, 10)
            cr.close_path()
            cr.fill()


class CPUIcon(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.set_size_request(50, 50)
        self.set_draw_func(self.draw)
        self.usage = 0
        self.pulse = 0
        GLib.timeout_add(16, self.update_pulse)

    def set_usage(self, usage_str):
        try:
            self.usage = float(usage_str.rstrip("%")) / 100.0
        except Exception:
            self.usage = 0
        self.queue_draw()

    def update_pulse(self):
        self.pulse = (self.pulse + 0.05) % (2 * math.pi)
        self.queue_draw()
        return True

    def draw(self, area, cr, width, height, *args):
        cr.translate(width / 2, height / 2)
        scale = min(width, height) / 50.0
        cr.scale(scale, scale)

        cr.set_source_rgb(0.2, 0.4, 1.0)
        cr.set_line_width(2)

        size = 20
        radius = 4
        x = -size
        y = -size

        cr.new_path()
        cr.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
        cr.arc(x + 2 * size - radius, y + radius, radius, 3 * math.pi / 2, 0)
        cr.arc(x + 2 * size - radius, y + 2 * size - radius, radius, 0, math.pi / 2)
        cr.arc(x + radius, y + 2 * size - radius, radius, math.pi / 2, math.pi)
        cr.close_path()
        cr.stroke()

        inner_size = 14
        inner_x = -inner_size
        inner_y = -inner_size
        cr.set_source_rgba(0.2, 0.4, 1.0, 0.2 + 0.3 * math.sin(self.pulse))
        cr.rectangle(inner_x, inner_y, inner_size * 2, inner_size * 2)
        cr.fill()

        cr.set_source_rgb(0.2, 0.4, 1.0)
        for i in range(4):
            cr.save()
            cr.rotate(i * math.pi / 2)
            cr.move_to(0, -size - 4)
            cr.line_to(0, -size - 10)
            cr.stroke()
            cr.restore()

        usage = int(self.usage * 100)
        sections = 4
        filled_sections = math.ceil(self.usage * sections * sections)
        section_size = size / 2

        cr.set_source_rgb(0.2, 0.4, 1.0)
        for i in range(sections):
            for j in range(sections):
                if (i * sections + j) < filled_sections:
                    cr.rectangle(
                        -size + i * section_size + 2,
                        -size + j * section_size + 2,
                        section_size - 4,
                        section_size - 4,
                    )
        cr.fill()

        cr.set_source_rgba(0.2, 0.4, 1.0, 0.3 + 0.2 * math.sin(self.pulse))
        cr.set_line_width(2)
        cr.rectangle(-size - 4, -size - 4, size * 2 + 8, size * 2 + 8)
        cr.stroke()
