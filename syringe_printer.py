"""
Syringe printer control module.

This gathers everything that used to live directly inside stage.ipynb
(configuration, serial connection, syringe pump + stage movement logic)
into a single, ordered .py file, plus an ipywidgets control panel similar
in spirit to enderscope.Panel to drive it interactively from a notebook.

Typical notebook usage:

    from syringe_printer import SyringePrinterController, PrintControlPanel

    controller = SyringePrinterController()
    panel = PrintControlPanel(controller)   # pick ports and click "Connect"
"""

import time
import threading
from math import ceil
from typing import Optional

import serial
import serial.tools.list_ports
from ipywidgets import widgets, Button, Layout, Output, FloatText, IntText, Dropdown, GridspecLayout, ToggleButtons
from IPython.display import display

from enderscope import SerialUtils, Stage


class _StageAborted(Exception):
    """Raised internally when emergency_stop() was triggered (e.g. from the
    STOP button, on another thread) while a move sequence is in progress, so
    the sequence unwinds immediately instead of sending further moves."""


class SyringePrinterController:
    """
    Owns the stage + syringe pump serial connections and all the movement /
    extrusion logic (purge, print_rectangle, emergency_stop, ...).

    All the tunable parameters that used to be plain notebook variables are
    now instance attributes, so they can be read/edited live (e.g. from
    PrintControlPanel) without touching this file.
    """

    def __init__(
        self,
        rectangle=(20, 15),           # in mm, x and y
        printer_print_speed=180,      # in mm/mn
        printer_jog_speed=1800,       # in mm/mn
        width_extrusion=2.5,          # in mm
        number_layer=1,
        layer_thickness=2,            # in mm
        syringe_purge_time=8,         # in seconds, time for the purge
        syringe_extrusion_speed=10,   # in steps per second
        horizontal_offset=12.86,      # in mm, horizontal distance between two holes
        vertical_offset=13.6,         # in mm, vertical distance between two holes
        clearance_height=10,          # in mm, z lift between two prints
    ):
        self.rectangle = list(rectangle)
        self.printer_print_speed = printer_print_speed
        self.printer_jog_speed = printer_jog_speed
        self.width_extrusion = width_extrusion
        self.number_layer = number_layer
        self.layer_thickness = layer_thickness
        self.syringe_purge_time = syringe_purge_time
        self.syringe_extrusion_speed = syringe_extrusion_speed
        self.horizontal_offset = horizontal_offset
        self.vertical_offset = vertical_offset
        self.clearance_height = clearance_height

        self.stage: Optional[Stage] = None
        self.syringe_pump: Optional[serial.Serial] = None

        # Set by emergency_stop(); cleared at the start of purge()/print_rectangle().
        # Used both to wake up an interruptible wait (purge) and as a
        # cooperative "stop as soon as possible" flag checked between moves,
        # so a background-thread action (e.g. triggered from a button) can
        # also be halted, not just a KeyboardInterrupt in a notebook cell.
        self._abort = threading.Event()

    @property
    def number_passages(self):
        # computes the smallest integer that is greater than or equal to x.
        return min(
            ceil(self.rectangle[0] / (2 * self.width_extrusion)),
            ceil(self.rectangle[1] / (2 * self.width_extrusion)),
        )

    # --- Connection -----------------------------------------------------

    # USB vendor ids used to auto-detect which physical port is which on this
    # rig: the stage board exposes a generic CH340 USB-serial adapter, while
    # the syringe pump runs on a genuine Arduino Uno (see platformio.ini).
    _PRINTER_VID = 0x1A86  # CH340
    _SYRINGE_VID = 0x2341  # Arduino Uno

    def list_ports(self):
        return SerialUtils.serial_ports()

    @classmethod
    def detect_ports(cls):
        """Best-effort auto-detection of the printer / syringe pump ports,
        based on their USB vendor id. Returns {'printer': device_or_None,
        'syringe': device_or_None}."""
        detected = {'printer': None, 'syringe': None}
        for info in serial.tools.list_ports.comports():
            if info.vid == cls._PRINTER_VID and detected['printer'] is None:
                detected['printer'] = info.device
            elif info.vid == cls._SYRINGE_VID and detected['syringe'] is None:
                detected['syringe'] = info.device
        return detected

    def connect(self, printer_port, syringe_pump_port, baudrate=115200):
        self.stage = Stage(printer_port, baudrate)
        self.syringe_pump = serial.Serial(port=syringe_pump_port, baudrate=baudrate, timeout=0.01, writeTimeout=1)

    def disconnect(self):
        if self.syringe_pump is not None:
            self.syringe_pump.close()
            self.syringe_pump = None
        if self.stage is not None:
            self.stage.serial.close()
            self.stage = None

    @property
    def is_connected(self):
        return self.stage is not None and self.syringe_pump is not None

    # --- Manual jogging ---------------------------------------------------

    def home(self):
        """Home the X and Y axes only (Z is not homed on this rig)."""
        self.stage.write_code("G28 X Y")

    def jog(self, axis, distance):
        """Relative move along a single axis ('x', 'y' or 'z'), in mm (can be negative)."""
        self.stage.set_speed(self.printer_jog_speed)
        self.stage.move_axis(axis, distance)

    def move_to(self, x, y, z=None):
        """Absolute move to the given X, Y position, in mm. Z is left
        untouched unless explicitly given."""
        self.stage.set_speed(self.printer_jog_speed)
        self.stage.move_absolute(x, y, z)

    def move_z_to(self, z):
        """Absolute move of the Z axis only, in mm."""
        self.stage.set_speed(self.printer_jog_speed)
        self.stage.set_absolute()
        self.stage.write_code(f"G0 Z {z}")

    def set_z_zero(self):
        """Zero the Z axis at the current physical position (G92 Z0)."""
        self.stage.write_code("G92 Z0")

    def _check_abort(self):
        """Raise _StageAborted if emergency_stop() was requested. Called
        before every move in a multi-move sequence (rectangle passages,
        positioning) so those sequences stop as soon as possible instead of
        only in between passages/layers."""
        if self._abort.is_set():
            raise _StageAborted()

    def _move_axis(self, axis, distance):
        self._check_abort()
        self.stage.move_axis(axis, distance)

    # --- Syringe pump : messages -----------------------------------------

    def start_extrusion(self, letter_syringe_pump):
        message = f"{letter_syringe_pump}\n".encode('utf8')
        self.syringe_pump.write(message)

    def stop_extrusion(self):
        message = b"S\n"
        self.syringe_pump.write(message)

    def set_extrusion_speed(self, speed):  # in steps per second
        message = f"V{speed}\n".encode('utf8')
        self.syringe_pump.write(message)

    # --- Emergency stop : stops all stage and syringe pump movements -----

    def emergency_stop(self):
        """Immediately stop the stage and the syringe pump.

        Call this (e.g. from a KeyboardInterrupt handler, or the panel's
        STOP button) to halt any ongoing movement as fast as possible.
        """
        self._abort.set()

        try:
            self.stop_extrusion()  # stop the syringe pump extrusion
        except Exception as e:
            print(f"Error while stopping the syringe pump: {e}")

        try:
            self.stage.serial.write(b"M410\n")  # Quickstop: halts stage motion immediately
        except Exception as e:
            print(f"Error while stopping the stage: {e}")

        print("Emergency stop: stage and syringe pump have been stopped.")

    # --- Purge -------------------------------------------------------------

    def _purge_syringe(self, letter_syringe_pump, stop_event):
        self.set_extrusion_speed(self.syringe_extrusion_speed)
        self.start_extrusion(letter_syringe_pump)
        stop_event.wait(self.syringe_purge_time)  # interruptible wait for the purge duration
        self.stop_extrusion()
        time.sleep(1)

    def purge(self, letter_syringe_pump='A'):
        self._abort.clear()
        thread = threading.Thread(target=self._purge_syringe, args=(letter_syringe_pump, self._abort), daemon=True)
        thread.start()
        try:
            thread.join()
        except KeyboardInterrupt:
            self.emergency_stop()  # sets self._abort, wakes up the waiting thread
            thread.join()
            raise

    # --- Rectangle printing --------------------------------------------------

    def _draw_rectangle(self):
        x_rectangle = self.rectangle[0]
        y_rectangle = self.rectangle[1]

        for _ in range(self.number_passages):
            self._check_abort()

            self.stage.set_speed(self.printer_print_speed)
            self._move_axis('x', x_rectangle)
            self._move_axis('y', y_rectangle)
            self._move_axis('x', -x_rectangle)
            self._move_axis('y', -(y_rectangle - self.width_extrusion))

            self._move_axis('x', self.width_extrusion)

            x_rectangle -= 2 * self.width_extrusion  # The next inner rectangle will have two fewer layers
            y_rectangle -= 2 * self.width_extrusion

        self._check_abort()
        self.stage.write_code("M400")

    def _draw_rectangle_single_pass(self, letter_syringe_pump):
        x_rectangle = self.rectangle[0]
        y_rectangle = self.rectangle[1]

        self._check_abort()

        self.stage.set_speed(self.printer_print_speed)
        self._move_axis('x', x_rectangle)
        self._move_axis('y', y_rectangle)
        self._move_axis('x', -x_rectangle)
        self._move_axis('y', -y_rectangle)

        self._check_abort()
        self.stage.write_code("M400")
        self.stop_extrusion()

        self._move_axis('z', self.clearance_height)  # move up for clearance

        self._fill_rectangle(letter_syringe_pump)

    def _fill_rectangle(self, letter_syringe_pump):
        x_len, y_len = self.rectangle
        if x_len >= y_len:
            length_axis, width_axis, length, width = 'x', 'y', x_len, y_len
        else:
            length_axis, width_axis, length, width = 'y', 'x', y_len, x_len

        # Travel (nozzle still lifted) to the start of the centerline:
        # centered across the width, inset from the starting edge by the
        # thickness of the border already printed.
        self.stage.set_speed(self.printer_jog_speed)
        self._move_axis(width_axis, width / 2)
        self._move_axis(length_axis, self.width_extrusion)
        self._move_axis('z', -self.clearance_height)  # back down to print level

        # Boost the extrusion rate so the single central pass spreads out
        # sideways and fills the whole interior width in one go.
        fill_extrusion_speed = self.syringe_extrusion_speed * (width / self.width_extrusion)
        self.set_extrusion_speed(fill_extrusion_speed)
        self.start_extrusion(letter_syringe_pump)
        self.stage.set_speed(self.printer_print_speed)
        self._move_axis(length_axis, length - 2 * self.width_extrusion)

        self._check_abort()
        self.stage.write_code("M400")

    def _draw_line(self, length, axis):
        self.stage.set_speed(self.printer_print_speed)
        self._move_axis(axis, length)
        self._check_abort()
        self.stage.write_code("M400")

    def go_to_start_position(self, letter_syringe_pump):
        """Go to the start position and adjust according to the syringe pump."""
        self.stage.set_speed(self.printer_jog_speed)

        if letter_syringe_pump == 'A':
            pass

        elif letter_syringe_pump == 'B':
            self._move_axis('x', self.rectangle[0] - self.horizontal_offset)  # horizontal distance between A and B
            self._move_axis('z', -(self.number_layer - 1) * self.layer_thickness)  # back to the initial z
            self.stage.write_code("M400")

        elif letter_syringe_pump == 'C':
            self._move_axis('x', self.rectangle[0] + self.horizontal_offset)
            self._move_axis('y', -self.vertical_offset)  # vertical distance between B and C
            self._move_axis('z', -(self.number_layer - 1) * self.layer_thickness)  # back to the initial z
            self.stage.write_code("M400")

    def print_rectangle(self, letter_syringe_pump):
        self._abort.clear()

        try:
            self.go_to_start_position(letter_syringe_pump)
            for i in range(self.number_layer):  # Repeat for each layer
                self._check_abort()
                if i > 1:
                    self._move_axis('z', self.layer_thickness)  # each new layer increases the height
                self.set_extrusion_speed(self.syringe_extrusion_speed)
                self.start_extrusion(letter_syringe_pump)
                self._draw_rectangle_single_pass(letter_syringe_pump)
                self.stop_extrusion()
                self.stage.set_speed(self.printer_jog_speed)  # mm/mn
                self._move_axis('z', self.clearance_height)  # move up for clearance

        except KeyboardInterrupt:
            self.emergency_stop()  # stop the stage and syringe pump if execution is killed
            raise
        except _StageAborted:
            pass  # emergency_stop() already stopped the stage and syringe pump

    def print_line(self, letter_syringe_pump, length, axis):
        self._abort.clear()

        try:
            self.set_extrusion_speed(self.syringe_extrusion_speed)
            self.start_extrusion(letter_syringe_pump)
            self._draw_line(length, axis)
            self.stop_extrusion()
            self.stage.set_speed(self.printer_jog_speed)  # mm/mn
            self._move_axis('z', self.clearance_height)  # move up for clearance

        except KeyboardInterrupt:
            self.emergency_stop()  # stop the stage and syringe pump if execution is killed
            raise
        except _StageAborted:
            pass  # emergency_stop() already stopped the stage and syringe pump


