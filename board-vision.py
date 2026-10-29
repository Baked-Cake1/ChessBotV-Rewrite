import csv
import hashlib
import math
import os
import re
import subprocess
import sys
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
try:
    import pygame
except ImportError:  # The non-GUI dataset helpers remain importable.
    pygame = None

BASE_DIR = Path(__file__).resolve().parent
DATASET_DIR = BASE_DIR / "datasets"
# Kept as a descriptive alias for the new folder-based dataset root.
OUTPUT_DIR = DATASET_DIR
BOARDS_DIR = DATASET_DIR / "_boards"
METADATA_CSV = DATASET_DIR / "metadata.csv"
LEGACY_DATASET_DIR = BASE_DIR / "board_debug"
LEGACY_DATASET_CSV = LEGACY_DATASET_DIR / "dataset.csv"
CNN_TRAINER_PATH = BASE_DIR / "cnn_trainer.py"

# Duplicate checks compare every tile at the trainer's canonical 64x64
# input size. Exact SHA-256 matches are always duplicates. Near matches
# must also be almost pixel-identical after that normalisation, so the
# same piece on a light versus a dark square is not collapsed together.
CANONICAL_TILE_SIZE = 64
NEAR_DUPLICATE_MAX_MEAN_ABS_ERROR = 4.0
NEAR_DUPLICATE_MAX_CHANGED_FRACTION = 0.02
NEAR_DUPLICATE_MAX_AHASH_DISTANCE = 8
NEAR_DUPLICATE_PREFILTER_MAX_MAE = 6.0

_PIL_RESAMPLING = getattr(Image, "Resampling", Image)
PIL_RESAMPLE_LANCZOS = _PIL_RESAMPLING.LANCZOS
PIL_RESAMPLE_BILINEAR = _PIL_RESAMPLING.BILINEAR
PIL_RESAMPLE_NEAREST = _PIL_RESAMPLING.NEAREST

SUPPORTED_IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff",
}

VIEW_W, VIEW_H = 1152, 864
PREVIEW_SIZE = 380
MANUAL_W, MANUAL_H = 1120, 760
MANUAL_TILE_SIZE = 360

# (short code, CNN class/folder name, display name). These codes and class
# names intentionally match the class folders read by cnn_trainer.py.
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
CODE_BY_LABEL = {label: code for code, label, _display in LABEL_OPTIONS}
LABEL_OPTION_BY_CODE = {option[0]: option for option in LABEL_OPTIONS}
LABEL_OPTION_BY_LABEL = {option[1]: option for option in LABEL_OPTIONS}
CLASS_FOLDER_BY_LABEL = {label: label for _code, label, _display in LABEL_OPTIONS}
CLASS_FOLDER_ALIASES = {}
for _code, _label, _display in LABEL_OPTIONS:
    CLASS_FOLDER_ALIASES[_label.lower()] = _label
    CLASS_FOLDER_ALIASES[_code.lower()] = _label
CLASS_FOLDER_ALIASES["blank"] = "empty"
FEN_SYMBOL_BY_CODE = {
    "wk": "K",
    "wq": "Q",
    "wr": "R",
    "wb": "B",
    "wn": "N",
    "wp": "P",
    "bk": "k",
    "bq": "q",
    "br": "r",
    "bb": "b",
    "bn": "n",
    "bp": "p",
    "blank": ".",
}
SQUARE_TYPE_OPTIONS = (
    ("light", "Light square"),
    ("dark", "Dark square"),
    ("highlighted", "Highlighted square"),
)
SQUARE_TYPE_DISPLAY = dict(SQUARE_TYPE_OPTIONS)
METADATA_FIELDS = (
    "image_path",
    "filename",
    "label",
    "label_code",
    "square_type",
    "row",
    "col",
    "source_image",
    "board_id",
    "game_id",
    "saved",
    "duplicate_of",
    "duplicate_kind",
    "duplicate_mean_abs_error",
    "duplicate_changed_fraction",
)
# Compatibility alias for callers that used the old master-CSV field name.
CSV_FIELDS = METADATA_FIELDS
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
    """Load one dropped/selected screenshot as an RGB NumPy array."""
    image_path = Path(image_path)
    try:
        with Image.open(image_path) as image:
            return np.asarray(
                ImageOps.exif_transpose(image).convert("RGB")
            ).copy()
    except Exception as exc:
        raise FileNotFoundError(
            f"Could not load image: {image_path.resolve()}"
        ) from exc


def _path_for_csv(path):
    """Path stored in metadata.csv (relative to this script when possible)."""
    path = Path(path)
    try:
        return path.resolve().relative_to(BASE_DIR).as_posix()
    except Exception:
        return str(path)


def create_game_id():
    """Create the game ID assigned to one uploaded board and its 64 tiles."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"game_{stamp}_{uuid.uuid4().hex[:6]}"


def record_game_id(record):
    """Game ID for a dataset row, with legacy board/source fallbacks."""
    for field in ("game_id", "board_id", "source_image", "image_path", "filename"):
        value = str(record.get(field, "") or "").strip()
        if value:
            return value
    return "unknown-game"


def _normalise_game_ids(records):
    """Fill game_id on scanned/manual records from their board grouping."""
    for record in records:
        record["game_id"] = record_game_id(record)
    return records


def dataset_game_counts(records):
    """Return the set of game IDs represented by folder records."""
    return {record_game_id(record) for record in records}


def _fen_castling_rights(board_grid):
    """Best-effort FEN castling field from home king/rook squares."""
    rights = ""
    if board_grid[7][4] == "K" and board_grid[7][7] == "R":
        rights += "K"
    if board_grid[7][4] == "K" and board_grid[7][0] == "R":
        rights += "Q"
    if board_grid[0][4] == "k" and board_grid[0][7] == "r":
        rights += "k"
    if board_grid[0][4] == "k" and board_grid[0][0] == "r":
        rights += "q"
    return rights or "-"


def fen_from_answers(tile_entries, answers, flipped=False):
    """Generate a FEN from the 64 reviewed classifications.

    ``tile_entries`` are in screenshot order: row 0 is the top of the
    uploaded board and column 0 is its left edge. A normal board is treated
    as White's perspective (rank 8 at the top, file a at the left). A
    flipped board is rotated 180 degrees before generating the FEN.

    Only the piece placement can be known with certainty from one image.
    For the remaining FEN fields, the side to move follows the board
    orientation (White for a normal board, Black for a flipped board,
    except that the exact starting position is always White to move),
    castling is inferred from home king/rook squares, and en passant and
    the clocks use ``- 0 1``.
    """
    if len(tile_entries) != len(answers):
        raise ValueError("Every tile must have a classification before FEN generation")

    visual_grid = [["."] * 8 for _ in range(8)]
    for (row, col, _tile), (label_option, _square_type) in zip(tile_entries, answers):
        if not (0 <= row < 8 and 0 <= col < 8):
            continue
        symbol = FEN_SYMBOL_BY_CODE.get(label_option[0], ".")
        visual_grid[row][col] = symbol

    if flipped:
        board_grid = [["."] * 8 for _ in range(8)]
        for row in range(8):
            for col in range(8):
                board_grid[7 - row][7 - col] = visual_grid[row][col]
    else:
        board_grid = visual_grid

    ranks = []
    for row in board_grid:
        text = ""
        empty = 0
        for symbol in row:
            if symbol == ".":
                empty += 1
            else:
                if empty:
                    text += str(empty)
                    empty = 0
                text += symbol
        if empty:
            text += str(empty)
        ranks.append(text)
    placement = "/".join(ranks)

    starting_placement = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"
    side_to_move = "w" if placement == starting_placement or not flipped else "b"
    return (
        f"{placement} {side_to_move} "
        f"{_fen_castling_rights(board_grid)} - 0 1"
    )


def save_fen_file(fen, game_id, output_dir=OUTPUT_DIR):
    """Save one generated FEN beside the board's CNN-dataset output."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_game_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(game_id))
    fen_path = output_dir / f"fen_{safe_game_id}.txt"
    fen_path.write_text(fen + "\n", encoding="utf-8")
    return fen_path


