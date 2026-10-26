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

# Semi-auto review has its own learning library. It is deliberately kept
# outside board_debug/ so assistant reference images can never be mixed
# into the CNN model dataset or its train/validation/test CSVs.
SEMI_AUTO_DIR = BASE_DIR / "board_vision_assistant"
SEMI_AUTO_EXAMPLES_DIR = SEMI_AUTO_DIR / "examples"
SEMI_AUTO_CSV = SEMI_AUTO_DIR / "examples.csv"
SEMI_AUTO_FIELDS = (
    "image_path",
    "filename",
    "label",
    "label_code",
    "square_type",
    "source_image",
    "board_id",
)
SEMI_AUTO_MAX_PER_GROUP = 16
SEMI_AUTO_MAX_NEW_PER_BOARD_GROUP = 4
SEMI_AUTO_FEATURE_K = 3

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
LABEL_OPTION_BY_CODE = {option[0]: option for option in LABEL_OPTIONS}
LABEL_OPTION_BY_LABEL = {option[1]: option for option in LABEL_OPTIONS}
SQUARE_TYPE_OPTIONS = (
    ("light", "Light square"),
    ("dark", "Dark square"),
    ("highlighted", "Highlighted square"),
)
SQUARE_TYPE_DISPLAY = dict(SQUARE_TYPE_OPTIONS)
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


def _resolve_local_path(value):
    """Resolve a board-vision CSV path relative to this script's folder."""
    path = Path(str(value))
    return path if path.is_absolute() else BASE_DIR / path


def _semi_auto_feature(tile):
    """Build an OpenCV feature vector for one board square.

    The vector intentionally combines a centre-weighted colour grid,
    Canny edges, a foreground mask (pixels unlike the corner-estimated
    square colour), an HSV histogram, and the four corner colours. That
    gives the assistant information about both the piece shape/colour and
    whether the underlying square looks light, dark, or highlighted.
    """
    try:
        if tile is None or getattr(tile, "size", 0) == 0:
            return None
        image = np.asarray(tile)
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        elif image.ndim == 3 and image.shape[2] == 4:
            image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
        elif image.ndim != 3 or image.shape[2] != 3:
            return None
        if image.dtype != np.uint8:
            image = np.clip(image, 0, 255).astype(np.uint8)

        interpolation = (
            cv2.INTER_AREA if max(image.shape[:2]) > 64 else cv2.INTER_LINEAR
        )
        resized = cv2.resize(image, (64, 64), interpolation=interpolation)

        axis = np.linspace(-1.0, 1.0, 16, dtype=np.float32)
        xx, yy = np.meshgrid(axis, axis)
        centre_weight = 0.7 + 0.8 * np.exp(-(xx * xx + yy * yy) / 0.75)

        colour_grid = cv2.resize(
            resized, (16, 16), interpolation=cv2.INTER_AREA
        ).astype(np.float32) / 255.0
        colour_grid *= centre_weight[:, :, None]

        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        edge_grid = cv2.resize(
            edges, (16, 16), interpolation=cv2.INTER_AREA
        ).astype(np.float32) / 255.0
        edge_grid *= centre_weight

        patch = 9
        corner_pixels = np.concatenate(
            [
                resized[:patch, :patch].reshape(-1, 3),
                resized[:patch, -patch:].reshape(-1, 3),
                resized[-patch:, :patch].reshape(-1, 3),
                resized[-patch:, -patch:].reshape(-1, 3),
            ],
            axis=0,
        )
        background = np.median(corner_pixels, axis=0)
        colour_difference = np.linalg.norm(
            resized.astype(np.float32) - background, axis=2
        )
        foreground = (colour_difference > 42).astype(np.uint8) * 255
        foreground_grid = cv2.resize(
            foreground, (16, 16), interpolation=cv2.INTER_AREA
        ).astype(np.float32) / 255.0
        foreground_grid *= centre_weight

        hsv = cv2.cvtColor(resized, cv2.COLOR_BGR2HSV)
        histogram = cv2.calcHist(
            [hsv], [0, 1, 2], None, [12, 4, 4],
            [0, 180, 0, 256, 0, 256],
        )
        cv2.normalize(histogram, histogram, alpha=1.0, norm_type=cv2.NORM_L1)

        corner_patch = 12
        corner_colours = np.concatenate(
            [
                resized[:corner_patch, :corner_patch].reshape(-1, 3).mean(axis=0),
                resized[:corner_patch, -corner_patch:].reshape(-1, 3).mean(axis=0),
                resized[-corner_patch:, :corner_patch].reshape(-1, 3).mean(axis=0),
                resized[-corner_patch:, -corner_patch:].reshape(-1, 3).mean(axis=0),
            ]
        ).astype(np.float32) / 255.0

        return np.concatenate(
            [
                colour_grid.ravel(),
                edge_grid.ravel() * 0.8,
                foreground_grid.ravel() * 0.65,
                np.sqrt(histogram.astype(np.float32).ravel()) * 0.45,
                corner_colours * 0.8,
            ]
        ).astype(np.float32)
    except Exception:
        return None


