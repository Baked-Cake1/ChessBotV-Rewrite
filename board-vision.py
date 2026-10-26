import math
import sys
from pathlib import Path

import cv2
import numpy as np

OUTPUT_DIR = Path("board_debug")
SUPPORTED_IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff",
}

VIEW_W, VIEW_H = 1152, 864
PREVIEW_SIZE = 380


def _clean_input_path(value):
    """Clean a path dropped/pasted into a terminal (quotes and whitespace)."""
    return str(value).strip().strip('"').strip("'").strip()


def _expand_image_inputs(values):
    """Expand dropped files/folders into a flat list of image Paths."""
    paths = []
    for value in values:
        if not value:
            continue
        path = Path(_clean_input_path(value)).expanduser()
        if path.is_dir():
            paths.extend(
                sorted(
                    item for item in path.iterdir()
                    if item.is_file()
                    and item.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
                )
            )
        else:
            paths.append(path)
    return paths


def _choose_with_file_dialog():
    """Open a native file picker for one or more screenshots, if possible."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        try:
            selected = filedialog.askopenfilenames(
                title="Choose a chess screenshot",
                filetypes=[
                    ("Image files", "*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff"),
                    ("All files", "*.*"),
                ],
            )
        finally:
            root.destroy()
        return _expand_image_inputs(selected)
    except Exception:
        return []


def choose_image_paths(argv=None):
    """Return screenshots supplied by drag-drop/CLI, a file picker, or paste.

    Dragging image files onto this script (or passing them on the command
    line) supplies ``argv``. With no arguments, a native file picker opens;
    if that is unavailable/cancelled, the terminal prompt accepts a file
    dropped or pasted into it.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    supplied = [arg for arg in args if arg and not arg.startswith("-")]
    if supplied:
        return _expand_image_inputs(supplied)

    selected = _choose_with_file_dialog()
    if selected:
        return selected

    try:
        raw = input("Drop an image here or paste its path (Enter to cancel): ")
    except (EOFError, KeyboardInterrupt):
        return []
    if not raw.strip():
        return []
    return _expand_image_inputs([raw])


def load_screenshot(image_path):
    """Load one dropped/selected screenshot with a clear error if bad."""
    image_path = Path(image_path)
    image = cv2.imread(str(image_path))
    if image is None:
        raise FileNotFoundError(
            f"Could not load image: {image_path.resolve()}"
        )
    return image


