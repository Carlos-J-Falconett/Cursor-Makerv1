# Cursor Maker

GTK4 app for building a complete Xcursor theme for GNOME (Wayland or X11) from any images. It can also export the same cursor set for Windows 11 and macOS.

## Run
```
sudo dnf install python3-gobject gtk4 python3-pillow python3-cairo ffmpeg   # usually already installed
python3 main.py
```

Optional, for AI object selection: `pip install --user numpy onnxruntime`. The first time you use it, the app offers to download the MobileSAM model (~45 MB, MIT license) to `~/.cache/cursor-maker/models/`.

## Window layout
The window works at any size and on rotated (portrait) screens:
- **Wide windows:** three resizable columns (cursor list | editor | preview).
- **Tall or portrait windows:** the preview moves under the editor.
- **Narrow windows (under 760 px):** the cursor list, editor and preview are stacked.
- Drag the dividers between panels to resize them. Toolbars wrap onto more lines instead of forcing a minimum width. If the window is ever smaller than the controls need, it scrolls rather than cutting anything off.

## Workflow
1. Pick a cursor role on the left (✱ = required, ○ = optional and uses the role after →).
2. **Open image…** (PNG, JPEG, WebP, BMP, GIF, TIFF… anything Pillow reads, any size). An animated GIF/WebP/APNG imports all of its frames. See [Animated cursors](#animated-cursors).
3. Select a square of any size:
   - Drag outside the current square to draw a new one.
   - Drag a corner handle to resize, or drag inside to move.
   - "Largest inside" and "Fit whole image" set the size for you. Ctrl+scroll, +/- or the Size box also work.
   - The square may extend past the image edges; that area becomes transparent.
   - The selection is scaled up or down to 256×256 automatically. The line under the editor says which.
4. Transparency:
   - *Keep clicked object (AI)*: click the object in the result preview and everything else becomes transparent. Left-click adds more of the object, right-click (or Ctrl+click) removes a part. Use Undo point / Clear points to fix mistakes. The original stays faintly visible under the result so removed parts can still be clicked.
   - *Remove color*: choose "Pick color", then click the result preview.
   - *Remove background*: flood fill from the corners, for plain backgrounds.
   - Tolerance controls how much the two non-AI options remove.
5. Set the hotspot (the pixel that actually clicks) by clicking the preview with "Set hotspot". Optionally tick **Show icon at hotspot** so it's easy to see where the cursor clicks:
   - **Cursor icon for this role** (default): each cursor shows its own standard icon. That's the arrow on *default*, the pointing hand on *pointer*, the I-beam on *text*, ↔ on the left/right resize, and so on. Each icon's own tip sits exactly on your hotspot.
   - Icons come from an installed cursor theme chosen in **Icons from** (Adwaita by default, the same for every cursor). Adwaita is licensed CC-BY-SA/LGPL, which is fine for personal themes.
   - A cursor copied to other roles, or used as a fallback, still gets the correct icon for each role.
   - **Size (px)** is the icon size on the 256×256 cursor. **Use this size for all cursors** applies it everywhere so they match.
   - **Flat arrow** is the older option: a simple arrow with 8 directions and your own fill/outline colors.
6. Text: type in the **Text** box to draw text on the cursor (Enter starts a new line). Choose the font, bold/italic, size, color, outline color and thickness. Position it with Center X/Y, or pick the "Place text" tool and click or drag in the preview.
7. **Copy this role to…** reuses an image for several roles, for example the resize arrows.
8. **Generate theme** writes:
   - `<output>/<name>/cursors/*`: each file holds 256/192/128/96 (plus 64/48/32/24 if ticked) and every animation frame.
   - Symlinks for cursors that fall back to another one, and for the legacy alias names.
   - `index.theme`.
9. **Apply to GNOME** sets `cursor-theme` and `cursor-size` with gsettings.
10. Optional: under **Also export for**, tick Windows 11 and/or macOS before generating. Those files go to `<Export folder>/<name>/` (default `~/Documents/Cursor Maker/<name>/`). See below.
    - **Export Windows only** and **Export macOS only** write just that package to the export folder, without generating or touching the Linux theme.
    - Generating and exporting run in the background. A progress bar shows the current step, the window stays usable, and **Cancel export** stops the run.
    - Files are written under a temporary `.part` name and only renamed when finished. A cancelled or interrupted export never leaves a half-written file that looks complete; the previous package stays as it was.
    - Big animated projects with AI background removal take a while the first time (about 1 s per frame for the AI). Finished AI masks are saved to `~/.cache/cursor-maker/masks/`, so later exports and later sessions reuse them and run much faster. Deleting that folder is safe; it's only a cache.

Your work is autosaved to `~/.config/cursor-maker/autosave.json` when you close the app or generate a theme. Use Save/Load project to keep more than one.

To go back to the default GNOME cursor: `gsettings set org.gnome.desktop.interface cursor-theme Adwaita` and `gsettings set org.gnome.desktop.interface cursor-size 24`.

## Importing an existing cursor set
**Import cursor set…** (next to Save/Load project) loads a finished set so you can change it. It replaces the cursors currently in the editor.
- **Folder:** a Linux cursor theme (for example `~/.icons/Bibata-…` or `/usr/share/icons/Adwaita`), or a folder of Windows `.cur`/`.ani` files.
- **File:** a `.zip` or `.tar.gz/.xz/.bz2` archive of either kind, a macOS Mousecape `.cape`, or a Windows `install.inf`.

Each cursor is imported at its largest size, with its hotspot and animation frames and speed. Windows cursors are assigned to roles from `install.inf`. Without one, they're matched by file name (normal, busy, link, …). Required cursors a set doesn't have are copied from the closest one: Windows has no single-edge resize or grab cursors, for example. The status bar lists these substitutions. Imported frames are saved to `~/.cache/cursor-maker/imports/`, and the theme name becomes `<name>-Edit` so the original isn't overwritten. Small originals (e.g. 32 px Windows cursors) look soft when scaled to 256 px.

## Windows 11 export
`<name>-Windows.zip` contains one `.cur` file per Windows pointer slot (17 in all), an `install.inf` and an `INSTALL.txt`. To install on Windows:
1. Extract the zip.
2. Right-click `install.inf` > "Show more options" > "Install" (allow the admin prompt). The files are copied to `C:\Windows\Cursors\<name>`.
3. Settings > Bluetooth & devices > Mouse > Additional mouse settings > Pointers tab. Pick the scheme and click Apply.
4. Pick the scheme with **exactly** the theme's name. A scheme from an earlier install under another name (for example `MyTheme_Windows`) points to a folder that doesn't exist and reports "missing or corrupt". Check the path in the error message, and delete old schemes with the **Delete** button.
5. If a pointer shows as missing, the install step didn't copy the files. Run "Install" again and approve the administrator prompt; the files must end up in `C:\Windows\Cursors\<name>`.

Each `.cur` (and every frame of an `.ani`) holds the same sizes as the Linux theme. Sizes of 64 px and up are stored PNG-compressed, which Windows supports since Vista. Without that, large animated cursors reached tens of MB, and Windows' Pointers tab rejected them as "missing or corrupt". Windows also refuses big animated cursors. It documents no limit, but in testing a 1.6 MB `.ani` loaded and every file from 2.4 MB up was rejected. So the export keeps each `.ani` under 1.6 MB by dropping frames evenly and lengthening each frame's display time: the animation lasts as long but is less smooth. The status bar lists which cursors were reduced and to how many frames. For smoother Windows animations, use fewer frames or a smaller selection.

**Windows: full size (test)** (checkbox in the export row) turns that limit off and keeps every frame. It's for testing whether Windows itself shows big animated cursors when you skip the Pointers tab. The package is named `<name>-FullSize` and installs as a separate scheme, so your normal one isn't touched. After installing it, run the included **Apply-Cursors.bat** (or sign out and back in) instead of using the Pointers tab.

Every Windows package includes `Apply-Cursors.bat`. It reloads the cursors that `install.inf` registered, using `SystemParametersInfo(SPI_SETCURSORS)`, without the Pointers dialog or signing out. Windows' default pointer size is 32 px, so tick **Also 64/48/32/24** when exporting for Windows. Windows has fewer slots than Linux, so some share an image:
- Handwriting and Alternate Select use the normal arrow.
- Location Select and Person Select use the link hand.

## macOS export
macOS has no built-in way to change the cursor theme, so the export is a `.cape` file for the free [Mousecape](https://github.com/alexzielenski/Mousecape/releases) app. Open Mousecape, drag the `.cape` in, then choose Cape > Apply. The cursors are 32 pt with images at Mousecape's standard 1×/2×/5×/10× (32–320 px). Make them bigger with System Settings > Accessibility > Display > Pointer size. Instructions are also written to `INSTALL-macOS.txt`.

**macOS 26 (Tahoe):** Tahoe looks up the arrow and text cursor under new names (`com.apple.coregraphics.ArrowS` / `IBeamS`), and the crosshair under `com.apple.cursor.20`. The cape includes these, copied from your arrow, text and crosshair cursors. Older Mousecape builds may still need the Tahoe-patched version. Install Mousecape's helper tool so the cape stays applied.

Animated cursors become vertical frame strips. Mousecape allows at most 24 frames, so longer animations are thinned evenly and keep the same total length.

## Animated cursors
Any cursor can be animated. Use the **Frames** row under the toolbar:
- **Open image…** with an animated GIF/WebP/APNG imports all of its frames and its speed.
- **Add frames…** adds one or more images (multi-select) after the current frame. Animated files add all their frames. Use **◀ ▶** to reorder and **Remove frame** to delete.
- **Import video…** (needs `ffmpeg`): pick a video, then choose the frames per second to sample, a start time and a length. The dialog shows how many frames that gives.
  - Frames are saved to `~/.cache/cursor-maker/video/`, so projects keep working. Big videos are scaled down to 1280 px first.
  - Remove the background with any transparency option; they apply to every frame.
- Click a thumbnail to edit that frame. **Same selection for all frames** (on by default) uses one square for every frame. Untick it to give each frame its own square, for example when frames come from different pictures.
- **AI object clicks per frame:** by default the "Keep clicked object (AI)" clicks are shared by all frames. That works when the object stays in place. If it moves, untick **Same object points for all frames**, pick a frame in the strip, and click the object on that frame. Each frame starts with a copy of the shared clicks. Frames with their own clicks show a • in the strip. **Copy these points to all frames** copies the current frame's clicks to every frame. Re-ticking the box makes the current frame's clicks the shared ones.
- The hotspot, hotspot icon and text apply to every frame.
- **FPS** sets the playback speed. **▶ Play** plays the animation in the preview and in the "hover to test" area. In AI mode, Play first detects the object in every frame, with progress shown in the status bar.
- Limit: 120 frames per cursor. Above 60, the app warns because the files get large.

How each system plays them:

| System | Format |
|---|---|
| Linux / GNOME | Xcursor frames with per-frame delay |
| Windows | `.ani` files, one multi-size `.cur` per frame. `install.inf` points at them automatically. Timing is rounded to 1/60 s. |
| macOS | Mousecape frame strips, up to 24 frames |

## Code layout
| File | Purpose |
|---|---|
| `main.py` | Starts the app |
| `cursor_maker/app.py` | GTK4 window, editor, previews, all controls |
| `cursor_maker/roles.py` | Cursor roles, fallbacks, legacy alias names |
| `cursor_maker/project.py` | Per-role settings, animation frames, project save/load, rendering of each 256×256 frame |
| `cursor_maker/imaging.py` | Pillow helpers: crop, transparency, resize, hotspot icon/arrow, text |
| `cursor_maker/cursor_icons.py` | Finds installed cursor themes and loads each role's standard icon |
| `cursor_maker/segment.py` | Optional MobileSAM "keep clicked object" segmentation |
| `cursor_maker/video.py` | Video → frames with ffmpeg (probe, extract, cache) |
| `cursor_maker/importer.py` | Imports Linux themes, Windows .cur/.ani sets and macOS .cape files for editing |
| `cursor_maker/theme.py` | Writes the Linux theme; `export_platform` exports Windows or macOS on its own |
| `cursor_maker/xcursor.py` | Xcursor file writer/reader, including pixel decoding (Linux) |
| `cursor_maker/wincur.py` | `.cur` and animated `.ani` writer/reader (Windows) |
| `cursor_maker/platforms.py` | Windows scheme package (`install.inf`, zip) and macOS Mousecape `.cape` |
| `cursor_maker/fileutil.py` | Crash-safe writing (`.part` files renamed when complete) |