def _semi_auto_distance(first, second):
    """Root-mean-square distance between two semi-auto feature vectors."""
    if first is None or second is None:
        return math.inf
    first = np.asarray(first, dtype=np.float32)
    second = np.asarray(second, dtype=np.float32)
    if first.shape != second.shape:
        return math.inf
    return float(np.sqrt(np.mean((first - second) ** 2)))


def _match_confidence(best, second=None):
    """Small display confidence from match quality and best/second margin."""
    if not math.isfinite(best):
        return 0.0
    similarity = math.exp(-max(float(best), 0.0) / 0.18)
    if second is None or not math.isfinite(second):
        return similarity * 0.75
    margin = (float(second) - float(best)) / max(float(second), 1e-9)
    margin = min(1.0, max(0.0, margin * 2.0))
    return math.sqrt(similarity * margin)


def _make_semi_auto_example(tile, label_option, square_type, feature=None):
    if feature is None:
        feature = _semi_auto_feature(tile)
    if feature is None:
        return None
    return {
        "label_option": label_option,
        "square_type": square_type,
        "feature": np.asarray(feature, dtype=np.float32),
        "path": None,
    }


def load_semi_auto_records(csv_path=SEMI_AUTO_CSV):
    """Load the separate semi-auto assistant examples.csv."""
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        return []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    records = []
    for row in rows:
        record = {
            field: str(row.get(field, "") or "") for field in SEMI_AUTO_FIELDS
        }
        if (
            record["image_path"]
            and record["label_code"] in LABEL_OPTION_BY_CODE
            and record["square_type"] in SQUARE_TYPE_DISPLAY
        ):
            records.append(record)
    return records