class PrintControlPanel:
    """
    ipywidgets control panel for a SyringePrinterController, in the same
    spirit as enderscope.Panel: buttons + live status output, but geared
    towards the purge / print-rectangle workflow instead of jogging.

    Long-running actions (purge, print) run in a background thread so the
    STOP button stays responsive and can trigger emergency_stop() at any time.
    """

    def __init__(self, controller: SyringePrinterController):
        self.controller = controller
        self.output = Output()
        self._busy = False

        # --- Connection widgets ---
        ports = controller.list_ports()
        self.printer_port_w = Dropdown(options=ports, description='Printer port')
        self.syringe_port_w = Dropdown(options=ports, description='Syringe port')
        self._auto_select_ports(ports)
        self.refresh_btn = self._make_button('Refresh ports', 'lightgrey', self._on_refresh_ports)
        self.connect_btn = self._make_button('Connect', 'lightyellow', self._on_connect)
        connection_box = widgets.HBox(
            [self.printer_port_w, self.syringe_port_w, self.refresh_btn, self.connect_btn]
        )

        # --- Current position display ---
        self.position_w = widgets.HTML(value="<b>Position:</b> not connected")
        self.refresh_position_btn = self._make_button('Refresh position', 'lightgrey', self._on_refresh_position)
        position_box = widgets.HBox([self.position_w, self.refresh_position_btn])

        # --- Parameter widgets ---
        c = controller
        self.rect_x_w = FloatText(value=c.rectangle[0], description='Rect X (mm)', layout=Layout(width='160px'))
        self.rect_y_w = FloatText(value=c.rectangle[1], description='Rect Y (mm)', layout=Layout(width='160px'))
        self.print_speed_w = FloatText(value=c.printer_print_speed, description='Print speed', layout=Layout(width='160px'))
        self.jog_speed_w = FloatText(value=c.printer_jog_speed, description='Jog speed', layout=Layout(width='160px'))
        self.extrusion_speed_w = IntText(value=c.syringe_extrusion_speed, description='Extrusion speed', layout=Layout(width='160px'))
        self.purge_time_w = IntText(value=c.syringe_purge_time, description='Purge time (s)', layout=Layout(width='160px'))
        self.number_layer_w = IntText(value=c.number_layer, description='Layers', layout=Layout(width='160px'))

        for w in (self.rect_x_w, self.rect_y_w, self.print_speed_w, self.jog_speed_w,
                  self.extrusion_speed_w, self.purge_time_w, self.number_layer_w):
            w.observe(self._on_param_changed, names='value')

        params_box = widgets.VBox([
            self.rect_x_w, self.rect_y_w, self.print_speed_w, self.jog_speed_w,
            self.extrusion_speed_w, self.purge_time_w, self.number_layer_w,
        ])

        # --- Manual jog widgets (relative move) ---
        self.step_w = ToggleButtons(options=['0.1', '1', '10'], value='1', description='Step (mm)')

        jog_grid = GridspecLayout(3, 3, height='140px', width='140px')
        jog_grid[0, 1] = self._make_jog_button('Y+', 'palegreen', 'y', 1)
        jog_grid[1, 0] = self._make_jog_button('X-', 'palegreen', 'x', -1)
        jog_grid[1, 2] = self._make_jog_button('X+', 'palegreen', 'x', 1)
        jog_grid[2, 1] = self._make_jog_button('Y-', 'palegreen', 'y', -1)
        z_box = widgets.VBox([
            self._make_jog_button('Z+', 'paleturquoise', 'z', 1),
            self._make_jog_button('Z-', 'paleturquoise', 'z', -1),
        ])
        jog_box = widgets.VBox([self.step_w, widgets.HBox([jog_grid, z_box])])

        # --- Absolute move widgets ---
        self.abs_x_w = FloatText(description='X (mm)', layout=Layout(width='150px'))
        self.abs_y_w = FloatText(description='Y (mm)', layout=Layout(width='150px'))
        self.go_abs_btn = self._make_button('Go to position', 'khaki', self._on_go_absolute)
        self.set_z0_btn = self._make_button('Set Z0', 'khaki', self._on_set_z0)
        self.goto_z0_btn = self._make_button('Go to Z0', 'khaki', self._on_goto_z0)
        abs_box = widgets.VBox([
            self.abs_x_w, self.abs_y_w, self.go_abs_btn, self.set_z0_btn, self.goto_z0_btn,
        ])

        movement_section = self._section(
            'Movement',
            widgets.HBox([
                widgets.VBox([widgets.HTML('<i>Jog (relative)</i>'), jog_box]),
                widgets.VBox([widgets.HTML('<i>Go to (absolute)</i>'), abs_box]),
            ]),
        )

        # --- Single line print widgets ---
        self.line_length_w = FloatText(value=20, description='Length (mm)', layout=Layout(width='160px'))
        self.line_axis_w = ToggleButtons(options=['x', 'y'], value='x', description='Axis')
        self.print_line_a_btn = self._make_button('Line A', 'palegreen', lambda b: self._on_print_line('A'))
        self.print_line_b_btn = self._make_button('Line B', 'palegreen', lambda b: self._on_print_line('B'))
        self.print_line_c_btn = self._make_button('Line C', 'palegreen', lambda b: self._on_print_line('C'))
        line_section = self._section(
            'Print single line',
            self.line_length_w, self.line_axis_w,
            widgets.HBox([self.print_line_a_btn, self.print_line_b_btn, self.print_line_c_btn]),
        )

        # --- Rectangle printing buttons ---
        self.print_a_btn = self._make_button('Print A', 'palegreen', lambda b: self._on_print('A'))
        self.print_b_btn = self._make_button('Print B', 'palegreen', lambda b: self._on_print('B'))
        self.print_c_btn = self._make_button('Print C', 'palegreen', lambda b: self._on_print('C'))
        rect_section = self._section(
            'Print rectangle',
            widgets.HBox([self.print_a_btn, self.print_b_btn, self.print_c_btn]),
        )

        # --- General actions + emergency stop ---
        self.home_btn = self._make_button('Home', 'lightyellow', self._on_home)
        self.purge_btn = self._make_button('Purge', 'paleturquoise', self._on_purge)
        self.stop_btn = self._make_button('STOP', 'salmon', self._on_stop)
        self.stop_btn.layout = Layout(height='50px', width='160px')
        general_section = self._section(
            'General',
            widgets.HBox([self.home_btn, self.purge_btn]),
        )

        self._action_buttons = [
            self.home_btn, self.purge_btn, self.print_a_btn, self.print_b_btn, self.print_c_btn,
            self.print_line_a_btn, self.print_line_b_btn, self.print_line_c_btn,
        ] + self._jog_buttons + [self.go_abs_btn, self.set_z0_btn, self.goto_z0_btn]

        self._set_actions_enabled(False)  # disabled until connected

        self.layout = widgets.VBox([
            self._section('Connection', connection_box),
            self._section('Position', position_box),
            self._section('Parameters', params_box),
            movement_section,
            widgets.HBox([rect_section, line_section]),
            widgets.HBox([general_section, self.stop_btn]),
        ])

        display(self.layout, self.output)

    # --- Helpers ----------------------------------------------------------

    def _auto_select_ports(self, ports):
        """Preselect the printer/syringe dropdowns using USB-vid detection,
        so they don't both default to the same first port."""
        detected = self.controller.detect_ports()
        if detected['printer'] in ports:
            self.printer_port_w.value = detected['printer']
        if detected['syringe'] in ports:
            self.syringe_port_w.value = detected['syringe']

    def _make_button(self, description, color, handler):
        b = Button(description=description, style=dict(button_color=color),
                   layout=Layout(height='36px', width='120px'))
        b.on_click(handler)
        return b

    def _section(self, title, *children):
        """Wrap widgets in a titled, bordered box so the panel reads as
        clearly separated groups instead of one big undifferentiated block."""
        return widgets.VBox(
            [widgets.HTML(f"<b>{title}</b>")] + list(children),
            layout=Layout(border='1px solid lightgray', padding='6px', margin='3px'),
        )

    def _make_jog_button(self, description, color, axis, sign):
        b = Button(description=description, style=dict(button_color=color),
                   layout=Layout(height='40px', width='40px'))
        b.on_click(lambda btn: self._on_jog(axis, sign))
        if not hasattr(self, '_jog_buttons'):
            self._jog_buttons = []
        self._jog_buttons.append(b)
        return b

    def _set_actions_enabled(self, enabled):
        for b in self._action_buttons:
            b.disabled = not enabled

    def _log(self, message):
        with self.output:
            self.output.clear_output()
            print(message)

    def _refresh_position(self):
        if not self.controller.is_connected:
            self.position_w.value = "<b>Position:</b> not connected"
            return
        try:
            pos = self.controller.stage.get_position(dict=True)
            self.position_w.value = f"<b>Position:</b> X={pos['X']:.2f}  Y={pos['Y']:.2f}  Z={pos['Z']:.2f}"
        except Exception as e:
            self.position_w.value = f"<b>Position:</b> error ({e})"

    def _run_in_background(self, target, *args):
        if self._busy:
            self._log("Already busy with another action, please wait (or press STOP).")
            return
        self._busy = True
        self._set_actions_enabled(False)

        def runner():
            try:
                target(*args)
            except Exception as e:
                self._log(f"Error: {e}")
            finally:
                self._busy = False
                self._set_actions_enabled(True)

        threading.Thread(target=runner, daemon=True).start()

    # --- Widget callbacks ---------------------------------------------------

    def _on_refresh_ports(self, b):
        ports = self.controller.list_ports()
        self.printer_port_w.options = ports
        self.syringe_port_w.options = ports
        self._auto_select_ports(ports)

    def _on_refresh_position(self, b):
        self._refresh_position()

    def _on_connect(self, b):
        if not self.printer_port_w.value or not self.syringe_port_w.value:
            self._log("Select both a printer port and a syringe pump port first.")
            return
        self._log("Connecting...")
        self.controller.connect(self.printer_port_w.value, self.syringe_port_w.value)
        self._set_actions_enabled(True)
        self._log("Connected.")
        self._refresh_position()

    def _on_param_changed(self, change):
        c = self.controller
        c.rectangle = [self.rect_x_w.value, self.rect_y_w.value]
        c.printer_print_speed = self.print_speed_w.value
        c.printer_jog_speed = self.jog_speed_w.value
        c.syringe_extrusion_speed = self.extrusion_speed_w.value
        c.syringe_purge_time = self.purge_time_w.value
        c.number_layer = self.number_layer_w.value

    def _on_home(self, b):
        def action():
            self._log("Homing X and Y...")
            self.controller.home()
            self.controller.stage.finish_moves()
            self._log(self.controller.stage.get_position(dict=True))
            self._refresh_position()
        self._run_in_background(action)

    def _on_purge(self, b):
        def action():
            self._log("Purging...")
            self.controller.purge('A')
            self._log("Purge done.")
        self._run_in_background(action)

    def _on_print(self, letter_syringe_pump):
        def action():
            c = self.controller
            pos_before = c.stage.get_position(dict=True)
            self._log(
                f"Printing rectangle {letter_syringe_pump}... "
                f"rect={c.rectangle} print_speed={c.printer_print_speed} "
                f"jog_speed={c.printer_jog_speed} width_extrusion={c.width_extrusion} "
                f"pos_before={pos_before}"
            )
            self.controller.print_rectangle(letter_syringe_pump)
            pos_after = c.stage.get_position(dict=True)
            self._log(f"Rectangle {letter_syringe_pump} done. pos_after={pos_after}")
            self._refresh_position()
        self._run_in_background(action)

    def _on_print_line(self, letter_syringe_pump):
        length = self.line_length_w.value
        axis = self.line_axis_w.value

        def action():
            self._log(f"Printing line {letter_syringe_pump} ({axis.upper()}, {length} mm)...")
            self.controller.print_line(letter_syringe_pump, length, axis)
            self._log(f"Line {letter_syringe_pump} done.")
            self._refresh_position()
        self._run_in_background(action)

    def _on_jog(self, axis, sign):
        step = float(self.step_w.value)
        distance = sign * step

        def action():
            self.controller.jog(axis, distance)
            self._log(f"Moved {axis.upper()} by {distance:+g} mm. Position: {self.controller.stage.get_position(dict=True)}")
            self._refresh_position()
        self._run_in_background(action)

    def _on_go_absolute(self, b):
        x, y = self.abs_x_w.value, self.abs_y_w.value

        def action():
            self._log(f"Moving to X={x} Y={y} (absolute)...")
            self.controller.move_to(x, y)
            self._log(self.controller.stage.get_position(dict=True))
            self._refresh_position()
        self._run_in_background(action)

    def _on_set_z0(self, b):
        def action():
            self.controller.set_z_zero()
            self._log("Z axis zeroed (current position set as Z0).")
            self._refresh_position()
        self._run_in_background(action)

    def _on_goto_z0(self, b):
        def action():
            self._log("Moving to Z=0 (absolute)...")
            self.controller.move_z_to(0)
            self._log(self.controller.stage.get_position(dict=True))
            self._refresh_position()
        self._run_in_background(action)

    def _on_stop(self, b):
        if not self.controller.is_connected:
            self._log("Not connected.")
            return
        self.controller.emergency_stop()
        self._log("EMERGENCY STOP triggered.")
        self._refresh_position()