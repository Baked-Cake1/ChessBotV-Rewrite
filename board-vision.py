import csv
import math
import random
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "board_debug"
DATASET_CSV = OUTPUT_DIR / "dataset.csv"
SPLIT_CSV_PATHS = {
    "train": OUTPUT_DIR / "train.csv",
    "validation": OUTPUT_DIR / "validation.csv",
    "test": OUTPUT_DIR / "test.csv",
}
CNN_TRAINER_PATH = BASE_DIR / "cnn_trainer.py"
SUPPORTED_IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff",
}

VIEW_W, VIEW_H = 1152, 864
PREVIEW_SIZE = 380
MANUAL_W, MANUAL_H = 1120, 700
MANUAL_TILE_SIZE = 360

# (trainer filename prefix, CNN class name, display name). These codes and
# class names intentionally match PREFIX_TO_CLASS in cnn_trainer.py.
LABEL_OPTIONS = (
    ("wk", "white_king", "White King"),
    ("wq", "white_queen", "White Queen"),
    ("wr", "white_rook", "White Rook"),
    ("wb", "white_bishop", "White Bishop"),
    ("wn", "white_knight", "White Knight"),
    ("wp", "white_pawn", "White Pawn"),
    ("bk", "black_king", "Black King"),
    ("bq", "black_queen", "Black Queen"),
    ("br", "black_rook", "Black Rook"),
    ("bb", "black_bishop", "Black Bishop"),
    ("bn", "black_knight", "Black Knight"),
    ("bp", "black_pawn", "Black Pawn"),
    ("blank", "empty", "Blank / Empty"),
)
LABEL_BY_CODE = {code: label for code, label, _display in LABEL_OPTIONS}
SQUARE_TYPE_OPTIONS = (
    ("light", "Light square"),
    ("dark", "Dark square"),
    ("highlighted", "Highlighted square"),
)
CSV_FIELDS = (
    "image_path",
    "filename",
    "label",
    "label_code",
    "square_type",
    "split",
    "row",
    "col",
    "source_image",
    "board_id",
)
SPLIT_FRACTIONS = (
    ("train", 0.70),
    ("validation", 0.15),
    ("test", 0.15),
)
SPLIT_SEED = 42
BALANCE_TOLERANCE = 2


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


def _path_for_csv(path):
    """Path stored in dataset.csv (relative to this script when possible)."""
    path = Path(path)
    try:
        return path.resolve().relative_to(BASE_DIR).as_posix()
    except Exception:
        return str(path)


def load_dataset_records(csv_path=DATASET_CSV):
    """Load the master dataset CSV, returning [] when it does not exist."""
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        return []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    records = []
    for row in rows:
        record = {field: str(row.get(field, "") or "") for field in CSV_FIELDS}
        if record["image_path"] or record["filename"]:
            records.append(record)
    return records


