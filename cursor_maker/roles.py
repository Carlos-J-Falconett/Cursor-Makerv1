"""Cursor roles that make up a complete theme.

Each role becomes one Xcursor file named after the CSS cursor name.  Legacy
X11 / Qt names are created as symlinks pointing at the role file.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    required: bool
    fallback: str | None = None          # role used when this one has no image
    hotspot: tuple[float, float] = (0.5, 0.5)  # default hotspot as fraction of size
    aliases: tuple[str, ...] = field(default_factory=tuple)


TOP_LEFT = (0.0, 0.0)
CENTER = (0.5, 0.5)

ROLES: list[Role] = [
    # ---- core (required) -------------------------------------------------
    Role("default", "Normal arrow pointer", True, None, TOP_LEFT,
         ("left_ptr", "arrow", "top_left_arrow", "x-cursor", "X_cursor", "right_ptr")),
    Role("pointer", "Hand over links / clickable items", True, None, CENTER,
         ("hand1", "hand2", "hand", "pointing_hand",
          "9d800788f1b08800ae810202380a0822", "e29285e634086352946a0e7090d73106")),
    Role("text", "Text selection I-beam", True, None, CENTER, ("xterm", "ibeam")),
    Role("wait", "Busy - cannot interact", True, None, CENTER, ("watch",)),
    Role("progress", "Busy in background - can still interact", True, None, TOP_LEFT,
         ("left_ptr_watch", "half-busy",
          "08e8e1c95fe2fc01f976f1e063a24ccd", "3ecb610c1bf2410f44200f48c40d3599")),
    Role("crosshair", "Precise selection", True, None, CENTER,
         ("cross", "tcross", "cross_reverse", "diamond_cross")),
    Role("move", "Something is being moved", True, None, CENTER,
         ("fleur", "size_all", "4498f0e0c1937ffe01fd06f973665830",
          "fcf21c00b30f7e3f83fe0dfd12e71cff")),
    Role("not-allowed", "Action not allowed", True, None, CENTER,
         ("crossed_circle", "forbidden", "circle", "03b6e0fcb3499374a867c041f52298f0")),
    Role("help", "Help available", True, None, TOP_LEFT,
         ("question_arrow", "whats_this", "left_ptr_help",
          "5c6cd98b3f3ebcb1f9c7f1c204630408", "d9ce0ab605698f320427677b458ad60b")),
    Role("grab", "Can be grabbed (open hand)", True, None, CENTER, ("openhand",)),
    Role("grabbing", "Being dragged (closed hand)", True, None, CENTER,
         ("closedhand", "dnd-none", "dnd-move", "9081237383d90e509aa00f00170e968f")),
    Role("n-resize", "Resize top edge", True, None, CENTER, ("top_side",)),
    Role("s-resize", "Resize bottom edge", True, None, CENTER, ("bottom_side",)),
    Role("e-resize", "Resize right edge", True, None, CENTER, ("right_side",)),
    Role("w-resize", "Resize left edge", True, None, CENTER, ("left_side",)),
    Role("ne-resize", "Resize top-right corner", True, None, CENTER, ("top_right_corner",)),
    Role("nw-resize", "Resize top-left corner", True, None, CENTER, ("top_left_corner",)),
    Role("se-resize", "Resize bottom-right corner", True, None, CENTER, ("bottom_right_corner",)),
    Role("sw-resize", "Resize bottom-left corner", True, None, CENTER, ("bottom_left_corner",)),
    # ---- optional (fall back to another role) ----------------------------
    Role("ew-resize", "Resize left/right (double arrow)", False, "e-resize", CENTER,
         ("sb_h_double_arrow", "h_double_arrow", "size_hor",
          "028006030e0e7ebffc7f7070c0600140")),
    Role("ns-resize", "Resize up/down (double arrow)", False, "n-resize", CENTER,
         ("sb_v_double_arrow", "v_double_arrow", "size_ver",
          "00008160000006810000408080010102")),
    Role("nesw-resize", "Resize diagonal / (double arrow)", False, "ne-resize", CENTER,
         ("fd_double_arrow", "size_bdiag", "fcf1c3c7cd4491d801f1e1c78f100000")),
    Role("nwse-resize", "Resize diagonal \\ (double arrow)", False, "nw-resize", CENTER,
         ("bd_double_arrow", "size_fdiag", "c7088f0f3e6c8088236ef8e1e3e70000")),
    Role("col-resize", "Resize column", False, "ew-resize", CENTER,
         ("split_h", "14fef782d02440884392942c11205230")),
    Role("row-resize", "Resize row", False, "ns-resize", CENTER,
         ("split_v", "2870a09082c103050810ffdffffe0204")),
    Role("all-scroll", "Scroll in any direction", False, "move", CENTER, ()),
    Role("context-menu", "Context menu available", False, "default", TOP_LEFT, ()),
    Role("alias", "Create shortcut / link", False, "default", TOP_LEFT,
         ("link", "dnd-link", "3085a0e285430894940527032f8b26df",
          "640fb0e74195791501fd1ed57b41487f")),
    Role("copy", "Copy", False, "default", TOP_LEFT,
         ("dnd-copy", "1081e37283d90000800003c07f3ef6bf",
          "6407b0e94181790501fd1e167b474872", "b66166c04f8c3109214a4fbd64a50fc8")),
    Role("no-drop", "Cannot drop here", False, "not-allowed", TOP_LEFT, ("dnd-no-drop",)),
    Role("dnd-ask", "Drop - ask what to do", False, "help", TOP_LEFT, ()),
    Role("cell", "Table cell selection", False, "crosshair", CENTER, ("plus",)),
    Role("vertical-text", "Vertical text selection", False, "text", CENTER, ()),
    Role("zoom-in", "Zoom in", False, "pointer", CENTER, ()),
    Role("zoom-out", "Zoom out", False, "pointer", CENTER, ()),
]

ROLE_BY_NAME: dict[str, Role] = {r.name: r for r in ROLES}
