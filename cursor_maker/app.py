"""GTK4 GUI for Cursor Maker."""

import copy
import os
import subprocess
import threading

import cairo
from PIL import Image
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import cursor_icons, imaging, importer, segment, video  # noqa: E402
from .imaging import MASTER_SIZE  # noqa: E402
from .project import (MAX_FRAMES, TRANS_COLOR, TRANS_FLOOD, TRANS_NONE,  # noqa: E402
                      TRANS_OBJECT, WARN_FRAMES, Frame, ImageCache, Project, RoleConfig,
                      add_overlays, apply_transparency, crop_key, crop_points, frame_crop,
                      frame_list, frame_points, mask_ready, render_crop)
from .roles import ROLE_BY_NAME, ROLES  # noqa: E402
from .theme import (EXPORT_SIZES, Cancelled, ThemeExistsError, build_theme,  # noqa: E402
                    export_platform, theme_dir)
from .xcursor import argb_premultiplied_bytes  # noqa: E402

AUTOSAVE = os.path.join(GLib.get_user_config_dir(), "cursor-maker", "autosave.json")
ZOOMS = [("Fit", None), ("25%", 0.25), ("50%", 0.5), ("100%", 1.0),
         ("200%", 2.0), ("400%", 4.0), ("800%", 8.0)]
MIN_SIDE = 8
HANDLE = 8         # corner handle grab radius in widget pixels
DRAW_THRESHOLD = 4  # widget pixels before a click outside starts drawing
PREVIEW_SIZES = [192, 128, 96]
# 3x3 direction picker layout (None = empty center cell)
ARROW_GRID = [[("up-left", "↖"), ("up", "↑"), ("up-right", "↗")],
              [("left", "←"), None, ("right", "→")],
              [("down-left", "↙"), ("down", "↓"), ("down-right", "↘")]]
ICON_DIRS = [os.path.expanduser("~/.icons"), os.path.expanduser("~/.local/share/icons"),
             "/usr/share/icons"]


def pil_to_surface(img):
    """RGBA PIL image -> (cairo.ImageSurface, backing buffer)."""
    w, h = img.size
    buf = bytearray(argb_premultiplied_bytes(img))
    surf = cairo.ImageSurface.create_for_data(buf, cairo.FORMAT_ARGB32, w, h, w * 4)
    return surf, buf