def write_dataset_csv(records, csv_path=DATASET_CSV):
    """Write all dataset rows to one CSV (existing rows are retained)."""
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = csv_path.with_suffix(csv_path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow({field: record.get(field, "") for field in CSV_FIELDS})
    temporary.replace(csv_path)
    return csv_path


def write_split_csvs(records, output_dir=OUTPUT_DIR):
    """Write train/validation/test CSVs after splits have been calculated."""
    output_dir = Path(output_dir)
    written = {}
    for split_name, _fraction in SPLIT_FRACTIONS:
        split_records = [
            record for record in records if record.get("split") == split_name
        ]
        path = output_dir / f"{split_name}.csv"
        write_dataset_csv(split_records, path)
        written[split_name] = path
    return written


def _split_targets(total):
    """Exact 70/15/15 counts (for example, 6400 -> 4480/960/960)."""
    quotas = [
        (name, total * fraction) for name, fraction in SPLIT_FRACTIONS
    ]
    counts = {name: int(math.floor(quota)) for name, quota in quotas}
    remaining = total - sum(counts.values())
    for name, quota in sorted(
        quotas, key=lambda item: quota_fraction(item[1]), reverse=True
    ):
        if remaining <= 0:
            break
        counts[name] += 1
        remaining -= 1
    return counts


def quota_fraction(value):
    return value - math.floor(value)


def recalculate_splits(records, seed=SPLIT_SEED):
    """Recalculate stratified 70/15/15 splits for every dataset row.

    Stratification is by (label, square_type), including highlighted
    squares. Highlighted squares are therefore represented in every split
    where their group is large enough, while the light/dark balance check
    below deliberately excludes them.
    """
    if not records:
        return records

    targets = _split_targets(len(records))
    groups = {}
    for record in records:
        key = (record.get("label", ""), record.get("square_type", ""))
        groups.setdefault(key, []).append(record)

    allocations = {}
    unassigned = {}
    fractions = {}
    current = {name: 0 for name, _fraction in SPLIT_FRACTIONS}
    for key, group in groups.items():
        allocations[key] = {}
        fractions[key] = {}
        used = 0
        for split_name, fraction in SPLIT_FRACTIONS:
            quota = len(group) * fraction
            count = int(math.floor(quota))
            allocations[key][split_name] = count
            fractions[key][split_name] = quota_fraction(quota)
            current[split_name] += count
            used += count
        unassigned[key] = len(group) - used

    # Distribute the few stratified remainders so the global counts match
    # the requested percentages exactly (subject only to integer totals).
    for split_name, _fraction in SPLIT_FRACTIONS:
        needed = targets[split_name] - current[split_name]
        while needed > 0:
            candidates = sorted(
                (key for key in groups if unassigned[key] > 0),
                key=lambda key: (
                    -fractions[key][split_name],
                    -unassigned[key],
                    key,
                ),
            )
            if not candidates:
                break
            progressed = False
            for key in candidates:
                if needed <= 0:
                    break
                allocations[key][split_name] += 1
                unassigned[key] -= 1
                needed -= 1
                progressed = True
            if not progressed:
                break

    # Any defensive leftovers go to test; normally there are none.
    for key, count in unassigned.items():
        if count:
            allocations[key]["test"] += count

    for (label, square_type), group in groups.items():
        shuffled = list(group)
        random.Random(f"{seed}:{label}:{square_type}").shuffle(shuffled)
        cursor = 0
        for split_name, _fraction in SPLIT_FRACTIONS:
            count = allocations[(label, square_type)][split_name]
            for record in shuffled[cursor:cursor + count]:
                record["split"] = split_name
            cursor += count
    return records


def append_dataset_records(new_records, csv_path=DATASET_CSV):
    """Append one reviewed board to dataset.csv without losing old rows.

    The first creation is deliberately left as ``unsplit``. Every later
    change recalculates all splits and refreshes train/validation/test.csv.
    Returns ``(all_records, csv_already_existed)``.
    """
    csv_path = Path(csv_path)
    csv_existed = csv_path.is_file()
    existing = load_dataset_records(csv_path)
    existing_paths = {
        record.get("image_path") for record in existing if record.get("image_path")
    }
    duplicate_paths = [
        record.get("image_path")
        for record in new_records
        if record.get("image_path") in existing_paths
    ]
    if duplicate_paths:
        raise ValueError(
            "Refusing to overwrite existing dataset row(s): "
            + ", ".join(duplicate_paths[:5])
        )

    all_records = existing + list(new_records)
    if csv_existed:
        recalculate_splits(all_records)
        write_dataset_csv(all_records, csv_path)
        write_split_csvs(all_records, csv_path.parent)
    else:
        for record in all_records:
            record["split"] = "unsplit"
        write_dataset_csv(all_records, csv_path)
    return all_records, csv_existed


def dataset_balance(records, tolerance=BALANCE_TOLERANCE):
    """Per-class light/dark/highlighted counts and imbalance details.

    Highlighted squares are reported but excluded from the light-vs-dark
    tolerance check, as requested.
    """
    counts = {
        label: {"light": 0, "dark": 0, "highlighted": 0}
        for _code, label, _display in LABEL_OPTIONS
    }
    for record in records:
        label = record.get("label", "")
        square_type = record.get("square_type", "")
        if label in counts and square_type in counts[label]:
            counts[label][square_type] += 1

    imbalanced = {}
    for label, values in counts.items():
        if abs(values["light"] - values["dark"]) > tolerance:
            imbalanced[label] = dict(values)
    return counts, imbalanced


def print_dataset_report(records, splits_recalculated=False):
    """Print split totals and the per-class light/dark balance report."""
    split_counts = {}
    for record in records:
        split = record.get("split") or "unsplit"
        split_counts[split] = split_counts.get(split, 0) + 1

    print(f"\nDataset CSV: {DATASET_CSV}")
    print(f"Total labelled images: {len(records)}")
    if splits_recalculated:
        print(
            "Split recalculated: "
            f"train={split_counts.get('train', 0)}, "
            f"validation={split_counts.get('validation', 0)}, "
            f"test={split_counts.get('test', 0)}"
        )
    else:
        print("Split: not calculated on first CSV creation (all rows are 'unsplit').")

    counts, imbalanced = dataset_balance(records)
    print("\nPer-class square counts (highlighted is excluded from balance):")
    for _code, label, display in LABEL_OPTIONS:
        values = counts[label]
        difference = abs(values["light"] - values["dark"])
        marker = "  <-- needs balancing" if label in imbalanced else ""
        print(
            f"  {display:15s}: light={values['light']:4d}, "
            f"dark={values['dark']:4d}, highlighted={values['highlighted']:4d}, "
            f"light/dark difference={difference}{marker}"
        )

    if imbalanced:
        print(
            f"\nBalance requirement not met: light and dark counts for each "
            f"class must differ by at most {BALANCE_TOLERANCE}."
        )
        for label, values in sorted(imbalanced.items()):
            needed_type = "light" if values["light"] < values["dark"] else "dark"
            needed = abs(values["light"] - values["dark"]) - BALANCE_TOLERANCE
            print(f"  {label}: add at least {needed} more {needed_type}-square image(s).")
    else:
        print("\nLight/dark balance requirement met for every class.")
    return counts, imbalanced


def _next_dataset_sequence(existing_records, output_dir=OUTPUT_DIR):
    """Next non-overwriting numeric suffix for saved square images."""
    highest = 0
    candidates = [record.get("filename", "") for record in existing_records]
    output_dir = Path(output_dir)
    if output_dir.is_dir():
        candidates.extend(path.name for path in output_dir.iterdir() if path.is_file())
    for name in candidates:
        match = re.search(r"_(\d+)$", Path(str(name)).stem)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def save_labelled_tiles(tile_entries, answers, source_image, board_id):
    """Save manually reviewed tiles under unique trainer-linked names."""
    if len(tile_entries) != len(answers):
        raise ValueError("Every tile must have a manual label and square type")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    existing = load_dataset_records(DATASET_CSV)
    sequence = _next_dataset_sequence(existing, OUTPUT_DIR)
    records = []

    for (row, col, tile), (label_option, square_type) in zip(tile_entries, answers):
        label_code, label, _display = label_option
        while True:
            filename = f"{label_code}_{square_type}_{sequence:06d}.png"
            image_path = OUTPUT_DIR / filename
            sequence += 1
            if not image_path.exists():
                break
        if not cv2.imwrite(str(image_path), tile):
            raise OSError(f"Could not save labelled square: {image_path}")
        records.append({
            "image_path": _path_for_csv(image_path),
            "filename": filename,
            "label": label,
            "label_code": label_code,
            "square_type": square_type,
            "split": "unsplit",
            "row": str(row),
            "col": str(col),
            "source_image": _path_for_csv(source_image),
            "board_id": board_id,
        })
    return records


def run_cnn_trainer(records):
    """Run cnn_trainer.py against dataset.csv after the balance check."""
    _counts, imbalanced = dataset_balance(records)
    if imbalanced:
        print("\nCNN training was not started because the light/dark balance")
        print(f"requirement (difference <= {BALANCE_TOLERANCE}) is not met yet.")
        print("Add another screenshot and label more of the needed square colour.")
        return False
    if not CNN_TRAINER_PATH.is_file():
        print(f"Could not find CNN trainer: {CNN_TRAINER_PATH}")
        return False

    command = [
        sys.executable,
        str(CNN_TRAINER_PATH),
        "--dataset-dir",
        str(OUTPUT_DIR),
        "--csv",
        str(DATASET_CSV),
        "--output",
        str(BASE_DIR / "chessbotv_cnn.pth"),
    ]
    print("\nStarting CNN trainer...")
    print("Command:", " ".join(command))
    completed = subprocess.run(command, cwd=str(BASE_DIR), check=False)
    if completed.returncode != 0:
        print(f"CNN trainer exited with code {completed.returncode}.")
        return False
    print("CNN trainer finished successfully.")
    return True


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


def _manual_text(canvas, text, origin, scale=0.62, colour=(255, 255, 255), thickness=1):
    cv2.putText(
        canvas, text, origin, cv2.FONT_HERSHEY_SIMPLEX,
        scale, (0, 0, 0), thickness + 2, cv2.LINE_AA,
    )
    cv2.putText(
        canvas, text, origin, cv2.FONT_HERSHEY_SIMPLEX,
        scale, colour, thickness, cv2.LINE_AA,
    )


def _manual_button(canvas, rect, text, selected=False):
    x, y, w, h = rect
    fill = (64, 145, 72) if selected else (62, 62, 72)
    border = (150, 255, 170) if selected else (165, 165, 175)
    cv2.rectangle(canvas, (x, y), (x + w - 1, y + h - 1), fill, -1)
    cv2.rectangle(canvas, (x, y), (x + w - 1, y + h - 1), border, 2)
    scale = 0.52
    (text_w, text_h), _baseline = cv2.getTextSize(
        text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1
    )
    _manual_text(
        canvas,
        text,
        (x + max(6, (w - text_w) // 2), y + (h + text_h) // 2),
        scale=scale,
    )


def manual_review_tiles(tile_entries):
    """Manually label every tile, then ask its square type.

    No automatic classification or preselected answer is used. For each
    square the user first chooses the piece/blank class, then chooses
    Light square, Dark square, or Highlighted square. Returns a list of
    ``(label_option, square_type)`` in tile order, or None if cancelled.
    """
    if not tile_entries:
        return []

    answers = [None] * len(tile_entries)
    index = 0
    stage = "label"
    selected_label = None
    click = {"point": None}
    title = "Manual piece review"

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            click["point"] = (x, y)

    cv2.namedWindow(title, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(title, MANUAL_W, MANUAL_H)
    cv2.setMouseCallback(title, on_mouse)

    try:
        while index < len(tile_entries):
            row, col, tile = tile_entries[index]
            canvas = np.full((MANUAL_H, MANUAL_W, 3), 36, np.uint8)

            _manual_text(
                canvas,
                f"Manual review {index + 1}/{len(tile_entries)}  -  square r{row} c{col}",
                (24, 36),
                scale=0.72,
                thickness=2,
            )
            if stage == "label":
                instruction = "Step 1: click what is on this square (manual only - no auto-review)"
            else:
                instruction = (
                    f"Step 2: {selected_label[2]} - is it on a light, dark, "
                    "or highlighted square?"
                )
            _manual_text(canvas, instruction, (24, 68), scale=0.54)

            if tile is not None and tile.size:
                interp = (
                    cv2.INTER_AREA
                    if max(tile.shape[:2]) > MANUAL_TILE_SIZE
                    else cv2.INTER_NEAREST
                )
                preview = cv2.resize(
                    tile,
                    (MANUAL_TILE_SIZE, MANUAL_TILE_SIZE),
                    interpolation=interp,
                )
            else:
                preview = np.full(
                    (MANUAL_TILE_SIZE, MANUAL_TILE_SIZE, 3), 70, np.uint8
                )
            canvas[104:104 + MANUAL_TILE_SIZE, 24:24 + MANUAL_TILE_SIZE] = preview
            cv2.rectangle(
                canvas,
                (24, 104),
                (24 + MANUAL_TILE_SIZE - 1, 104 + MANUAL_TILE_SIZE - 1),
                (230, 230, 230),
                2,
            )
            _manual_text(
                canvas,
                f"Board square r{row} c{col}",
                (24, 104 + MANUAL_TILE_SIZE + 34),
                scale=0.58,
            )

            buttons = []
            if stage == "label":
                start_x, start_y = 424, 108
                button_w, button_h, gap = 208, 54, 12
                for option_index, option in enumerate(LABEL_OPTIONS):
                    grid_col = option_index % 3
                    grid_row = option_index // 3
                    rect = (
                        start_x + grid_col * (button_w + gap),
                        start_y + grid_row * (button_h + gap),
                        button_w,
                        button_h,
                    )
                    _manual_button(
                        canvas,
                        rect,
                        f"{option[2]} ({option[0]})",
                        selected=(selected_label == option),
                    )
                    buttons.append((rect, "label", option))
            else:
                start_x, start_y = 460, 140
                button_w, button_h, gap = 360, 68, 18
                for option_index, (square_type, display) in enumerate(SQUARE_TYPE_OPTIONS):
                    rect = (
                        start_x,
                        start_y + option_index * (button_h + gap),
                        button_w,
                        button_h,
                    )
                    _manual_button(canvas, rect, display)
                    buttons.append((rect, "square", square_type))
                _manual_text(
                    canvas,
                    "Keys: L = Light square, D = Dark square, H = Highlighted square",
                    (430, 452),
                    scale=0.52,
                )

            back_rect = (424, MANUAL_H - 78, 190, 48)
            cancel_rect = (630, MANUAL_H - 78, 190, 48)
            _manual_button(canvas, back_rect, "Back (B)")
            _manual_button(canvas, cancel_rect, "Cancel (Esc)")
            buttons.append((back_rect, "back", None))
            buttons.append((cancel_rect, "cancel", None))
            _manual_text(
                canvas,
                "Every square is labelled manually, including blank squares.",
                (24, MANUAL_H - 24),
                scale=0.52,
                colour=(190, 220, 255),
            )

            cv2.imshow(title, canvas)
            key = cv2.waitKey(20) & 0xFF

            action = None
            if click["point"] is not None:
                px, py = click["point"]
                click["point"] = None
                for rect, kind, value in buttons:
                    x, y, w, h = rect
                    if x <= px < x + w and y <= py < y + h:
                        action = (kind, value)
                        break

            if key == 27:
                action = ("cancel", None)
            elif key in (8, ord("b"), ord("B")):
                action = ("back", None)
            elif stage == "square" and key in (ord("l"), ord("L")):
                action = ("square", "light")
            elif stage == "square" and key in (ord("d"), ord("D")):
                action = ("square", "dark")
            elif stage == "square" and key in (ord("h"), ord("H")):
                action = ("square", "highlighted")

            if action is None:
                continue

            kind, value = action
            if kind == "cancel":
                return None
            if kind == "back":
                if stage == "square":
                    stage = "label"
                elif index > 0:
                    index -= 1
                    previous = answers[index]
                    selected_label = previous[0] if previous else None
                    stage = "label"
                continue
            if kind == "label" and stage == "label":
                selected_label = value
                stage = "square"
                continue
            if kind == "square" and stage == "square" and selected_label is not None:
                answers[index] = (selected_label, value)
                index += 1
                selected_label = None
                stage = "label"
                continue
    finally:
        cv2.destroyWindow(title)

    return answers


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
                # is exactly the tile that will be manually labelled next.
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
                    "(manual label chosen after this grid check)"
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
    """Crop one screenshot, manually label its 64 squares, and append CSV rows.

    Returns the full dataset record list after this board is appended, or
    None if board selection/manual review was cancelled. No labelled tile
    or CSV row from this board is saved when manual review is cancelled.
    """
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
        return None
    x, y, w, h = roi

    # Crop the selected board.
    board = image[y:y + h, x:x + w].copy()

    # The standard board is square. Correct the crop to a square
    # if the selection has a small accidental width/height mismatch.
    size = min(w, h)
    board = cv2.resize(board, (size, size))

    square_size = size / 8
    debug = board.copy()
    tile_entries = []

    # Split the board into the same 64 equal squares as before. Tiles are
    # held in memory until manual labelling is complete, then saved under
    # unique class/square-type filenames so later boards never overwrite
    # earlier dataset images.
    for row in range(8):
        for col in range(8):
            left = round(col * square_size)
            top = round(row * square_size)
            right = round((col + 1) * square_size)
            bottom = round((row + 1) * square_size)

            square = board[top:bottom, left:right].copy()
            tile_entries.append((row, col, square))

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

    print(f"Board size: {size} x {size}")
    print(f"Square size: {square_size:.2f} pixels")
    print("Inspecting grid: hover a square to preview its tile; "
          "wheel zooms, right-drag pans, Q/ESC closes.")
    show_grid_debug(board, debug, square_size)

    print("\nManual review: for every square, first choose the piece/blank,")
    print("then choose Light square, Dark square, or Highlighted square.")
    print("No auto-review or preselected label is used.")
    answers = manual_review_tiles(tile_entries)
    if answers is None:
        print("Manual review cancelled; no tiles or CSV rows were saved for this board.")
        return None

    board_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    board_path = OUTPUT_DIR / f"board_{board_id}.png"
    grid_path = OUTPUT_DIR / f"grid_debug_{board_id}.png"
    cv2.imwrite(str(board_path), board)
    cv2.imwrite(str(grid_path), debug)

    new_records = save_labelled_tiles(
        tile_entries, answers, image_path, board_id
    )
    all_records, csv_existed = append_dataset_records(new_records)

    print(f"Saved board crop: {board_path}")
    print(f"Saved grid debug: {grid_path}")
    print(f"Saved {len(new_records)} manually labelled square images to: {OUTPUT_DIR.resolve()}")
    if csv_existed:
        print("Appended these rows to the existing dataset.csv and recalculated its splits.")
    else:
        print("Created dataset.csv. Its rows are 'unsplit' on this first creation.")
    print_dataset_report(all_records, splits_recalculated=csv_existed)
    return all_records


def _ask_yes_no(prompt):
    """Terminal yes/no prompt used after each completed board."""
    while True:
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no", ""):
            return False
        print("Please answer Y or N.")


def _wait_ready_for_another(next_path=None):
    """Wait at the terminal before another screenshot/file picker is used."""
    if next_path is not None:
        print(f"\nNext supplied screenshot: {next_path}")
    else:
        print("\nPrepare another chessboard screenshot to add to the dataset.")
        print("The file picker will not open yet.")
    try:
        answer = input(
            "Press Enter when you are ready, or type Q then Enter to finish: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer not in ("q", "quit", "exit")


def main(argv=None):
    """Create/append the CNN dataset one manually reviewed board at a time.

    After every completed board, the user may create the CNN model through
    cnn_trainer.py or add another screenshot. When they choose another,
    the terminal waits for Enter before any next supplied image is used or
    the Tk file picker is opened. Existing dataset rows/images are kept;
    new boards are appended and then the split is recalculated.
    """
    image_paths = list(choose_image_paths(argv))
    if not image_paths:
        print("No image selected. Drop an image onto board-vision.py, pass")
        print("its path on the command line, or choose it in the file picker.")
        return False

    completed = False
    index = 0
    while index < len(image_paths):
        image_path = image_paths[index]
        index += 1
        print(f"\nScreenshot: {image_path}")

        records = process_image(image_path)
        if records is None:
            # A cancelled ROI/manual review produces no new dataset rows.
            # Offer another screenshot without opening the picker until the
            # user confirms at the terminal that they are ready.
            next_path = image_paths[index] if index < len(image_paths) else None
            if not _wait_ready_for_another(next_path):
                break
            if index >= len(image_paths):
                more_paths = choose_image_paths([])
                if not more_paths:
                    print("No image selected.")
                    break
                image_paths.extend(more_paths)
            continue

        completed = True
        if _ask_yes_no("\nCreate the new CNN model now with cnn_trainer.py? [y/N]: "):
            if run_cnn_trainer(records):
                break
            print("The model was not created; you can add another screenshot.")

        next_path = image_paths[index] if index < len(image_paths) else None
        if not _wait_ready_for_another(next_path):
            break
        if index >= len(image_paths):
            more_paths = choose_image_paths([])
            if not more_paths:
                print("No image selected.")
                break
            image_paths.extend(more_paths)

    return completed


if __name__ == "__main__":
    main()