def _as_rgb_array(value):
    """Return an image/tile as a contiguous RGB uint8 NumPy array."""
    if isinstance(value, Image.Image):
        return np.ascontiguousarray(np.asarray(value.convert("RGB")), dtype=np.uint8)

    array = np.asarray(value)
    if array.ndim == 2:
        array = np.repeat(array[:, :, None], 3, axis=2)
    elif array.ndim == 3 and array.shape[2] == 1:
        array = np.repeat(array, 3, axis=2)
    elif array.ndim == 3 and array.shape[2] >= 3:
        # Dataset tiles are stored/displayed as RGB. Any alpha channel is
        # ignored because the trainer also converts its inputs to RGB.
        array = array[:, :, :3]
    else:
        raise ValueError(f"Unsupported image array shape: {array.shape}")

    if array.dtype != np.uint8:
        array = array.astype(np.float32, copy=False)
        if array.size and float(np.nanmax(array)) <= 1.0:
            array *= 255.0
        array = np.clip(array, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(array)


def _resize_rgb_array(array, size, resample=PIL_RESAMPLE_LANCZOS):
    """Resize an RGB array to ``size`` using Pillow (no OpenCV)."""
    rgb = _as_rgb_array(array)
    if isinstance(size, int):
        size = (size, size)
    if rgb.shape[1] == size[0] and rgb.shape[0] == size[1]:
        return rgb.copy()
    with Image.fromarray(rgb) as image:
        return np.asarray(image.resize(size, resample=resample)).copy()


def _load_rgb_image(path):
    """Load an image file as an RGB NumPy array."""
    with Image.open(path) as image:
        return np.asarray(
            ImageOps.exif_transpose(image).convert("RGB")
        ).copy()


def _save_rgb_image(path, array):
    """Save an RGB NumPy/Pillow image, creating its parent folder."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(_as_rgb_array(array)).save(path)
    return path


def canonical_tile_array(tile, size=CANONICAL_TILE_SIZE):
    """Normalise a tile to the trainer's square RGB input size."""
    return _resize_rgb_array(tile, (size, size), PIL_RESAMPLE_LANCZOS)


def ensure_dataset_layout(dataset_dir=DATASET_DIR):
    """Create ``datasets/`` and one folder for every CNN class."""
    dataset_dir = Path(dataset_dir)
    dataset_dir.mkdir(parents=True, exist_ok=True)
    (dataset_dir / "_boards").mkdir(parents=True, exist_ok=True)
    for _code, label, _display in LABEL_OPTIONS:
        (dataset_dir / CLASS_FOLDER_BY_LABEL[label]).mkdir(
            parents=True, exist_ok=True
        )
    return dataset_dir


def _class_folder_label(folder_name):
    """Canonical CNN label for a class-folder name/alias, if recognised."""
    return CLASS_FOLDER_ALIASES.get(str(folder_name).strip().lower())


def _parse_tile_filename(path):
    """Best-effort label-code/square/game/row/col data from a tile name."""
    path = Path(path)
    stem = path.stem
    parsed = {
        "label_code": "",
        "square_type": "",
        "game_id": "",
        "row": "",
        "col": "",
    }

    sections = stem.split("__")
    first = sections[0] if sections else stem
    first_parts = first.split("_")
    if first_parts and first_parts[0].lower() in LABEL_OPTION_BY_CODE:
        parsed["label_code"] = first_parts[0].lower()
    for part in first_parts[1:]:
        if part.lower() in SQUARE_TYPE_DISPLAY:
            parsed["square_type"] = part.lower()
            break

    for section in sections[1:]:
        if section.lower().startswith("game_") or section.lower().startswith("game-"):
            parsed["game_id"] = section
        else:
            match = re.fullmatch(r"r(\d+)c(\d+)", section.lower())
            if match:
                parsed["row"], parsed["col"] = match.groups()

    if not parsed["square_type"]:
        tokens = [token.lower() for token in re.split(r"[^A-Za-z]+", stem)]
        for token in tokens:
            if token in SQUARE_TYPE_DISPLAY:
                parsed["square_type"] = token
                break
    if not parsed["game_id"]:
        match = re.search(r"(game[_-][A-Za-z0-9_.-]+?)(?:__|$)", stem)
        if match:
            parsed["game_id"] = match.group(1)
    if not parsed["row"]:
        match = re.search(r"\br(\d+)c(\d+)\b", stem.lower())
        if match:
            parsed["row"], parsed["col"] = match.groups()
    return parsed


def scan_dataset_records(dataset_dir=DATASET_DIR):
    """Scan ``datasets/<class>/`` folders into trainer-style records.

    The folder is authoritative for the label; board-vision filenames also
    carry the square type, game ID, and board row/column. Manually added
    images are accepted too, with any missing metadata left blank.
    """
    dataset_dir = Path(dataset_dir)
    records = []
    if not dataset_dir.is_dir():
        return records

    for folder in sorted(
        (item for item in dataset_dir.iterdir() if item.is_dir()),
        key=lambda item: item.name.lower(),
    ):
        if folder.name.startswith("_"):
            continue
        label = _class_folder_label(folder.name)
        if label is None:
            continue
        default_code = CODE_BY_LABEL[label]
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
                continue
            parsed = _parse_tile_filename(path)
            label_code = parsed["label_code"] or default_code
            if LABEL_BY_CODE.get(label_code) != label:
                label_code = default_code
            records.append({
                "image_path": _path_for_csv(path),
                "filename": path.name,
                "label": label,
                "label_code": label_code,
                "square_type": parsed["square_type"],
                "split": "unsplit",
                "row": parsed["row"],
                "col": parsed["col"],
                "source_image": "",
                "board_id": parsed["game_id"],
                "game_id": parsed["game_id"] or "manually-added",
                "path": path,
            })
    return records


def load_dataset_records(dataset_dir=DATASET_DIR):
    """Compatibility wrapper: load records from the class folders."""
    return scan_dataset_records(dataset_dir)


def append_metadata_records(records, dataset_dir=DATASET_DIR):
    """Append board-review provenance/duplicate outcomes to metadata.csv."""
    if not records:
        return Path(dataset_dir) / "metadata.csv"
    dataset_dir = ensure_dataset_layout(dataset_dir)
    metadata_path = Path(dataset_dir) / "metadata.csv"
    write_header = not metadata_path.is_file() or metadata_path.stat().st_size == 0
    with metadata_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=METADATA_FIELDS)
        if write_header:
            writer.writeheader()
        for record in records:
            writer.writerow({field: record.get(field, "") for field in METADATA_FIELDS})
    return metadata_path


def dataset_balance(records, tolerance=BALANCE_TOLERANCE):
    """Per-class light/dark/highlighted counts and imbalance details.

    Highlighted squares are reported but excluded from the light-vs-dark
    tolerance check, as requested.
    """
    counts = {
        label: {"light": 0, "dark": 0, "highlighted": 0, "unknown": 0, "total": 0}
        for _code, label, _display in LABEL_OPTIONS
    }
    for record in records:
        label = record.get("label", "")
        if label not in counts:
            continue
        counts[label]["total"] += 1
        square_type = record.get("square_type", "")
        if square_type in ("light", "dark", "highlighted"):
            counts[label][square_type] += 1
        else:
            counts[label]["unknown"] += 1

    imbalanced = {}
    for label, values in counts.items():
        if abs(values["light"] - values["dark"]) > tolerance:
            imbalanced[label] = dict(values)
    return counts, imbalanced