def draw_checker(cr, w, h, cell=16):
    cr.set_source_rgb(0.8, 0.8, 0.8)
    cr.rectangle(0, 0, w, h)
    cr.fill()
    cr.set_source_rgb(0.6, 0.6, 0.6)
    for y in range(0, h, cell):
        for x in range((y // cell) % 2 * cell, w, cell * 2):
            cr.rectangle(x, y, cell, cell)
    cr.fill()


def draw_hotspot(cr, x, y, radius=6):
    cr.set_line_width(1)
    for color, off in (((0, 0, 0), 1), ((1, 0.1, 0.1), 0)):
        cr.set_source_rgb(*color)
        cr.move_to(x - radius - 4 + off, y + 0.5 + off)
        cr.line_to(x + radius + 4 + off, y + 0.5 + off)
        cr.move_to(x + 0.5 + off, y - radius - 4 + off)
        cr.line_to(x + 0.5 + off, y + radius + 4 + off)
        cr.stroke()
        cr.arc(x + 0.5 + off, y + 0.5 + off, radius, 0, 6.2832)
        cr.stroke()


class WrapRow(Gtk.FlowBox):
    """A row of controls that wraps onto more lines when the window is narrow.

    Used instead of a horizontal Gtk.Box so no toolbar forces a minimum window
    width. A label is kept together with the widget appended right after it
    (e.g. "Size" + its spin button), so pairs never get split across lines.
    """

    def __init__(self, spacing=6):
        super().__init__(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False,
                         max_children_per_line=100, column_spacing=spacing, row_spacing=4,
                         valign=Gtk.Align.START)
        self._last_label = None

    def append(self, widget):
        widget.set_halign(Gtk.Align.START)       # keep natural width, don't stretch
        if isinstance(widget, Gtk.Label):
            Gtk.FlowBox.append(self, widget)
            self._last_label = widget
            return
        label, self._last_label = self._last_label, None
        if label is None:
            Gtk.FlowBox.append(self, widget)
            return
        child = label.get_parent()               # the label's FlowBoxChild
        index = child.get_index()
        child.set_child(None)                    # detach the label, then drop its slot
        self.remove(child)
        pair = Gtk.Box(spacing=4, halign=Gtk.Align.START)
        pair.append(label)
        pair.append(widget)
        self.insert(pair, index)


class Selection:
    """The square selection the editor is working on.

    Reads/writes the role's shared crop (crop_x/y/side), or the current
    frame's own crop when the role's frames don't share one selection.
    """

    def __init__(self, win):
        self.win = win

    def _get(self):
        return frame_crop(self.win.cfg, self.win.frame_idx)

    def _set(self, k, value):
        cfg = self.win.cfg
        t = list(self._get())
        t[k] = int(value)
        if not cfg.same_crop and cfg.frames:
            cfg.frames[self.win.frame_idx].crop = tuple(t)
        else:
            cfg.crop_x, cfg.crop_y, cfg.crop_side = t

    x = property(lambda self: self._get()[0], lambda self, v: self._set(0, v))
    y = property(lambda self: self._get()[1], lambda self, v: self._set(1, v))
    side = property(lambda self: self._get()[2], lambda self, v: self._set(2, v))


class CursorMakerWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Cursor Maker")
        self.set_default_size(1400, 900)   # any size works; the layout adapts

        self.project = Project()
        self.cache = ImageCache()
        self.current = ROLES[0].name
        self.frame_idx = 0            # animation frame being edited
        self.sel = Selection(self)    # its square selection
        self._loading = False
        self._render_pending = False
        self._src_surface = None      # (path, surface, buffer, w, h)
        self._crop = None             # current crop (pre-transparency) PIL image
        self._master = None           # current 256 master PIL image
        self._master_surf = None
        self._size_surfs = {}
        self._crop_surf = None        # faint original shown under object-mode result
        self._thumbs = {}             # (source, index) -> Gdk texture for the frame strip
        self._frame_buttons = []
        self._anim_cache = {}         # frame index -> rendered 256 master while playing
        self._play_id = None          # GLib timeout id while "Play" runs
        self._play_idx = 0
        self._drag_offset = (0, 0)
        self._drag_start = (0, 0)
        self._drag_mode = None        # "move" | "draw" | "resize"
        self._drag_anchor = (0, 0)    # fixed corner (image coords) for draw/resize
        self._downloading = False
        # background segmentation worker: only the newest request is processed
        self._seg_cond = threading.Condition()
        self._seg_request = None
        threading.Thread(target=self._seg_worker, daemon=True).start()

        self._build_ui()
        self._load_autosave()
        self._sync_project_widgets()
        self._refresh_role_list()
        self.role_list.select_row(self.role_list.get_row_at_index(0))
        self.connect("close-request", self._on_close)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for side in ("top", "bottom", "start", "end"):
            getattr(root, f"set_margin_{side}")(6)
        # the whole window scrolls if it is ever smaller than the controls need,
        # so nothing is cut off on very small or rotated screens
        outer_scroll = Gtk.ScrolledWindow(child=root)
        self.set_child(outer_scroll)

        # [roles | [editor | preview]] in resizable panes; _apply_layout flips
        # their orientation to suit wide, tall (portrait) or narrow windows
        self.inner_pane = Gtk.Paned(start_child=self._build_editor_panel(),
                                    end_child=self._build_preview_panel(),
                                    shrink_start_child=False, shrink_end_child=False)
        self.outer_pane = Gtk.Paned(start_child=self._build_role_panel(),
                                    end_child=self.inner_pane, vexpand=True,
                                    shrink_start_child=False, shrink_end_child=False,
                                    resize_start_child=False)
        root.append(self.outer_pane)
        self._layout_mode = None

        root.append(Gtk.Separator())
        root.append(self._build_bottom_bar())
        root.append(self._build_export_bar())
        self.status = Gtk.Label(xalign=0, wrap=True, selectable=True)
        root.append(self.status)

    def _build_role_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_size_request(170, 120)
        self.progress_label = Gtk.Label(xalign=0)
        box.append(self.progress_label)
        legend = Gtk.Label(xalign=0, wrap=True,
                           label="✔ set   ✱ required, missing   ○ optional (uses →)")
        legend.add_css_class("dim-label")
        box.append(legend)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        self.role_list = Gtk.ListBox()
        self.role_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.role_labels = {}
        for role in ROLES:
            lbl = Gtk.Label(xalign=0)
            lbl.set_margin_top(2)
            lbl.set_margin_bottom(2)
            lbl.set_margin_start(4)
            row = Gtk.ListBoxRow()
            row.set_child(lbl)
            row.role_name = role.name
            self.role_list.append(row)
            self.role_labels[role.name] = lbl
        self.role_list.connect("row-selected", self._on_role_selected)
        scroll.set_child(self.role_list)
        box.append(scroll)
        return box

    def _build_editor_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)

        self.role_title = Gtk.Label(xalign=0, wrap=True)
        self.role_title.add_css_class("title-3")
        box.append(self.role_title)

        bar = WrapRow()
        for label, cb in (("Open image…", self._on_open_image),
                          ("Copy this role to…", self._on_copy_to),
                          ("Clear role", self._on_clear_role)):
            b = Gtk.Button(label=label)
            b.connect("clicked", cb)
            bar.append(b)
        box.append(bar)
        self.source_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        box.append(self.source_label)
        box.append(self._build_frames_bar())

        self.editor_scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self.editor_scroll.set_size_request(-1, 220)   # stays usable in stacked layouts
        self.editor = Gtk.DrawingArea(hexpand=True, vexpand=True, focusable=True)
        self.editor.set_draw_func(self._draw_editor)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        self.editor.add_controller(drag)
        scroll_ctl = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll_ctl.connect("scroll", self._on_editor_scroll)
        self.editor.add_controller(scroll_ctl)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_editor_key)
        self.editor.add_controller(keys)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_editor_motion)
        self.editor.add_controller(motion)
        self.editor_scroll.set_child(self.editor)
        frame = Gtk.Frame()
        frame.set_child(self.editor_scroll)
        box.append(frame)

        sel = WrapRow()
        self.spin_x = Gtk.SpinButton.new_with_range(-100000, 100000, 1)
        self.spin_y = Gtk.SpinButton.new_with_range(-100000, 100000, 1)
        self.spin_side = Gtk.SpinButton.new_with_range(MIN_SIDE, 100000, 1)
        for lbl, spin in (("X", self.spin_x), ("Y", self.spin_y), ("Size", self.spin_side)):
            sel.append(Gtk.Label(label=lbl))
            spin.connect("value-changed", self._on_sel_spin)
            sel.append(spin)
        b256 = Gtk.Button(label="256")
        b256.set_tooltip_text("Set selection to 256×256 source pixels")
        b256.connect("clicked", lambda *_: self._set_side(256))
        sel.append(b256)
        binside = Gtk.Button(label="Largest inside")
        binside.set_tooltip_text("Biggest square that stays inside the image")
        binside.connect("clicked", lambda *_: self._fit_selection(inside=True))
        sel.append(binside)
        bfit = Gtk.Button(label="Fit whole image")
        bfit.set_tooltip_text("Square around the whole image; the extra area is transparent")
        bfit.connect("clicked", lambda *_: self._fit_selection(inside=False))
        sel.append(bfit)
        sel.append(Gtk.Label(label="  Zoom"))
        self.zoom_dd = Gtk.DropDown.new_from_strings([z[0] for z in ZOOMS])
        self.zoom_dd.connect("notify::selected", self._on_zoom_changed)
        sel.append(self.zoom_dd)
        box.append(sel)
        self.scale_label = Gtk.Label(xalign=0, wrap=True)
        box.append(self.scale_label)
        hint = Gtk.Label(xalign=0, wrap=True, label=(
            "Drag outside the square to draw a new one of any size, drag a corner to "
            "resize, drag inside to move. Ctrl+scroll or +/- also resize, arrow keys nudge "
            "(Shift = 10px). Parts outside the image become transparent."))
        hint.add_css_class("dim-label")
        box.append(hint)
        return box

    def _build_preview_panel(self):
        # scrolls both ways so it can be as narrow or short as the layout needs
        scroll = Gtk.ScrolledWindow()
        scroll.set_size_request(200, 150)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_end(12)
        scroll.set_child(box)

        box.append(Gtk.Label(xalign=0, label="Result 256×256 (click to use tool below)"))
        self.preview = Gtk.DrawingArea(halign=Gtk.Align.START)
        self.preview.set_content_width(MASTER_SIZE)
        self.preview.set_content_height(MASTER_SIZE)
        self.preview.set_draw_func(self._draw_preview)
        click = Gtk.GestureClick()
        click.set_button(0)   # all buttons; right-click adds exclude points
        click.connect("pressed", self._on_preview_click)
        self.preview.add_controller(click)
        drag = Gtk.GestureDrag()
        drag.connect("drag-update", self._on_preview_drag)
        self.preview.add_controller(drag)
        self._preview_drag = drag
        box.append(self.preview)

        tool = WrapRow()
        tool.append(Gtk.Label(label="Click tool:"))
        self.tool_hotspot = Gtk.CheckButton(label="Set hotspot", active=True)
        self.tool_pick = Gtk.CheckButton(label="Pick color", group=self.tool_hotspot)
        self.tool_object = Gtk.CheckButton(label="Select object", group=self.tool_hotspot)
        tool.append(self.tool_hotspot)
        tool.append(self.tool_pick)
        tool.append(self.tool_object)
        self.tool_text = Gtk.CheckButton(label="Place text", group=self.tool_hotspot)
        tool.append(self.tool_text)
        box.append(tool)

        hot = WrapRow()
        hot.append(Gtk.Label(label="Hotspot X"))
        self.spin_hx = Gtk.SpinButton.new_with_range(0, MASTER_SIZE - 1, 1)
        hot.append(self.spin_hx)
        hot.append(Gtk.Label(label="Y"))
        self.spin_hy = Gtk.SpinButton.new_with_range(0, MASTER_SIZE - 1, 1)
        hot.append(self.spin_hy)
        for s in (self.spin_hx, self.spin_hy):
            s.connect("value-changed", self._on_hot_spin)
        b = Gtk.Button(label="Role default")
        b.connect("clicked", self._on_default_hotspot)
        hot.append(b)
        box.append(hot)
        box.append(self._build_arrow_section())
        box.append(Gtk.Separator())
        box.append(self._build_text_section())

        box.append(Gtk.Separator())
        box.append(Gtk.Label(xalign=0, label="Transparency"))
        tbox = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=2,
                           column_spacing=6)
        self.trans_none = Gtk.CheckButton(label="None", active=True)
        self.trans_object = Gtk.CheckButton(label="Keep clicked object (AI)",
                                            group=self.trans_none)
        self.trans_color = Gtk.CheckButton(label="Remove color", group=self.trans_none)
        self.trans_flood = Gtk.CheckButton(label="Remove background (from corners)",
                                           group=self.trans_none)
        for w in (self.trans_none, self.trans_object, self.trans_color, self.trans_flood):
            w.connect("toggled", self._on_trans_changed)
            tbox.append(w)
        box.append(tbox)

        obox = Gtk.Box(spacing=6)
        self.obj_label = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        obox.append(self.obj_label)
        self.undo_point_btn = Gtk.Button(label="Undo point")
        self.undo_point_btn.connect("clicked", self._on_undo_point)
        obox.append(self.undo_point_btn)
        self.clear_points_btn = Gtk.Button(label="Clear points")
        self.clear_points_btn.connect("clicked", self._on_clear_points)
        obox.append(self.clear_points_btn)
        box.append(obox)
        # animated cursors: object clicks shared by all frames, or per frame
        fbox = WrapRow()
        self.same_points_check = Gtk.CheckButton(label="Same object points for all frames")
        self.same_points_check.set_tooltip_text(
            "Untick to click the object separately on each frame (select the frame in the "
            "Frames strip first). Frames without their own points use the shared ones.")
        self.same_points_check.connect("toggled", self._on_same_points)
        fbox.append(self.same_points_check)
        self.copy_points_btn = Gtk.Button(label="Copy these points to all frames")
        self.copy_points_btn.connect("clicked", self._on_copy_points_all)
        fbox.append(self.copy_points_btn)
        box.append(fbox)

        cbox = Gtk.Box(spacing=6)
        cbox.append(Gtk.Label(label="Color:"))
        self.color_swatch = Gtk.DrawingArea()
        self.color_swatch.set_content_width(32)
        self.color_swatch.set_content_height(20)
        self.color_swatch.set_draw_func(self._draw_swatch)
        cbox.append(self.color_swatch)
        self.color_label = Gtk.Label()
        cbox.append(self.color_label)
        box.append(cbox)

        tol = Gtk.Box(spacing=6)
        tol.append(Gtk.Label(label="Tolerance"))
        self.tol_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 255, 1)
        self.tol_scale.set_hexpand(True)
        self.tol_scale.set_draw_value(True)
        self.tol_scale.connect("value-changed", self._on_trans_changed)
        tol.append(self.tol_scale)
        box.append(tol)

        box.append(Gtk.Separator())
        box.append(Gtk.Label(xalign=0, label="Exported sizes (actual pixels)"))
        self.size_areas = {}
        for size in PREVIEW_SIZES:
            box.append(Gtk.Label(xalign=0, label=f"{size}×{size}"))
            area = Gtk.DrawingArea(halign=Gtk.Align.START)
            area.set_content_width(size)
            area.set_content_height(size)
            area.set_draw_func(self._draw_size_preview, size)
            box.append(area)
            self.size_areas[size] = area

        box.append(Gtk.Separator())
        trow = WrapRow()
        trow.append(Gtk.Label(label="Live test at size"))
        self.test_size_dd = Gtk.DropDown.new_from_strings([str(s) for s in EXPORT_SIZES])
        self.test_size_dd.set_selected(len(EXPORT_SIZES) - 1)
        self.test_size_dd.connect("notify::selected", lambda *_: self._update_test_cursor())
        trow.append(self.test_size_dd)
        box.append(trow)
        self.test_area = Gtk.Label(label="Hover here to try the cursor")
        self.test_area.set_size_request(-1, 160)
        test_frame = Gtk.Frame()
        test_frame.set_child(self.test_area)
        box.append(test_frame)
        return scroll

    def _build_arrow_section(self):
        """Hotspot icon: the role's standard cursor icon (from an installed
        cursor theme) or a flat arrow, drawn at the hotspot."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(Gtk.Label(xalign=0, label="Hotspot icon"))
        top = WrapRow()
        self.arrow_check = Gtk.CheckButton(label="Show icon at hotspot")
        self.arrow_check.connect("toggled", self._on_arrow_changed)
        top.append(self.arrow_check)
        top.append(Gtk.Label(label="  Icons from"))
        self.icon_themes = cursor_icons.list_cursor_themes() or [cursor_icons.DEFAULT_THEME]
        self.icon_theme_dd = Gtk.DropDown.new_from_strings(self.icon_themes)
        self.icon_theme_dd.set_tooltip_text("Installed cursor theme the standard icons are "
                                            "taken from (same for every cursor)")
        self.icon_theme_dd.connect("notify::selected", self._on_icon_theme_changed)
        top.append(self.icon_theme_dd)
        box.append(top)

        style = WrapRow()
        self.style_icon = Gtk.CheckButton(label="Cursor icon for this role", active=True)
        self.style_icon.set_tooltip_text("Arrow for default, hand for pointer, I-beam for "
                                         "text, … placed with its own tip on the hotspot")
        self.style_flat = Gtk.CheckButton(label="Flat arrow", group=self.style_icon)
        for w in (self.style_icon, self.style_flat):
            w.connect("toggled", self._on_arrow_changed)
            style.append(w)
        box.append(style)

        size_row = WrapRow()
        size_row.append(Gtk.Label(label="Size (px)"))
        self.arrow_size = Gtk.SpinButton.new_with_range(8, MASTER_SIZE, 1)
        self.arrow_size.set_tooltip_text("Icon size on the 256×256 cursor; it scales down "
                                         "with the smaller exported sizes")
        self.arrow_size.connect("value-changed", self._on_arrow_changed)
        size_row.append(self.arrow_size)
        all_btn = Gtk.Button(label="Use this size for all cursors")
        all_btn.connect("clicked", self._on_icon_size_all)
        size_row.append(all_btn)
        box.append(size_row)

        # flat arrow only: direction grid + colors
        flat = WrapRow()
        grid = Gtk.Grid(row_spacing=2, column_spacing=2)
        self.arrow_buttons = {}
        first = None
        for r, row in enumerate(ARROW_GRID):
            for c, cell in enumerate(row):
                if cell is None:
                    continue
                name, glyph = cell
                btn = Gtk.ToggleButton(label=glyph)
                btn.set_tooltip_text(f"Point {name}")
                if first is None:
                    first = btn
                else:
                    btn.set_group(first)
                btn.connect("toggled", self._on_arrow_changed)
                grid.attach(btn, c, r, 1, 1)
                self.arrow_buttons[name] = btn
        flat.append(grid)
        color_row = WrapRow()
        self.arrow_fill_btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False))
        self.arrow_outline_btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False))
        for label, btn in (("Fill", self.arrow_fill_btn), ("Outline", self.arrow_outline_btn)):
            color_row.append(Gtk.Label(label=label))
            btn.connect("notify::rgba", self._on_arrow_changed)
            color_row.append(btn)
        flat.append(color_row)
        box.append(flat)
        self.flat_widgets = [self.arrow_fill_btn, self.arrow_outline_btn,
                             *self.arrow_buttons.values()]
        self.arrow_widgets = [self.arrow_check, self.style_icon, self.style_flat,
                              self.arrow_size, all_btn, *self.flat_widgets]
        return box

    def _on_icon_theme_changed(self, *_):
        if self._loading:
            return
        self.project.icon_theme = self.icon_themes[self.icon_theme_dd.get_selected()]
        self.schedule_render()

    def _on_icon_size_all(self, *_):
        """Give every cursor the same hotspot icon size."""
        size = int(self.arrow_size.get_value())
        for cfg in self.project.roles.values():
            cfg.arrow_size = size
        self.set_status(f"Hotspot icon size set to {size} px for all cursors.")
        self.schedule_render()

    @staticmethod
    def _rgba(rgb):
        c = Gdk.RGBA()
        c.red, c.green, c.blue, c.alpha = (*(v / 255 for v in rgb), 1.0)
        return c

    @staticmethod
    def _rgb(rgba):
        return tuple(round(v * 255) for v in (rgba.red, rgba.green, rgba.blue))

    def _on_arrow_changed(self, *_):
        if self._loading or not self.cfg.is_set:
            return
        cfg = self.cfg
        cfg.arrow = self.arrow_check.get_active()
        cfg.arrow_style = "flat" if self.style_flat.get_active() else "icon"
        for w in self.flat_widgets:
            w.set_sensitive(cfg.arrow_style == "flat")
        cfg.arrow_dir = next((n for n, b in self.arrow_buttons.items() if b.get_active()),
                             cfg.arrow_dir)
        cfg.arrow_size = int(self.arrow_size.get_value())
        cfg.arrow_fill = self._rgb(self.arrow_fill_btn.get_rgba())
        cfg.arrow_outline = self._rgb(self.arrow_outline_btn.get_rgba())
        self.schedule_render()

    def _load_arrow_widgets(self):
        cfg = self.cfg
        self.arrow_check.set_active(cfg.arrow)
        (self.style_flat if cfg.arrow_style == "flat" else self.style_icon).set_active(True)
        self.arrow_buttons[cfg.arrow_dir].set_active(True)
        self.arrow_size.set_value(cfg.arrow_size)
        self.arrow_fill_btn.set_rgba(self._rgba(cfg.arrow_fill))
        self.arrow_outline_btn.set_rgba(self._rgba(cfg.arrow_outline))
        for w in self.arrow_widgets:
            w.set_sensitive(cfg.is_set)
        for w in self.flat_widgets:
            w.set_sensitive(cfg.is_set and cfg.arrow_style == "flat")

    def _build_text_section(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.text_check = Gtk.CheckButton(label="Show text on cursor")
        box.append(self.text_check)
        self.text_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.NONE, accepts_tab=False)
        self.text_view.set_size_request(-1, 48)
        self.text_view.set_tooltip_text("Text to draw on the cursor (Enter for a new line)")
        frame = Gtk.Frame()
        frame.set_child(self.text_view)
        box.append(frame)

        font_row = WrapRow()
        font_row.append(Gtk.Label(label="Font"))
        self.text_font_btn = Gtk.FontDialogButton(dialog=Gtk.FontDialog(),
                                                  level=Gtk.FontLevel.FAMILY)
        font_row.append(self.text_font_btn)
        self.text_bold = Gtk.CheckButton(label="Bold")
        self.text_italic = Gtk.CheckButton(label="Italic")
        font_row.append(self.text_bold)
        font_row.append(self.text_italic)
        box.append(font_row)

        size_row = Gtk.Box(spacing=6)
        size_row.append(Gtk.Label(label="Size"))
        self.text_size = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 8, 200, 1)
        self.text_size.set_hexpand(True)
        size_row.append(self.text_size)
        box.append(size_row)

        color_row = WrapRow()
        self.text_fill_btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False))
        self.text_outline_btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False))
        for label, btn in (("Color", self.text_fill_btn), ("Outline", self.text_outline_btn)):
            color_row.append(Gtk.Label(label=label))
            color_row.append(btn)
        color_row.append(Gtk.Label(label="Thickness"))
        self.text_outline_w = Gtk.SpinButton.new_with_range(0, 20, 1)
        color_row.append(self.text_outline_w)
        box.append(color_row)

        pos_row = WrapRow()
        pos_row.append(Gtk.Label(label="Center X"))
        self.text_x = Gtk.SpinButton.new_with_range(-128, MASTER_SIZE + 128, 1)
        pos_row.append(self.text_x)
        pos_row.append(Gtk.Label(label="Y"))
        self.text_y = Gtk.SpinButton.new_with_range(-128, MASTER_SIZE + 128, 1)
        pos_row.append(self.text_y)
        hint = Gtk.Label(label="(or use “Place text” and click the preview)")
        hint.add_css_class("dim-label")
        pos_row.append(hint)
        box.append(pos_row)

        self.text_check.connect("toggled", self._on_text_changed)
        self.text_view.get_buffer().connect("changed", self._on_text_changed)
        self.text_font_btn.connect("notify::font-desc", self._on_text_changed)
        for w in (self.text_bold, self.text_italic):
            w.connect("toggled", self._on_text_changed)
        self.text_size.connect("value-changed", self._on_text_changed)
        for w in (self.text_fill_btn, self.text_outline_btn):
            w.connect("notify::rgba", self._on_text_changed)
        for w in (self.text_outline_w, self.text_x, self.text_y):
            w.connect("value-changed", self._on_text_changed)
        self.text_widgets = [self.text_check, self.text_view, self.text_font_btn, self.text_bold,
                             self.text_italic, self.text_size, self.text_fill_btn,
                             self.text_outline_btn, self.text_outline_w, self.text_x, self.text_y]
        return box

    def _on_text_changed(self, *_):
        if self._loading or not self.cfg.is_set:
            return
        cfg = self.cfg
        buf = self.text_view.get_buffer()
        cfg.text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        if cfg.text.strip() and not cfg.text_on and _ and _[0] is buf:
            # typing text turns the option on
            cfg.text_on = True
            self._loading = True
            self.text_check.set_active(True)
            self._loading = False
        else:
            cfg.text_on = self.text_check.get_active()
        desc = self.text_font_btn.get_font_desc()
        if desc is not None and desc.get_family():
            cfg.text_font = desc.get_family()
        cfg.text_bold = self.text_bold.get_active()
        cfg.text_italic = self.text_italic.get_active()
        cfg.text_size = int(self.text_size.get_value())
        cfg.text_fill = self._rgb(self.text_fill_btn.get_rgba())
        cfg.text_outline = self._rgb(self.text_outline_btn.get_rgba())
        cfg.text_outline_width = int(self.text_outline_w.get_value())
        cfg.text_x = int(self.text_x.get_value())
        cfg.text_y = int(self.text_y.get_value())
        self.schedule_render()

    def _load_text_widgets(self):
        cfg = self.cfg
        self.text_check.set_active(cfg.text_on)
        self.text_view.get_buffer().set_text(cfg.text)
        self.text_font_btn.set_font_desc(Pango.FontDescription.from_string(cfg.text_font))
        self.text_bold.set_active(cfg.text_bold)
        self.text_italic.set_active(cfg.text_italic)
        self.text_size.set_value(cfg.text_size)
        self.text_fill_btn.set_rgba(self._rgba(cfg.text_fill))
        self.text_outline_btn.set_rgba(self._rgba(cfg.text_outline))
        self.text_outline_w.set_value(cfg.text_outline_width)
        self.text_x.set_value(cfg.text_x)
        self.text_y.set_value(cfg.text_y)
        for w in self.text_widgets:
            w.set_sensitive(cfg.is_set)

    def _build_export_bar(self):
        """Second bottom row: optional Windows 11 / macOS packages and their folder."""
        box = WrapRow()
        box.append(Gtk.Label(label="Also export for:"))
        self.win_check = Gtk.CheckButton(label="Windows 11 (.zip with .cur + install.inf)")
        self.win_check.set_tooltip_text(
            "Uses the same sizes as the Linux theme. Tick “Also 64/48/32/24” too: "
            "Windows' default pointer is 32 px.")
        box.append(self.win_check)
        self.mac_check = Gtk.CheckButton(label="macOS (Mousecape .cape)")
        self.mac_check.set_tooltip_text(
            "macOS has no built-in cursor themes; the .cape is applied with the free "
            "Mousecape app.")
        box.append(self.mac_check)
        self.win_full_check = Gtk.CheckButton(label="Windows: full size (test)")
        self.win_full_check.set_tooltip_text(
            "Keep every frame in Windows animated cursors instead of shrinking them to "
            "~1.6 MB. Windows' Pointers tab may reject files over ~2 MB; this is for "
            "testing whether Windows itself shows them (use Apply-Cursors.bat). "
            "Packaged as a separate scheme named “<theme>-FullSize”.")
        box.append(self.win_full_check)
        box.append(Gtk.Label(label="  Export folder"))
        self.export_entry = Gtk.Entry(width_chars=24)
        self.export_entry.set_tooltip_text("Files go into <this folder>/<theme name>/")
        box.append(self.export_entry)
        b = Gtk.Button(label="Browse…")
        b.connect("clicked", lambda *_: self._browse_folder(
            self.export_entry, "Folder for the Windows and macOS files"))
        box.append(b)
        # export one platform on its own, without regenerating the Linux theme
        self._export_buttons = []
        for label, platform in (("Export Windows only", "windows"),
                                ("Export macOS only", "mac")):
            btn = Gtk.Button(label=label)
            btn.set_tooltip_text("Write just this package into the export folder "
                                 "(the Linux theme is not touched)")
            btn.connect("clicked", self._on_export_platform, platform)
            box.append(btn)
            self._export_buttons.append(btn)
        # progress of a running export (they run in the background)
        self.export_progress = Gtk.ProgressBar(show_text=True, visible=False)
        self.export_progress.set_size_request(200, -1)
        box.append(self.export_progress)
        self.cancel_btn = Gtk.Button(label="Cancel export", visible=False)
        self.cancel_btn.connect("clicked", self._on_cancel_export)
        box.append(self.cancel_btn)
        return box

    # -- background exports
    def _run_export(self, title, work, on_done):
        """Run `work(project_copy, cache, progress, cancel)` in a thread.

        The window stays responsive: a snapshot of the project is exported (so
        editing meanwhile is safe), a progress bar pulses with the latest step,
        and "Cancel export" stops between frames. on_done(result, error) runs
        on the main thread; error is None, a Cancelled or another exception.
        """
        if getattr(self, "_export_cancel", None) is not None:
            return                                   # one export at a time
        snapshot = copy.deepcopy(self.project)
        cancel = threading.Event()
        self._export_cancel = cancel
        state = {"text": title, "last": 0.0}
        for b in self._export_buttons + [self.gen_btn]:
            b.set_sensitive(False)
        self.export_progress.set_text(title)
        self.export_progress.set_visible(True)
        self.cancel_btn.set_visible(True)
        self.cancel_btn.set_sensitive(True)
        self.set_status(title)

        def progress(text):
            state["text"] = text             # the pulse timer shows the latest step

        def pulse():
            if self._export_cancel is not cancel:
                return False
            self.export_progress.pulse()
            self.export_progress.set_text(state["text"])
            return True
        GLib.timeout_add(150, pulse)

        def finish(result, error):
            self._export_cancel = None
            for b in self._export_buttons + [self.gen_btn]:
                b.set_sensitive(True)
            self.export_progress.set_visible(False)
            self.cancel_btn.set_visible(False)
            on_done(result, error)
            return False

        def run():
            try:
                result = work(snapshot, ImageCache(), progress, cancel)
                GLib.idle_add(finish, result, None)
            except Exception as e:         # includes Cancelled / ThemeExistsError
                GLib.idle_add(finish, None, e)
        threading.Thread(target=run, daemon=True).start()

    def _on_cancel_export(self, *_):
        if getattr(self, "_export_cancel", None) is not None:
            self._export_cancel.set()
            self.cancel_btn.set_sensitive(False)
            self.export_progress.set_text("Cancelling…")

    def _on_export_platform(self, _btn, platform):
        self._read_project_widgets()
        missing = self.project.missing_required()
        if missing:
            self.alert("Some required cursors have no image", "Missing: " + ", ".join(missing))
            return
        name = "Windows 11" if platform == "windows" else "macOS"

        notes = []

        def work(project, cache, progress, cancel):
            return export_platform(project, platform, cache, progress, cancel, notes)

        def done(path, error):
            if isinstance(error, Cancelled):
                self.set_status(f"{name} export cancelled; the previous package (if any) "
                                "was left untouched.")
                return
            if error is not None:
                self.alert(f"{name} export failed", str(error))
                self.set_status(f"{name} export failed.")
                return
            self._autosave()
            tip = ("Extract it on Windows and right-click install.inf > Install."
                   if platform == "windows" else "Open it with the Mousecape app on the Mac.")
            self.set_status(f"{name} package written: {path}. {tip}"
                            + self._thinned_note(notes)
                            + (self._windows_size_warning(path) if platform == "windows"
                               else ""))
        self._run_export(f"Exporting {name} package…", work, done)

    def _build_bottom_bar(self):
        box = WrapRow()
        box.append(Gtk.Label(label="Theme name"))
        self.name_entry = Gtk.Entry(width_chars=16)
        box.append(self.name_entry)
        box.append(Gtk.Label(label="Output folder"))
        self.dir_entry = Gtk.Entry(width_chars=24)
        box.append(self.dir_entry)
        b = Gtk.Button(label="Browse…")
        b.connect("clicked", lambda *_: self._browse_folder(
            self.dir_entry, "Output folder (theme folder is created inside)"))
        box.append(b)
        self.small_check = Gtk.CheckButton(label="Also 64/48/32/24")
        self.small_check.set_tooltip_text(
            "Add small sizes so the theme also looks right at normal cursor sizes")
        box.append(self.small_check)
        for label, cb in (("Save project…", self._on_save_project),
                          ("Load project…", self._on_load_project),
                          ("Import cursor set…", self._on_import_set)):
            b = Gtk.Button(label=label)
            b.connect("clicked", cb)
            box.append(b)
        gen = self.gen_btn = Gtk.Button(label="Generate theme")
        gen.add_css_class("suggested-action")
        gen.connect("clicked", self._on_generate)
        box.append(gen)
        box.append(Gtk.Label(label="Size"))
        self.apply_size_dd = Gtk.DropDown.new_from_strings([str(s) for s in EXPORT_SIZES])
        self.apply_size_dd.set_selected(len(EXPORT_SIZES) - 1)
        box.append(self.apply_size_dd)
        ap = Gtk.Button(label="Apply to GNOME")
        ap.connect("clicked", self._on_apply)
        box.append(ap)
        return box

    # ---------------------------------------------------------- layout
    @staticmethod
    def layout_for(width, height):
        """Layout mode for a window size: "wide" = three columns, "tall" =
        editor above preview (portrait screens), "narrow" = everything stacked."""
        if width < 760:
            return "narrow"
        if width < 1250 or height > width * 0.9:
            return "tall"
        return "wide"

    def do_size_allocate(self, width, height, baseline):
        Gtk.ApplicationWindow.do_size_allocate(self, width, height, baseline)
        mode = self.layout_for(width, height)
        if mode != self._layout_mode:
            self._layout_mode = mode
            # widgets must not be rearranged during allocation: do it right after
            GLib.idle_add(self._apply_layout, mode, width, height)

    def _apply_layout(self, mode, width, height):
        H, V = Gtk.Orientation.HORIZONTAL, Gtk.Orientation.VERTICAL
        self.outer_pane.set_orientation(V if mode == "narrow" else H)
        self.inner_pane.set_orientation(H if mode == "wide" else V)
        if mode == "narrow":
            self.outer_pane.set_position(max(140, int(height * 0.22)))
            self.inner_pane.set_position(max(250, int(height * 0.40)))
        else:
            self.outer_pane.set_position(230)
            if mode == "wide":
                self.inner_pane.set_position(max(400, width - 230 - 480))
            else:
                self.inner_pane.set_position(max(300, int(height * 0.50)))
        return False

    # --------------------------------------------------------- helpers
    @property
    def cfg(self) -> RoleConfig:
        return self.project.roles[self.current]

    def set_status(self, text):
        self.status.set_label(text)

    def alert(self, message, detail=""):
        dlg = Gtk.AlertDialog(message=message, detail=detail)
        dlg.show(self)

    def _sync_project_widgets(self):
        self.name_entry.set_text(self.project.theme_name)
        self.dir_entry.set_text(self.project.output_dir)
        self.small_check.set_active(self.project.include_small)
        self.win_check.set_active(self.project.export_windows)
        self.mac_check.set_active(self.project.export_mac)
        self.win_full_check.set_active(self.project.windows_full_size)
        self.export_entry.set_text(self.project.export_dir)
        if self.project.icon_theme not in self.icon_themes:   # e.g. theme uninstalled
            self.project.icon_theme = cursor_icons.DEFAULT_THEME
        prev, self._loading = self._loading, True
        self.icon_theme_dd.set_selected(self.icon_themes.index(self.project.icon_theme))
        self._loading = prev

    def _read_project_widgets(self):
        self.project.theme_name = self.name_entry.get_text().strip()
        self.project.output_dir = self.dir_entry.get_text().strip()
        self.project.include_small = self.small_check.get_active()
        self.project.export_windows = self.win_check.get_active()
        self.project.export_mac = self.mac_check.get_active()
        self.project.windows_full_size = self.win_full_check.get_active()
        self.project.export_dir = self.export_entry.get_text().strip()

    def _refresh_role_list(self):
        for role in ROLES:
            cfg = self.project.roles[role.name]
            if cfg.is_set:
                text = f"✔ {role.name}"
                if cfg.is_animated:
                    text += f"  ▶{len(cfg.frames)}"
            elif role.required:
                text = f"✱ {role.name}"
            else:
                used = self.project.resolve(role.name) or "Adwaita"
                text = f"○ {role.name}  → {used}"
            self.role_labels[role.name].set_label(text)
        req = [r for r in ROLES if r.required]
        done = sum(self.project.roles[r.name].is_set for r in req)
        opt = sum(self.project.roles[r.name].is_set for r in ROLES if not r.required)
        self.progress_label.set_label(
            f"Required: {done}/{len(req)}   Optional: {opt}/{len(ROLES) - len(req)}")

    def _current_frame(self) -> Frame:
        frames = frame_list(self.cfg)
        self.frame_idx = max(0, min(self.frame_idx, len(frames) - 1))
        return frames[self.frame_idx]

    def _source_image(self):
        """Source image of the frame being edited (None if the role has no image)."""
        if not self.cfg.is_set:
            return None
        f = self._current_frame()
        try:
            return self.cache.get_frame(f.source, f.index)
        except Exception as e:  # missing / unreadable file
            self.set_status(f"Cannot read {f.source}: {e}")
            return None

    def _ensure_src_surface(self):
        img = self._source_image()
        if img is None:
            self._src_surface = None
            return None
        f = self._current_frame()
        key = (f.source, f.index)
        if self._src_surface is None or self._src_surface[0] != key:
            surf, buf = pil_to_surface(img)
            self._src_surface = (key, surf, buf, img.width, img.height)
        return self._src_surface

    def _clamp_selection(self):
        img = self._source_image()
        if img is None:
            return
        cfg = self.cfg
        self.sel.side = max(MIN_SIDE, min(self.sel.side, max(img.width, img.height)))
        (x0, x1), (y0, y1) = self._position_range(img, self.sel.side)
        self.sel.x = max(x0, min(self.sel.x, x1))
        self.sel.y = max(y0, min(self.sel.y, y1))

    @staticmethod
    def _position_range(img, side):
        """Allowed crop_x / crop_y ranges: the image always spans one full axis."""
        return ((min(0, img.width - side), max(0, img.width - side)),
                (min(0, img.height - side), max(0, img.height - side)))

    def _set_spin_ranges(self, img):
        (x0, x1), (y0, y1) = self._position_range(img, self.sel.side)
        self.spin_x.set_range(x0, x1)
        self.spin_y.set_range(y0, y1)
        self.spin_side.set_range(MIN_SIDE, max(img.width, img.height))

    def _update_scale_label(self):
        if self._source_image() is None:
            self.scale_label.set_label("")
            return
        side = self.sel.side
        how = ("used as is" if side == MASTER_SIZE else
               "scaled down" if side > MASTER_SIZE else "scaled up")
        self.scale_label.set_label(f"Selection {side}×{side} → {how} to 256×256 "
                                   f"(exports {'/'.join(map(str, EXPORT_SIZES))})")

    def _load_role_into_widgets(self):
        self._loading = True
        try:
            role = ROLE_BY_NAME[self.current]
            req = "required" if role.required else f"optional, falls back to {role.fallback}"
            self.role_title.set_label(f"{role.name} — {role.description} ({req})")
            cfg = self.cfg
            self.source_label.set_label(cfg.source or "No image yet — click “Open image…”")
            img = self._source_image()
            if img is not None:
                self._set_spin_ranges(img)
            self._update_scale_label()
            self.spin_x.set_value(self.sel.x)
            self.spin_y.set_value(self.sel.y)
            self.spin_side.set_value(self.sel.side)
            self.spin_hx.set_value(cfg.hot_x)
            self.spin_hy.set_value(cfg.hot_y)
            {TRANS_NONE: self.trans_none, TRANS_COLOR: self.trans_color,
             TRANS_FLOOD: self.trans_flood, TRANS_OBJECT: self.trans_object
             }[cfg.trans_mode].set_active(True)
            if cfg.trans_mode == TRANS_OBJECT:
                self.tool_object.set_active(True)
            self.tol_scale.set_value(cfg.tolerance)
            self._update_color_label()
            for w in (self.spin_x, self.spin_y, self.spin_side, self.spin_hx, self.spin_hy,
                      self.trans_none, self.trans_color, self.trans_flood, self.trans_object,
                      self.tol_scale):
                w.set_sensitive(cfg.is_set)
            self._update_obj_widgets()
            self._load_arrow_widgets()
            self._load_text_widgets()
            self._load_frame_widgets()
        finally:
            self._loading = False

    def _points(self):
        """AI object clicks that apply to the frame being edited."""
        return frame_points(self.cfg, self.frame_idx)

    def _set_points(self, points):
        """Store clicks for the current frame (per-frame mode) or for all frames."""
        cfg = self.cfg
        if not cfg.same_points and cfg.frames:
            cfg.frames[self.frame_idx].obj_points = list(points)
        else:
            cfg.obj_points = list(points)
        self._update_obj_widgets()
        self.schedule_render()

    def _update_obj_widgets(self):
        cfg = self.cfg
        active = cfg.is_set and cfg.trans_mode == TRANS_OBJECT
        pts = self._points() if cfg.is_set else []
        keep = sum(1 for p in pts if p[2] == 1)
        per_frame = cfg.is_animated and not cfg.same_points
        if active:
            where = f" on frame {self.frame_idx + 1}" if per_frame else ""
            self.obj_label.set_label(
                f"With “Select object”: left-click the object to keep it, right-click "
                f"(or Ctrl+click) parts to remove. Points{where}: {keep} keep, "
                f"{len(pts) - keep} remove.")
        else:
            self.obj_label.set_label("")
        self.undo_point_btn.set_sensitive(active and bool(pts))
        self.clear_points_btn.set_sensitive(active and bool(pts))
        prev, self._loading = self._loading, True
        self.same_points_check.set_active(cfg.same_points)
        self._loading = prev
        self.same_points_check.set_sensitive(active and cfg.is_animated)
        self.copy_points_btn.set_sensitive(active and per_frame and bool(pts))
        # mark frames that have their own clicks with a dot in the strip
        for i, lbl in enumerate(getattr(self, "_frame_labels", [])):
            own = per_frame and i < len(cfg.frames) and bool(cfg.frames[i].obj_points)
            lbl.set_label(f"{i + 1}{' •' if own else ''}")

    def _on_same_points(self, *_):
        if self._loading or not self.cfg.frames:
            return
        cfg = self.cfg
        if self.same_points_check.get_active():
            # the current frame's clicks become the shared ones
            cfg.obj_points = list(frame_points(cfg, self.frame_idx))
            for f in cfg.frames:
                f.obj_points = None
            cfg.same_points = True
        else:
            # every frame starts with its own copy of the shared clicks
            for f in cfg.frames:
                f.obj_points = list(cfg.obj_points)
            cfg.same_points = False
        self._update_obj_widgets()
        self.schedule_render()

    def _on_copy_points_all(self, *_):
        pts = list(self._points())
        for f in self.cfg.frames:
            f.obj_points = list(pts)
        self.set_status(f"Copied frame {self.frame_idx + 1}'s points to all "
                        f"{len(self.cfg.frames)} frames.")
        self._update_obj_widgets()
        self.schedule_render()

    def _update_selection_spins(self):
        self._loading = True
        try:
            img = self._source_image()
            if img is not None:
                self._set_spin_ranges(img)
            self._update_scale_label()
            self.spin_x.set_value(self.sel.x)
            self.spin_y.set_value(self.sel.y)
            self.spin_side.set_value(self.sel.side)
        finally:
            self._loading = False

    def _update_color_label(self):
        r, g, b = self.cfg.trans_color
        self.color_label.set_label(f"#{r:02x}{g:02x}{b:02x}  (use “Pick transparent color”"
                                   " and click the result)")
        self.color_swatch.queue_draw()

    # --------------------------------------------------------- rendering
    def schedule_render(self):
        self._anim_cache.clear()       # any change invalidates the played frames
        if not self._render_pending:
            self._render_pending = True
            GLib.idle_add(self._render_now)

    def _render_now(self):
        self._render_pending = False
        self._crop = self._master = self._master_surf = self._crop_surf = None
        self._size_surfs = {}
        if self._source_image() is not None:
            try:
                cfg = self.cfg
                i = self.frame_idx
                self._crop = render_crop(cfg, self.cache, i)
                if cfg.trans_mode == TRANS_OBJECT:
                    self._crop_surf = pil_to_surface(imaging.resize(self._crop, MASTER_SIZE))
                if self._ai_usable() and not mask_ready(cfg, i):
                    # show the unmasked image until the worker delivers the mask
                    self._request_mask(cfg, self._crop)
                    shown = self._crop
                elif cfg.trans_mode == TRANS_OBJECT and not self._ai_usable():
                    shown = self._crop
                else:
                    shown = apply_transparency(self._crop, cfg, i)
                self._master = add_overlays(imaging.resize(shown, MASTER_SIZE), cfg,
                                            self.current, self.project.icon_theme)
                self._master_surf = pil_to_surface(self._master)
                for size in PREVIEW_SIZES:
                    self._size_surfs[size] = pil_to_surface(imaging.resize(self._master, size))
            except Exception as e:
                self.set_status(f"Render failed: {e}")
        self.preview.queue_draw()
        for a in self.size_areas.values():
            a.queue_draw()
        self._update_test_cursor()
        return False

    # ------------------------------------------------- AI object selection
    def _ai_usable(self):
        return (not self._downloading and segment.deps_available()
                and segment.models_present())

    def _request_mask(self, cfg, crop):
        key = crop_key(cfg, self.frame_idx)
        points = crop_points(cfg, crop.width, self.frame_idx)
        with self._seg_cond:
            self._seg_request = (crop, key, points)
            self._seg_cond.notify()
        self.set_status("Detecting object…")

    def _seg_worker(self):
        while True:
            with self._seg_cond:
                while self._seg_request is None:
                    self._seg_cond.wait()
                crop, key, points = self._seg_request
                self._seg_request = None
            try:
                segment.get_segmenter().mask(crop, key, points)
                GLib.idle_add(self._on_mask_done, None)
            except Exception as e:
                GLib.idle_add(self._on_mask_done, str(e))

    def _on_mask_done(self, error):
        if error:
            self.set_status(f"Object detection failed: {error}")
        else:
            with self._seg_cond:
                busy = self._seg_request is not None
            if not busy:
                self.set_status("Object detected. Left-click to add more of it, right-click "
                                "to remove parts.")
            self.schedule_render()
        return False

    def _ensure_ai_ready(self):
        """Check packages/model for AI mode. Returns False if it cannot be used now."""
        if not segment.deps_available():
            self.alert("AI object selection needs extra packages",
                       f"Install them with:\n\n{segment.INSTALL_HINT}\n\nthen restart the app.")
            return False
        if segment.models_present() or self._downloading:
            return True
        dlg = Gtk.AlertDialog(message="Download the object selection model?",
                              detail="MobileSAM (~45 MB, one time) will be saved to\n"
                                     f"{segment.MODEL_DIR}")
        dlg.set_buttons(["Cancel", "Download"])
        dlg.set_cancel_button(0)
        dlg.set_default_button(1)

        def chosen(dlg, res):
            try:
                ok = dlg.choose_finish(res) == 1
            except GLib.Error:
                ok = False
            if ok:
                self._start_download()
            else:
                self.cfg.trans_mode = TRANS_NONE
                self._load_role_into_widgets()
                self.schedule_render()
        dlg.choose(self, None, chosen)
        return True

    def _start_download(self):
        self._downloading = True

        def progress(done, total, name):
            pct = f"{done * 100 // total}%" if total else f"{done // 1048576} MB"
            GLib.idle_add(self.set_status, f"Downloading {name}: {pct}")

        def run():
            try:
                segment.download_models(progress)
                err = None
            except Exception as e:
                err = str(e)
            GLib.idle_add(finished, err)

        def finished(err):
            self._downloading = False
            if err:
                self.alert("Model download failed", err)
                self.set_status("Model download failed.")
            else:
                self.set_status("Model ready. Click the object in the result preview.")
                self.schedule_render()
            return False
        threading.Thread(target=run, daemon=True).start()

    def _add_object_point(self, x, y, label):
        cfg = self.cfg
        if cfg.trans_mode != TRANS_OBJECT:
            cfg.trans_mode = TRANS_OBJECT
            self._load_role_into_widgets()
            if not self._ensure_ai_ready():
                cfg.trans_mode = TRANS_NONE
                self._load_role_into_widgets()
                return
        self._set_points(self._points() + [(x, y, label)])

    def _on_undo_point(self, *_):
        self._set_points(self._points()[:-1])

    def _on_clear_points(self, *_):
        self._set_points([])

    def _update_test_cursor(self):
        if self._master is None:
            self.test_area.set_cursor(None)
            return
        size = EXPORT_SIZES[self.test_size_dd.get_selected()]
        img = imaging.resize(self._master, size)
        tex = Gdk.MemoryTexture.new(size, size, Gdk.MemoryFormat.R8G8B8A8,
                                    GLib.Bytes.new(img.tobytes()), size * 4)
        scale = size / MASTER_SIZE
        hx = min(size - 1, round(self.cfg.hot_x * scale))
        hy = min(size - 1, round(self.cfg.hot_y * scale))
        self.test_area.set_cursor(Gdk.Cursor.new_from_texture(tex, hx, hy, None))

    # ------------------------------------------------------ editor canvas
    def _canvas(self):
        """Area (image coords) the view shows: every square position that is allowed."""
        _, _, _, iw, ih = self._src_surface
        m = max(iw, ih)
        return iw - m, ih - m, 2 * m - iw, 2 * m - ih   # x0, y0, width, height

    def _editor_scale(self, w, h):
        if self._src_surface is None:
            return 1.0
        z = ZOOMS[self.zoom_dd.get_selected()][1]
        if z is None:
            _, _, cw, ch = self._canvas()
            return min(w / cw, h / ch)
        return z

    def _editor_transform(self):
        """(scale, ox, oy) so that widget = o + image * scale."""
        w, h = self.editor.get_width(), self.editor.get_height()
        s = self._editor_scale(w, h)
        cx0, cy0, cw, ch = self._canvas()
        ox = max(0.0, (w - cw * s) / 2) - cx0 * s
        oy = max(0.0, (h - ch * s) / 2) - cy0 * s
        return s, ox, oy

    def _draw_editor(self, area, cr, w, h):
        cr.set_source_rgb(0.25, 0.25, 0.25)
        cr.paint()
        src = self._ensure_src_surface()
        if src is None:
            cr.set_source_rgb(0.9, 0.9, 0.9)
            cr.move_to(20, 30)
            cr.set_font_size(16)
            cr.show_text("Open an image for this cursor role.")
            return
        _, surf, _, iw, ih = src
        s, ox, oy = self._editor_transform()
        cr.save()
        cr.translate(ox, oy)
        cr.scale(s, s)
        draw_checker_scaled = iw * s * ih * s < 4e6
        if draw_checker_scaled:
            cr.rectangle(0, 0, iw, ih)
            cr.set_source_rgb(0.7, 0.7, 0.7)
            cr.fill()
        cr.set_source_surface(surf, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_NEAREST if s >= 2 else cairo.FILTER_GOOD)
        cr.paint()
        cr.restore()

        cfg = self.cfg
        rx, ry = ox + self.sel.x * s, oy + self.sel.y * s
        rs = self.sel.side * s
        # darken outside selection
        cr.set_source_rgba(0, 0, 0, 0.5)
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.rectangle(ox, oy, iw * s, ih * s)
        cr.rectangle(rx, ry, rs, rs)
        cr.fill()
        cr.set_fill_rule(cairo.FILL_RULE_WINDING)
        # part of the square outside the image (becomes transparent)
        cr.save()
        cr.rectangle(rx, ry, rs, rs)
        cr.clip()
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.rectangle(rx, ry, rs, rs)
        cr.rectangle(ox, oy, iw * s, ih * s)
        cr.set_source_rgba(0.6, 0.6, 1, 0.25)
        cr.fill()
        cr.restore()
        cr.set_line_width(2)
        cr.set_source_rgb(1, 0.2, 0.2)
        cr.rectangle(rx, ry, rs, rs)
        cr.stroke()
        for hx, hy in self._corners_widget():
            cr.rectangle(hx - 5, hy - 5, 10, 10)
            cr.set_source_rgb(1, 1, 1)
            cr.fill_preserve()
            cr.set_source_rgb(1, 0.2, 0.2)
            cr.set_line_width(1.5)
            cr.stroke()
        cr.set_source_rgb(1, 1, 1)
        cr.set_font_size(13)
        cr.move_to(rx + 4, max(14, ry - 4))
        cr.show_text(f"{self.sel.side}×{self.sel.side} at ({self.sel.x}, {self.sel.y})")

    def _widget_to_image(self, x, y):
        s, ox, oy = self._editor_transform()
        return (x - ox) / s, (y - oy) / s

    def _corners_widget(self):
        """Selection corners in widget coords: top-left, top-right, bottom-left, bottom-right."""
        s, ox, oy = self._editor_transform()
        cfg = self.cfg
        x0, y0 = ox + self.sel.x * s, oy + self.sel.y * s
        x1, y1 = x0 + self.sel.side * s, y0 + self.sel.side * s
        return [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]

    def _hit_test(self, x, y):
        """('corner', index) | ('inside', None) | ('outside', None) for a widget point."""
        corners = self._corners_widget()
        for i, (cx, cy) in enumerate(corners):
            if abs(x - cx) <= HANDLE and abs(y - cy) <= HANDLE:
                return "corner", i
        (x0, y0), (x1, y1) = corners[0], corners[3]
        if x0 <= x <= x1 and y0 <= y <= y1:
            return "inside", None
        return "outside", None

    def _on_editor_motion(self, ctl, x, y):
        if self._src_surface is None:
            self.editor.set_cursor(None)
            return
        kind, idx = self._hit_test(x, y)
        name = {"inside": "move", "outside": "crosshair"}.get(kind)
        if kind == "corner":
            name = "nwse-resize" if idx in (0, 3) else "nesw-resize"
        self.editor.set_cursor(Gdk.Cursor.new_from_name(name, None))

    def _on_drag_begin(self, gesture, x, y):
        self.editor.grab_focus()
        self._drag_mode = None
        if self._ensure_src_surface() is None:
            return
        self._drag_start = (x, y)
        ix, iy = self._widget_to_image(x, y)
        cfg = self.cfg
        kind, idx = self._hit_test(x, y)
        if kind == "inside":
            self._drag_mode = "move"
            self._drag_offset = (ix - self.sel.x, iy - self.sel.y)
        elif kind == "corner":
            self._drag_mode = "resize"
            x0, y0 = self.sel.x, self.sel.y
            x1, y1 = x0 + self.sel.side, y0 + self.sel.side
            self._drag_anchor = [(x1, y1), (x0, y1), (x1, y0), (x0, y0)][idx]
        else:
            self._drag_mode = "draw"
            self._drag_anchor = (ix, iy)

    def _on_drag_update(self, gesture, dx, dy):
        if self._src_surface is None or self._drag_mode is None:
            return
        if self._drag_mode == "draw" and max(abs(dx), abs(dy)) < DRAW_THRESHOLD:
            return   # a plain click outside the square does nothing
        ix, iy = self._widget_to_image(self._drag_start[0] + dx, self._drag_start[1] + dy)
        cfg = self.cfg
        if self._drag_mode == "move":
            self.sel.x = round(ix - self._drag_offset[0])
            self.sel.y = round(iy - self._drag_offset[1])
        else:
            # square spanned from the fixed anchor toward the pointer
            ax, ay = self._drag_anchor
            img = self._source_image()
            side = max(abs(ix - ax), abs(iy - ay))
            side = max(MIN_SIDE, min(round(side), max(img.width, img.height)))
            self.sel.side = side
            self.sel.x = round(ax if ix >= ax else ax - side)
            self.sel.y = round(ay if iy >= ay else ay - side)
        self._selection_changed()

    def _selection_changed(self):
        self._clamp_selection()
        self._update_selection_spins()
        self.editor.queue_draw()
        self.schedule_render()

    def _set_side(self, side):
        if self._source_image() is None:
            return
        cfg = self.cfg
        cx, cy = self.sel.x + self.sel.side / 2, self.sel.y + self.sel.side / 2
        img = self._source_image()
        self.sel.side = max(MIN_SIDE, min(int(side), max(img.width, img.height)))
        self.sel.x = round(cx - self.sel.side / 2)
        self.sel.y = round(cy - self.sel.side / 2)
        self._selection_changed()

    def _fit_selection(self, inside):
        img = self._source_image()
        if img is None:
            return
        cfg = self.cfg
        self.sel.side = (min if inside else max)(img.width, img.height)
        self.sel.x = (img.width - self.sel.side) // 2
        self.sel.y = (img.height - self.sel.side) // 2
        self._selection_changed()

    def _on_editor_scroll(self, ctl, dx, dy):
        state = ctl.get_current_event_state()
        if not state & Gdk.ModifierType.CONTROL_MASK or self._source_image() is None:
            return False
        step = max(1, round(self.sel.side * 0.05))
        self._set_side(self.sel.side + (step if dy < 0 else -step))
        return True

    def _on_editor_key(self, ctl, keyval, keycode, state):
        if self._source_image() is None:
            return False
        step = 10 if state & Gdk.ModifierType.SHIFT_MASK else 1
        cfg = self.cfg
        moves = {Gdk.KEY_Left: (-step, 0), Gdk.KEY_Right: (step, 0),
                 Gdk.KEY_Up: (0, -step), Gdk.KEY_Down: (0, step)}
        if keyval in moves:
            self.sel.x += moves[keyval][0]
            self.sel.y += moves[keyval][1]
            self._selection_changed()
            return True
        if keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self._set_side(self.sel.side + step)
            return True
        if keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self._set_side(self.sel.side - step)
            return True
        return False

    def _on_sel_spin(self, *_):
        if self._loading or self._source_image() is None:
            return
        cfg = self.cfg
        new_side = int(self.spin_side.get_value())
        if new_side != self.sel.side:
            self._set_side(new_side)
            return
        self.sel.x = int(self.spin_x.get_value())
        self.sel.y = int(self.spin_y.get_value())
        self._selection_changed()

    def _on_zoom_changed(self, *_):
        self._apply_zoom_size()
        self.editor.queue_draw()

    def _apply_zoom_size(self):
        z = ZOOMS[self.zoom_dd.get_selected()][1]
        src = self._ensure_src_surface()
        if z is None or src is None:
            self.editor.set_content_width(1)
            self.editor.set_content_height(1)
        else:
            _, _, cw, ch = self._canvas()
            self.editor.set_content_width(int(cw * z))
            self.editor.set_content_height(int(ch * z))

    # ----------------------------------------------------- preview panel
    def _draw_preview(self, area, cr, w, h):
        draw_checker(cr, MASTER_SIZE, MASTER_SIZE)
        if self._master_surf is None:
            return
        if self._crop_surf is not None:
            # faint original so removed parts stay visible and clickable
            cr.set_source_surface(self._crop_surf[0], 0, 0)
            cr.paint_with_alpha(0.3)
        cr.set_source_surface(self._master_surf[0], 0, 0)
        cr.paint()
        if self.cfg.trans_mode == TRANS_OBJECT:
            for x, y, label in self._points():
                cr.arc(x + 0.5, y + 0.5, 5, 0, 6.2832)
                cr.set_source_rgb(*((0.1, 0.8, 0.2) if label else (0.9, 0.1, 0.1)))
                cr.fill_preserve()
                cr.set_source_rgb(1, 1, 1)
                cr.set_line_width(1.5)
                cr.stroke()
        draw_hotspot(cr, self.cfg.hot_x, self.cfg.hot_y)

    def _draw_size_preview(self, area, cr, w, h, size):
        draw_checker(cr, size, size, 8)
        entry = self._size_surfs.get(size)
        if entry is None:
            return
        cr.set_source_surface(entry[0], 0, 0)
        cr.paint()
        scale = size / MASTER_SIZE
        draw_hotspot(cr, round(self.cfg.hot_x * scale), round(self.cfg.hot_y * scale), 4)

    def _draw_swatch(self, area, cr, w, h):
        cr.set_source_rgb(*(c / 255 for c in self.cfg.trans_color))
        cr.rectangle(0, 0, w, h)
        cr.fill()

    def _preview_point(self, x, y):
        return (max(0, min(MASTER_SIZE - 1, int(x))), max(0, min(MASTER_SIZE - 1, int(y))))

    def _on_preview_click(self, gesture, n, x, y):
        if self._crop is None:
            return
        px, py = self._preview_point(x, y)
        button = gesture.get_current_button()
        if self.tool_object.get_active():
            ctrl = gesture.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK
            if button in (1, 3):
                self._add_object_point(px, py, 0 if button == 3 or ctrl else 1)
        elif button != 1:
            return
        elif self.tool_text.get_active():
            self.cfg.text_x, self.cfg.text_y = px, py
            self._loading = True
            self.text_x.set_value(px)
            self.text_y.set_value(py)
            self._loading = False
            self.schedule_render()
        elif self.tool_hotspot.get_active():
            self._set_hotspot(px, py)
        else:
            side = self._crop.width
            cx = min(side - 1, int(px * side / MASTER_SIZE))
            cy = min(side - 1, int(py * side / MASTER_SIZE))
            self.cfg.trans_color = tuple(self._crop.getpixel((cx, cy))[:3])
            self.cfg.trans_mode = TRANS_COLOR
            self._load_role_into_widgets()
            self.schedule_render()

    def _on_preview_drag(self, gesture, dx, dy):
        if self._crop is None:
            return
        ok, sx, sy = gesture.get_start_point()
        if not ok:
            return
        px, py = self._preview_point(sx + dx, sy + dy)
        if self.tool_hotspot.get_active():
            self._set_hotspot(px, py)
        elif self.tool_text.get_active():
            self.text_x.set_value(px)   # triggers _on_text_changed -> render
            self.text_y.set_value(py)

    def _set_hotspot(self, x, y):
        self.cfg.hot_x, self.cfg.hot_y = x, y
        self._loading = True
        self.spin_hx.set_value(x)
        self.spin_hy.set_value(y)
        self._loading = False
        self._redraw_hotspot()

    def _redraw_hotspot(self):
        if self.cfg.arrow:
            self.schedule_render()   # the hotspot icon moves with the hotspot
            return
        self.preview.queue_draw()
        for a in self.size_areas.values():
            a.queue_draw()
        self._update_test_cursor()

    def _on_hot_spin(self, *_):
        if self._loading:
            return
        self.cfg.hot_x = int(self.spin_hx.get_value())
        self.cfg.hot_y = int(self.spin_hy.get_value())
        self._redraw_hotspot()

    def _default_hotspot(self, name):
        fx, fy = ROLE_BY_NAME[name].hotspot
        return round(fx * (MASTER_SIZE - 1)), round(fy * (MASTER_SIZE - 1))

    def _on_default_hotspot(self, *_):
        if self.cfg.is_set:
            self._set_hotspot(*self._default_hotspot(self.current))

    def _on_trans_changed(self, *_):
        if self._loading or not self.cfg.is_set:
            return
        if self.trans_object.get_active():
            if self.cfg.trans_mode != TRANS_OBJECT:
                self.cfg.trans_mode = TRANS_OBJECT
                self.tool_object.set_active(True)
                if not self._ensure_ai_ready():
                    self.cfg.trans_mode = TRANS_NONE
                    self._load_role_into_widgets()
                    return
                if not self._points():
                    self.set_status("Click the object you want to keep in the result preview.")
        elif self.trans_color.get_active():
            self.cfg.trans_mode = TRANS_COLOR
        elif self.trans_flood.get_active():
            self.cfg.trans_mode = TRANS_FLOOD
        else:
            self.cfg.trans_mode = TRANS_NONE
        self.cfg.tolerance = int(self.tol_scale.get_value())
        self._update_obj_widgets()
        self.schedule_render()

    # ------------------------------------------------------ role actions
    def _on_role_selected(self, listbox, row):
        if row is None:
            return
        self.current = row.role_name
        self.frame_idx = 0
        self._src_surface = None
        self._clamp_selection()
        self._load_role_into_widgets()
        self._apply_zoom_size()
        self.editor.queue_draw()
        self.schedule_render()

    def _on_open_image(self, *_):
        dlg = Gtk.FileDialog(title=f"Image for “{self.current}”")
        flt = Gtk.FileFilter(name="Images")
        flt.add_mime_type("image/*")
        flt_all = Gtk.FileFilter(name="All files")
        flt_all.add_pattern("*")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        store.append(flt_all)
        dlg.set_filters(store)
        if self.cfg.source:
            dlg.set_initial_folder(Gio.File.new_for_path(os.path.dirname(self.cfg.source)))
        dlg.open(self, None, self._on_open_image_done)

    def _on_open_image_done(self, dlg, res):
        try:
            path = dlg.open_finish(res).get_path()
        except GLib.Error:
            return
        try:
            img = self.cache.get(path)
            n, duration = imaging.frame_info(path)
        except Exception as e:
            self.alert("Could not open image", str(e))
            return
        cfg = self.cfg
        first = not cfg.is_set
        cfg.source = path
        cfg.same_crop = True
        # an animated GIF/WebP/APNG brings all of its frames and its timing
        cfg.frames = [Frame(path, i) for i in range(min(n, MAX_FRAMES))] if n > 1 else []
        if n > 1 and duration:
            cfg.fps = round(1000 / duration, 2)
        cfg.crop_side = min(MASTER_SIZE, img.width, img.height)
        cfg.crop_x = (img.width - cfg.crop_side) // 2
        cfg.crop_y = (img.height - cfg.crop_side) // 2
        if first:
            cfg.hot_x, cfg.hot_y = self._default_hotspot(self.current)
        self._after_frames_changed(0)
        anim = f", animated: {len(cfg.frames)} frames at {cfg.fps:g} fps" if n > 1 else ""
        self.set_status(f"{self.current}: loaded {path} ({img.width}×{img.height}{anim})")

    def _on_clear_role(self, *_):
        self.project.roles[self.current] = RoleConfig()
        self._on_role_selected(None, self.role_list.get_selected_row())
        self._refresh_role_list()

    def _on_copy_to(self, *_):
        if not self.cfg.is_set:
            self.alert("This role has no image yet")
            return
        win = Gtk.Window(title=f"Copy “{self.current}” to…", transient_for=self,
                         modal=True, default_width=360, default_height=600)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(8)
        box.append(Gtk.Label(xalign=0, wrap=True, label=(
            "Copies image, selection, transparency and hotspot. "
            "You can adjust each role afterwards.")))
        scroll = Gtk.ScrolledWindow(vexpand=True)
        lb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        checks = {}
        for role in ROLES:
            if role.name == self.current:
                continue
            state = "✔" if self.project.roles[role.name].is_set else (
                "✱" if role.required else "○")
            c = Gtk.CheckButton(label=f"{state} {role.name}")
            checks[role.name] = c
            lb.append(c)
        scroll.set_child(lb)
        box.append(scroll)
        btns = Gtk.Box(spacing=6, halign=Gtk.Align.END)

        def select_unset(*_):
            for n, c in checks.items():
                c.set_active(not self.project.roles[n].is_set)

        def do_copy(*_):
            names = [n for n, c in checks.items() if c.get_active()]
            for n in names:
                self.project.roles[n] = copy.deepcopy(self.cfg)   # frames not shared
            self._refresh_role_list()
            self.set_status(f"Copied {self.current} to: {', '.join(names) or 'nothing'}")
            win.close()

        for label, cb in (("Select all unset", select_unset),
                          ("Cancel", lambda *_: win.close()), ("Copy", do_copy)):
            b = Gtk.Button(label=label)
            b.connect("clicked", cb)
            btns.append(b)
        box.append(btns)
        win.set_child(box)
        win.present()

    # ------------------------------------------------------- animation
    def _build_frames_bar(self):
        """Frame strip + frame buttons + fps/play controls under the toolbar."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        ctl = WrapRow()
        ctl.append(Gtk.Label(label="Frames:"))
        for label, tip, cb in (
                ("Add frames…", "Add images as frames after the current one "
                                "(animated GIF/WebP/APNG add all their frames)",
                 self._on_add_frames),
                ("Import video…", "Turn part of a video into frames", self._on_import_video),
                ("Remove frame", "Remove the current frame", self._on_remove_frame),
                ("◀", "Move the current frame earlier", lambda *_: self._move_frame(-1)),
                ("▶", "Move the current frame later", lambda *_: self._move_frame(1))):
            b = Gtk.Button(label=label)
            b.set_tooltip_text(tip)
            b.connect("clicked", cb)
            ctl.append(b)
        self.same_crop_check = Gtk.CheckButton(label="Same selection for all frames")
        self.same_crop_check.connect("toggled", self._on_same_crop)
        ctl.append(self.same_crop_check)
        ctl.append(Gtk.Label(label="FPS"))
        self.fps_spin = Gtk.SpinButton.new_with_range(1, 60, 0.5)
        self.fps_spin.set_digits(1)
        self.fps_spin.connect("value-changed", self._on_fps_changed)
        ctl.append(self.fps_spin)
        self.play_btn = Gtk.ToggleButton(label="▶ Play")
        self.play_btn.set_tooltip_text("Play the animation in the preview and the test area")
        self.play_btn.connect("toggled", self._on_play_toggled)
        ctl.append(self.play_btn)
        self.frame_label = Gtk.Label(xalign=0)
        ctl.append(self.frame_label)
        box.append(ctl)
        scroll = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_size_request(-1, 84)
        self.frames_box = Gtk.Box(spacing=2)
        scroll.set_child(self.frames_box)
        box.append(scroll)
        return box

    def _thumb(self, frame: Frame):
        """56 px thumbnail texture of a frame's source image (cached)."""
        key = (frame.source, frame.index)
        tex = self._thumbs.get(key)
        if tex is None:
            try:
                img = self.cache.get_frame(frame.source, frame.index).copy()
            except Exception:
                img = Image.new("RGBA", (56, 56), (120, 0, 0, 255))   # unreadable frame
            img.thumbnail((56, 56))
            tex = Gdk.MemoryTexture.new(img.width, img.height, Gdk.MemoryFormat.R8G8B8A8,
                                        GLib.Bytes.new(img.tobytes()), img.width * 4)
            self._thumbs[key] = tex
        return tex

    def _rebuild_frame_strip(self):
        while (child := self.frames_box.get_first_child()) is not None:
            self.frames_box.remove(child)
        self._frame_buttons = []
        self._frame_labels = []
        cfg = self.cfg
        if not cfg.is_set:
            self.frames_box.append(Gtk.Label(label="  Open an image first."))
            return
        first = None
        for i, f in enumerate(frame_list(cfg)):
            btn = Gtk.ToggleButton()
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            pic = Gtk.Picture.new_for_paintable(self._thumb(f))
            pic.set_size_request(56, 56)
            inner.append(pic)
            num = Gtk.Label(label=str(i + 1))
            inner.append(num)
            self._frame_labels.append(num)
            btn.set_child(inner)
            if first is None:
                first = btn
            else:
                btn.set_group(first)
            btn.set_active(i == self.frame_idx)
            btn.connect("toggled", self._on_frame_button, i)
            self.frames_box.append(btn)
            self._frame_buttons.append(btn)
        if not cfg.frames:
            hint = Gtk.Label(label="  Still cursor — use “Add frames…” or “Import video…” to animate.")
            hint.add_css_class("dim-label")
            self.frames_box.append(hint)
        self._update_obj_widgets()     # refresh the "own points" dots

    def _load_frame_widgets(self):
        """Sync the frame controls with the current role (called with _loading set)."""
        cfg = self.cfg
        self.same_crop_check.set_active(cfg.same_crop)
        self.fps_spin.set_value(cfg.fps)
        n = len(frame_list(cfg)) if cfg.is_set else 0
        warn = f"  ⚠ over {WARN_FRAMES} frames makes large files" if n > WARN_FRAMES else ""
        self.frame_label.set_label(f"Frame {self.frame_idx + 1}/{n}{warn}" if n else "")
        for w in (self.same_crop_check, self.fps_spin, self.play_btn):
            w.set_sensitive(cfg.is_animated)
        if not cfg.is_animated and self.play_btn.get_active():
            self.play_btn.set_active(False)
        self._rebuild_frame_strip()

    def _after_frames_changed(self, new_idx):
        """Refresh everything after frames were added/removed/reordered."""
        cfg = self.cfg
        if cfg.frames:
            cfg.source = cfg.frames[0].source   # keeps is_set / fallbacks working
        self.frame_idx = new_idx
        self._src_surface = None
        self._clamp_selection()
        self._load_role_into_widgets()
        self._apply_zoom_size()
        self._refresh_role_list()
        self.editor.queue_draw()
        self.schedule_render()

    def _on_frame_button(self, btn, i):
        if self._loading or not btn.get_active() or i == self.frame_idx:
            return
        self.frame_idx = i
        self._src_surface = None
        self._clamp_selection()
        self._loading = True
        try:
            img = self._source_image()
            if img is not None:
                self._set_spin_ranges(img)
            self.spin_x.set_value(self.sel.x)
            self.spin_y.set_value(self.sel.y)
            self.spin_side.set_value(self.sel.side)
            self._update_scale_label()
            n = len(frame_list(self.cfg))
            self.frame_label.set_label(f"Frame {i + 1}/{n}")
        finally:
            self._loading = False
        self._update_obj_widgets()     # per-frame points follow the selected frame
        self._apply_zoom_size()
        self.editor.queue_draw()
        self.schedule_render()

    def _image_filters(self):
        flt = Gtk.FileFilter(name="Images")
        flt.add_mime_type("image/*")
        flt_all = Gtk.FileFilter(name="All files")
        flt_all.add_pattern("*")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        store.append(flt_all)
        return store

    def _on_add_frames(self, *_):
        dlg = Gtk.FileDialog(title=f"Add frames to “{self.current}”")
        dlg.set_filters(self._image_filters())
        dlg.open_multiple(self, None, self._on_add_frames_done)

    def _on_add_frames_done(self, dlg, res):
        try:
            files = dlg.open_multiple_finish(res)
        except GLib.Error:
            return
        paths = sorted(files.get_item(i).get_path() for i in range(files.get_n_items()))
        new = []
        for path in paths:
            try:
                n, _ = imaging.frame_info(path)
                self.cache.get(path)
            except Exception as e:
                self.alert("Could not open image", f"{path}\n{e}")
                return
            new += [Frame(path, i) for i in range(n)]
        cfg = self.cfg
        first = not cfg.is_set
        frames = list(frame_list(cfg)) if cfg.is_set else []
        room = MAX_FRAMES - len(frames)
        if len(new) > room:
            self.set_status(f"Only {room} more frames fit (limit {MAX_FRAMES}); extra ones skipped.")
            new = new[:room]
        if not new:
            return
        pos = self.frame_idx + 1 if frames else 0
        cfg.frames = frames[:pos] + new + frames[pos:]
        if first:
            img = self.cache.get(new[0].source)
            cfg.crop_side = min(MASTER_SIZE, img.width, img.height)
            cfg.crop_x = (img.width - cfg.crop_side) // 2
            cfg.crop_y = (img.height - cfg.crop_side) // 2
            cfg.hot_x, cfg.hot_y = self._default_hotspot(self.current)
        if not cfg.same_crop:   # new frames start from the current selection
            for f in new:
                f.crop = (self.sel.x, self.sel.y, self.sel.side)
        self._after_frames_changed(pos)
        self.set_status(f"Added {len(new)} frame(s); {len(cfg.frames)} in total.")

    def _on_remove_frame(self, *_):
        cfg = self.cfg
        if len(cfg.frames) <= 1:
            self.set_status("A still cursor has one image; use “Clear role” to remove it.")
            return
        del cfg.frames[self.frame_idx]
        if len(cfg.frames) == 1:          # back to a still cursor
            only = cfg.frames[0]
            if not cfg.same_crop and only.crop:
                cfg.crop_x, cfg.crop_y, cfg.crop_side = only.crop
            cfg.frames = [] if only.index == 0 else cfg.frames
            cfg.source = only.source
        self._after_frames_changed(min(self.frame_idx, max(0, len(cfg.frames) - 1)))

    def _move_frame(self, step):
        cfg = self.cfg
        i, j = self.frame_idx, self.frame_idx + step
        if len(cfg.frames) < 2 or not 0 <= j < len(cfg.frames):
            return
        cfg.frames[i], cfg.frames[j] = cfg.frames[j], cfg.frames[i]
        self._after_frames_changed(j)

    def _on_same_crop(self, *_):
        if self._loading or not self.cfg.frames:
            return
        cfg = self.cfg
        if self.same_crop_check.get_active():
            # the current frame's selection becomes the shared one
            cfg.crop_x, cfg.crop_y, cfg.crop_side = frame_crop(cfg, self.frame_idx)
            for f in cfg.frames:
                f.crop = None
            cfg.same_crop = True
        else:
            # every frame starts with its own copy of the shared selection
            for f in cfg.frames:
                f.crop = (cfg.crop_x, cfg.crop_y, cfg.crop_side)
            cfg.same_crop = False
        self.editor.queue_draw()
        self.schedule_render()

    def _on_fps_changed(self, *_):
        if self._loading or not self.cfg.is_set:
            return
        self.cfg.fps = float(self.fps_spin.get_value())
        if self._play_id is not None:     # restart the timer at the new speed
            self._stop_play()
            self._start_play()

    # -- playback
    def _on_play_toggled(self, btn):
        if btn.get_active():
            self._start_play()
        else:
            self._stop_play()
            self.schedule_render()

    def _start_play(self):
        cfg = self.cfg
        if not cfg.is_animated:
            self.play_btn.set_active(False)
            return
        self._play_idx = 0
        if (cfg.trans_mode == TRANS_OBJECT and self._ai_usable()
                and any(frame_points(cfg, i) for i in range(len(cfg.frames)))
                and not all(mask_ready(cfg, i) for i in range(len(cfg.frames)))):
            self._precompute_masks()
        self._play_id = GLib.timeout_add(max(10, round(1000 / cfg.fps)), self._play_tick)

    def _stop_play(self):
        if self._play_id is not None:
            GLib.source_remove(self._play_id)
            self._play_id = None

    def _play_frame(self, i):
        """Rendered 256 master of frame i for playback (unmasked if its AI mask
        is not computed yet)."""
        hit = self._anim_cache.get(i)
        if hit is not None:
            return hit
        cfg = self.cfg
        crop = render_crop(cfg, self.cache, i)
        if cfg.trans_mode == TRANS_OBJECT and not (self._ai_usable() and mask_ready(cfg, i)):
            shown, complete = crop, False
        else:
            shown, complete = apply_transparency(crop, cfg, i), True
        master = add_overlays(imaging.resize(shown, MASTER_SIZE), cfg, self.current,
                              self.project.icon_theme)
        entry = (master, pil_to_surface(master))
        if complete:
            self._anim_cache[i] = entry
        return entry

    def _play_tick(self):
        cfg = self.cfg
        if not cfg.is_animated:
            self._play_id = None
            self.play_btn.set_active(False)
            return False
        self._play_idx = (self._play_idx + 1) % len(cfg.frames)
        try:
            master, surf = self._play_frame(self._play_idx)
        except Exception as e:
            self.set_status(f"Playback failed: {e}")
            self._play_id = None
            self.play_btn.set_active(False)
            return False
        self._master, self._master_surf = master, surf
        self.preview.queue_draw()
        self._update_test_cursor()
        return True

    def _precompute_masks(self):
        """Compute AI masks for every frame in the background (with progress)."""
        cfg = copy.deepcopy(self.cfg)
        n = len(cfg.frames)

        def run():
            cache = ImageCache()          # own cache: ImageCache is not thread-safe
            try:
                for i in range(n):
                    GLib.idle_add(self.set_status, f"Detecting object in frame {i + 1}/{n}…")
                    crop = render_crop(cfg, cache, i)
                    segment.get_segmenter().mask(crop, crop_key(cfg, i),
                                                 crop_points(cfg, crop.width, i))
                GLib.idle_add(self.set_status, f"Object detected in all {n} frames.")
            except Exception as e:
                GLib.idle_add(self.set_status, f"Object detection failed: {e}")
        threading.Thread(target=run, daemon=True).start()

    # -- video
    def _on_import_video(self, *_):
        if not video.available():
            self.alert("ffmpeg is needed for video import",
                       "Install it with: sudo dnf install ffmpeg")
            return
        dlg = Gtk.FileDialog(title=f"Video for “{self.current}”")
        flt = Gtk.FileFilter(name="Videos")
        flt.add_mime_type("video/*")
        flt_all = Gtk.FileFilter(name="All files")
        flt_all.add_pattern("*")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        store.append(flt_all)
        dlg.set_filters(store)

        def chosen(dlg, res):
            try:
                path = dlg.open_finish(res).get_path()
                duration, src_fps = video.probe(path)
            except GLib.Error:
                return
            except Exception as e:
                self.alert("Could not read the video", str(e))
                return
            self._video_dialog(path, duration, src_fps)
        dlg.open(self, None, chosen)

    def _video_dialog(self, path, duration, src_fps):
        """Ask which part of the video to use and at what frame rate."""
        win = Gtk.Window(title="Import video", transient_for=self, modal=True)
        grid = Gtk.Grid(row_spacing=6, column_spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(grid, f"set_margin_{side}")(12)
        grid.attach(Gtk.Label(xalign=0, label=f"{os.path.basename(path)}\n"
                              f"{duration:.1f} s at {src_fps:.0f} fps"), 0, 0, 2, 1)
        fps = Gtk.SpinButton.new_with_range(1, 60, 1)
        fps.set_value(min(12, src_fps or 12))
        start = Gtk.SpinButton.new_with_range(0, max(0, duration - 0.1), 0.1)
        start.set_digits(1)
        length = Gtk.SpinButton.new_with_range(0.1, max(0.1, duration), 0.1)
        length.set_digits(1)
        length.set_value(min(duration, 5.0))
        count = Gtk.Label(xalign=0)
        rows = (("Frames per second", fps), ("Start at (s)", start), ("Length (s)", length))
        for r, (label, w) in enumerate(rows, start=1):
            grid.attach(Gtk.Label(xalign=0, label=label), 0, r, 1, 1)
            grid.attach(w, 1, r, 1, 1)
        grid.attach(count, 0, 4, 2, 1)

        def update(*_):
            n = min(MAX_FRAMES, max(1, round(fps.get_value() * length.get_value())))
            warn = f" (over {WARN_FRAMES}: large files)" if n > WARN_FRAMES else ""
            count.set_label(f"→ {n} frames{warn}. The same selection and transparency "
                            "apply to every frame.")
        for w in (fps, length):
            w.connect("value-changed", update)
        update()
        btns = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: win.close())
        ok = Gtk.Button(label="Import")
        ok.add_css_class("suggested-action")

        def do_import(*_):
            args = (path, fps.get_value(), start.get_value(), length.get_value())
            win.close()
            self._extract_video(*args)
        ok.connect("clicked", do_import)
        btns.append(cancel)
        btns.append(ok)
        grid.attach(btns, 0, 5, 2, 1)
        win.set_child(grid)
        win.present()

    def _extract_video(self, path, fps, start, length):
        """Run ffmpeg in the background, then turn the frames into this role's animation."""
        role = self.current
        self.set_status("Extracting video frames…")

        def progress(done, total):
            GLib.idle_add(self.set_status, f"Extracting video frames… {done}/{total}")

        def run():
            try:
                frames = video.extract_frames(path, fps, start, length, MAX_FRAMES, progress)
                GLib.idle_add(finished, frames, None)
            except Exception as e:
                GLib.idle_add(finished, None, str(e))

        def finished(frames, err):
            if err:
                self.alert("Video import failed", err)
                self.set_status("Video import failed.")
                return False
            cfg = self.project.roles[role]
            first = not cfg.is_set
            img = self.cache.get(frames[0])
            cfg.frames = [Frame(f) for f in frames]
            cfg.source = frames[0]
            cfg.fps = float(fps)
            cfg.same_crop = True
            cfg.crop_side = min(img.width, img.height)      # largest square inside
            cfg.crop_x = (img.width - cfg.crop_side) // 2
            cfg.crop_y = (img.height - cfg.crop_side) // 2
            if first:
                cfg.hot_x, cfg.hot_y = self._default_hotspot(role)
            if role == self.current:
                self._after_frames_changed(0)
            else:
                self._refresh_role_list()
            self.set_status(f"{role}: {len(frames)} video frames at {fps:g} fps. Pick a "
                            "transparency option to remove the background.")
            return False
        threading.Thread(target=run, daemon=True).start()

    # ---------------------------------------------------- project / theme
    def _browse_folder(self, entry, title):
        """Folder picker that writes the chosen path into `entry`."""
        dlg = Gtk.FileDialog(title=title)
        d = os.path.expanduser(entry.get_text().strip() or "~")
        if os.path.isdir(d):
            dlg.set_initial_folder(Gio.File.new_for_path(d))

        def done(dlg, res):
            try:
                entry.set_text(dlg.select_folder_finish(res).get_path())
            except GLib.Error:
                pass
        dlg.select_folder(self, None, done)

    def _json_filters(self):
        flt = Gtk.FileFilter(name="Cursor Maker project")
        flt.add_pattern("*.json")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        return store

    def _on_save_project(self, *_):
        self._read_project_widgets()
        dlg = Gtk.FileDialog(title="Save project")
        dlg.set_filters(self._json_filters())
        dlg.set_initial_name(f"{self.project.theme_name or 'cursors'}.cursorproj.json")

        def done(dlg, res):
            try:
                path = dlg.save_finish(res).get_path()
            except GLib.Error:
                return
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.project.to_json())
            self.set_status(f"Project saved to {path}")
        dlg.save(self, None, done)

    def _on_load_project(self, *_):
        dlg = Gtk.FileDialog(title="Load project")
        dlg.set_filters(self._json_filters())

        def done(dlg, res):
            try:
                path = dlg.open_finish(res).get_path()
                with open(path, encoding="utf-8") as f:
                    self.project = Project.from_json(f.read())
            except GLib.Error:
                return
            except Exception as e:
                self.alert("Could not load project", str(e))
                return
            self._after_project_loaded()
            self.set_status(f"Loaded project {path}")
        dlg.open(self, None, done)

    # -- import an existing cursor set (Linux theme, Windows set, macOS cape)
    def _on_import_set(self, *_):
        """Ask folder or file, confirm replacing the current cursors, then import."""
        dlg = Gtk.AlertDialog(
            message="Import an existing cursor set",
            detail="Folder: a Linux cursor theme (e.g. ~/.icons/Name) or a folder of "
                   "Windows .cur/.ani files.\nFile: a .zip or .tar.gz archive, a macOS "
                   "Mousecape .cape, or a Windows install.inf.\n\nThe imported cursors "
                   "replace the ones in the editor (save your project first if needed).")
        dlg.set_buttons(["Cancel", "Folder…", "File…"])
        dlg.set_cancel_button(0)
        dlg.set_default_button(2)

        def chosen(dlg, res):
            try:
                choice = dlg.choose_finish(res)
            except GLib.Error:
                return
            if choice == 1:
                picker = Gtk.FileDialog(title="Cursor theme or Windows cursor folder")
                picker.select_folder(self, None, self._on_import_picked, "folder")
            elif choice == 2:
                picker = Gtk.FileDialog(title="Cursor set file")
                flt = Gtk.FileFilter(name="Cursor sets (.zip .tar.* .cape .inf .cur .ani)")
                for pat in ("*.zip", "*.tar", "*.tar.gz", "*.tgz", "*.tar.xz", "*.txz",
                            "*.tar.bz2", "*.cape", "*.inf", "*.cur", "*.ani"):
                    flt.add_pattern(pat)
                store = Gio.ListStore.new(Gtk.FileFilter)
                store.append(flt)
                picker.set_filters(store)
                picker.open(self, None, self._on_import_picked, "file")
        dlg.choose(self, None, chosen)

    def _on_import_picked(self, picker, res, kind):
        try:
            f = picker.select_folder_finish(res) if kind == "folder" else picker.open_finish(res)
        except GLib.Error:
            return
        path = f.get_path()
        self.set_status(f"Importing {path}…")

        def run():
            try:
                imp = importer.import_set(path)
                GLib.idle_add(done, imp, None)
            except Exception as e:
                GLib.idle_add(done, None, e)

        def done(imp, error):
            if error is not None:
                self.alert("Could not import this cursor set", str(error))
                self.set_status("Import failed.")
                return False
            try:
                notes = importer.apply_import(self.project, imp, path)
            except Exception as e:
                self.alert("Could not import this cursor set", str(e))
                return False
            self.cache = ImageCache()
            self._thumbs.clear()
            self._after_project_loaded()
            kinds = {"linux": "Linux theme", "windows": "Windows cursor set",
                     "mac": "macOS cape"}
            animated = sum(1 for c in imp.cursors.values() if len(c.frames) > 1)
            msg = (f"Imported {kinds[imp.kind]} “{imp.name}”: {len(imp.cursors)} cursors "
                   f"({animated} animated). Theme name set to “{self.project.theme_name}”.")
            if notes:
                msg += "\n" + "; ".join(notes[:8]) + (" …" if len(notes) > 8 else "")
            self.set_status(msg)
            return False
        threading.Thread(target=run, daemon=True).start()

    def _after_project_loaded(self):
        self._sync_project_widgets()
        self._refresh_role_list()
        self._on_role_selected(None, self.role_list.get_selected_row())

    def _on_generate(self, *_, overwrite=False):
        self._read_project_widgets()
        missing = self.project.missing_required()
        if missing:
            self.alert("Some required cursors have no image",
                       "Missing: " + ", ".join(missing))
            return

        def work(project, cache, progress, cancel):
            return build_theme(project, cache, overwrite_foreign=overwrite,
                               progress=progress, cancel=cancel)
        self._run_export("Generating theme…", work, self._generate_done)

    def _generate_done(self, result, e):
        if isinstance(e, Cancelled):
            self.set_status("Generating cancelled. Files already finished were kept; "
                            "nothing was left half-written.")
            return
        if isinstance(e, ThemeExistsError):
            dlg = Gtk.AlertDialog(message="Folder already contains another theme",
                                  detail=f"{e}\nwas not created by Cursor Maker. Overwrite it?")
            dlg.set_buttons(["Cancel", "Overwrite"])
            dlg.set_cancel_button(0)
            dlg.set_default_button(0)

            def chosen(dlg, res):
                try:
                    if dlg.choose_finish(res) == 1:
                        self._on_generate(overwrite=True)
                except GLib.Error:
                    pass
            dlg.choose(self, None, chosen)
            return
        if e is not None:
            self.alert("Generating the theme failed", str(e))
            self.set_status("Generating the theme failed.")
            return
        out, notes, extra = result
        self._autosave()
        sizes = "256/192/128/96" + ("/64/48/32/24" if self.project.include_small else "")
        fallback = sum(1 for n in notes if "using image" in n)
        msg = (f"Theme written to {out} (sizes {sizes}; {fallback} roles use a "
               f"fallback image). Click “Apply to GNOME” to use it.")
        if extra:
            msg += "\nAlso written: " + "  ·  ".join(extra)
            msg += self._thinned_note([n for n in notes if "Windows size limit" in n])
            msg += "".join(self._windows_size_warning(p) for p in extra if p.endswith(".zip"))
        self.set_status(msg)

    @staticmethod
    def _thinned_note(notes):
        """Status text listing Windows cursors that had frames dropped."""
        thinned = [n.replace(" (Windows size limit)", "") for n in notes
                   if "Windows size limit" in n]
        if not thinned:
            return ""
        return ("\nWindows only loads animated cursors up to about 2 MB, so frames were "
                "dropped evenly (each animation keeps its length but is less smooth): "
                + "; ".join(thinned))

    @staticmethod
    def _windows_size_warning(zip_path):
        """Status text naming Windows cursor files that may be too big to load."""
        from .platforms import large_windows_files
        big = large_windows_files(zip_path)
        if not big:
            return ""
        names = ", ".join(f"{n} ({size / 1e6:.0f} MB)" for n, size in big)
        return (f"\n⚠ Very large Windows cursors: {names}. Windows may call these "
                "“missing or corrupt”; use fewer frames for those cursors if it does.")

    def _on_apply(self, *_):
        self._read_project_widgets()
        out = theme_dir(self.project)
        if not os.path.isfile(os.path.join(out, "index.theme")):
            self.alert("Theme not generated yet", f"{out} has no index.theme")
            return
        parent = os.path.realpath(os.path.dirname(out))
        if parent not in [os.path.realpath(d) for d in ICON_DIRS]:
            self.alert("GNOME will not find this theme",
                       "Generate it into ~/.icons or ~/.local/share/icons to apply it.")
            return
        size = EXPORT_SIZES[self.apply_size_dd.get_selected()]
        name = os.path.basename(out)
        key = "org.gnome.desktop.interface"
        try:
            # switching away first forces GNOME to reload a regenerated theme
            subprocess.run(["gsettings", "set", key, "cursor-theme", "Adwaita"], check=True)
            subprocess.run(["gsettings", "set", key, "cursor-theme", name], check=True)
            subprocess.run(["gsettings", "set", key, "cursor-size", str(size)], check=True)
        except (OSError, subprocess.CalledProcessError) as e:
            self.alert("gsettings failed", str(e))
            return
        self.set_status(f"Applied cursor theme “{name}” at size {size}. "
                        "Some apps pick it up only after restarting them (or logging out).")

    # ------------------------------------------------------- autosave
    def _autosave(self):
        self._read_project_widgets()
        try:
            os.makedirs(os.path.dirname(AUTOSAVE), exist_ok=True)
            with open(AUTOSAVE, "w", encoding="utf-8") as f:
                f.write(self.project.to_json())
        except OSError as e:
            print("autosave failed:", e)

    def _load_autosave(self):
        try:
            with open(AUTOSAVE, encoding="utf-8") as f:
                self.project = Project.from_json(f.read())
        except FileNotFoundError:
            pass
        except Exception as e:
            print("could not read autosave:", e)

    def _on_close(self, *_):
        if getattr(self, "_export_cancel", None) is not None:
            self._export_cancel.set()      # stop a running export; no half files remain
        self._autosave()
        return False


class CursorMakerApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="io.github.cursormaker.CursorMaker",
                         flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_activate(self):
        CursorMakerWindow(self).present()


def main():
    return CursorMakerApp().run(None)