def write_semi_auto_records(records, csv_path=SEMI_AUTO_CSV):
    """Write the semi-auto learning index (kept outside board_debug/)."""
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = csv_path.with_suffix(csv_path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SEMI_AUTO_FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow(
                {field: record.get(field, "") for field in SEMI_AUTO_FIELDS}
            )
    temporary.replace(csv_path)
    return csv_path


def _scan_semi_auto_records(examples_dir=SEMI_AUTO_EXAMPLES_DIR):
    """Fallback records for manually seeded examples/<code>/<square>/ files."""
    examples_dir = Path(examples_dir)
    records = []
    if not examples_dir.is_dir():
        return records
    for path in sorted(examples_dir.rglob("*")):
        if (
            not path.is_file()
            or path.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS
        ):
            continue
        parts = path.relative_to(examples_dir).parts
        if len(parts) < 3:
            continue
        label_code, square_type = parts[-3], parts[-2]
        option = LABEL_OPTION_BY_CODE.get(label_code)
        if option is None or square_type not in SQUARE_TYPE_DISPLAY:
            continue
        records.append(
            {
                "image_path": _path_for_csv(path.resolve()),
                "filename": path.name,
                "label": option[1],
                "label_code": label_code,
                "square_type": square_type,
                "source_image": "",
                "board_id": "",
            }
        )
    return records


def _next_semi_auto_sequence(records, examples_dir=SEMI_AUTO_EXAMPLES_DIR):
    highest = 0
    candidates = [record.get("filename", "") for record in records]
    examples_dir = Path(examples_dir)
    if examples_dir.is_dir():
        candidates.extend(
            path.name for path in examples_dir.rglob("*") if path.is_file()
        )
    for name in candidates:
        match = re.search(r"_(\d+)$", Path(str(name)).stem)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


class SemiAutoAssistant:
    """OpenCV example matcher that learns only from user-confirmed choices.

    Persistent examples live under board_vision_assistant/, separate from
    board_debug/'s CNN dataset. During one board, manual_review_tiles can
    also pass the already-confirmed squares from that board as temporary
    examples, so suggestions improve immediately while the user is working.
    """

    def __init__(self, base_dir=SEMI_AUTO_DIR, examples=None, records=None):
        self.base_dir = Path(base_dir)
        self.examples_dir = self.base_dir / "examples"
        self.csv_path = self.base_dir / "examples.csv"
        self.examples = list(examples or [])
        self.records = list(records or [])

    @classmethod
    def load(cls, base_dir=SEMI_AUTO_DIR):
        assistant = cls(base_dir=base_dir)
        assistant.examples_dir.mkdir(parents=True, exist_ok=True)
        records = load_semi_auto_records(assistant.csv_path)
        if not records:
            records = _scan_semi_auto_records(assistant.examples_dir)
        assistant.records = records

        for record in records:
            option = LABEL_OPTION_BY_CODE.get(record.get("label_code", ""))
            square_type = record.get("square_type", "")
            if option is None or square_type not in SQUARE_TYPE_DISPLAY:
                continue
            image_path = _resolve_local_path(record.get("image_path", ""))
            if not image_path.is_file():
                continue
            image = cv2.imread(str(image_path))
            example = _make_semi_auto_example(image, option, square_type)
            if example is not None:
                example["path"] = image_path
                assistant.examples.append(example)
        return assistant

    @property
    def example_count(self):
        return len(self.examples)

    def _group_scores(self, feature, examples):
        usable = [
            example
            for example in examples
            if example is not None
            and example.get("feature") is not None
            and np.asarray(example["feature"]).shape == np.asarray(feature).shape
        ]
        if not usable:
            return {}

        vectors = np.stack(
            [np.asarray(example["feature"], dtype=np.float32) for example in usable]
        )
        distances = np.sqrt(
            np.mean(
                (vectors - np.asarray(feature, dtype=np.float32)) ** 2,
                axis=1,
            )
        )

        grouped = {}
        for example, distance in zip(usable, distances):
            option = example.get("label_option")
            square_type = example.get("square_type")
            if (
                not option
                or option[0] not in LABEL_OPTION_BY_CODE
                or square_type not in SQUARE_TYPE_DISPLAY
            ):
                continue
            grouped.setdefault((option[0], square_type), []).append(float(distance))

        return {
            key: float(np.mean(sorted(values)[:SEMI_AUTO_FEATURE_K]))
            for key, values in grouped.items()
            if values
        }

    def suggest_feature(self, feature, extra_examples=(), label_option=None):
        """Suggest a piece and/or square type for one feature vector."""
        if feature is None:
            return None
        examples = self.examples + [
            example for example in extra_examples if example is not None
        ]
        group_scores = self._group_scores(feature, examples)
        if not group_scores:
            return None

        sorted_groups = sorted(group_scores.items(), key=lambda item: item[1])
        piece_scores = {}
        for (label_code, _square_type), score in group_scores.items():
            piece_scores[label_code] = min(score, piece_scores.get(label_code, math.inf))
        sorted_pieces = sorted(piece_scores.items(), key=lambda item: item[1])

        if label_option is None:
            best_piece_code = sorted_groups[0][0][0]
            chosen_option = LABEL_OPTION_BY_CODE[best_piece_code]
            combined_best = sorted_groups[0][1]
            combined_second = (
                sorted_groups[1][1] if len(sorted_groups) > 1 else None
            )
            combined_confidence = _match_confidence(
                combined_best, combined_second
            )
            alternatives = [
                (
                    LABEL_OPTION_BY_CODE[label_code],
                    square_type,
                    score,
                )
                for (label_code, square_type), score in sorted_groups[:3]
            ]
        else:
            chosen_option = label_option
            combined_confidence = _match_confidence(
                sorted_groups[0][1],
                sorted_groups[1][1] if len(sorted_groups) > 1 else None,
            )
            alternatives = []

        piece_best = sorted_pieces[0][1]
        piece_second = sorted_pieces[1][1] if len(sorted_pieces) > 1 else None
        piece_confidence = _match_confidence(piece_best, piece_second)

        square_candidates = sorted(
            (
                (square_type, score)
                for (label_code, square_type), score in group_scores.items()
                if label_code == chosen_option[0]
            ),
            key=lambda item: item[1],
        )
        if not square_candidates:
            # No saved example for this manually chosen piece yet. Fall back
            # to the closest learned square colours across all pieces.
            global_square_scores = {}
            for (_label_code, square_type), score in group_scores.items():
                global_square_scores[square_type] = min(
                    score, global_square_scores.get(square_type, math.inf)
                )
            square_candidates = sorted(
                global_square_scores.items(), key=lambda item: item[1]
            )

        square_type, square_score = square_candidates[0]
        square_second = (
            square_candidates[1][1] if len(square_candidates) > 1 else None
        )
        square_confidence = _match_confidence(square_score, square_second)

        return {
            "label_option": chosen_option,
            "square_type": square_type,
            "confidence": (
                square_confidence if label_option is not None else combined_confidence
            ),
            "combined_confidence": combined_confidence,
            "piece_confidence": piece_confidence,
            "square_confidence": square_confidence,
            "distance": square_score if label_option is not None else sorted_groups[0][1],
            "example_count": len(examples),
            "group_count": len(group_scores),
            "alternatives": alternatives,
        }

    def suggest(self, tile, extra_examples=(), label_option=None):
        return self.suggest_feature(
            _semi_auto_feature(tile),
            extra_examples=extra_examples,
            label_option=label_option,
        )

    def learn_from_board(self, tile_entries, answers, source_image, board_id):
        """Save a bounded, diverse set of this board's confirmed examples.

        Only final answers from a completed board should be passed here.
        Existing assistant examples are preserved. At most
        SEMI_AUTO_MAX_PER_GROUP references per (piece, square type) are kept.
        """
        if len(tile_entries) != len(answers):
            raise ValueError("Every tile must have a confirmed semi-auto answer")

        candidates = {}
        for (_row, _col, tile), (label_option, square_type) in zip(
            tile_entries, answers
        ):
            feature = _semi_auto_feature(tile)
            if feature is None:
                continue
            key = (label_option[0], square_type)
            candidates.setdefault(key, []).append(
                {
                    "tile": tile,
                    "label_option": label_option,
                    "square_type": square_type,
                    "feature": feature,
                }
            )

        existing_counts = {}
        existing_features = {}
        for record in self.records:
            key = (record.get("label_code", ""), record.get("square_type", ""))
            existing_counts[key] = existing_counts.get(key, 0) + 1
        for example in self.examples:
            option = example.get("label_option")
            if not option:
                continue
            key = (option[0], example.get("square_type"))
            existing_features.setdefault(key, []).append(example["feature"])
            existing_counts[key] = max(
                existing_counts.get(key, 0), len(existing_features[key])
            )

        additions = []
        for key, group_candidates in candidates.items():
            room = SEMI_AUTO_MAX_PER_GROUP - existing_counts.get(key, 0)
            limit = min(room, SEMI_AUTO_MAX_NEW_PER_BOARD_GROUP)
            if limit <= 0:
                continue

            remaining = list(group_candidates)
            references = list(existing_features.get(key, []))
            while remaining and limit > 0:
                scored = []
                for candidate_index, candidate in enumerate(remaining):
                    nearest = min(
                        (
                            _semi_auto_distance(candidate["feature"], reference)
                            for reference in references
                        ),
                        default=math.inf,
                    )
                    scored.append((nearest, candidate_index, candidate))
                scored.sort(key=lambda item: item[0], reverse=True)
                nearest, chosen_index, chosen = scored[0]

                # One duplicate can help early on; after that, prefer saving
                # room for a future board/theme instead of near-identical tiles.
                if references and len(references) >= 2 and nearest < 0.01:
                    break

                additions.append(chosen)
                references.append(chosen["feature"])
                del remaining[chosen_index]
                limit -= 1

        if not additions:
            return {
                "saved": 0,
                "considered": len(tile_entries),
                "total_examples": self.example_count,
            }

        self.examples_dir.mkdir(parents=True, exist_ok=True)
        sequence = _next_semi_auto_sequence(self.records, self.examples_dir)
        saved = 0

        for candidate in additions:
            label_option = candidate["label_option"]
            square_type = candidate["square_type"]
            group_dir = self.examples_dir / label_option[0] / square_type
            group_dir.mkdir(parents=True, exist_ok=True)

            while True:
                filename = (
                    f"assistant_{label_option[0]}_{square_type}_"
                    f"{sequence:06d}.png"
                )
                image_path = group_dir / filename
                sequence += 1
                if not image_path.exists():
                    break

            if not cv2.imwrite(str(image_path), candidate["tile"]):
                raise OSError(f"Could not save semi-auto example: {image_path}")

            self.records.append(
                {
                    "image_path": _path_for_csv(image_path),
                    "filename": filename,
                    "label": label_option[1],
                    "label_code": label_option[0],
                    "square_type": square_type,
                    "source_image": _path_for_csv(source_image),
                    "board_id": board_id,
                }
            )
            example = _make_semi_auto_example(
                candidate["tile"],
                label_option,
                square_type,
                feature=candidate["feature"],
            )
            if example is not None:
                example["path"] = image_path
                self.examples.append(example)
            saved += 1

        write_semi_auto_records(self.records, self.csv_path)
        return {
            "saved": saved,
            "considered": len(tile_entries),
            "total_examples": self.example_count,
        }


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


def _manual_button(canvas, rect, text, selected=False, suggested=False):
    x, y, w, h = rect
    if selected:
        fill = (64, 145, 72)
        border = (150, 255, 170)
    elif suggested:
        # Cyan/blue marks an OpenCV suggestion; it is never auto-confirmed.
        fill = (92, 72, 25)
        border = (255, 240, 90)
    else:
        fill = (62, 62, 72)
        border = (165, 165, 175)
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


def manual_review_tiles(tile_entries, assistant=None, semi_auto_enabled=True):
    """Review every tile, with optional OpenCV semi-auto suggestions.

    The user always makes the final choice. For each square they first
    confirm the piece/blank class, then confirm Light square, Dark square,
    or Highlighted square. When semi-auto is enabled, the assistant's best
    learned match is highlighted in cyan and can be accepted with A/Enter;
    already-confirmed squares from this same board are used immediately as
    temporary learning examples. Returns ``(label_option, square_type)``
    pairs in tile order, or None if cancelled.
    """
    if not tile_entries:
        return []

    answers = [None] * len(tile_entries)
    index = 0
    stage = "label"
    selected_label = None
    semi_auto_enabled = bool(semi_auto_enabled and assistant is not None)
    tile_features = {}
    suggestion_cache = {}
    click = {"point": None}
    title = "Semi-auto piece review" if semi_auto_enabled else "Manual piece review"

    def feature_for(tile_index):
        if tile_index not in tile_features:
            tile_features[tile_index] = _semi_auto_feature(
                tile_entries[tile_index][2]
            )
        return tile_features[tile_index]

    def temporary_examples(up_to_index):
        examples = []
        for answer_index in range(up_to_index):
            answer = answers[answer_index]
            if answer is None:
                continue
            example = _make_semi_auto_example(
                tile_entries[answer_index][2],
                answer[0],
                answer[1],
                feature=feature_for(answer_index),
            )
            if example is not None:
                examples.append(example)
        return examples

    def suggestion_for(tile_index, label_option=None):
        if not semi_auto_enabled:
            return None
        cache_key = (
            tile_index,
            tuple(answers[:tile_index]),
            label_option[0] if label_option else "",
        )
        if cache_key not in suggestion_cache:
            try:
                suggestion_cache[cache_key] = assistant.suggest_feature(
                    feature_for(tile_index),
                    extra_examples=temporary_examples(tile_index),
                    label_option=label_option,
                )
            except Exception:
                suggestion_cache[cache_key] = None
        return suggestion_cache[cache_key]

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

            suggestion = suggestion_for(
                index, selected_label if stage == "square" else None
            )

            review_kind = "Semi-auto" if semi_auto_enabled else "Manual"
            _manual_text(
                canvas,
                f"{review_kind} review {index + 1}/{len(tile_entries)}  -  square r{row} c{col}",
                (24, 36),
                scale=0.72,
                thickness=2,
            )
            if stage == "label":
                if suggestion:
                    instruction = "Step 1: confirm piece. Cyan = OpenCV suggestion; A/Enter uses it"
                elif semi_auto_enabled:
                    instruction = "Step 1: choose piece. Semi-auto learns from each choice"
                else:
                    instruction = "Step 1: click what is on this square"
            else:
                if suggestion:
                    instruction = (
                        f"Step 2: {selected_label[2]} - confirm square. "
                        "Cyan = OpenCV suggestion; A/Enter uses it"
                    )
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

            if semi_auto_enabled:
                if suggestion:
                    confidence = float(suggestion.get("confidence", 0.0))
                    if stage == "label":
                        suggestion_text = (
                            f"OpenCV: {suggestion['label_option'][2]} + "
                            f"{SQUARE_TYPE_DISPLAY[suggestion['square_type']]} "
                            f"({confidence:.0%})"
                        )
                    else:
                        suggestion_text = (
                            "OpenCV: "
                            f"{SQUARE_TYPE_DISPLAY[suggestion['square_type']]} "
                            f"({confidence:.0%})"
                        )
                    suggestion_colour = (
                        (120, 255, 150)
                        if confidence >= 0.65
                        else (110, 230, 255)
                        if confidence >= 0.35
                        else (130, 180, 255)
                    )
                else:
                    suggestion_text = "OpenCV: no suggestion yet - still learning"
                    suggestion_colour = (210, 210, 210)
                _manual_text(
                    canvas,
                    suggestion_text,
                    (24, 104 + MANUAL_TILE_SIZE + 72),
                    scale=0.52,
                    colour=suggestion_colour,
                )
                _manual_text(
                    canvas,
                    f"Learned: {assistant.example_count} saved + {index} this board",
                    (24, 104 + MANUAL_TILE_SIZE + 104),
                    scale=0.48,
                )
                _manual_text(
                    canvas,
                    "S = suggestions on/off",
                    (24, 104 + MANUAL_TILE_SIZE + 134),
                    scale=0.48,
                    colour=(200, 220, 255),
                )
            elif assistant is not None:
                _manual_text(
                    canvas,
                    "Suggestions: OFF (S to turn on)",
                    (24, 104 + MANUAL_TILE_SIZE + 72),
                    scale=0.52,
                    colour=(210, 210, 210),
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
                    is_suggested = bool(
                        suggestion and suggestion.get("label_option") == option
                    )
                    _manual_button(
                        canvas,
                        rect,
                        f"{'> ' if is_suggested else ''}{option[2]} ({option[0]})",
                        selected=(selected_label == option),
                        suggested=is_suggested,
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
                    is_suggested = bool(
                        suggestion and suggestion.get("square_type") == square_type
                    )
                    _manual_button(
                        canvas,
                        rect,
                        f"{'> ' if is_suggested else ''}{display}",
                        suggested=is_suggested,
                    )
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
            if suggestion:
                accept_rect = (836, MANUAL_H - 78, 260, 48)
                accept_text = "Use suggestion (A)"
                _manual_button(
                    canvas, accept_rect, accept_text, suggested=True
                )
                buttons.append((accept_rect, "accept_suggestion", None))
            _manual_text(
                canvas,
                (
                    "You make the final choice; confirmed squares teach the assistant."
                    if semi_auto_enabled
                    else "Every square is labelled manually, including blank squares."
                ),
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
            elif assistant is not None and key in (ord("s"), ord("S")):
                action = ("toggle_semi_auto", None)
            elif suggestion and key in (10, 13, ord("a"), ord("A")):
                action = ("accept_suggestion", None)
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
            if kind == "toggle_semi_auto":
                semi_auto_enabled = not semi_auto_enabled
                suggestion_cache.clear()
                continue
            if kind == "accept_suggestion" and suggestion:
                if stage == "label":
                    selected_label = suggestion["label_option"]
                    stage = "square"
                elif (
                    stage == "square"
                    and selected_label is not None
                    and suggestion.get("square_type") in SQUARE_TYPE_DISPLAY
                ):
                    answers[index] = (selected_label, suggestion["square_type"])
                    index += 1
                    selected_label = None
                    stage = "label"
                continue
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
                # is exactly the tile that will be reviewed next.
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
                    "(label reviewed after this grid check)"
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


def process_image(image_path, semi_auto_enabled=True):
    """Crop one screenshot, review its 64 squares, and append CSV rows.

    Semi-auto suggestions are enabled by default: OpenCV proposes the piece
    and square type from previously confirmed examples, while the user makes
    every final choice. Returns the full CNN dataset record list after this
    board is appended, or None if board selection/review was cancelled. No
    CNN-dataset tile/CSV row or persistent semi-auto example from this board
    is saved when review is cancelled.
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

    print("\nReview: for every square, first confirm the piece/blank,")
    print("then confirm Light square, Dark square, or Highlighted square.")

    assistant = None
    if semi_auto_enabled:
        try:
            assistant = SemiAutoAssistant.load()
            print(
                "Semi-auto review: OpenCV suggestions are highlighted in cyan; "
                "press A/Enter to use one, or click any other answer to correct it."
            )
            print(
                f"Semi-auto learning folder (separate from the CNN dataset): "
                f"{SEMI_AUTO_DIR.resolve()} "
                f"({assistant.example_count} saved example(s) loaded)."
            )
            print("Choices confirmed earlier on this board also teach the assistant immediately.")
        except Exception as exc:
            print(f"Semi-auto assistant unavailable; continuing manually: {exc}")
            assistant = None
    else:
        print("Semi-auto review disabled; every answer is chosen manually.")

    answers = manual_review_tiles(
        tile_entries,
        assistant=assistant,
        semi_auto_enabled=semi_auto_enabled,
    )
    if answers is None:
        print(
            "Review cancelled; no CNN-dataset tiles/CSV rows or semi-auto "
            "examples were saved for this board."
        )
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
    print(f"Saved {len(new_records)} reviewed square images to: {OUTPUT_DIR.resolve()}")
    if csv_existed:
        print("Appended these rows to the existing dataset.csv and recalculated its splits.")
    else:
        print("Created dataset.csv. Its rows are 'unsplit' on this first creation.")

    if assistant is not None:
        try:
            learning = assistant.learn_from_board(
                tile_entries, answers, image_path, board_id
            )
            print(
                "Semi-auto assistant learned from this completed board: "
                f"saved {learning['saved']} new reference example(s) to "
                f"{SEMI_AUTO_DIR.resolve()} "
                f"({learning['total_examples']} total; separate from {OUTPUT_DIR.name}/)."
            )
        except Exception as exc:
            print(f"Warning: semi-auto learning examples were not saved: {exc}")

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
    """Create/append the CNN dataset one reviewed board at a time.

    Semi-auto OpenCV suggestions are on by default; pass ``--manual-only``
    (or ``--no-semi-auto``) to disable both suggestions and assistant
    learning. After every completed board, the user may create the CNN model
    through cnn_trainer.py or add another screenshot. When they choose
    another, the terminal waits for Enter before any next supplied image is
    used or the Tk file picker is opened. Existing dataset rows/images are
    kept; new boards are appended and then the split is recalculated.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    manual_only_flags = {"--manual-only", "--no-semi-auto"}
    semi_auto_enabled = not any(arg in manual_only_flags for arg in args)
    image_args = [
        arg
        for arg in args
        if arg not in manual_only_flags and arg != "--semi-auto"
    ]
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

        records = process_image(image_path, semi_auto_enabled=semi_auto_enabled)
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