class ZoomView:
    """Zoomable/pannable OpenCV window for pixel-accurate work.

    Mouse wheel (or +/-) zooms around the cursor, right- or middle-drag
    pans, 0 resets to fit. When zoomed in, rendering uses INTER_NEAREST
    so individual source pixels stay crisp instead of blurring together.
    """

    def __init__(self, title, image):
        self.title = title
        self.img_h, self.img_w = image.shape[:2]
        self.fit_zoom = min(VIEW_W / self.img_w, VIEW_H / self.img_h)
        self.min_zoom = self.fit_zoom * 0.25
        self.max_zoom = max(60.0, self.fit_zoom * 120.0)
        self.mouse = (VIEW_W // 2, VIEW_H // 2)
        self.ldown = False
        self._panning = False
        self._pan_last = None
        self.reset()
        cv2.namedWindow(title, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(title, VIEW_W, VIEW_H)
        cv2.setMouseCallback(title, self._on_mouse)

    def reset(self):
        """Fit the whole image in the window, centred."""
        self.zoom = self.fit_zoom
        self.ox = (self.img_w - VIEW_W / self.zoom) / 2.0
        self.oy = (self.img_h - VIEW_H / self.zoom) / 2.0

    def mouse_image(self):
        """Current mouse position in image pixel coordinates."""
        fx = self.ox + self.mouse[0] / self.zoom
        fy = self.oy + self.mouse[1] / self.zoom
        return math.floor(fx), math.floor(fy)

    def zoom_by(self, factor):
        """Zoom in/out keeping the image point under the cursor fixed."""
        mx, my = self.mouse
        ix = self.ox + mx / self.zoom
        iy = self.oy + my / self.zoom
        self.zoom = float(np.clip(self.zoom * factor, self.min_zoom, self.max_zoom))
        self.ox = ix - mx / self.zoom
        self.oy = iy - my / self.zoom
        self._clamp_offset()

    def _clamp_offset(self):
        vis_w = VIEW_W / self.zoom
        vis_h = VIEW_H / self.zoom
        self.ox = float(np.clip(self.ox, -0.7 * vis_w, self.img_w - 0.3 * vis_w))
        self.oy = float(np.clip(self.oy, -0.7 * vis_h, self.img_h - 0.3 * vis_h))

    def _on_mouse(self, event, x, y, flags, param):
        self.mouse = (x, y)
        if event == cv2.EVENT_MOUSEWHEEL:
            if hasattr(cv2, "getMouseWheelDelta"):
                delta = cv2.getMouseWheelDelta(flags)
            else:
                delta = flags
            steps = float(np.clip(delta / 120.0, -3.0, 3.0))
            if steps:
                self.zoom_by(1.2 ** steps)
        elif event == cv2.EVENT_LBUTTONDOWN:
            self.ldown = True
        elif event == cv2.EVENT_LBUTTONUP:
            self.ldown = False
        elif event in (cv2.EVENT_RBUTTONDOWN, cv2.EVENT_MBUTTONDOWN):
            self._panning = True
            self._pan_last = (x, y)
        elif event in (cv2.EVENT_RBUTTONUP, cv2.EVENT_MBUTTONUP):
            self._panning = False
            self._pan_last = None
        elif event == cv2.EVENT_MOUSEMOVE and self._panning and self._pan_last:
            self.ox -= (x - self._pan_last[0]) / self.zoom
            self.oy -= (y - self._pan_last[1]) / self.zoom
            self._pan_last = (x, y)
            self._clamp_offset()

    def render(self, display, hud_lines=()):
        """Draw the visible part of `display` at the current zoom/pan."""
        canvas = np.full((VIEW_H, VIEW_W, 3), 40, np.uint8)
        x0, y0 = math.floor(self.ox), math.floor(self.oy)
        x1 = math.ceil(self.ox + VIEW_W / self.zoom)
        y1 = math.ceil(self.oy + VIEW_H / self.zoom)
        sx0, sy0 = max(x0, 0), max(y0, 0)
        sx1, sy1 = min(x1, self.img_w), min(y1, self.img_h)
        if sx1 > sx0 and sy1 > sy0:
            patch = display[sy0:sy1, sx0:sx1]
            pw = max(1, round((sx1 - sx0) * self.zoom))
            ph = max(1, round((sy1 - sy0) * self.zoom))
            interp = cv2.INTER_NEAREST if self.zoom >= 1.0 else cv2.INTER_AREA
            patch = cv2.resize(patch, (pw, ph), interpolation=interp)
            dx = round((sx0 - self.ox) * self.zoom)
            dy = round((sy0 - self.oy) * self.zoom)
            cx0, cy0 = max(dx, 0), max(dy, 0)
            cx1, cy1 = min(dx + pw, VIEW_W), min(dy + ph, VIEW_H)
            if cx1 > cx0 and cy1 > cy0:
                canvas[cy0:cy1, cx0:cx1] = patch[cy0 - dy:cy1 - dy, cx0 - dx:cx1 - dx]
        if hud_lines:
            box_h = 22 * len(hud_lines) + 12
            cv2.rectangle(canvas, (0, 0), (VIEW_W, box_h), (20, 20, 20), -1)
            for i, line in enumerate(hud_lines):
                cv2.putText(
                    canvas, line, (10, 24 + 22 * i),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA,
                )
        cv2.imshow(self.title, canvas)


def _view_key(view, key):
    """Shared zoom keys for the interactive windows."""
    if key in (ord("+"), ord("=")):
        view.zoom_by(1.25)
    elif key in (ord("-"), ord("_")):
        view.zoom_by(0.8)
    elif key == ord("0"):
        view.reset()


def select_board_roi(image):
    """Drag-select the board on a zoomable view.

    Returns (x, y, w, h) in image pixels, or None if cancelled. Zoom in
    with the mouse wheel to hit the board's outer edge pixel-exactly; the
    HUD shows the live cursor pixel and the current rectangle.
    """
    img_h, img_w = image.shape[:2]
    view = ZoomView("Select chessboard", image)
    drag_start = None
    drag_now = None
    rect = None  # finalised (x0, y0, x1, y1), inclusive
    try:
        while True:
            ix, iy = view.mouse_image()
            ix = int(np.clip(ix, 0, img_w - 1))
            iy = int(np.clip(iy, 0, img_h - 1))

            if view.ldown:
                if drag_start is None:
                    drag_start = (ix, iy)
                drag_now = (ix, iy)
            elif drag_start is not None:
                rect = (
                    min(drag_start[0], drag_now[0]), min(drag_start[1], drag_now[1]),
                    max(drag_start[0], drag_now[0]), max(drag_start[1], drag_now[1]),
                )
                drag_start = drag_now = None

            shown = rect
            if drag_start is not None:
                shown = (
                    min(drag_start[0], drag_now[0]), min(drag_start[1], drag_now[1]),
                    max(drag_start[0], drag_now[0]), max(drag_start[1], drag_now[1]),
                )

            disp = image.copy()
            # Crosshair through the cursor pixel to help align the edges.
            cv2.line(disp, (ix, 0), (ix, img_h - 1), (0, 255, 255), 1)
            cv2.line(disp, (0, iy), (img_w - 1, iy), (0, 255, 255), 1)
            if shown:
                cv2.rectangle(disp, (shown[0], shown[1]), (shown[2], shown[3]), (0, 255, 0), 1)

            hud = [
                f"mouse px: ({ix}, {iy})   zoom: {view.zoom:.2f}x   "
                "wheel=zoom  right-drag=pan  0=fit"
            ]
            if shown:
                hud.append(
                    f"selection: x={shown[0]} y={shown[1]} "
                    f"w={shown[2] - shown[0] + 1} h={shown[3] - shown[1] + 1}"
                    "   ENTER/SPACE=confirm  ESC/C=cancel"
                )
            else:
                hud.append(
                    "drag the OUTER EDGE of the chessboard (zoom in for pixel "
                    "accuracy)   ENTER/SPACE=confirm  ESC/C=cancel"
                )
            view.render(disp, hud)

            key = cv2.waitKey(20) & 0xFF
            if key in (27, ord("c"), ord("C")):
                return None
            if key in (13, 10, 32) and shown:
                w = shown[2] - shown[0] + 1
                h = shown[3] - shown[1] + 1
                if w >= 16 and h >= 16:
                    return shown[0], shown[1], w, h
            _view_key(view, key)
    finally:
        cv2.destroyWindow("Select chessboard")


def _labelled_preview(text_lines, tile=None):
    """Build the square-preview image (magnified tile, or a placeholder)."""
    if tile is None or tile.size == 0:
        preview = np.full((PREVIEW_SIZE, PREVIEW_SIZE, 3), 40, np.uint8)
    else:
        # INTER_NEAREST keeps the tile's pixels exact when magnified.
        preview = cv2.resize(
            tile, (PREVIEW_SIZE, PREVIEW_SIZE), interpolation=cv2.INTER_NEAREST
        )
    for i, (text, y) in enumerate(text_lines):
        cv2.putText(
            preview, text, (10, y),
            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 3, cv2.LINE_AA,
        )
        cv2.putText(
            preview, text, (10, y),
            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 1, cv2.LINE_AA,
        )
    return preview


def show_grid_debug(board, debug, square_size):
    """Interactive grid check: zoom/pan the debug view, and hovering a
    square highlights it and previews its exact cropped tile."""
    size = board.shape[0]
    view = ZoomView("Detected board grid", debug)
    cv2.namedWindow("Square preview", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("Square preview", PREVIEW_SIZE, PREVIEW_SIZE)
    try:
        while True:
            ix, iy = view.mouse_image()
            hover = None
            if 0 <= ix < size and 0 <= iy < size:
                hover = (min(int(iy // square_size), 7), min(int(ix // square_size), 7))

            disp = debug.copy()
            hud = [f"zoom: {view.zoom:.2f}x   wheel=zoom  right-drag=pan  0=fit  Q/ESC=close"]
            if hover is not None:
                row, col = hover
                # Same rounding as the tile-splitting code, so the preview
                # is exactly what was saved as r{row}_c{col}.png.
                left = round(col * square_size)
                top = round(row * square_size)
                right = round((col + 1) * square_size)
                bottom = round((row + 1) * square_size)
                cv2.rectangle(disp, (left, top), (right - 1, bottom - 1), (0, 255, 255), 2)
                preview = _labelled_preview(
                    [
                        (f"r{row} c{col}", 30),
                        (f"x {left}-{right - 1}  y {top}-{bottom - 1}", PREVIEW_SIZE - 14),
                    ],
                    tile=board[top:bottom, left:right],
                )
                hud.append(
                    f"square r{row} c{col}: x {left}-{right - 1}, y {top}-{bottom - 1} "
                    f"(saved as r{row}_c{col}.png)"
                )
            else:
                preview = _labelled_preview([("Hover a square", PREVIEW_SIZE // 2)])
                hud.append("hover a square to preview its cropped tile")
            cv2.imshow("Square preview", preview)
            view.render(disp, hud)

            key = cv2.waitKey(20) & 0xFF
            if key in (27, ord("q"), ord("Q")):
                break
            _view_key(view, key)
    finally:
        cv2.destroyAllWindows()


def process_image(image_path):
    """Select/crop one dropped screenshot and save its 64 board squares."""
    image_path = Path(image_path)
    image = load_screenshot(image_path)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Loaded image: {image_path}")
    print("Select the OUTER EDGE of the chessboard.")
    print("Drag to select; mouse wheel zooms (right-drag pans) for pixel accuracy.")
    print("Press ENTER or SPACE to confirm; ESC or C to cancel.")

    roi = select_board_roi(image)
    if roi is None:
        print("Board selection cancelled.")
        return False
    x, y, w, h = roi

    # Crop the selected board.
    board = image[y:y + h, x:x + w].copy()

    # The standard board is square. Correct the crop to a square
    # if the selection has a small accidental width/height mismatch.
    size = min(w, h)
    board = cv2.resize(board, (size, size))

    # Save the board crop.
    cv2.imwrite(str(OUTPUT_DIR / "board.png"), board)

    square_size = size / 8
    debug = board.copy()

    # Split the board into 64 equal squares.
    for row in range(8):
        for col in range(8):
            left = round(col * square_size)
            top = round(row * square_size)
            right = round((col + 1) * square_size)
            bottom = round((row + 1) * square_size)

            square = board[top:bottom, left:right]

            # Save each tile for later piece-classifier training.
            name = f"r{row}_c{col}.png"
            cv2.imwrite(str(OUTPUT_DIR / name), square)

            # Draw grid lines on the debug image.
            cv2.rectangle(
                debug,
                (left, top),
                (right - 1, bottom - 1),
                (0, 255, 0),
                1
            )

            # Label rows and columns for debugging.
            cv2.putText(
                debug,
                f"{row},{col}",
                (left + 3, top + 15),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.35,
                (0, 0, 255),
                1,
                cv2.LINE_AA
            )

    cv2.imwrite(str(OUTPUT_DIR / "grid_debug.png"), debug)

    print(f"Board size: {size} x {size}")
    print(f"Square size: {square_size:.2f} pixels")
    print(f"Saved 64 square images to: {OUTPUT_DIR.resolve()}")
    print("Inspecting grid: hover a square to preview its tile; "
          "wheel zooms, right-drag pans, Q/ESC closes.")

    show_grid_debug(board, debug, square_size)
    return True


def main(argv=None):
    """Process each dropped/selected screenshot in turn.

    Every run freshly writes board_debug/r<row>_c<col>.png; labelled
    CNN training examples from earlier runs are left alone.
    """
    image_paths = choose_image_paths(argv)
    if not image_paths:
        print("No image selected. Drop an image onto board-vision.py, pass")
        print("its path on the command line, or choose it in the file picker.")
        return False

    completed = False
    for index, image_path in enumerate(image_paths, start=1):
        if len(image_paths) > 1:
            print(f"\nImage {index} of {len(image_paths)}: {image_path}")
        completed = process_image(image_path) or completed
    return completed


if __name__ == "__main__":
    main()