def print_dataset_report(records, dataset_dir=DATASET_DIR):
    """Print class-folder totals and the light/dark balance report."""
    total_games = len(dataset_game_counts(records))
    print(f"\nDataset folder: {Path(dataset_dir).resolve()}")
    print(f"Total unique labelled images in class folders: {len(records)}")
    print(f"Total game IDs: {total_games}")
    print(
        "Training split: cnn_trainer.py will read these same class folders "
        "and create its training/validation/test split internally."
    )

    counts, imbalanced = dataset_balance(records)
    print("\nPer-class square counts (highlighted is excluded from balance):")
    for _code, label, display in LABEL_OPTIONS:
        values = counts[label]
        difference = abs(values["light"] - values["dark"])
        marker = "  <-- needs balancing" if label in imbalanced else ""
        print(
            f"  {display:15s}: total={values['total']:4d}, "
            f"light={values['light']:4d}, dark={values['dark']:4d}, "
            f"highlighted={values['highlighted']:4d}, "
            f"unknown={values['unknown']:4d}, "
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


def _next_dataset_sequence(existing_records, dataset_dir=DATASET_DIR):
    """Next non-overwriting numeric suffix for saved square images."""
    highest = 0
    candidates = [record.get("filename", "") for record in existing_records]
    dataset_dir = Path(dataset_dir)
    if dataset_dir.is_dir():
        candidates.extend(
            path.name
            for path in dataset_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
        )
    for name in candidates:
        match = re.search(r"(\d+)$", Path(str(name)).stem)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def _exact_tile_hash(canonical):
    """SHA-256 of a canonical tile's exact RGB pixels and shape."""
    digest = hashlib.sha256()
    digest.update(repr(tuple(canonical.shape)).encode("ascii"))
    digest.update(b"\0")
    digest.update(np.ascontiguousarray(canonical, dtype=np.uint8).tobytes())
    return digest.hexdigest()


def _average_hash(canonical):
    """64-bit average hash used only to find near-duplicate candidates."""
    grey = np.rint(np.mean(canonical.astype(np.float32), axis=2)).astype(np.uint8)
    with Image.fromarray(grey) as image:
        small = np.asarray(
            image.resize((8, 8), resample=PIL_RESAMPLE_BILINEAR),
            dtype=np.float32,
        )
    threshold = float(np.mean(small))
    value = 0
    for pixel in small.ravel():
        value = (value << 1) | int(pixel > threshold)
    return value


def _tile_fingerprint(canonical):
    """16x16 block-average RGB fingerprint of a canonical 64x64 tile."""
    size = canonical.shape[0]
    if size != canonical.shape[1] or size % 16:
        with Image.fromarray(canonical) as image:
            return np.asarray(
                image.resize((16, 16), resample=PIL_RESAMPLE_BILINEAR),
                dtype=np.float32,
            ).reshape(-1)
    block = size // 16
    return canonical.astype(np.float32).reshape(
        16, block, 16, block, 3
    ).mean(axis=(1, 3)).reshape(-1)


def _ahash_bands(average_hash):
    """Sixteen 4-bit bands; near hashes necessarily share several bands."""
    for band in range(16):
        yield band, (int(average_hash) >> (band * 4)) & 0xF


def _duplicate_metrics(first, second):
    """Mean absolute RGB error and fraction of visibly changed pixels."""
    difference = np.abs(first.astype(np.int16) - second.astype(np.int16))
    mean_abs_error = float(np.mean(difference))
    changed_fraction = float(np.mean(np.max(difference, axis=2) > 16))
    return mean_abs_error, changed_fraction


def _is_near_duplicate(mean_abs_error, changed_fraction, ahash_distance):
    return (
        mean_abs_error <= NEAR_DUPLICATE_MAX_MEAN_ABS_ERROR
        and changed_fraction <= NEAR_DUPLICATE_MAX_CHANGED_FRACTION
        and ahash_distance <= NEAR_DUPLICATE_MAX_AHASH_DISTANCE
    )


class DuplicateIndex:
    """Exact/near duplicate index over every image in the class folders.

    The index intentionally searches across *all* classes and square types:
    an exact/near pixel match under another label is a conflicting training
    example, not a useful new one. Board-vision reports such a match as
    soon as the user marks the piece and does not save the duplicate.
    """

    def __init__(self, dataset_dir=DATASET_DIR, records=None):
        self.dataset_dir = Path(dataset_dir)
        self.entries = []
        self._exact = {}
        self._bands = defaultdict(list)
        self.unreadable = []
        if records is None:
            records = scan_dataset_records(self.dataset_dir)
        for record in records:
            path = record.get("path")
            if path is None:
                raw_path = str(record.get("image_path", ""))
                path = Path(raw_path)
                if not path.is_absolute():
                    path = BASE_DIR / path
            try:
                entry = self.make_entry(
                    _load_rgb_image(path),
                    label=record.get("label", ""),
                    label_code=record.get("label_code", ""),
                    square_type=record.get("square_type", ""),
                    path=path,
                    source="datasets folder",
                )
            except Exception:
                self.unreadable.append(Path(path))
                continue
            self._register(entry)

    @classmethod
    def load(cls, dataset_dir=DATASET_DIR):
        ensure_dataset_layout(dataset_dir)
        return cls(dataset_dir)

    @property
    def example_count(self):
        return len(self.entries)

    def make_entry(
        self,
        tile,
        label_option=None,
        square_type="",
        *,
        label="",
        label_code="",
        path=None,
        source="current board",
    ):
        if label_option is not None:
            label_code, label, _display = label_option
        canonical = canonical_tile_array(tile)
        return {
            "path": path,
            "label": label,
            "label_code": label_code,
            "square_type": square_type or "",
            "source": source,
            "exact_hash": _exact_tile_hash(canonical),
            "average_hash": _average_hash(canonical),
            "fingerprint": _tile_fingerprint(canonical),
            "canonical": canonical if path is None else None,
        }

    def _register(self, entry):
        index = len(self.entries)
        self.entries.append(entry)
        self._exact.setdefault(entry["exact_hash"], entry)
        for band_key in _ahash_bands(entry["average_hash"]):
            self._bands[band_key].append(index)
        return entry

    def add_saved(self, record, tile):
        """Add one just-saved tile so later boards in this run see it."""
        path = record.get("path")
        if path is None and record.get("image_path"):
            path = Path(record["image_path"])
            if not path.is_absolute():
                path = BASE_DIR / path
        entry = self.make_entry(
            tile,
            label=record.get("label", ""),
            label_code=record.get("label_code", ""),
            square_type=record.get("square_type", ""),
            path=path,
            source="datasets folder",
        )
        return self._register(entry)

    @staticmethod
    def _report(kind, entry, mean_abs_error, changed_fraction, ahash_distance):
        path = entry.get("path")
        return {
            "kind": kind,
            "path": str(path) if path is not None else entry.get("source", "current board"),
            "label": entry.get("label", ""),
            "label_code": entry.get("label_code", ""),
            "square_type": entry.get("square_type", ""),
            "source": entry.get("source", ""),
            "mean_abs_error": float(mean_abs_error),
            "changed_fraction": float(changed_fraction),
            "average_hash_distance": int(ahash_distance),
        }

    def find_duplicate(self, tile, label_option=None, square_type="", extra_entries=()):
        """Return an exact/near duplicate report for ``tile``, or None.

        ``label_option`` and ``square_type`` describe the user's new mark for
        API clarity; matching itself is pixel-based across every class.
        ``extra_entries`` are unique tiles already accepted from the board
        currently being reviewed but not saved to disk yet.
        """
        candidate = self.make_entry(
            tile,
            label_option=label_option,
            square_type=square_type,
            path=None,
            source="candidate",
        )
        canonical = candidate["canonical"]

        exact_entry = self._exact.get(candidate["exact_hash"])
        if exact_entry is not None:
            return self._report("exact", exact_entry, 0.0, 0.0, 0)
        for entry in extra_entries:
            if entry["exact_hash"] == candidate["exact_hash"]:
                return self._report("exact", entry, 0.0, 0.0, 0)

        candidate_indices = set()
        for band_key in _ahash_bands(candidate["average_hash"]):
            candidate_indices.update(self._bands.get(band_key, ()))
        possible_entries = [self.entries[index] for index in sorted(candidate_indices)]
        possible_entries.extend(extra_entries)

        best = None
        for entry in possible_entries:
            ahash_distance = int(
                (candidate["average_hash"] ^ entry["average_hash"]).bit_count()
            )
            if ahash_distance > NEAR_DUPLICATE_MAX_AHASH_DISTANCE:
                continue
            fingerprint_mae = float(
                np.mean(np.abs(candidate["fingerprint"] - entry["fingerprint"]))
            )
            if fingerprint_mae > NEAR_DUPLICATE_PREFILTER_MAX_MAE:
                continue

            other = entry.get("canonical")
            if other is None:
                try:
                    other = canonical_tile_array(_load_rgb_image(entry["path"]))
                except Exception:
                    continue
            mean_abs_error, changed_fraction = _duplicate_metrics(canonical, other)
            if not _is_near_duplicate(
                mean_abs_error, changed_fraction, ahash_distance
            ):
                continue
            report = self._report(
                "near", entry, mean_abs_error, changed_fraction, ahash_distance
            )
            if best is None or report["mean_abs_error"] < best["mean_abs_error"]:
                best = report
        return best


def duplicate_message(report):
    """User-facing explanation shown when a marked tile is not saved."""
    if not report:
        return ""
    kind = "exact-pixel" if report.get("kind") == "exact" else "near-duplicate"
    existing_label = report.get("label") or "an existing class"
    square = report.get("square_type") or "unknown square type"
    return (
        f"DUPLICATE ({kind}) — not saved. It matches {existing_label} "
        f"({square}) at {report.get('path', 'an existing tile')}. "
        "Duplicate images make CNN training slower and can make the model "
        "faulty/over-confident, so only the first unique copy is kept."
    )


def save_labelled_tiles(
    tile_entries,
    answers,
    source_image,
    board_id,
    game_id=None,
    duplicate_reports=None,
    duplicate_index=None,
    dataset_dir=DATASET_DIR,
):
    """Save unique reviewed tiles into ``datasets/<class>/`` folders.

    Returns ``(saved_records, skipped_duplicate_records)``. A tile already
    reported as a duplicate during manual review is not written. As a final
    safety check, every other tile is checked again against the folders and
    against unique tiles saved earlier from this same board.
    """
    if len(tile_entries) != len(answers):
        raise ValueError("Every tile must have a manual label and square type")
    if duplicate_reports is not None and len(duplicate_reports) != len(answers):
        raise ValueError("Every tile must have a duplicate-check result")

    dataset_dir = ensure_dataset_layout(dataset_dir)
    if duplicate_index is None:
        duplicate_index = DuplicateIndex.load(dataset_dir)

    existing = scan_dataset_records(dataset_dir)
    sequence = _next_dataset_sequence(existing, dataset_dir)
    saved_records = []
    skipped_records = []
    metadata_records = []
    game_id = game_id or board_id

    for tile_index, ((row, col, tile), (label_option, square_type)) in enumerate(
        zip(tile_entries, answers)
    ):
        label_code, label, _display = label_option
        report = None
        if duplicate_reports is not None:
            report = duplicate_reports[tile_index]
        if report is None:
            report = duplicate_index.find_duplicate(
                tile, label_option=label_option, square_type=square_type
            )

        metadata = {
            "image_path": "",
            "filename": "",
            "label": label,
            "label_code": label_code,
            "square_type": square_type,
            "row": str(row),
            "col": str(col),
            "source_image": _path_for_csv(source_image),
            "board_id": board_id,
            "game_id": game_id,
            "saved": "0",
            "duplicate_of": "",
            "duplicate_kind": "",
            "duplicate_mean_abs_error": "",
            "duplicate_changed_fraction": "",
        }

        if report is not None:
            metadata.update({
                "duplicate_of": str(report.get("path", "")),
                "duplicate_kind": str(report.get("kind", "")),
                "duplicate_mean_abs_error": f"{float(report.get('mean_abs_error', 0.0)):.4f}",
                "duplicate_changed_fraction": f"{float(report.get('changed_fraction', 0.0)):.4f}",
            })
            skipped = dict(metadata)
            skipped["duplicate_report"] = report
            skipped_records.append(skipped)
            metadata_records.append(metadata)
            continue

        class_dir = dataset_dir / CLASS_FOLDER_BY_LABEL[label]
        while True:
            filename = (
                f"{label_code}_{square_type}__{game_id}__"
                f"r{row}c{col}__{sequence:06d}.png"
            )
            image_path = class_dir / filename
            sequence += 1
            if not image_path.exists():
                break

        _save_rgb_image(image_path, tile)
        metadata.update({
            "image_path": _path_for_csv(image_path),
            "filename": filename,
            "saved": "1",
        })
        record = dict(metadata)
        record["split"] = "unsplit"
        record["path"] = image_path
        saved_records.append(record)
        metadata_records.append(metadata)
        duplicate_index.add_saved(record, tile)

    append_metadata_records(metadata_records, dataset_dir)
    return saved_records, skipped_records


def _resolve_legacy_image_path(value, csv_path, legacy_dir):
    """Resolve an image path stored in the old board_debug CSV."""
    raw_path = Path(str(value))
    if raw_path.is_absolute():
        return raw_path
    candidates = [
        Path(csv_path).parent / raw_path,
        Path(legacy_dir) / raw_path.name,
        BASE_DIR / raw_path,
        Path.cwd() / raw_path,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def load_legacy_records(csv_path=LEGACY_DATASET_CSV, legacy_dir=LEGACY_DATASET_DIR):
    """Load old flat board_debug rows for a one-time folder migration."""
    csv_path = Path(csv_path)
    legacy_dir = Path(legacy_dir)
    if not csv_path.is_file():
        raise FileNotFoundError(f"Legacy dataset CSV does not exist: {csv_path}")

    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    records = []
    for row in rows:
        if str(row.get("saved", "1")).strip() == "0":
            continue
        image_value = row.get("image_path") or row.get("filename") or ""
        if not image_value:
            continue

        label = str(row.get("label") or "").strip()
        label_code = str(row.get("label_code") or "").strip().lower()
        if not label and label_code in LABEL_BY_CODE:
            label = LABEL_BY_CODE[label_code]
        if label == "blank":
            label = "empty"
        if label not in LABEL_OPTION_BY_LABEL:
            continue
        label_option = LABEL_OPTION_BY_LABEL[label]
        label_code = label_option[0]

        square_type = str(row.get("square_type") or "").strip().lower()
        if square_type not in SQUARE_TYPE_DISPLAY:
            parsed = _parse_tile_filename(image_value)
            square_type = parsed["square_type"] or "light"

        image_path = _resolve_legacy_image_path(image_value, csv_path, legacy_dir)
        game_id = str(
            row.get("game_id")
            or row.get("board_id")
            or row.get("source_image")
            or "legacy-import"
        ).strip()
        records.append({
            "path": image_path,
            "label": label,
            "label_code": label_code,
            "label_option": label_option,
            "square_type": square_type,
            "row": str(row.get("row") or "0"),
            "col": str(row.get("col") or "0"),
            "source_image": str(row.get("source_image") or image_value),
            "board_id": str(row.get("board_id") or game_id),
            "game_id": game_id,
        })
    return records


def import_legacy_dataset(
    legacy_dir=LEGACY_DATASET_DIR,
    csv_path=None,
    dataset_dir=DATASET_DIR,
    duplicate_index=None,
):
    """Copy unique old board_debug tiles into ``datasets/<class>/``.

    This is a one-time migration helper. The legacy split columns are not
    copied: the migrated images become part of the same shared class-folder
    source, and cnn_trainer.py derives fresh whole-game splits from the
    preserved game IDs. Exact/near duplicates—including duplicates already
    inside the old dataset—are reported in the summary/metadata and skipped.
    """
    legacy_dir = Path(legacy_dir)
    csv_path = Path(csv_path) if csv_path is not None else legacy_dir / "dataset.csv"
    dataset_dir = ensure_dataset_layout(dataset_dir)
    if duplicate_index is None:
        duplicate_index = DuplicateIndex.load(dataset_dir)

    legacy_records = load_legacy_records(csv_path, legacy_dir)
    existing = scan_dataset_records(dataset_dir)
    sequence = _next_dataset_sequence(existing, dataset_dir)
    saved_records = []
    skipped_records = []
    missing_records = []
    metadata_records = []

    print(f"\nImporting {len(legacy_records)} legacy row(s) from {csv_path}...")
    for legacy in legacy_records:
        label_option = legacy["label_option"]
        label_code, label, _display = label_option
        square_type = legacy["square_type"]
        metadata = {
            "image_path": "",
            "filename": "",
            "label": label,
            "label_code": label_code,
            "square_type": square_type,
            "row": legacy["row"],
            "col": legacy["col"],
            "source_image": _path_for_csv(legacy["source_image"]),
            "board_id": legacy["board_id"],
            "game_id": legacy["game_id"],
            "saved": "0",
            "duplicate_of": "",
            "duplicate_kind": "",
            "duplicate_mean_abs_error": "",
            "duplicate_changed_fraction": "",
        }

        try:
            tile = _load_rgb_image(legacy["path"])
        except Exception:
            missing_records.append(legacy)
            metadata_records.append(metadata)
            continue

        report = duplicate_index.find_duplicate(
            tile, label_option=label_option, square_type=square_type
        )
        if report is not None:
            metadata.update({
                "duplicate_of": str(report.get("path", "")),
                "duplicate_kind": str(report.get("kind", "")),
                "duplicate_mean_abs_error": f"{float(report.get('mean_abs_error', 0.0)):.4f}",
                "duplicate_changed_fraction": f"{float(report.get('changed_fraction', 0.0)):.4f}",
            })
            skipped = dict(metadata)
            skipped["duplicate_report"] = report
            skipped_records.append(skipped)
            metadata_records.append(metadata)
            continue

        try:
            row = int(legacy["row"])
            col = int(legacy["col"])
        except (TypeError, ValueError):
            row = col = 0
        safe_game_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(legacy["game_id"]))
        class_dir = dataset_dir / CLASS_FOLDER_BY_LABEL[label]
        while True:
            filename = (
                f"{label_code}_{square_type}__{safe_game_id}__"
                f"r{row}c{col}__{sequence:06d}.png"
            )
            image_path = class_dir / filename
            sequence += 1
            if not image_path.exists():
                break

        _save_rgb_image(image_path, tile)
        metadata.update({
            "image_path": _path_for_csv(image_path),
            "filename": filename,
            "saved": "1",
        })
        record = dict(metadata)
        record["split"] = "unsplit"
        record["path"] = image_path
        saved_records.append(record)
        metadata_records.append(metadata)
        duplicate_index.add_saved(record, tile)

    append_metadata_records(metadata_records, dataset_dir)
    print(f"Imported {len(saved_records)} unique legacy image(s) into {dataset_dir}.")
    if skipped_records:
        print(
            f"Skipped {len(skipped_records)} legacy duplicate image(s). "
            "Duplicates slow training and can make the model faulty, so they "
            "were not copied into the class folders."
        )
        for skipped in skipped_records[:10]:
            report = skipped["duplicate_report"]
            print(f"  duplicate ({report.get('kind')}): {duplicate_message(report)}")
        if len(skipped_records) > 10:
            print(f"  ... and {len(skipped_records) - 10} more (see metadata.csv)")
    if missing_records:
        print(f"Skipped {len(missing_records)} missing/unreadable legacy image(s).")

    all_records = scan_dataset_records(dataset_dir)
    print_dataset_report(all_records, dataset_dir)
    return all_records


def run_cnn_trainer(records, dataset_dir=DATASET_DIR):
    """Run cnn_trainer.py against the shared ``datasets/<class>/`` folders."""
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
        str(Path(dataset_dir)),
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


_FONT_CACHE = {}


def _require_pygame():
    if pygame is None:
        raise RuntimeError(
            "pygame is required for board-vision.py's manual windows. "
            "Install it with: pip install pygame"
        )
    return pygame


def _font(size=20, bold=False):
    _require_pygame()
    key = (int(size), bool(bold))
    if key not in _FONT_CACHE:
        font = pygame.font.Font(None, int(size))
        font.set_bold(bool(bold))
        _FONT_CACHE[key] = font
    return _FONT_CACHE[key]


def _open_window(size, title):
    _require_pygame()
    if not pygame.get_init():
        pygame.init()
    if not pygame.display.get_init():
        pygame.display.init()
    if not pygame.font.get_init():
        pygame.font.init()
    screen = pygame.display.set_mode(size)
    pygame.display.set_caption(title)
    return screen


def _close_gui():
    if pygame is not None and pygame.display.get_init():
        pygame.display.quit()


def _surface_from_rgb(array):
    _require_pygame()
    rgb = _as_rgb_array(array)
    height, width = rgb.shape[:2]
    surface = pygame.image.frombuffer(rgb.tobytes(), (width, height), "RGB")
    return surface.convert()


def _draw_text(surface, text, position, size=20, colour=(255, 255, 255),
               bold=False, center_x=None):
    font = _font(size, bold)
    rendered = font.render(str(text), True, colour)
    x, y = position
    if center_x is not None:
        x = int(center_x - rendered.get_width() / 2)
    # A subtle dark shadow keeps white text readable over bright tiles.
    shadow = font.render(str(text), True, (0, 0, 0))
    surface.blit(shadow, (x + 1, y + 1))
    surface.blit(rendered, (x, y))
    return rendered.get_rect(topleft=(x, y))


def _wrap_text(text, max_width, size=18, bold=False):
    font = _font(size, bold)
    lines = []
    for paragraph in str(text).splitlines() or [""]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if font.size(candidate)[0] <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


class ZoomView:
    """Zoomable/pannable Pygame view for pixel-accurate board work.

    Mouse wheel (or +/-) zooms around the cursor and right/middle-drag
    pans. This replaces the old OpenCV HighGUI plumbing while keeping the
    same pixel-based selection workflow.
    """

    def __init__(self, title, image):
        _require_pygame()
        self.title = title
        rgb = _as_rgb_array(image)
        self.img_h, self.img_w = rgb.shape[:2]
        self.surface = _surface_from_rgb(rgb)
        self.fit_zoom = min(VIEW_W / self.img_w, VIEW_H / self.img_h)
        self.min_zoom = self.fit_zoom * 0.25
        self.max_zoom = max(60.0, self.fit_zoom * 120.0)
        self.mouse = (VIEW_W // 2, VIEW_H // 2)
        self.ldown = False
        self._panning = False
        self._pan_last = None
        self.reset()
        pygame.display.set_caption(title)

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

    def screen_to_image(self, point):
        return (
            self.ox + point[0] / self.zoom,
            self.oy + point[1] / self.zoom,
        )

    def image_to_screen(self, x, y=None):
        if y is None:
            x, y = x
        return (
            int(round((float(x) - self.ox) * self.zoom)),
            int(round((float(y) - self.oy) * self.zoom)),
        )

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

    def handle_event(self, event):
        _require_pygame()
        if event.type == pygame.MOUSEMOTION:
            self.mouse = event.pos
            if self._panning and self._pan_last is not None:
                self.ox -= (event.pos[0] - self._pan_last[0]) / self.zoom
                self.oy -= (event.pos[1] - self._pan_last[1]) / self.zoom
                self._pan_last = event.pos
                self._clamp_offset()
        elif event.type == pygame.MOUSEWHEEL:
            self.mouse = pygame.mouse.get_pos()
            steps = float(np.clip(getattr(event, "y", 0), -3.0, 3.0))
            if steps:
                self.zoom_by(1.2 ** steps)
        elif event.type == pygame.MOUSEBUTTONDOWN:
            self.mouse = event.pos
            if event.button == 1:
                self.ldown = True
            elif event.button in (2, 3):
                self._panning = True
                self._pan_last = event.pos
            elif event.button == 4:
                self.zoom_by(1.2)
            elif event.button == 5:
                self.zoom_by(1.0 / 1.2)
        elif event.type == pygame.MOUSEBUTTONUP:
            self.mouse = event.pos
            if event.button == 1:
                self.ldown = False
            elif event.button in (2, 3):
                self._panning = False
                self._pan_last = None

    def render(self, screen, hud_lines=(), overlay=None):
        """Draw the visible image area into the left VIEW_W x VIEW_H pane."""
        _require_pygame()
        screen.fill((40, 40, 40), pygame.Rect(0, 0, VIEW_W, VIEW_H))
        x0, y0 = math.floor(self.ox), math.floor(self.oy)
        x1 = math.ceil(self.ox + VIEW_W / self.zoom)
        y1 = math.ceil(self.oy + VIEW_H / self.zoom)
        sx0, sy0 = max(x0, 0), max(y0, 0)
        sx1, sy1 = min(x1, self.img_w), min(y1, self.img_h)

        if sx1 > sx0 and sy1 > sy0:
            source_rect = pygame.Rect(sx0, sy0, sx1 - sx0, sy1 - sy0)
            patch = self.surface.subsurface(source_rect)
            width = max(1, int(round((sx1 - sx0) * self.zoom)))
            height = max(1, int(round((sy1 - sy0) * self.zoom)))
            if self.zoom >= 1.0:
                scaled = pygame.transform.scale(patch, (width, height))
            else:
                scaled = pygame.transform.smoothscale(patch, (width, height))
            dx = int(round((sx0 - self.ox) * self.zoom))
            dy = int(round((sy0 - self.oy) * self.zoom))
            screen.blit(scaled, (dx, dy))

        if overlay is not None:
            overlay(screen, self)

        if hud_lines:
            box_h = 24 * len(hud_lines) + 12
            pygame.draw.rect(screen, (20, 20, 20), (0, 0, VIEW_W, box_h))
            for index, line in enumerate(hud_lines):
                _draw_text(screen, line, (10, 7 + 24 * index), size=18)


def _view_key_event(view, event):
    """Shared zoom keys for the interactive Pygame windows."""
    _require_pygame()
    if event.type != pygame.KEYDOWN:
        return False
    if event.key in (pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS):
        view.zoom_by(1.25)
        return True
    if event.key in (pygame.K_MINUS, pygame.K_UNDERSCORE, pygame.K_KP_MINUS):
        view.zoom_by(0.8)
        return True
    if event.key in (pygame.K_0, pygame.K_KP0):
        view.reset()
        return True
    return False


def select_board_roi(image):
    """Drag-select the board on a zoomable Pygame view.

    Returns ``(x, y, w, h)`` in image pixels, or None if cancelled.
    """
    _require_pygame()
    rgb = _as_rgb_array(image)
    img_h, img_w = rgb.shape[:2]
    screen = _open_window((VIEW_W, VIEW_H), "Select chessboard")
    view = ZoomView("Select chessboard", rgb)
    clock = pygame.time.Clock()
    drag_start = None
    drag_now = None
    rect = None

    try:
        while True:
            pressed_keys = set()
            quit_requested = False
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    quit_requested = True
                elif event.type == pygame.KEYDOWN:
                    pressed_keys.add(event.key)
                    _view_key_event(view, event)
                else:
                    view.handle_event(event)
            if quit_requested or pressed_keys & {pygame.K_ESCAPE, pygame.K_c}:
                return None

            view.mouse = pygame.mouse.get_pos()
            ix, iy = view.mouse_image()
            ix = int(np.clip(ix, 0, img_w - 1))
            iy = int(np.clip(iy, 0, img_h - 1))

            if view.ldown:
                if drag_start is None:
                    drag_start = (ix, iy)
                drag_now = (ix, iy)
            elif drag_start is not None:
                rect = (
                    min(drag_start[0], drag_now[0]),
                    min(drag_start[1], drag_now[1]),
                    max(drag_start[0], drag_now[0]),
                    max(drag_start[1], drag_now[1]),
                )
                drag_start = drag_now = None

            shown = rect
            if drag_start is not None:
                shown = (
                    min(drag_start[0], drag_now[0]),
                    min(drag_start[1], drag_now[1]),
                    max(drag_start[0], drag_now[0]),
                    max(drag_start[1], drag_now[1]),
                )

            if shown and pressed_keys & {
                pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE
            }:
                width = shown[2] - shown[0] + 1
                height = shown[3] - shown[1] + 1
                if width >= 16 and height >= 16:
                    return shown[0], shown[1], width, height

            def overlay(surface, current_view, shown_rect=shown):
                mouse_x, mouse_y = current_view.mouse
                if 0 <= mouse_x < VIEW_W:
                    pygame.draw.line(
                        surface, (0, 255, 255), (mouse_x, 0), (mouse_x, VIEW_H - 1), 1
                    )
                if 0 <= mouse_y < VIEW_H:
                    pygame.draw.line(
                        surface, (0, 255, 255), (0, mouse_y), (VIEW_W - 1, mouse_y), 1
                    )
                if shown_rect:
                    left, top = current_view.image_to_screen(shown_rect[0], shown_rect[1])
                    right, bottom = current_view.image_to_screen(
                        shown_rect[2] + 1, shown_rect[3] + 1
                    )
                    select_rect = pygame.Rect(
                        min(left, right), min(top, bottom),
                        max(1, abs(right - left)), max(1, abs(bottom - top)),
                    )
                    pygame.draw.rect(surface, (0, 255, 0), select_rect, 1)

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
            view.render(screen, hud, overlay=overlay)
            pygame.display.flip()
            clock.tick(60)
    finally:
        _close_gui()


def _manual_button(surface, rect, text, selected=False):
    _require_pygame()
    if selected:
        fill = (64, 145, 72)
        border = (150, 255, 170)
    else:
        fill = (62, 62, 72)
        border = (165, 165, 175)
    pygame.draw.rect(surface, fill, rect)
    pygame.draw.rect(surface, border, rect, 2)
    font = _font(19)
    rendered = font.render(str(text), True, (255, 255, 255))
    text_rect = rendered.get_rect(center=rect.center)
    shadow = font.render(str(text), True, (0, 0, 0))
    surface.blit(shadow, (text_rect.x + 1, text_rect.y + 1))
    surface.blit(rendered, text_rect)


def manual_review_tiles(tile_entries, duplicate_checker=None, assistant=None,
                        semi_auto_enabled=False):
    """Manually mark every tile and immediately check it for duplicates.

    There are deliberately no OpenCV/model suggestions: the user first
    clicks the piece/blank on the square, then Light, Dark, or Highlighted.
    As soon as the piece is marked, the tile's normalised pixels are checked
    against all existing class-folder images and unique tiles accepted
    earlier on this board. Exact/near duplicates are announced in the GUI
    and terminal and returned as skipped items; they are not saved.

    Returns ``(answers, duplicate_reports)``, or None if cancelled. The two
    legacy keyword arguments are accepted and ignored for compatibility.
    """
    _ = (assistant, semi_auto_enabled)  # Manual marking is now always used.
    if not tile_entries:
        return [], []

    _require_pygame()
    answers = [None] * len(tile_entries)
    duplicate_reports = [None] * len(tile_entries)
    preview_cache = {}
    index = 0
    stage = "label"
    selected_label = None
    pending_duplicate = None
    pending_duplicate_checked = False
    status_message = ""
    status_colour = (210, 225, 255)
    duplicate_warning_printed = False

    def preview_surface(tile_index):
        if tile_index not in preview_cache:
            tile = tile_entries[tile_index][2]
            if tile is None or np.asarray(tile).size == 0:
                preview = np.full(
                    (MANUAL_TILE_SIZE, MANUAL_TILE_SIZE, 3), 70, np.uint8
                )
            else:
                rgb = _as_rgb_array(tile)
                resample = (
                    PIL_RESAMPLE_LANCZOS
                    if max(rgb.shape[:2]) > MANUAL_TILE_SIZE
                    else PIL_RESAMPLE_NEAREST
                )
                preview = _resize_rgb_array(
                    rgb, (MANUAL_TILE_SIZE, MANUAL_TILE_SIZE), resample
                )
            preview_cache[tile_index] = _surface_from_rgb(preview)
        return preview_cache[tile_index]

    def provisional_entries():
        if duplicate_checker is None:
            return []
        entries = []
        for answer_index in range(index):
            answer = answers[answer_index]
            if answer is None or duplicate_reports[answer_index] is not None:
                continue
            prior_row, prior_col, prior_tile = tile_entries[answer_index]
            entries.append(
                duplicate_checker.make_entry(
                    prior_tile,
                    label_option=answer[0],
                    square_type=answer[1],
                    source=f"current board square r{prior_row} c{prior_col}",
                )
            )
        return entries

    def check_current(label_option, square_type=""):
        if duplicate_checker is None:
            return None
        try:
            return duplicate_checker.find_duplicate(
                tile_entries[index][2],
                label_option=label_option,
                square_type=square_type,
                extra_entries=provisional_entries(),
            )
        except Exception as exc:
            print(f"Warning: duplicate check failed for this tile: {exc}")
            return None

    def completed_counts():
        unique = 0
        duplicates = 0
        for answer_index in range(index):
            if answers[answer_index] is None:
                continue
            if duplicate_reports[answer_index] is None:
                unique += 1
            else:
                duplicates += 1
        return unique, duplicates

    def make_buttons():
        buttons = []
        if stage == "label":
            start_x, start_y = 424, 108
            button_w, button_h, gap = 208, 54, 12
            for option_index, option in enumerate(LABEL_OPTIONS):
                grid_col = option_index % 3
                grid_row = option_index // 3
                rect = pygame.Rect(
                    start_x + grid_col * (button_w + gap),
                    start_y + grid_row * (button_h + gap),
                    button_w,
                    button_h,
                )
                buttons.append((rect, "label", option))
        else:
            start_x, start_y = 460, 140
            button_w, button_h, gap = 360, 68, 18
            for option_index, (square_type, _display) in enumerate(SQUARE_TYPE_OPTIONS):
                rect = pygame.Rect(
                    start_x,
                    start_y + option_index * (button_h + gap),
                    button_w,
                    button_h,
                )
                buttons.append((rect, "square", square_type))

        back_rect = pygame.Rect(424, MANUAL_H - 78, 190, 48)
        cancel_rect = pygame.Rect(630, MANUAL_H - 78, 190, 48)
        buttons.append((back_rect, "back", None))
        buttons.append((cancel_rect, "cancel", None))
        return buttons

    def draw(buttons):
        row, col, _tile = tile_entries[index]
        screen.fill((36, 36, 36))
        _draw_text(
            screen,
            f"Manual review {index + 1}/{len(tile_entries)}  -  square r{row} c{col}",
            (24, 18),
            size=27,
            bold=True,
        )
        if stage == "label":
            instruction = "Step 1: click the piece/blank that is on this square"
        elif pending_duplicate is not None:
            instruction = (
                f"Step 2: {selected_label[2]} is a duplicate — still choose its "
                "square type to finish marking it"
            )
        else:
            instruction = (
                f"Step 2: {selected_label[2]} - light, dark, or highlighted square?"
            )
        _draw_text(screen, instruction, (24, 55), size=20)

        preview_rect = pygame.Rect(24, 104, MANUAL_TILE_SIZE, MANUAL_TILE_SIZE)
        screen.blit(preview_surface(index), preview_rect)
        pygame.draw.rect(screen, (230, 230, 230), preview_rect, 2)
        _draw_text(screen, f"Board square r{row} c{col}", (24, 478), size=21)
        unique_count, duplicate_count = completed_counts()
        _draw_text(
            screen,
            f"Unique to save: {unique_count}   Duplicates skipped: {duplicate_count}",
            (24, 510),
            size=19,
            colour=(190, 220, 255),
        )
        if duplicate_checker is not None:
            _draw_text(
                screen,
                f"Existing dataset images checked: {duplicate_checker.example_count}",
                (24, 538),
                size=18,
                colour=(200, 200, 200),
            )

        for rect, kind, value in buttons:
            if kind == "label":
                _manual_button(
                    screen, rect, f"{value[2]} ({value[0]})",
                    selected=(selected_label == value),
                )
            elif kind == "square":
                _manual_button(screen, rect, SQUARE_TYPE_DISPLAY[value])
            elif kind == "back":
                _manual_button(screen, rect, "Back (B)")
            elif kind == "cancel":
                _manual_button(screen, rect, "Cancel (Esc)")

        if stage == "square":
            _draw_text(
                screen,
                "Keys: L = Light square, D = Dark square, H = Highlighted square",
                (430, 424),
                size=19,
            )

        if status_message:
            y = 486
            for line in _wrap_text(status_message, 660, size=18)[:8]:
                _draw_text(screen, line, (424, y), size=18, colour=status_colour)
                y += 22

        _draw_text(
            screen,
            "Every square is marked manually; duplicate pixels are skipped.",
            (24, MANUAL_H - 28),
            size=19,
            colour=(190, 220, 255),
        )

    screen = _open_window((MANUAL_W, MANUAL_H), "Manual piece review")
    clock = pygame.time.Clock()

    try:
        while index < len(tile_entries):
            buttons = make_buttons()
            action = None
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return None
                if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    for rect, kind, value in buttons:
                        if rect.collidepoint(event.pos):
                            action = (kind, value)
                            break
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        action = ("cancel", None)
                    elif event.key in (pygame.K_BACKSPACE, pygame.K_b):
                        action = ("back", None)
                    elif stage == "square" and event.key == pygame.K_l:
                        action = ("square", "light")
                    elif stage == "square" and event.key == pygame.K_d:
                        action = ("square", "dark")
                    elif stage == "square" and event.key == pygame.K_h:
                        action = ("square", "highlighted")
                if action is not None:
                    break

            if action is not None:
                kind, value = action
                if kind == "cancel":
                    return None
                if kind == "back":
                    if stage == "square":
                        stage = "label"
                        pending_duplicate = None
                        pending_duplicate_checked = False
                        status_message = ""
                    elif index > 0:
                        index -= 1
                        previous = answers[index]
                        selected_label = previous[0] if previous else None
                        stage = "label"
                        pending_duplicate = None
                        pending_duplicate_checked = False
                        status_message = ""
                elif kind == "label" and stage == "label":
                    selected_label = value
                    stage = "square"
                    # Check immediately after the piece is marked, before
                    # asking for its square type.
                    pending_duplicate = check_current(selected_label)
                    pending_duplicate_checked = True
                    duplicate_warning_printed = False
                    if pending_duplicate is not None:
                        status_message = duplicate_message(pending_duplicate)
                        status_colour = (255, 150, 150)
                    else:
                        status_message = (
                            "No exact/near pixel duplicate found yet. Choose the "
                            "square type to finish this unique tile."
                        )
                        status_colour = (170, 255, 190)
                elif kind == "square" and stage == "square" and selected_label is not None:
                    answers[index] = (selected_label, value)
                    report = (
                        pending_duplicate
                        if pending_duplicate_checked
                        else check_current(selected_label, value)
                    )
                    duplicate_reports[index] = report
                    row, col, _tile = tile_entries[index]
                    if report is not None:
                        status_message = duplicate_message(report)
                        status_colour = (255, 150, 150)
                        if not duplicate_warning_printed:
                            print(
                                f"Duplicate at r{row} c{col}: "
                                f"{duplicate_message(report)}"
                            )
                            duplicate_warning_printed = True
                    else:
                        status_message = (
                            f"Unique tile — it will be saved to "
                            f"datasets/{selected_label[1]}/ after this board."
                        )
                        status_colour = (170, 255, 190)
                    index += 1
                    selected_label = None
                    pending_duplicate = None
                    pending_duplicate_checked = False
                    stage = "label"

            if index >= len(tile_entries):
                break
            draw(make_buttons())
            pygame.display.flip()
            clock.tick(60)
    finally:
        _close_gui()

    return answers, duplicate_reports


def _make_grid_debug_image(board, square_size):
    """Create the saved board image with green grid lines and r,c labels."""
    rgb = _as_rgb_array(board)
    image = Image.fromarray(rgb.copy())
    draw = ImageDraw.Draw(image)
    for row in range(8):
        for col in range(8):
            left = round(col * square_size)
            top = round(row * square_size)
            right = round((col + 1) * square_size) - 1
            bottom = round((row + 1) * square_size) - 1
            draw.rectangle((left, top, right, bottom), outline=(0, 255, 0), width=1)
            draw.text((left + 3, top + 3), f"{row},{col}", fill=(255, 0, 0))
    return np.asarray(image).copy()


def show_grid_debug(board, square_size):
    """Inspect the detected 8x8 grid and exact hovered tile in Pygame."""
    _require_pygame()
    rgb = _as_rgb_array(board)
    size = rgb.shape[0]
    panel_width = PREVIEW_SIZE + 24
    screen = _open_window(
        (VIEW_W + panel_width, VIEW_H), "Detected board grid"
    )
    view = ZoomView("Detected board grid", rgb)
    clock = pygame.time.Clock()
    preview_cache = {}

    def tile_surface(row, col):
        key = (row, col)
        if key not in preview_cache:
            left = round(col * square_size)
            top = round(row * square_size)
            right = round((col + 1) * square_size)
            bottom = round((row + 1) * square_size)
            tile = rgb[top:bottom, left:right]
            preview_cache[key] = _surface_from_rgb(
                _resize_rgb_array(
                    tile, (PREVIEW_SIZE, PREVIEW_SIZE), PIL_RESAMPLE_NEAREST
                )
            )
        return preview_cache[key]

    try:
        while True:
            should_close = False
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    should_close = True
                elif event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_ESCAPE, pygame.K_q):
                        should_close = True
                    else:
                        _view_key_event(view, event)
                else:
                    view.handle_event(event)
            if should_close:
                break

            view.mouse = pygame.mouse.get_pos()
            ix, iy = view.mouse_image()
            hover = None
            hover_bounds = None
            if view.mouse[0] < VIEW_W and 0 <= ix < size and 0 <= iy < size:
                hover = (
                    min(int(iy // square_size), 7),
                    min(int(ix // square_size), 7),
                )
                row, col = hover
                hover_bounds = (
                    round(col * square_size),
                    round(row * square_size),
                    round((col + 1) * square_size),
                    round((row + 1) * square_size),
                )

            def overlay(surface, current_view, hover_bounds=hover_bounds):
                for boundary in range(9):
                    position = round(boundary * square_size)
                    x_screen, top_screen = current_view.image_to_screen(position, 0)
                    _x2, bottom_screen = current_view.image_to_screen(position, size)
                    if 0 <= x_screen < VIEW_W:
                        pygame.draw.line(
                            surface, (0, 255, 0),
                            (x_screen, max(0, top_screen)),
                            (x_screen, min(VIEW_H - 1, bottom_screen)), 1,
                        )
                    left_screen, y_screen = current_view.image_to_screen(0, position)
                    right_screen, _y2 = current_view.image_to_screen(size, position)
                    if 0 <= y_screen < VIEW_H:
                        pygame.draw.line(
                            surface, (0, 255, 0),
                            (max(0, left_screen), y_screen),
                            (min(VIEW_W - 1, right_screen), y_screen), 1,
                        )

                if square_size * current_view.zoom >= 28:
                    for row in range(8):
                        for col in range(8):
                            left = round(col * square_size)
                            top = round(row * square_size)
                            sx, sy = current_view.image_to_screen(left, top)
                            if 0 <= sx < VIEW_W - 24 and 55 <= sy < VIEW_H - 16:
                                _draw_text(
                                    surface, f"{row},{col}", (sx + 3, sy + 3),
                                    size=14, colour=(255, 70, 70),
                                )
                if hover_bounds is not None:
                    left, top = current_view.image_to_screen(
                        hover_bounds[0], hover_bounds[1]
                    )
                    right, bottom = current_view.image_to_screen(
                        hover_bounds[2], hover_bounds[3]
                    )
                    rect = pygame.Rect(
                        min(left, right), min(top, bottom),
                        max(1, abs(right - left)), max(1, abs(bottom - top)),
                    )
                    pygame.draw.rect(surface, (0, 255, 255), rect, 2)

            hud = [
                f"zoom: {view.zoom:.2f}x   wheel=zoom  right-drag=pan  "
                "0=fit  Q/ESC=close"
            ]
            if hover is not None and hover_bounds is not None:
                row, col = hover
                left, top, right, bottom = hover_bounds
                hud.append(
                    f"square r{row} c{col}: x {left}-{right - 1}, "
                    f"y {top}-{bottom - 1} (label reviewed next)"
                )
            else:
                hud.append("hover a square to preview its exact cropped tile")
            view.render(screen, hud, overlay=overlay)

            panel_rect = pygame.Rect(VIEW_W, 0, panel_width, VIEW_H)
            pygame.draw.rect(screen, (28, 28, 32), panel_rect)
            preview_rect = pygame.Rect(
                VIEW_W + 12, 12, PREVIEW_SIZE, PREVIEW_SIZE
            )
            if hover is not None:
                screen.blit(tile_surface(*hover), preview_rect)
            else:
                pygame.draw.rect(screen, (55, 55, 55), preview_rect)
                _draw_text(
                    screen, "Hover a square",
                    (preview_rect.x, preview_rect.centery - 12),
                    size=22, center_x=preview_rect.centerx,
                )
            pygame.draw.rect(screen, (230, 230, 230), preview_rect, 2)
            text_y = preview_rect.bottom + 18
            if hover is not None and hover_bounds is not None:
                row, col = hover
                left, top, right, bottom = hover_bounds
                lines = [
                    f"Square r{row} c{col}",
                    f"x {left}-{right - 1}   y {top}-{bottom - 1}",
                    "This exact tile is marked manually next.",
                ]
            else:
                lines = ["Hover a square to inspect it."]
            for line in lines:
                _draw_text(screen, line, (VIEW_W + 14, text_y), size=19)
                text_y += 26

            pygame.display.flip()
            clock.tick(60)
    finally:
        _close_gui()


def process_image(image_path, game_id=None, duplicate_index=None,
                  dataset_dir=DATASET_DIR, **_legacy_kwargs):
    """Crop one screenshot, manually mark 64 squares, and save unique tiles.

    A new game ID is created for this upload unless one is supplied. The
    user manually marks every piece/blank and square type; no OpenCV or
    learned suggestion is used. Each marked tile is checked for exact/near
    pixel duplicates in ``datasets/<class>/`` and only unique tiles are
    saved, after the complete board has been reviewed.
    """
    image_path = Path(image_path)
    dataset_dir = ensure_dataset_layout(dataset_dir)
    image = load_screenshot(image_path)

    if duplicate_index is None:
        duplicate_index = DuplicateIndex.load(dataset_dir)
        print(
            f"Loaded {duplicate_index.example_count} existing dataset image(s) "
            "for duplicate checks."
        )
        if duplicate_index.unreadable:
            print(
                f"Warning: {len(duplicate_index.unreadable)} dataset image(s) "
                "could not be read and were ignored by the duplicate index."
            )

    game_id = game_id or create_game_id()
    board_id = game_id

    print(f"Loaded image: {image_path}")
    print(f"Game ID for this upload: {game_id}")
    print("Select the OUTER EDGE of the chessboard.")
    print("Drag to select; mouse wheel zooms (right-drag pans) for pixel accuracy.")
    print("Press ENTER or SPACE to confirm; ESC or C to cancel.")

    roi = select_board_roi(image)
    if roi is None:
        print("Board selection cancelled.")
        return None
    x, y, w, h = roi

    board = image[y:y + h, x:x + w].copy()
    size = min(w, h)
    board = _resize_rgb_array(board, (size, size), PIL_RESAMPLE_LANCZOS)
    square_size = size / 8
    tile_entries = []

    for row in range(8):
        for col in range(8):
            left = round(col * square_size)
            top = round(row * square_size)
            right = round((col + 1) * square_size)
            bottom = round((row + 1) * square_size)
            tile_entries.append((row, col, board[top:bottom, left:right].copy()))

    print(f"Board size: {size} x {size}")
    print(f"Square size: {square_size:.2f} pixels")
    print("Inspecting grid: hover a square to preview its tile; "
          "wheel zooms, right-drag pans, Q/ESC closes.")
    show_grid_debug(board, square_size)

    print("\nManual review: mark what piece/blank is on each square, then")
    print("confirm Light square, Dark square, or Highlighted square.")
    print("After each piece is marked, its pixels are checked against the")
    print("existing class folders. Duplicates are announced and skipped because")
    print("duplicate images make training slower and can make the model faulty.")

    review = manual_review_tiles(
        tile_entries, duplicate_checker=duplicate_index
    )
    if review is None:
        print("Review cancelled; no tiles or metadata were saved for this board.")
        return None
    answers, duplicate_reports = review

    board_flipped = _ask_yes_no(
        "\nIs this board flipped (viewed from Black's side)? [y/N]: "
    )
    fen = fen_from_answers(tile_entries, answers, flipped=board_flipped)
    print(f"Generated FEN ({'flipped' if board_flipped else 'normal'} board): {fen}")

    board_dir = Path(dataset_dir) / "_boards" / board_id
    board_path = _save_rgb_image(board_dir / "board.png", board)
    grid_path = _save_rgb_image(
        board_dir / "grid_debug.png",
        _make_grid_debug_image(board, square_size),
    )
    fen_path = save_fen_file(fen, game_id, board_dir)

    saved_records, skipped_records = save_labelled_tiles(
        tile_entries,
        answers,
        image_path,
        board_id,
        game_id=game_id,
        duplicate_reports=duplicate_reports,
        duplicate_index=duplicate_index,
        dataset_dir=dataset_dir,
    )
    all_records = scan_dataset_records(dataset_dir)

    print(f"Saved board crop: {board_path}")
    print(f"Saved grid debug: {grid_path}")
    print(f"Saved FEN: {fen_path}")
    print(
        f"Saved {len(saved_records)} unique square image(s) into class "
        f"folders under: {Path(dataset_dir).resolve()}"
    )
    if skipped_records:
        print(
            f"Skipped {len(skipped_records)} duplicate square image(s). "
            "Duplicates slow CNN training and can make the model faulty, so "
            "they were not added to the class folders."
        )
    else:
        print("No duplicate square images were found on this board.")

    print_dataset_report(all_records, dataset_dir)
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
    """Build ``datasets/<class>/`` one manually marked board at a time.

    Old ``--manual-only``/``--no-semi-auto`` flags are accepted but are now
    redundant: manual marking is the only workflow, OpenCV is not used, and
    every marked tile is duplicate-checked before it can be saved. Use
    ``--import-legacy`` for a one-time duplicate-cleaning migration of an
    old ``board_debug/dataset.csv`` dataset into the class folders.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    ignored_flags = {"--manual-only", "--no-semi-auto", "--semi-auto"}
    image_args = []
    import_legacy = False
    legacy_dir = LEGACY_DATASET_DIR
    legacy_csv = None
    cursor = 0
    while cursor < len(args):
        arg = args[cursor]
        cursor += 1
        if arg in ignored_flags:
            continue
        if arg == "--import-legacy":
            import_legacy = True
            continue
        if arg in ("--legacy-dir", "--legacy-csv"):
            if cursor >= len(args):
                print(f"Missing value after {arg}.")
                return False
            value = Path(args[cursor])
            cursor += 1
            if arg == "--legacy-dir":
                legacy_dir = value
            else:
                legacy_csv = value
            continue
        if arg.startswith("--legacy-dir="):
            legacy_dir = Path(arg.split("=", 1)[1])
            continue
        if arg.startswith("--legacy-csv="):
            legacy_csv = Path(arg.split("=", 1)[1])
            continue
        image_args.append(arg)

    ensure_dataset_layout(DATASET_DIR)
    duplicate_index = DuplicateIndex.load(DATASET_DIR)
    print(f"Dataset folder: {DATASET_DIR.resolve()}")
    print(
        f"Loaded {duplicate_index.example_count} existing image(s) from the "
        "class folders for exact/near duplicate checks."
    )
    if duplicate_index.unreadable:
        print(
            f"Warning: {len(duplicate_index.unreadable)} existing dataset "
            "image(s) could not be read and will be ignored by duplicate checks."
        )

    if import_legacy:
        try:
            import_legacy_dataset(
                legacy_dir,
                csv_path=legacy_csv,
                dataset_dir=DATASET_DIR,
                duplicate_index=duplicate_index,
            )
        except Exception as exc:
            print(f"Legacy dataset import failed: {exc}")
            return False
        if not image_args:
            return True
    elif duplicate_index.example_count == 0 and LEGACY_DATASET_CSV.is_file():
        print(
            "An old board_debug dataset was found. To copy its unique images "
            "into datasets/<class>/ (skipping duplicates), run: "
            "python board-vision.py --import-legacy"
        )

    image_paths = list(choose_image_paths(image_args))
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

        records = process_image(
            image_path, duplicate_index=duplicate_index, dataset_dir=DATASET_DIR
        )
        if records is None:
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
            if run_cnn_trainer(records, DATASET_DIR):
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

    _close_gui()
    return completed


if __name__ == "__main__":
    main()
