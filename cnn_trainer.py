
from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import math
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import traceback
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, UnidentifiedImageError

import torch
from torch import nn
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms

from sklearn.metrics import classification_report
from tqdm import tqdm


# ============================================================
# CHESSBOTV CNN TRAINER
# Preferred input is the physical datasets/training/<class>/ and
# datasets/validation/<class>/ layout created by board-vision.py.
# Validation is also the final evaluation set; there is no separate test
# split. Legacy unsplit folders still work through an internal 80/20
# fallback, and legacy board-vision CSVs remain available through --csv.
# A live Tk dashboard is the default interface; --no-ui keeps the
# original terminal/tqdm workflow, and --ui --start begins immediately.
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DATASET_DIR = BASE_DIR / "datasets"
LEGACY_DATASET_DIR = BASE_DIR / "board_debug"
DEFAULT_CSV = LEGACY_DATASET_DIR / "dataset.csv"
OUTPUT_MODEL = BASE_DIR / "chessbotv_cnn.pth"

IMAGE_SIZE = 64
BATCH_SIZE = 32
EPOCHS = 25
LEARNING_RATE = 0.001
TRAIN_SPLIT = 0.80
VALIDATION_SPLIT = 0.20
RANDOM_SEED = 42
NORMALIZE_MEAN = (0.5, 0.5, 0.5)
NORMALIZE_STD = (0.5, 0.5, 0.5)

# Duplicate removal happens before training/validation use. These match
# board-vision.py:
# exact hashes always collide, while a near duplicate must be almost
# pixel-identical at the model's 64x64 input size.
NEAR_DUPLICATE_MAX_MEAN_ABS_ERROR = 4.0
NEAR_DUPLICATE_MAX_CHANGED_FRACTION = 0.02
NEAR_DUPLICATE_MAX_AHASH_DISTANCE = 8
NEAR_DUPLICATE_PREFILTER_MAX_MAE = 6.0
_PIL_RESAMPLING = getattr(Image, "Resampling", Image)
PIL_RESAMPLE_LANCZOS = _PIL_RESAMPLING.LANCZOS
PIL_RESAMPLE_BILINEAR = _PIL_RESAMPLING.BILINEAR

# Windows: 0 workers is the simplest reliable starting point.
NUM_WORKERS = 0

# Filename prefix -> human-readable class.
# Example filenames: bb_123.png, wp_27.jpg, blank_900.png
PREFIX_TO_CLASS = {
    "wp": "white_pawn",
    "wn": "white_knight",
    "wb": "white_bishop",
    "wr": "white_rook",
    "wq": "white_queen",
    "wk": "white_king",

    "bp": "black_pawn",
    "bn": "black_knight",
    "bb": "black_bishop",
    "br": "black_rook",
    "bq": "black_queen",
    "bk": "black_king",

    "blank": "empty",
}

CLASS_TO_PREFIX = {
    class_name: prefix for prefix, class_name in PREFIX_TO_CLASS.items()
}

# Folder name -> class. board-vision.py creates canonical class names;
# accepting the short codes too makes manually assembled datasets forgiving.
CLASS_FOLDER_TO_CLASS = {}
for _prefix, _class_name in PREFIX_TO_CLASS.items():
    CLASS_FOLDER_TO_CLASS[_prefix.lower()] = _class_name
    CLASS_FOLDER_TO_CLASS[_class_name.lower()] = _class_name
CLASS_FOLDER_TO_CLASS["blank"] = "empty"

SPLIT_FOLDER_TO_SPLIT = {
    "train": "train",
    "training": "train",
    "validation": "validation",
    "valid": "validation",
    "val": "validation",
    # There is no separate test set any more. Read old test folders as
    # validation so those images still participate in final evaluation.
    "test": "validation",
    "testing": "validation",
}

SUPPORTED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"
}


class TrainingCancelled(RuntimeError):
    """Raised when a UI/console caller asks a training run to stop."""


class TrainerUIUnavailableError(RuntimeError):
    """Raised when the optional Tk dashboard cannot be opened."""


def _raise_if_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise TrainingCancelled("Training was cancelled.")


def _emit_training_event(event_callback, event_type, **payload):
    """Send one structured progress event to the console/UI observer."""
    if event_callback is not None:
        event_callback({"type": event_type, **payload})


def _format_percent(value):
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _format_duration(seconds):
    if seconds is None:
        return "—"
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        return "—"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _estimate_remaining_seconds(elapsed_seconds, overall_fraction):
    """Estimate seconds left from elapsed time and a 0..1 progress value."""
    try:
        elapsed = float(elapsed_seconds)
        fraction = float(overall_fraction)
    except (TypeError, ValueError):
        return None
    if elapsed <= 0 or fraction <= 0.005 or fraction >= 1.0:
        return 0.0 if fraction >= 1.0 else None
    return max(0.0, elapsed * (1.0 - fraction) / fraction)


def _accuracy_graph_xy(epoch_value, accuracy, plot_rect, total_epochs):
    """Convert an epoch/accuracy pair to graph canvas coordinates."""
    left, top, right, bottom = (float(value) for value in plot_rect)
    epoch_span = max(1.0, float(total_epochs or 1))
    epoch = min(max(float(epoch_value or 0.0), 0.0), epoch_span)
    value = min(max(float(accuracy or 0.0), 0.0), 1.0)
    x = left + (epoch / epoch_span) * max(0.0, right - left)
    y = bottom - value * max(0.0, bottom - top)
    return x, y


class _QueueLogWriter:
    """Line-buffered stdout/stderr adapter used by the trainer dashboard.

    The training worker is the only thread that writes to this object.
    Complete lines are passed to a thread-safe UI queue; the worker never
    touches a Tk widget directly.
    """

    encoding = "utf-8"

    def __init__(self, log_callback):
        self._log_callback = log_callback
        self._buffer = ""

    def write(self, text):
        if text is None:
            return 0
        value = str(text)
        self._buffer += value.replace("\r\n", "\n").replace("\r", "\n")
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._log_callback(line.rstrip())
        return len(value)

    def flush(self):
        if self._buffer.strip():
            self._log_callback(self._buffer.rstrip())
        self._buffer = ""

    def isatty(self):
        return False

    def fileno(self):
        raise OSError("The trainer UI log does not have a file descriptor")


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


class ChessImageDataset(Dataset):
    def __init__(self, records, class_to_idx, training=False):
        self.records = records
        self.class_to_idx = class_to_idx
        self.training = training

        self.transform = transforms.Compose([
            transforms.Resize((IMAGE_SIZE, IMAGE_SIZE)),
            transforms.RandomRotation(8),
            transforms.ColorJitter(
                brightness=0.08,
                contrast=0.08,
                saturation=0.04,
            ),
            transforms.ToTensor(),
            transforms.Normalize(
                mean=NORMALIZE_MEAN,
                std=NORMALIZE_STD,
            ),
        ])
        self.eval_transform = transforms.Compose([
            transforms.Resize((IMAGE_SIZE, IMAGE_SIZE)),
            transforms.ToTensor(),
            transforms.Normalize(
                mean=NORMALIZE_MEAN,
                std=NORMALIZE_STD,
            ),
        ])

    def __len__(self):
        return len(self.records)

    def __getitem__(self, index):
        record = self.records[index]
        if isinstance(record, dict):
            image_path = record["path"]
            class_name = record["label"]
        else:  # backwards-compatible tuple records
            image_path, class_name = record

        transform = self.transform if self.training else self.eval_transform

        with Image.open(image_path) as image:
            image = image.convert("RGB")
            image_tensor = transform(image)

        label = self.class_to_idx[class_name]
        return image_tensor, label


class ChessCNN(nn.Module):
    """Small CNN for classifying individual chessboard squares."""

    def __init__(self, num_classes: int):
        super().__init__()

        self.features = nn.Sequential(
            # 64 x 64
            nn.Conv2d(3, 32, kernel_size=3, padding=1),
            nn.BatchNorm2d(32),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),

            # 32 x 32
            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),

            # 16 x 16
            nn.Conv2d(64, 128, kernel_size=3, padding=1),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),

            # 8 x 8
            nn.Conv2d(128, 128, kernel_size=3, padding=1),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),

            nn.AdaptiveAvgPool2d((1, 1)),
        )

        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.Dropout(0.25),
            nn.Linear(128, 64),
            nn.ReLU(inplace=True),
            nn.Dropout(0.15),
            nn.Linear(64, num_classes),
        )

    def forward(self, x):
        x = self.features(x)
        return self.classifier(x)


def _verify_image(path):
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except (OSError, UnidentifiedImageError, ValueError):
        return False


def _print_record_summary(records):
    counts = {}
    for record in records:
        label = record["label"] if isinstance(record, dict) else record[1]
        counts[label] = counts.get(label, 0) + 1

    print("\nClass distribution:")
    for class_name in sorted(counts):
        print(f"  {class_name:15s}: {counts[class_name]}")

    missing_classes = sorted(set(PREFIX_TO_CLASS.values()) - set(counts))
    if missing_classes:
        print("\nWARNING: No images found for:", ", ".join(missing_classes))
    return counts


def _normalise_split(value):
    """Return ``train``, ``validation``, or ``unsplit`` for a split value.

    Old ``test`` values deliberately become validation: this trainer no
    longer maintains a third held-out set, and validation is its final
    evaluation set.
    """
    split = str(value or "unsplit").strip().lower()
    return SPLIT_FOLDER_TO_SPLIT.get(split, "unsplit")


def _resolve_csv_image_path(value, csv_path, dataset_dir):
    raw_path = Path(str(value))
    if raw_path.is_absolute():
        return raw_path
    candidates = [
        csv_path.parent / raw_path,
        dataset_dir / raw_path.name,
        BASE_DIR / raw_path,
        Path.cwd() / raw_path,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def load_csv_records(
    csv_path,
    dataset_dir=DATASET_DIR,
    *,
    cancel_event=None,
    show_progress=True,
):
    """Load board-vision's dataset.csv into trainer records.

    Expected columns include image_path/filename, label/label_code, and
    split. Old ``test`` split values are read as validation. A legacy CSV
    may contain only ``unsplit`` rows; split_records() handles those with
    an internal 80/20 fallback without changing the CSV on disk.
    """
    csv_path = Path(csv_path)
    dataset_dir = Path(dataset_dir)
    if not csv_path.is_file():
        raise FileNotFoundError(f"Dataset CSV does not exist: {csv_path}")

    records = []
    skipped_bad = []
    skipped_unknown = []

    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    print(f"Reading {len(rows)} rows from {csv_path}...")
    for row in tqdm(
        rows, desc="Reading dataset CSV", disable=not show_progress
    ):
        _raise_if_cancelled(cancel_event)
        image_value = row.get("image_path") or row.get("filename") or ""
        if not image_value:
            skipped_bad.append("<missing image_path>")
            continue

        label = (row.get("label") or row.get("class") or "").strip()
        label_code = (row.get("label_code") or "").strip().lower()
        if not label and label_code in PREFIX_TO_CLASS:
            label = PREFIX_TO_CLASS[label_code]
        if label == "blank":
            label = "empty"
        if label not in set(PREFIX_TO_CLASS.values()):
            skipped_unknown.append(f"{image_value}: {label or label_code}")
            continue

        image_path = _resolve_csv_image_path(image_value, csv_path, dataset_dir)
        if not image_path.is_file() or not _verify_image(image_path):
            skipped_bad.append(str(image_path))
            continue

        split = _normalise_split(row.get("split"))

        game_id = (
            row.get("game_id")
            or row.get("board_id")
            or row.get("source_image")
            or ""
        ).strip() or None

        records.append({
            "path": image_path,
            "label": label,
            "label_code": label_code,
            "split": split,
            "game_id": game_id,
            "board_id": (row.get("board_id") or "").strip() or None,
            "source_image": (row.get("source_image") or "").strip() or None,
        })

    print(f"\nUsable CSV images: {len(records)}")
    print(f"Unknown CSV labels: {len(skipped_unknown)}")
    print(f"Missing/unreadable CSV images: {len(skipped_bad)}")
    if skipped_unknown:
        print("Unknown label examples:", skipped_unknown[:10])
    if skipped_bad:
        print("Missing/unreadable examples:", skipped_bad[:10])
    if not records:
        raise RuntimeError(f"No usable images found in {csv_path}")

    _print_record_summary(records)

    split_counts = defaultdict(int)
    for record in records:
        split_counts[record.get("split") or "unsplit"] += 1
    print("\nCSV splits:", dict(sorted(split_counts.items())))
    game_ids = {record.get("game_id") for record in records if record.get("game_id")}
    print(f"Game IDs: {len(game_ids)}")
    return records


def _record_path_and_label(record):
    if isinstance(record, dict):
        return Path(record["path"]), str(record.get("label", ""))
    return Path(record[0]), str(record[1])


def _canonical_image_array(path):
    """Load one tile at the CNN's canonical RGB input size."""
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        if rgb.size != (IMAGE_SIZE, IMAGE_SIZE):
            rgb = rgb.resize(
                (IMAGE_SIZE, IMAGE_SIZE), resample=PIL_RESAMPLE_LANCZOS
            )
        return np.asarray(rgb).copy()


def _exact_image_hash(canonical):
    digest = hashlib.sha256()
    digest.update(repr(tuple(canonical.shape)).encode("ascii"))
    digest.update(b"\0")
    digest.update(np.ascontiguousarray(canonical, dtype=np.uint8).tobytes())
    return digest.hexdigest()


def _average_image_hash(canonical):
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


def _image_fingerprint(canonical):
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
    for band in range(16):
        yield band, (int(average_hash) >> (band * 4)) & 0xF


def _duplicate_metrics(first, second):
    difference = np.abs(first.astype(np.int16) - second.astype(np.int16))
    return (
        float(np.mean(difference)),
        float(np.mean(np.max(difference, axis=2) > 16)),
    )


def deduplicate_records(
    records,
    mode="exact-near",
    *,
    cancel_event=None,
    show_progress=True,
):
    """Remove exact/near duplicate pixels before training/validation use.

    Returns ``(unique_records, duplicate_reports)``. Matching is deliberately
    across every label: identical pixels under two classes are conflicting
    faulty training data, and the first path in deterministic sorted order
    is retained while later copies are reported and excluded.

    Removing duplicates before training/validation use is essential;
    otherwise the same tile could appear in both sets, making validation
    look artificially good while also wasting training time.
    """
    if mode not in ("exact-near", "exact", "off"):
        raise ValueError(f"Unsupported dedupe mode: {mode}")
    if mode == "off" or not records:
        return list(records), []

    ordered = sorted(
        records,
        key=lambda record: (
            str(_record_path_and_label(record)[0]).lower(),
            _record_path_and_label(record)[1],
        ),
    )
    kept_entries = []
    exact_entries = {}
    bands = defaultdict(list)
    reports = []

    for record in tqdm(
        ordered, desc="Checking duplicate images", disable=not show_progress
    ):
        _raise_if_cancelled(cancel_event)
        path, label = _record_path_and_label(record)
        canonical = _canonical_image_array(path)
        exact_hash = _exact_image_hash(canonical)
        exact_entry = exact_entries.get(exact_hash)
        if exact_entry is not None:
            reports.append({
                "record": record,
                "duplicate_of": exact_entry["record"],
                "kind": "exact",
                "mean_abs_error": 0.0,
                "changed_fraction": 0.0,
                "average_hash_distance": 0,
            })
            continue

        average_hash = _average_image_hash(canonical)
        fingerprint = _image_fingerprint(canonical)
        best = None

        if mode == "exact-near":
            candidate_indices = set()
            for band_key in _ahash_bands(average_hash):
                candidate_indices.update(bands.get(band_key, ()))
            for candidate_index in sorted(candidate_indices):
                entry = kept_entries[candidate_index]
                ahash_distance = int(
                    (average_hash ^ entry["average_hash"]).bit_count()
                )
                if ahash_distance > NEAR_DUPLICATE_MAX_AHASH_DISTANCE:
                    continue
                fingerprint_mae = float(
                    np.mean(np.abs(fingerprint - entry["fingerprint"]))
                )
                if fingerprint_mae > NEAR_DUPLICATE_PREFILTER_MAX_MAE:
                    continue
                other_path, _other_label = _record_path_and_label(entry["record"])
                mean_abs_error, changed_fraction = _duplicate_metrics(
                    canonical, _canonical_image_array(other_path)
                )
                if not (
                    mean_abs_error <= NEAR_DUPLICATE_MAX_MEAN_ABS_ERROR
                    and changed_fraction <= NEAR_DUPLICATE_MAX_CHANGED_FRACTION
                ):
                    continue
                report = {
                    "record": record,
                    "duplicate_of": entry["record"],
                    "kind": "near",
                    "mean_abs_error": mean_abs_error,
                    "changed_fraction": changed_fraction,
                    "average_hash_distance": ahash_distance,
                }
                if best is None or mean_abs_error < best["mean_abs_error"]:
                    best = report

        if best is not None:
            reports.append(best)
            continue

        entry = {
            "record": record,
            "path": path,
            "label": label,
            "exact_hash": exact_hash,
            "average_hash": average_hash,
            "fingerprint": fingerprint,
        }
        kept_index = len(kept_entries)
        kept_entries.append(entry)
        exact_entries[exact_hash] = entry
        if mode == "exact-near":
            for band_key in _ahash_bands(average_hash):
                bands[band_key].append(kept_index)

    return [entry["record"] for entry in kept_entries], reports


def _split_target_counts(total):
    """Exact integer 80/20 target counts for a temporary split."""
    quotas = [
        ("train", total * TRAIN_SPLIT),
        ("validation", total * VALIDATION_SPLIT),
    ]
    counts = {name: int(quota) for name, quota in quotas}
    remaining = total - sum(counts.values())
    for name, quota in sorted(
        quotas, key=lambda item: item[1] - int(item[1]), reverse=True
    ):
        if remaining <= 0:
            break
        counts[name] += 1
        remaining -= 1
    return counts


def _internal_stratified_split(records):
    """Temporary label-stratified 80/20 split for unsplit/legacy records.

    Allocations start from each label group's proportional floor, then
    remainders are assigned so the global training/validation totals hit
    the requested percentages as exactly as integer counts allow.
    """
    groups = defaultdict(list)
    for record in records:
        key = record["label"] if isinstance(record, dict) else record[1]
        groups[key].append(record)

    split_names = ("train", "validation")
    fractions = {
        "train": TRAIN_SPLIT,
        "validation": VALIDATION_SPLIT,
    }
    targets = _split_target_counts(len(records))
    allocations = {}
    remainders = {}
    fraction_parts = {}
    current = {name: 0 for name in split_names}

    for key, group in groups.items():
        allocations[key] = {}
        fraction_parts[key] = {}
        used = 0
        for name in split_names:
            quota = len(group) * fractions[name]
            count = int(quota)
            allocations[key][name] = count
            fraction_parts[key][name] = quota - count
            current[name] += count
            used += count
        remainders[key] = len(group) - used

    for name in split_names:
        needed = targets[name] - current[name]
        while needed > 0:
            candidates = sorted(
                (key for key in groups if remainders[key] > 0),
                key=lambda key: (-fraction_parts[key][name], -remainders[key], key),
            )
            if not candidates:
                break
            progressed = False
            for key in candidates:
                if needed <= 0:
                    break
                allocations[key][name] += 1
                remainders[key] -= 1
                needed -= 1
                progressed = True
            if not progressed:
                break

    for key, count in remainders.items():
        if count:
            allocations[key]["train"] += count

    split_records_by_name = {name: [] for name in split_names}
    rng = random.Random(RANDOM_SEED)
    for key in sorted(groups):
        group = list(groups[key])
        random.Random(f"{RANDOM_SEED}:{key}").shuffle(group)
        cursor = 0
        for name in split_names:
            count = allocations[key][name]
            split_records_by_name[name].extend(group[cursor:cursor + count])
            cursor += count

    for split in split_records_by_name.values():
        rng.shuffle(split)
    return (
        split_records_by_name["train"],
        split_records_by_name["validation"],
    )


def split_records(records):
    """Return physical training/validation records, with an 80/20 fallback.

    Records from ``datasets/training/<class>/`` and
    ``datasets/validation/<class>/`` keep their folder assignment. Legacy
    records marked ``test`` are normalised to validation because validation
    is now the final evaluation set and there is no separate test set.

    Direct legacy class-folder records and other ``unsplit`` records are
    divided only in memory with a deterministic label-stratified 80/20
    split; no files are moved. If duplicate removal leaves a physically
    assigned dataset without validation images, all remaining records are
    temporarily re-split the same way. At least two unique images are
    required to form non-empty training and validation sets.
    """
    assigned = {"train": [], "validation": []}
    unsplit = []
    for record in records:
        split = (
            _normalise_split(record.get("split"))
            if isinstance(record, dict)
            else "unsplit"
        )
        if split in assigned:
            assigned[split].append(record)
        else:
            unsplit.append(record)

    if unsplit:
        extra_train, extra_val = _internal_stratified_split(unsplit)
        assigned["train"].extend(extra_train)
        assigned["validation"].extend(extra_val)
        print(
            "Some/all images were 'unsplit'; deriving training/validation "
            "with a temporary label-stratified 80/20 split, without moving "
            "or changing files on disk."
        )

    if not assigned["train"]:
        raise RuntimeError("No training images after applying dataset splits")
    if not assigned["validation"]:
        if len(records) < 2:
            raise RuntimeError(
                "At least two unique images are required so both training "
                "and validation are non-empty"
            )
        print(
            "The assigned folders had no validation images after duplicate "
            "removal; re-splitting temporarily with a label-stratified "
            "80/20 split."
        )
        train_records, validation_records = _internal_stratified_split(records)
        if not train_records or not validation_records:
            raise RuntimeError(
                "Could not form non-empty training and validation sets"
            )
        return train_records, validation_records
    return assigned["train"], assigned["validation"]


def _parse_folder_filename(path):
    """Best-effort metadata encoded in a board-vision/manual tile filename."""
    stem = Path(path).stem
    parsed = {
        "label_code": "",
        "game_id": None,
        "row": None,
        "col": None,
    }

    sections = stem.split("__")
    first_parts = sections[0].split("_") if sections else []
    if first_parts and first_parts[0].lower() in PREFIX_TO_CLASS:
        parsed["label_code"] = first_parts[0].lower()

    for section in sections[1:]:
        lower = section.lower()
        if lower.startswith("game_") or lower.startswith("game-"):
            parsed["game_id"] = section
        else:
            match = re.fullmatch(r"r(\d+)c(\d+)", lower)
            if match:
                parsed["row"] = int(match.group(1))
                parsed["col"] = int(match.group(2))

    if parsed["game_id"] is None:
        match = re.search(r"(game[_-][A-Za-z0-9_.-]+?)(?:__|$)", stem)
        if match:
            parsed["game_id"] = match.group(1)
    if parsed["row"] is None:
        match = re.search(r"\br(\d+)c(\d+)\b", stem.lower())
        if match:
            parsed["row"] = int(match.group(1))
            parsed["col"] = int(match.group(2))
    return parsed


def _make_folder_record(path, label, split="unsplit"):
    parsed = _parse_folder_filename(path)
    label_code = parsed["label_code"] or CLASS_TO_PREFIX[label]
    if PREFIX_TO_CLASS.get(label_code) != label:
        label_code = CLASS_TO_PREFIX[label]
    return {
        "path": Path(path),
        "label": label,
        "label_code": label_code,
        "split": _normalise_split(split),
        "game_id": parsed["game_id"],
        "board_id": parsed["game_id"],
        "source_image": None,
        "row": parsed["row"],
        "col": parsed["col"],
    }


def _class_folder_candidates(dataset_dir):
    """(path,label,split) candidates from class and optional split folders."""
    candidates = []
    unknown_folders = []

    def add_class_folder(folder, split):
        label = CLASS_FOLDER_TO_CLASS.get(folder.name.strip().lower())
        if label is None:
            if not folder.name.startswith(("_", ".")):
                unknown_folders.append(str(folder))
            return
        for path in sorted(folder.rglob("*")):
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                candidates.append((path, label, split))

    for child in sorted(
        (item for item in dataset_dir.iterdir() if item.is_dir()),
        key=lambda item: item.name.lower(),
    ):
        if child.name.startswith(("_", ".")):
            continue
        split = SPLIT_FOLDER_TO_SPLIT.get(child.name.strip().lower())
        if split is not None:
            for class_folder in sorted(
                (item for item in child.iterdir() if item.is_dir()),
                key=lambda item: item.name.lower(),
            ):
                add_class_folder(class_folder, split)
        else:
            add_class_folder(child, "unsplit")
    return candidates, unknown_folders


def discover_images(
    dataset_dir=DATASET_DIR,
    *,
    cancel_event=None,
    show_progress=True,
):
    """Discover the folder dataset used for training and validation.

    Preferred layout::

        datasets/
          training/
            white_king/*.png
            white_queen/*.png
            ...
            empty/*.png
          validation/
            white_king/*.png
            white_queen/*.png
            ...
            empty/*.png

    The physical parent folder assigns each record to training or
    validation. A legacy ``train/<class>`` folder is read as training, and
    a legacy ``test/<class>`` folder is read as validation because there is
    no separate test set. Direct legacy ``datasets/<class>/`` folders are
    read as ``unsplit`` for the internal fallback in ``split_records``. If
    no class-folder images exist, the old flat filename-prefix layout is
    used as a fallback.
    """
    dataset_dir = Path(dataset_dir)
    if not dataset_dir.exists():
        raise FileNotFoundError(
            f"Dataset folder does not exist: {dataset_dir}\n"
            "Run board-vision.py to create datasets/training/<class>/ and "
            "datasets/validation/<class>/ folders, or pass "
            "--dataset-dir/--csv for a legacy dataset."
        )

    candidates, unknown_folders = _class_folder_candidates(dataset_dir)
    if candidates:
        records = []
        skipped_bad = []
        print(
            f"Scanning {len(candidates)} image files from class folders "
            f"under {dataset_dir}..."
        )
        for path, label, split in tqdm(
            candidates, desc="Reading class folders", disable=not show_progress
        ):
            _raise_if_cancelled(cancel_event)
            if not _verify_image(path):
                skipped_bad.append(str(path))
                continue
            records.append(_make_folder_record(path, label, split))

        print(f"\nUsable class-folder images: {len(records)}")
        print(f"Unreadable/corrupt images: {len(skipped_bad)}")
        if skipped_bad:
            print("Unreadable examples:", skipped_bad[:10])
        if unknown_folders:
            print("Ignored unknown class folders:", unknown_folders[:10])
        if not records:
            raise RuntimeError(
                f"No usable images found in class folders under {dataset_dir}"
            )

        counts = _print_record_summary(records)
        small_classes = {
            name: count for name, count in counts.items() if count < 2
        }
        if small_classes:
            print(
                "WARNING: Very small classes may not appear in both training "
                f"and validation: {small_classes}"
            )
        split_counts = defaultdict(int)
        game_ids = set()
        for record in records:
            split_counts[record.get("split") or "unsplit"] += 1
            if record.get("game_id"):
                game_ids.add(record["game_id"])
        print("\nFolder splits:", dict(sorted(split_counts.items())))
        print(f"Game IDs parsed from filenames: {len(game_ids)}")
        return records

    # Legacy fallback: a flat folder such as old board_debug/*.png, where
    # the first filename token is wp/wn/.../blank.
    records = []
    skipped_unknown = []
    skipped_bad = []
    files = sorted(
        path for path in dataset_dir.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )

    print(f"Scanning {len(files)} flat image files...")
    for path in tqdm(
        files, desc="Reading legacy flat dataset", disable=not show_progress
    ):
        _raise_if_cancelled(cancel_event)
        prefix = path.stem.split("_", 1)[0].lower()
        if prefix not in PREFIX_TO_CLASS:
            skipped_unknown.append(path.name)
            continue
        if not _verify_image(path):
            skipped_bad.append(path.name)
            continue
        record = _make_folder_record(
            path, PREFIX_TO_CLASS[prefix], split="unsplit"
        )
        record["label_code"] = prefix
        records.append(record)

    print(f"\nUsable images: {len(records)}")
    print(f"Unknown filename prefixes: {len(skipped_unknown)}")
    print(f"Unreadable/corrupt images: {len(skipped_bad)}")
    if skipped_unknown:
        print("Unknown examples:", skipped_unknown[:10])
    if skipped_bad:
        print("Unreadable examples:", skipped_bad[:10])
    if unknown_folders:
        print("Ignored unknown class folders:", unknown_folders[:10])
    if not records:
        raise RuntimeError(
            "No usable images found. Expected "
            "datasets/training/<class>/*.png and "
            "datasets/validation/<class>/*.png (for example "
            "datasets/training/white_pawn/...), legacy unsplit class "
            "folders, or legacy prefix-named files in the dataset root."
        )

    counts = _print_record_summary(records)
    small_classes = {name: count for name, count in counts.items() if count < 2}
    if small_classes:
        print(
            "WARNING: Very small classes may not appear in both training "
            f"and validation: {small_classes}"
        )
    return records


def evaluate(
    model,
    loader,
    device,
    criterion,
    *,
    cancel_event=None,
    progress_callback=None,
):
    model.eval()

    total_loss = 0.0
    total_correct = 0
    total_samples = 0
    all_predictions = []
    all_labels = []

    try:
        total_batches = len(loader)
    except (TypeError, AttributeError):
        total_batches = 0

    with torch.inference_mode():
        for batch_index, (images, labels) in enumerate(loader, start=1):
            _raise_if_cancelled(cancel_event)
            images = images.to(device)
            labels = labels.to(device)

            logits = model(images)
            loss = criterion(logits, labels)

            predictions = logits.argmax(dim=1)

            total_loss += loss.item() * labels.size(0)
            total_correct += (predictions == labels).sum().item()
            total_samples += labels.size(0)

            all_predictions.extend(predictions.cpu().tolist())
            all_labels.extend(labels.cpu().tolist())

            if progress_callback is not None:
                progress_callback({
                    "batch": batch_index,
                    "total_batches": total_batches,
                    "samples_seen": total_samples,
                    "loss": total_loss / max(total_samples, 1),
                    "accuracy": total_correct / max(total_samples, 1),
                })

    average_loss = total_loss / max(total_samples, 1)
    accuracy = total_correct / max(total_samples, 1)

    return average_loss, accuracy, all_labels, all_predictions


def _dataset_has_discoverable_images(dataset_dir):
    """Whether folder/flat discovery has anything to read in a dataset root."""
    dataset_dir = Path(dataset_dir)
    if not dataset_dir.is_dir():
        return False
    candidates, _unknown = _class_folder_candidates(dataset_dir)
    if candidates:
        return True
    return any(
        path.is_file()
        and path.suffix.lower() in SUPPORTED_EXTENSIONS
        and path.stem.split("_", 1)[0].lower() in PREFIX_TO_CLASS
        for path in dataset_dir.iterdir()
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Train the ChessBotV square CNN")
    parser.add_argument(
        "--dataset-dir", type=Path, default=DATASET_DIR,
        help=(
            "Dataset root containing training/<class>/ and "
            "validation/<class>/ folders (default: datasets/)"
        ),
    )
    parser.add_argument(
        "--csv", type=Path, default=None,
        help=(
            "Optional legacy board-vision CSV; training/validation folders "
            "are used when omitted"
        ),
    )
    parser.add_argument(
        "--dedupe",
        choices=("exact-near", "exact", "off"),
        default="exact-near",
        help=(
            "Remove exact/near duplicate pixels before training/validation "
            "use (default: exact-near)"
        ),
    )
    parser.add_argument(
        "--output", type=Path, default=OUTPUT_MODEL,
        help="Output .pth model path",
    )
    parser.add_argument("--epochs", type=int, default=EPOCHS)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument(
        "--learning-rate", type=float, default=LEARNING_RATE
    )
    parser.add_argument("--seed", type=int, default=RANDOM_SEED)
    parser.add_argument(
        "--ui",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Open the live training dashboard (default). Use --no-ui for "
            "the original terminal/tqdm trainer."
        ),
    )
    parser.add_argument(
        "--start",
        action="store_true",
        help="Start training automatically after the dashboard opens.",
    )
    return parser.parse_args(argv)


def train_model(args, *, event_callback=None, cancel_event=None):
    """Train the CNN and return a summary dictionary.

    This is the shared engine for the terminal trainer and the live Tk
    dashboard. ``event_callback`` receives structured dictionaries for
    dataset/epoch/batch/validation progress; with no callback the original
    terminal output and ``tqdm`` bars are used. ``cancel_event`` is a
    ``threading.Event``-compatible object checked between images, batches,
    validation batches, and epochs.
    """
    run_started_at = time.perf_counter()
    dataset_dir = Path(getattr(args, "dataset_dir", DATASET_DIR))
    output_model = Path(getattr(args, "output", OUTPUT_MODEL))
    epochs = max(1, int(getattr(args, "epochs", EPOCHS)))
    configured_batch_size = max(
        1, int(getattr(args, "batch_size", BATCH_SIZE))
    )
    learning_rate = float(
        getattr(args, "learning_rate", LEARNING_RATE)
    )
    if not math.isfinite(learning_rate) or learning_rate <= 0:
        raise ValueError("Learning rate must be a positive number")
    seed = int(getattr(args, "seed", RANDOM_SEED))
    dedupe_mode = str(getattr(args, "dedupe", "exact-near"))
    csv_arg = getattr(args, "csv", None)
    show_progress = event_callback is None

    _raise_if_cancelled(cancel_event)
    set_seed(seed)

    # Avoid excessive CPU thread contention on smaller systems.
    torch.set_num_threads(min(8, torch.get_num_threads()))

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"\nTraining device: {device}")
    if device.type == "cpu":
        print("CPU training enabled. This is expected on your current setup.")

    _emit_training_event(
        event_callback,
        "started",
        dataset_dir=str(dataset_dir),
        output_model=str(output_model),
        epochs=epochs,
        batch_size=configured_batch_size,
        learning_rate=learning_rate,
        seed=seed,
        dedupe=dedupe_mode,
        device=str(device),
    )
    _emit_training_event(
        event_callback,
        "phase",
        phase="loading_dataset",
        message="Loading dataset…",
    )

    records_before_dedupe = 0
    duplicate_reports = []
    dataset_layout = "training_validation_folders"
    if csv_arg is not None:
        dataset_layout = "csv"
        csv_path = Path(csv_arg)
        if not csv_path.is_file():
            raise FileNotFoundError(f"Dataset CSV does not exist: {csv_path}")
        print(f"Using explicitly supplied legacy CSV: {csv_path}")
        records = load_csv_records(
            csv_path,
            dataset_dir,
            cancel_event=cancel_event,
            show_progress=show_progress,
        )
    elif (
        dataset_dir.resolve() == DATASET_DIR.resolve()
        and DEFAULT_CSV.is_file()
        and not _dataset_has_discoverable_images(dataset_dir)
    ):
        # Migration-friendly fallback: a checkout may contain new empty
        # training/validation placeholders while the user's older local
        # data is still in board_debug/. New folder data always wins.
        dataset_layout = "legacy_csv_fallback"
        print(
            "No images found in datasets/training/ or datasets/validation/ "
            f"yet; using the legacy board_debug CSV at {DEFAULT_CSV}."
        )
        records = load_csv_records(
            DEFAULT_CSV,
            LEGACY_DATASET_DIR,
            cancel_event=cancel_event,
            show_progress=show_progress,
        )
    else:
        print(
            "Using the physical training/validation class folders: "
            f"{dataset_dir}"
        )
        records = discover_images(
            dataset_dir,
            cancel_event=cancel_event,
            show_progress=show_progress,
        )

    _raise_if_cancelled(cancel_event)
    _emit_training_event(
        event_callback,
        "phase",
        phase="deduplicating",
        message="Checking for duplicate images…",
    )
    records_before_dedupe = len(records)
    records, duplicate_reports = deduplicate_records(
        records,
        dedupe_mode,
        cancel_event=cancel_event,
        show_progress=show_progress,
    )
    if duplicate_reports:
        print(
            f"\nRemoved {len(duplicate_reports)} duplicate image(s) before "
            f"training/validation use ({records_before_dedupe} -> "
            f"{len(records)}). Duplicates slow training and can make the "
            "model faulty/leaky if copies appear in both training and "
            "validation."
        )
        for report in duplicate_reports[:10]:
            duplicate_path, duplicate_label = _record_path_and_label(
                report["record"]
            )
            kept_path, kept_label = _record_path_and_label(
                report["duplicate_of"]
            )
            print(
                f"  duplicate ({report['kind']}): {duplicate_path} "
                f"[{duplicate_label}] matches {kept_path} [{kept_label}]"
            )
        if len(duplicate_reports) > 10:
            print(f"  ... and {len(duplicate_reports) - 10} more duplicate(s)")
        _print_record_summary(records)
    elif dedupe_mode != "off":
        print(
            "\nNo exact/near duplicate images found before "
            "training/validation use."
        )
    if not records:
        raise RuntimeError("No unique images remain after duplicate removal")

    _raise_if_cancelled(cancel_event)
    _emit_training_event(
        event_callback,
        "phase",
        phase="preparing",
        message="Preparing training and validation sets…",
    )
    labels = [record["label"] for record in records]
    train_records, val_records = split_records(records)

    class_names = sorted(set(labels))
    class_to_idx = {
        class_name: index
        for index, class_name in enumerate(class_names)
    }

    print(f"\nTraining images:   {len(train_records)}")
    print(f"Validation images: {len(val_records)}")
    print(f"Classes: {len(class_names)}")
    _emit_training_event(
        event_callback,
        "dataset_ready",
        dataset_dir=str(dataset_dir),
        dataset_layout=dataset_layout,
        total_images=len(records),
        training_images=len(train_records),
        validation_images=len(val_records),
        class_count=len(class_names),
        class_names=list(class_names),
        duplicates_removed=len(duplicate_reports),
        images_before_dedupe=records_before_dedupe,
        device=str(device),
    )

    train_dataset = ChessImageDataset(
        train_records, class_to_idx, training=True
    )
    val_dataset = ChessImageDataset(
        val_records, class_to_idx, training=False
    )

    train_loader = DataLoader(
        train_dataset,
        batch_size=configured_batch_size,
        shuffle=True,
        num_workers=NUM_WORKERS,
        pin_memory=(device.type == "cuda"),
    )

    val_loader = DataLoader(
        val_dataset,
        batch_size=configured_batch_size,
        shuffle=False,
        num_workers=NUM_WORKERS,
        pin_memory=(device.type == "cuda"),
    )


    model = ChessCNN(num_classes=len(class_names)).to(device)
    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=learning_rate,
        weight_decay=0.0005,
    )

    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer,
        mode="max",
        factor=0.5,
        patience=3,
    )

    best_accuracy = -1.0
    best_epoch = 0
    history = []
    train_batches = max(0, len(train_loader))
    val_batches = max(0, len(val_loader))
    total_work_batches = max(
        1, epochs * (train_batches + val_batches) + val_batches
    )
    completed_work_batches = 0
    training_started_at = time.perf_counter()

    def overall_fraction(extra_batches=0):
        return min(
            1.0,
            max(
                0.0,
                (completed_work_batches + extra_batches)
                / total_work_batches,
            ),
        )

    def make_validation_callback(epoch_number, *, final=False):
        def validation_progress(progress):
            _emit_training_event(
                event_callback,
                "validation_batch",
                epoch=epoch_number,
                total_epochs=epochs,
                final=final,
                batch=int(progress["batch"]),
                total_batches=int(
                    progress["total_batches"] or val_batches
                ),
                samples_seen=int(progress["samples_seen"]),
                total_samples=len(val_records),
                validation_loss=float(progress["loss"]),
                validation_accuracy=float(progress["accuracy"]),
                elapsed_seconds=(
                    time.perf_counter() - training_started_at
                ),
                overall_fraction=overall_fraction(
                    int(progress["batch"])
                ),
            )

        return validation_progress

    print("\nStarting training...\n")

    for epoch in range(1, epochs + 1):
        _raise_if_cancelled(cancel_event)
        _emit_training_event(
            event_callback,
            "epoch_started",
            epoch=epoch,
            total_epochs=epochs,
            total_batches=train_batches,
            total_samples=len(train_records),
            learning_rate=float(optimizer.param_groups[0]["lr"]),
            overall_fraction=overall_fraction(),
            elapsed_seconds=time.perf_counter() - training_started_at,
        )
        model.train()
        running_loss = 0.0
        correct = 0
        total = 0

        if event_callback is None:
            progress = tqdm(
                train_loader,
                desc=f"Epoch {epoch:02d}/{epochs}",
                leave=True,
            )
        else:
            progress = train_loader

        for batch_index, (images, labels_batch) in enumerate(
            progress, start=1
        ):
            _raise_if_cancelled(cancel_event)
            images = images.to(device)
            labels_batch = labels_batch.to(device)

            optimizer.zero_grad(set_to_none=True)

            logits = model(images)
            loss = criterion(logits, labels_batch)
            loss.backward()
            optimizer.step()

            batch_size = labels_batch.size(0)
            running_loss += loss.item() * batch_size
            correct += (
                logits.argmax(dim=1) == labels_batch
            ).sum().item()
            total += batch_size

            if event_callback is None:
                progress.set_postfix(
                    loss=f"{loss.item():.4f}",
                    accuracy=f"{correct / max(total, 1):.1%}",
                )
            else:
                current_train_loss = running_loss / max(total, 1)
                current_train_accuracy = correct / max(total, 1)
                _emit_training_event(
                    event_callback,
                    "batch",
                    epoch=epoch,
                    total_epochs=epochs,
                    epoch_fraction=(
                        (epoch - 1)
                        + batch_index / max(train_batches, 1)
                    ),
                    batch=batch_index,
                    total_batches=train_batches,
                    samples_seen=total,
                    total_samples=len(train_records),
                    batch_loss=float(loss.item()),
                    training_loss=float(current_train_loss),
                    training_accuracy=float(
                        current_train_accuracy
                    ),
                    learning_rate=float(
                        optimizer.param_groups[0]["lr"]
                    ),
                    elapsed_seconds=(
                        time.perf_counter() - training_started_at
                    ),
                    overall_fraction=overall_fraction(batch_index),
                )

        completed_work_batches += train_batches
        train_loss = running_loss / max(total, 1)
        train_accuracy = correct / max(total, 1)

        _raise_if_cancelled(cancel_event)
        _emit_training_event(
            event_callback,
            "validation_started",
            epoch=epoch,
            total_epochs=epochs,
            final=False,
            total_batches=val_batches,
            total_samples=len(val_records),
            overall_fraction=overall_fraction(),
            elapsed_seconds=time.perf_counter() - training_started_at,
        )
        val_loss, val_accuracy, _, _ = evaluate(
            model,
            val_loader,
            device,
            criterion,
            cancel_event=cancel_event,
            progress_callback=make_validation_callback(epoch),
        )
        completed_work_batches += val_batches

        scheduler.step(val_accuracy)
        current_learning_rate = float(optimizer.param_groups[0]["lr"])
        epoch_metrics = {
            "epoch": int(epoch),
            "training_loss": float(train_loss),
            "training_accuracy": float(train_accuracy),
            "validation_loss": float(val_loss),
            "validation_accuracy": float(val_accuracy),
            "learning_rate": current_learning_rate,
        }
        history.append(epoch_metrics)
        is_best = val_accuracy > best_accuracy
        if is_best:
            best_accuracy = float(val_accuracy)
            best_epoch = int(epoch)

        print(
            f"\nEpoch {epoch:02d}: "
            f"train_loss={train_loss:.4f}, "
            f"train_acc={train_accuracy:.2%}, "
            f"val_loss={val_loss:.4f}, "
            f"val_acc={val_accuracy:.2%}, "
            f"lr={current_learning_rate:.6g}"
        )
        _emit_training_event(
            event_callback,
            "epoch_completed",
            epoch=epoch,
            total_epochs=epochs,
            metrics=dict(epoch_metrics),
            best_accuracy=float(best_accuracy),
            best_epoch=int(best_epoch),
            is_best=bool(is_best),
            overall_fraction=overall_fraction(),
            elapsed_seconds=time.perf_counter() - training_started_at,
        )

        if is_best:

            output_model.parent.mkdir(parents=True, exist_ok=True)
            torch.save(
                {
                    "model_state_dict": model.state_dict(),
                    "class_to_idx": class_to_idx,
                    "class_names": class_names,
                    "image_size": IMAGE_SIZE,
                    "model_name": "ChessCNN",
                    "mean": list(NORMALIZE_MEAN),
                    "std": list(NORMALIZE_STD),
                    "training_images": len(train_records),
                    "validation_images": len(val_records),
                    "dataset_dir": str(dataset_dir),
                    "dataset_layout": dataset_layout,
                    "dedupe_mode": dedupe_mode,
                    "duplicate_images_removed": len(duplicate_reports),
                    "dataset_images_before_dedupe": records_before_dedupe,
                    "epoch": int(epoch),
                    "epochs_configured": int(epochs),
                    "batch_size": int(configured_batch_size),
                    "learning_rate": float(current_learning_rate),
                    "training_loss": float(train_loss),
                    "training_accuracy": float(train_accuracy),
                    "validation_loss": float(val_loss),
                    "validation_accuracy": float(val_accuracy),
                    "metrics_history": [
                        dict(item) for item in history
                    ],
                },
                output_model,
            )

            print(f"  Saved new best model: {output_model}")
            _emit_training_event(
                event_callback,
                "checkpoint_saved",
                epoch=int(epoch),
                total_epochs=int(epochs),
                output_model=str(output_model),
                validation_accuracy=float(val_accuracy),
                best_accuracy=float(best_accuracy),
                best_epoch=int(best_epoch),
            )

    _raise_if_cancelled(cancel_event)
    _emit_training_event(
        event_callback,
        "phase",
        phase="final_evaluation",
        message="Running final validation on the best checkpoint…",
    )

    # Validation is the final evaluation set; there is no separate test set.
    # Reload the best checkpoint selected by validation accuracy and report
    # its per-class validation performance.
    checkpoint = torch.load(
        output_model, map_location=device, weights_only=True
    )
    model.load_state_dict(checkpoint["model_state_dict"])

    _emit_training_event(
        event_callback,
        "validation_started",
        epoch=int(best_epoch or epochs),
        total_epochs=int(epochs),
        final=True,
        total_batches=int(val_batches),
        total_samples=len(val_records),
        overall_fraction=overall_fraction(),
        elapsed_seconds=time.perf_counter() - training_started_at,
    )
    _, final_accuracy, true_labels, predicted_labels = evaluate(
        model,
        val_loader,
        device,
        criterion,
        cancel_event=cancel_event,
        progress_callback=make_validation_callback(
            int(best_epoch or epochs), final=True
        ),
    )
    completed_work_batches += val_batches

    idx_to_class = {
        index: name for name, index in class_to_idx.items()
    }
    target_names = [
        idx_to_class[i] for i in range(len(class_names))
    ]

    print("\n" + "=" * 60)
    print("TRAINING COMPLETE")
    print(f"Best validation accuracy during training: {best_accuracy:.2%}")
    print(f"Final validation accuracy: {final_accuracy:.2%}")
    print(f"Model saved to: {output_model.resolve()}")
    print("=" * 60)

    classification_text = classification_report(
        true_labels,
        predicted_labels,
        labels=list(range(len(class_names))),
        target_names=target_names,
        zero_division=0,
    )
    print("\nPer-class validation report:")
    print(classification_text)

    summary = {
        "status": "completed",
        "output_model": str(output_model.resolve()),
        "dataset_dir": str(dataset_dir),
        "dataset_layout": dataset_layout,
        "device": str(device),
        "epochs": int(epochs),
        "batch_size": int(configured_batch_size),
        "seed": int(seed),
        "training_images": len(train_records),
        "validation_images": len(val_records),
        "total_images": len(records),
        "class_names": list(class_names),
        "class_count": len(class_names),
        "duplicates_removed": len(duplicate_reports),
        "images_before_dedupe": int(records_before_dedupe),
        "best_accuracy": float(best_accuracy),
        "best_epoch": int(best_epoch),
        "final_accuracy": float(final_accuracy),
        "history": [dict(item) for item in history],
        "classification_report": classification_text,
        "elapsed_seconds": float(time.perf_counter() - run_started_at),
        "training_elapsed_seconds": float(
            time.perf_counter() - training_started_at
        ),
        "overall_fraction": 1.0,
    }
    _emit_training_event(
        event_callback,
        "completed",
        summary=summary,
        overall_fraction=1.0,
        elapsed_seconds=summary["training_elapsed_seconds"],
    )
    return summary


def run_training_ui(initial_args):
    """Open the live CNN training dashboard and return its exit code.

    Tk is created only here, on the process main thread. Training runs in
    a worker thread and communicates back solely through a queue, so the
    worker never touches a Tk widget. If Tk/display support is unavailable,
    ``TrainerUIUnavailableError`` lets the caller use the terminal trainer.
    """
    if threading.current_thread() is not threading.main_thread():
        raise TrainerUIUnavailableError(
            "the Tk dashboard must be opened from the main thread"
        )

    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except Exception as exc:  # Tk is missing from some Linux Pythons.
        raise TrainerUIUnavailableError(
            f"Tkinter could not be imported ({exc})"
        ) from exc

    try:
        root = tk.Tk()
    except Exception as exc:
        raise TrainerUIUnavailableError(
            f"a display window could not be created ({exc})"
        ) from exc

    colours = {
        "bg": "#0f141c",
        "panel": "#171f2a",
        "panel_2": "#202b39",
        "panel_3": "#111923",
        "border": "#2c394a",
        "text": "#eef4fb",
        "muted": "#9aabbf",
        "faint": "#718198",
        "train": "#38bdf8",
        "validation": "#fb923c",
        "green": "#34d399",
        "green_dark": "#063b2a",
        "red": "#f87171",
        "amber": "#fbbf24",
        "purple": "#c084fc",
        "grey": "#64748b",
    }

    class TrainerDashboard:
        def __init__(self, dashboard_root, args):
            self.root = dashboard_root
            self.initial_args = args
            self.events = queue.Queue()
            self.worker = None
            self.cancel_event = None
            self.running = False
            self.destroyed = False
            self.close_when_done = False
            self.has_started_run = False
            self.exit_code = 0

            self.history = []
            self.live_train = None
            self.live_validation = None
            self.current_epoch = 0
            raw_epochs = getattr(args, "epochs", EPOCHS)
            self.total_epochs = max(
                1, int(EPOCHS if raw_epochs is None else raw_epochs)
            )
            self.current_batch = 0
            self.current_total_batches = 0
            self.training_accuracy = None
            self.training_loss = None
            self.validation_accuracy = None
            self.validation_loss = None
            self.best_accuracy = None
            self.best_epoch = None
            self.learning_rate = float(
                getattr(args, "learning_rate", LEARNING_RATE)
                or LEARNING_RATE
            )
            self.overall_fraction = 0.0
            self.run_started_at = None
            self.training_started_at = None
            self.phase_indeterminate = False
            self.graph_dirty = True
            self.input_widgets = []
            self.metric_labels = {}
            self.detail_labels = {}

            self.dataset_var = tk.StringVar(
                value=str(getattr(args, "dataset_dir", DATASET_DIR))
            )
            csv_value = getattr(args, "csv", None)
            self.csv_var = tk.StringVar(
                value="" if csv_value is None else str(csv_value)
            )
            self.output_var = tk.StringVar(
                value=str(getattr(args, "output", OUTPUT_MODEL))
            )
            self.epochs_var = tk.StringVar(value=str(self.total_epochs))
            self.batch_size_var = tk.StringVar(
                value=str(getattr(args, "batch_size", BATCH_SIZE))
            )
            self.learning_rate_var = tk.StringVar(
                value=f"{self.learning_rate:g}"
            )
            self.seed_var = tk.StringVar(
                value=str(getattr(args, "seed", RANDOM_SEED))
            )
            self.dedupe_var = tk.StringVar(
                value=str(getattr(args, "dedupe", "exact-near"))
            )
            self.overall_var = tk.DoubleVar(value=0.0)
            self.phase_var = tk.DoubleVar(value=0.0)
            self.overall_text_var = tk.StringVar(value="0.0%")
            self.phase_title_var = tk.StringVar(value="Current step: Ready")
            self.phase_text_var = tk.StringVar(value="Waiting to start")
            self.elapsed_var = tk.StringVar(value="Elapsed 0:00")
            self.eta_var = tk.StringVar(value="ETA —")
            self.samples_var = tk.StringVar(value="No batch running")

            self._configure_window()
            self._build_ui()
            self._reset_run_display(clear_log=True)
            self.root.protocol("WM_DELETE_WINDOW", self._request_close)
            self.root.after(80, self._poll_events)
            if bool(getattr(args, "start", False)):
                self.root.after(300, self.start_training)

        # ----------------------------------------------------------
        # Window construction
        # ----------------------------------------------------------
        def _configure_window(self):
            self.root.title("ChessBotV CNN Trainer")
            self.root.geometry("1280x840")
            self.root.minsize(1060, 720)
            self.root.configure(bg=colours["bg"])
            self.root.grid_rowconfigure(1, weight=1)
            self.root.grid_columnconfigure(0, weight=0)
            self.root.grid_columnconfigure(1, weight=1)

            style = ttk.Style(self.root)
            for theme in ("clam", "alt", "default"):
                if theme in style.theme_names():
                    try:
                        style.theme_use(theme)
                        break
                    except tk.TclError:
                        pass

            # Style options vary between ttk themes/platforms. Styling is
            # cosmetic, so an unsupported option must never stop the
            # dashboard itself from opening.
            def configure_style(name, **kwargs):
                try:
                    style.configure(name, **kwargs)
                except tk.TclError:
                    pass

            def map_style(name, **kwargs):
                try:
                    style.map(name, **kwargs)
                except tk.TclError:
                    pass

            default_font = ("Segoe UI", 10)
            configure_style(
                "TButton",
                font=default_font,
                padding=(12, 7),
                background=colours["panel_2"],
                foreground=colours["text"],
                bordercolor=colours["border"],
                lightcolor=colours["panel_2"],
                darkcolor=colours["panel_2"],
            )
            map_style(
                "TButton",
                background=[
                    ("active", "#2a394e"),
                    ("disabled", "#1b2532"),
                ],
                foreground=[
                    ("disabled", colours["faint"]),
                ],
            )
            configure_style(
                "Accent.TButton",
                font=("Segoe UI", 11, "bold"),
                padding=(16, 10),
                background=colours["green"],
                foreground="#052e20",
                bordercolor=colours["green"],
            )
            map_style(
                "Accent.TButton",
                background=[
                    ("active", "#6ee7b7"),
                    ("disabled", "#245647"),
                ],
                foreground=[
                    ("disabled", "#9fb8af"),
                ],
            )
            configure_style(
                "Danger.TButton",
                font=("Segoe UI", 11, "bold"),
                padding=(16, 10),
                background="#7f1d1d",
                foreground="#fee2e2",
                bordercolor="#991b1b",
            )
            map_style(
                "Danger.TButton",
                background=[
                    ("active", "#991b1b"),
                    ("disabled", "#33272b"),
                ],
                foreground=[
                    ("disabled", colours["faint"]),
                ],
            )
            for widget_style in ("TEntry", "TCombobox", "TSpinbox"):
                configure_style(
                    widget_style,
                    fieldbackground="#0c121b",
                    background=colours["panel_2"],
                    foreground=colours["text"],
                    bordercolor=colours["border"],
                    lightcolor=colours["border"],
                    darkcolor=colours["border"],
                    insertcolor=colours["text"],
                    padding=5,
                )
                map_style(
                    widget_style,
                    fieldbackground=[
                        ("disabled", "#131b25"),
                        ("readonly", "#0c121b"),
                    ],
                    foreground=[
                        ("disabled", colours["faint"]),
                        ("readonly", colours["text"]),
                    ],
                )
            configure_style(
                "Overall.Horizontal.TProgressbar",
                troughcolor="#0b1018",
                background=colours["train"],
                bordercolor=colours["border"],
                lightcolor=colours["train"],
                darkcolor=colours["train"],
                thickness=18,
            )
            configure_style(
                "Phase.Horizontal.TProgressbar",
                troughcolor="#0b1018",
                background=colours["validation"],
                bordercolor=colours["border"],
                lightcolor=colours["validation"],
                darkcolor=colours["validation"],
                thickness=14,
            )
            configure_style(
                "Vertical.TScrollbar",
                background=colours["panel_2"],
                troughcolor=colours["panel"],
                bordercolor=colours["border"],
                arrowcolor=colours["muted"],
            )

        def _card(self, parent, title=None):
            outer = tk.Frame(
                parent,
                bg=colours["panel"],
                highlightbackground=colours["border"],
                highlightthickness=1,
                bd=0,
            )
            if title:
                tk.Label(
                    outer,
                    text=title,
                    bg=colours["panel"],
                    fg=colours["text"],
                    font=("Segoe UI", 12, "bold"),
                    anchor="w",
                ).pack(fill="x", padx=14, pady=(12, 5))
                body = tk.Frame(outer, bg=colours["panel"])
                body.pack(fill="both", expand=True, padx=14, pady=(0, 12))
            else:
                body = tk.Frame(outer, bg=colours["panel"])
                body.pack(fill="both", expand=True, padx=14, pady=12)
            return outer, body

        def _small_label(self, parent, text):
            return tk.Label(
                parent,
                text=text,
                bg=colours["panel"],
                fg=colours["muted"],
                font=("Segoe UI", 9, "bold"),
                anchor="w",
            )

        def _metric_card(self, parent, key, title, accent):
            frame = tk.Frame(
                parent,
                bg=colours["panel"],
                highlightbackground=colours["border"],
                highlightthickness=1,
                bd=0,
            )
            tk.Frame(frame, bg=accent, height=4).pack(fill="x")
            tk.Label(
                frame,
                text=title.upper(),
                bg=colours["panel"],
                fg=colours["muted"],
                font=("Segoe UI", 9, "bold"),
                anchor="w",
            ).pack(fill="x", padx=12, pady=(9, 0))
            value = tk.Label(
                frame,
                text="—",
                bg=colours["panel"],
                fg=colours["text"],
                font=("Segoe UI", 21, "bold"),
                anchor="w",
            )
            value.pack(fill="x", padx=12, pady=(1, 0))
            detail = tk.Label(
                frame,
                text="Waiting",
                bg=colours["panel"],
                fg=colours["faint"],
                font=("Segoe UI", 9),
                anchor="w",
                wraplength=210,
                justify="left",
            )
            detail.pack(fill="x", padx=12, pady=(0, 10))
            self.metric_labels[key] = (value, detail)
            return frame

        def _path_row(self, parent, title, variable, command):
            self._small_label(parent, title).pack(
                fill="x", pady=(8, 3)
            )
            row = tk.Frame(parent, bg=colours["panel"])
            row.pack(fill="x")
            entry = ttk.Entry(row, textvariable=variable)
            entry.pack(side="left", fill="x", expand=True)
            button = ttk.Button(row, text="Browse…", command=command)
            button.pack(side="left", padx=(7, 0))
            self.input_widgets.extend((entry, button))
            return entry

        def _detail_row(self, parent, key, title):
            row = tk.Frame(parent, bg=colours["panel"])
            row.pack(fill="x", pady=2)
            tk.Label(
                row,
                text=title,
                width=17,
                anchor="w",
                bg=colours["panel"],
                fg=colours["muted"],
                font=("Segoe UI", 9),
            ).pack(side="left")
            value = tk.Label(
                row,
                text="—",
                anchor="w",
                bg=colours["panel"],
                fg=colours["text"],
                font=("Segoe UI", 9, "bold"),
            )
            value.pack(side="left", fill="x", expand=True)
            self.detail_labels[key] = value

        def _build_ui(self):
            header = tk.Frame(self.root, bg=colours["bg"])
            header.grid(
                row=0,
                column=0,
                columnspan=2,
                sticky="ew",
                padx=16,
                pady=(13, 9),
            )
            title_box = tk.Frame(header, bg=colours["bg"])
            title_box.pack(side="left", fill="x", expand=True)
            tk.Label(
                title_box,
                text="ChessBotV CNN Trainer",
                bg=colours["bg"],
                fg=colours["text"],
                font=("Segoe UI", 22, "bold"),
                anchor="w",
            ).pack(fill="x")
            tk.Label(
                title_box,
                text=(
                    "Live square-classifier training from the physical "
                    "training/validation folders"
                ),
                bg=colours["bg"],
                fg=colours["muted"],
                font=("Segoe UI", 10),
                anchor="w",
            ).pack(fill="x", pady=(1, 0))
            self.status_label = tk.Label(
                header,
                text="● Ready",
                bg="#263241",
                fg=colours["text"],
                font=("Segoe UI", 11, "bold"),
                padx=15,
                pady=8,
            )
            self.status_label.pack(side="right", padx=(16, 0))

            left = tk.Frame(self.root, bg=colours["bg"], width=342)
            left.grid(
                row=1,
                column=0,
                sticky="nsew",
                padx=(16, 8),
                pady=(0, 16),
            )
            left.grid_propagate(False)

            setup_card, setup = self._card(left, "Training setup")
            setup_card.pack(fill="x")
            self._path_row(
                setup, "Dataset folder", self.dataset_var,
                self._browse_dataset,
            )
            self._path_row(
                setup, "Legacy CSV (optional)", self.csv_var,
                self._browse_csv,
            )
            self._path_row(
                setup, "Output model", self.output_var,
                self._browse_output,
            )

            settings = tk.Frame(setup, bg=colours["panel"])
            settings.pack(fill="x", pady=(10, 0))
            settings.grid_columnconfigure(0, weight=1, uniform="settings")
            settings.grid_columnconfigure(1, weight=1, uniform="settings")

            self._small_label(settings, "Epochs").grid(
                row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 3)
            )
            epochs_spin = ttk.Spinbox(
                settings,
                from_=1,
                to=1000,
                increment=1,
                textvariable=self.epochs_var,
                width=12,
            )
            epochs_spin.grid(
                row=1, column=0, sticky="ew", padx=(0, 8), pady=(0, 8)
            )

            self._small_label(settings, "Batch size").grid(
                row=0, column=1, sticky="w", pady=(0, 3)
            )
            batch_spin = ttk.Spinbox(
                settings,
                from_=1,
                to=1024,
                increment=1,
                textvariable=self.batch_size_var,
                width=12,
            )
            batch_spin.grid(row=1, column=1, sticky="ew", pady=(0, 8))

            self._small_label(settings, "Learning rate").grid(
                row=2, column=0, sticky="w", padx=(0, 8), pady=(0, 3)
            )
            lr_entry = ttk.Entry(
                settings, textvariable=self.learning_rate_var, width=12
            )
            lr_entry.grid(
                row=3, column=0, sticky="ew", padx=(0, 8), pady=(0, 8)
            )

            self._small_label(settings, "Seed").grid(
                row=2, column=1, sticky="w", pady=(0, 3)
            )
            seed_spin = ttk.Spinbox(
                settings,
                from_=0,
                to=999999999,
                increment=1,
                textvariable=self.seed_var,
                width=12,
            )
            seed_spin.grid(row=3, column=1, sticky="ew", pady=(0, 8))
            self.input_widgets.extend(
                (epochs_spin, batch_spin, lr_entry, seed_spin)
            )

            self._small_label(setup, "Duplicate removal").pack(
                fill="x", pady=(1, 3)
            )
            self.dedupe_combo = ttk.Combobox(
                setup,
                textvariable=self.dedupe_var,
                values=("exact-near", "exact", "off"),
                state="readonly",
            )
            self.dedupe_combo.pack(fill="x")
            self.input_widgets.append(self.dedupe_combo)

            controls = tk.Frame(setup, bg=colours["panel"])
            controls.pack(fill="x", pady=(14, 0))
            self.start_button = ttk.Button(
                controls,
                text="▶  Start training",
                style="Accent.TButton",
                command=self.start_training,
            )
            self.start_button.pack(side="left", fill="x", expand=True)
            self.stop_button = ttk.Button(
                controls,
                text="■  Stop",
                style="Danger.TButton",
                command=self.stop_training,
                state="disabled",
            )
            self.stop_button.pack(side="left", fill="x", padx=(8, 0))
            self.open_folder_button = ttk.Button(
                setup,
                text="Open output folder",
                command=self._open_output_folder,
            )
            self.open_folder_button.pack(fill="x", pady=(9, 0))
            ttk.Button(
                setup,
                text="Close trainer",
                command=self._request_close,
            ).pack(fill="x", pady=(8, 0))

            details_card, details = self._card(left, "Run details")
            details_card.pack(fill="x", pady=(12, 0))
            self._detail_row(details, "device", "Device")
            self._detail_row(details, "images", "Unique images")
            self._detail_row(details, "training", "Training")
            self._detail_row(details, "validation", "Validation")
            self._detail_row(details, "classes", "Classes")
            self._detail_row(details, "duplicates", "Duplicates removed")
            self._detail_row(details, "checkpoint", "Best checkpoint")
            self._detail_row(details, "learning_rate", "Learning rate")

            tk.Label(
                left,
                text=(
                    "The best validation checkpoint is saved automatically. "
                    "Stop is checked between images and batches, so a very "
                    "large batch may finish before the run ends."
                ),
                bg=colours["bg"],
                fg=colours["faint"],
                font=("Segoe UI", 9),
                wraplength=326,
                justify="left",
                anchor="w",
            ).pack(fill="x", pady=(12, 0))

            right = tk.Frame(self.root, bg=colours["bg"])
            right.grid(
                row=1,
                column=1,
                sticky="nsew",
                padx=(8, 16),
                pady=(0, 16),
            )
            right.grid_rowconfigure(1, weight=3)
            right.grid_rowconfigure(3, weight=2)
            right.grid_columnconfigure(0, weight=1)

            metrics = tk.Frame(right, bg=colours["bg"])
            metrics.grid(row=0, column=0, sticky="ew", pady=(0, 10))
            for index in range(4):
                metrics.grid_columnconfigure(
                    index, weight=1, uniform="metric"
                )
            self._metric_card(
                metrics, "epoch", "Epoch", colours["train"]
            ).grid(row=0, column=0, sticky="nsew", padx=(0, 5))
            self._metric_card(
                metrics,
                "training_accuracy",
                "Training accuracy",
                colours["train"],
            ).grid(row=0, column=1, sticky="nsew", padx=5)
            self._metric_card(
                metrics,
                "validation_accuracy",
                "Validation accuracy",
                colours["validation"],
            ).grid(row=0, column=2, sticky="nsew", padx=5)
            self._metric_card(
                metrics,
                "best_accuracy",
                "Best validation",
                colours["green"],
            ).grid(row=0, column=3, sticky="nsew", padx=(5, 0))

            graph_card, graph_body = self._card(
                right, "Live accuracy graph"
            )
            graph_card.grid(row=1, column=0, sticky="nsew")
            legend = tk.Frame(graph_body, bg=colours["panel"])
            legend.pack(fill="x", pady=(0, 5))
            tk.Label(
                legend,
                text="● Training accuracy",
                bg=colours["panel"],
                fg=colours["train"],
                font=("Segoe UI", 9, "bold"),
            ).pack(side="left")
            tk.Label(
                legend,
                text="● Validation accuracy",
                bg=colours["panel"],
                fg=colours["validation"],
                font=("Segoe UI", 9, "bold"),
            ).pack(side="left", padx=(18, 0))
            tk.Label(
                legend,
                text="Dashed points are the current unfinished epoch",
                bg=colours["panel"],
                fg=colours["faint"],
                font=("Segoe UI", 9),
            ).pack(side="right")
            self.graph_canvas = tk.Canvas(
                graph_body,
                bg="#0b111a",
                highlightbackground=colours["border"],
                highlightthickness=1,
                bd=0,
            )
            self.graph_canvas.pack(fill="both", expand=True)
            self.graph_canvas.bind(
                "<Configure>", self._on_graph_configure
            )

            progress_card, progress = self._card(right, "Progress")
            progress_card.grid(
                row=2, column=0, sticky="ew", pady=(10, 0)
            )
            overall_header = tk.Frame(progress, bg=colours["panel"])
            overall_header.pack(fill="x")
            tk.Label(
                overall_header,
                text="Overall training",
                bg=colours["panel"],
                fg=colours["text"],
                font=("Segoe UI", 10, "bold"),
            ).pack(side="left")
            tk.Label(
                overall_header,
                textvariable=self.overall_text_var,
                bg=colours["panel"],
                fg=colours["train"],
                font=("Segoe UI", 10, "bold"),
            ).pack(side="right")
            tk.Label(
                overall_header,
                textvariable=self.eta_var,
                bg=colours["panel"],
                fg=colours["muted"],
                font=("Segoe UI", 9),
            ).pack(side="right", padx=(0, 14))
            tk.Label(
                overall_header,
                textvariable=self.elapsed_var,
                bg=colours["panel"],
                fg=colours["muted"],
                font=("Segoe UI", 9),
            ).pack(side="right", padx=(0, 14))
            self.overall_progress = ttk.Progressbar(
                progress,
                style="Overall.Horizontal.TProgressbar",
                variable=self.overall_var,
                maximum=100.0,
                mode="determinate",
            )
            self.overall_progress.pack(fill="x", pady=(5, 10))

            phase_header = tk.Frame(progress, bg=colours["panel"])
            phase_header.pack(fill="x")
            tk.Label(
                phase_header,
                textvariable=self.phase_title_var,
                bg=colours["panel"],
                fg=colours["text"],
                font=("Segoe UI", 10, "bold"),
            ).pack(side="left")
            tk.Label(
                phase_header,
                textvariable=self.phase_text_var,
                bg=colours["panel"],
                fg=colours["validation"],
                font=("Segoe UI", 9, "bold"),
            ).pack(side="right")
            self.phase_progress = ttk.Progressbar(
                progress,
                style="Phase.Horizontal.TProgressbar",
                variable=self.phase_var,
                maximum=100.0,
                mode="determinate",
            )
            self.phase_progress.pack(fill="x", pady=(5, 6))
            tk.Label(
                progress,
                textvariable=self.samples_var,
                bg=colours["panel"],
                fg=colours["muted"],
                font=("Segoe UI", 9),
                anchor="w",
            ).pack(fill="x")

            log_card, log_body = self._card(right, "Training log")
            log_card.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
            log_tools = tk.Frame(log_body, bg=colours["panel"])
            log_tools.pack(fill="x", pady=(0, 6))
            tk.Label(
                log_tools,
                text="Dataset messages, epoch results, and the final report",
                bg=colours["panel"],
                fg=colours["faint"],
                font=("Segoe UI", 9),
            ).pack(side="left")
            ttk.Button(
                log_tools, text="Clear", command=self._clear_log
            ).pack(side="right")
            log_frame = tk.Frame(log_body, bg=colours["panel"])
            log_frame.pack(fill="both", expand=True)
            self.log_text = tk.Text(
                log_frame,
                wrap="word",
                height=8,
                bg="#0b1018",
                fg="#cbd5e1",
                insertbackground=colours["text"],
                selectbackground="#334155",
                relief="flat",
                bd=0,
                padx=9,
                pady=8,
                font=("Consolas", 9),
                state="disabled",
            )
            scrollbar = ttk.Scrollbar(
                log_frame, orient="vertical", command=self.log_text.yview
            )
            self.log_text.configure(yscrollcommand=scrollbar.set)
            self.log_text.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")

        # ----------------------------------------------------------
        # Form and file dialogs
        # ----------------------------------------------------------
        @staticmethod
        def _existing_initial_dir(value):
            try:
                path = Path(str(value or "")).expanduser()
            except Exception:
                return str(BASE_DIR)
            if path.is_dir():
                return str(path)
            if path.parent.is_dir():
                return str(path.parent)
            return str(BASE_DIR)

        def _browse_dataset(self):
            selected = filedialog.askdirectory(
                title="Choose the dataset folder",
                initialdir=self._existing_initial_dir(
                    self.dataset_var.get()
                ),
                mustexist=False,
            )
            if selected:
                self.dataset_var.set(selected)

        def _browse_csv(self):
            selected = filedialog.askopenfilename(
                title="Choose an optional legacy dataset CSV",
                initialdir=self._existing_initial_dir(self.csv_var.get()),
                filetypes=(
                    ("CSV files", "*.csv"),
                    ("All files", "*.*"),
                ),
            )
            if selected:
                self.csv_var.set(selected)

        def _browse_output(self):
            selected = filedialog.asksaveasfilename(
                title="Save the trained CNN model",
                initialdir=self._existing_initial_dir(
                    self.output_var.get()
                ),
                initialfile=Path(
                    self.output_var.get() or OUTPUT_MODEL
                ).name,
                defaultextension=".pth",
                filetypes=(
                    ("PyTorch model", "*.pth"),
                    ("All files", "*.*"),
                ),
            )
            if selected:
                self.output_var.set(selected)

        def _open_output_folder(self):
            raw_output = self.output_var.get().strip() or str(OUTPUT_MODEL)
            folder = Path(raw_output).expanduser().parent or BASE_DIR
            if not folder.is_dir():
                messagebox.showinfo(
                    "CNN Trainer",
                    f"The output folder does not exist yet:\n{folder}",
                )
                return
            try:
                if sys.platform.startswith("win"):
                    os.startfile(str(folder))
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(folder)])
                else:
                    subprocess.Popen(["xdg-open", str(folder)])
            except Exception as exc:
                messagebox.showerror(
                    "CNN Trainer",
                    f"Could not open the output folder:\n{exc}",
                )

        def _read_form_args(self):
            errors = []
            dataset_dir = self.dataset_var.get().strip()
            if not dataset_dir:
                errors.append("Choose a dataset folder.")
            output = self.output_var.get().strip()
            if not output:
                errors.append("Choose an output model path.")

            def parse_int(name, variable, minimum, maximum):
                try:
                    value = int(variable.get().strip())
                except (TypeError, ValueError):
                    errors.append(f"{name} must be a whole number.")
                    return minimum
                if not minimum <= value <= maximum:
                    errors.append(
                        f"{name} must be between {minimum} and {maximum}."
                    )
                return value

            epochs = parse_int(
                "Epochs", self.epochs_var, 1, 1000
            )
            batch_size = parse_int(
                "Batch size", self.batch_size_var, 1, 1024
            )
            seed = parse_int(
                "Seed", self.seed_var, 0, 999999999
            )
            try:
                learning_rate = float(self.learning_rate_var.get().strip())
                if (
                    not math.isfinite(learning_rate)
                    or not 0 < learning_rate < 1
                ):
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(
                    "Learning rate must be a number greater than 0 and "
                    "less than 1, for example 0.001."
                )
                learning_rate = LEARNING_RATE

            dedupe = self.dedupe_var.get().strip()
            if dedupe not in ("exact-near", "exact", "off"):
                errors.append("Choose a duplicate-removal mode.")

            if errors:
                messagebox.showerror(
                    "CNN Trainer",
                    "Please fix the training setup:\n\n• "
                    + "\n• ".join(errors),
                )
                return None

            csv_text = self.csv_var.get().strip()
            return argparse.Namespace(
                dataset_dir=Path(dataset_dir),
                csv=Path(csv_text) if csv_text else None,
                dedupe=dedupe,
                output=Path(output),
                epochs=epochs,
                batch_size=batch_size,
                learning_rate=learning_rate,
                seed=seed,
                ui=True,
                start=True,
            )

        # ----------------------------------------------------------
        # Small display helpers
        # ----------------------------------------------------------
        def _set_status(self, text, kind="ready"):
            palette = {
                "ready": ("#263241", colours["text"]),
                "working": ("#123653", "#bae6fd"),
                "training": ("#123653", "#bae6fd"),
                "validating": ("#4a2b12", "#fed7aa"),
                "complete": (colours["green_dark"], "#a7f3d0"),
                "failed": ("#5b1616", "#fecaca"),
                "cancelled": ("#4a3510", "#fde68a"),
            }
            bg, fg = palette.get(kind, palette["ready"])
            self.status_label.configure(text=f"● {text}", bg=bg, fg=fg)

        def _set_metric(self, key, value, detail=None):
            if key not in self.metric_labels:
                return
            value_label, detail_label = self.metric_labels[key]
            value_label.configure(text=value)
            if detail is not None:
                detail_label.configure(text=detail)

        def _set_detail(self, key, value):
            if key in self.detail_labels:
                self.detail_labels[key].configure(text=str(value))

        def _set_phase_indeterminate(self, enabled):
            if enabled and not self.phase_indeterminate:
                self.phase_progress.configure(mode="indeterminate")
                self.phase_progress.start(14)
                self.phase_indeterminate = True
            elif not enabled and self.phase_indeterminate:
                self.phase_progress.stop()
                self.phase_progress.configure(mode="determinate")
                self.phase_indeterminate = False

        def _set_overall_progress(self, fraction):
            if fraction is None:
                return
            self.overall_fraction = min(1.0, max(0.0, float(fraction)))
            self.overall_var.set(self.overall_fraction * 100.0)
            self.overall_text_var.set(
                f"{self.overall_fraction * 100:.1f}%"
            )

        def _set_phase_progress(self, completed, total, text=None):
            self._set_phase_indeterminate(False)
            total = max(0, int(total or 0))
            completed = max(0, int(completed or 0))
            fraction = completed / total if total else 0.0
            self.phase_var.set(min(100.0, max(0.0, fraction * 100.0)))
            if text is None:
                text = (
                    f"Batch {completed}/{total} · {fraction * 100:.1f}%"
                    if total
                    else "Waiting"
                )
            self.phase_text_var.set(text)

        def _append_log(self, text):
            value = str(text)
            if not value:
                return
            self.log_text.configure(state="normal")
            try:
                self.log_text.insert("end", value.rstrip() + "\n")
                line_count = int(
                    self.log_text.index("end-1c").split(".", 1)[0]
                )
                if line_count > 1800:
                    self.log_text.delete("1.0", "300.0")
                self.log_text.see("end")
            finally:
                self.log_text.configure(state="disabled")

        def _clear_log(self):
            self.log_text.configure(state="normal")
            try:
                self.log_text.delete("1.0", "end")
            finally:
                self.log_text.configure(state="disabled")

        def _reset_run_display(self, *, clear_log=False):
            self.history = []
            self.live_train = None
            self.live_validation = None
            self.current_epoch = 0
            self.current_batch = 0
            self.current_total_batches = 0
            self.training_accuracy = None
            self.training_loss = None
            self.validation_accuracy = None
            self.validation_loss = None
            self.best_accuracy = None
            self.best_epoch = None
            self.overall_fraction = 0.0
            self.training_started_at = None

            self._set_status("Ready", "ready")
            self._set_metric(
                "epoch", f"0/{self.total_epochs}", "Waiting to start"
            )
            self._set_metric(
                "training_accuracy", "—", "Loss —"
            )
            self._set_metric(
                "validation_accuracy", "—", "Loss —"
            )
            self._set_metric(
                "best_accuracy", "—", "No checkpoint yet"
            )
            for key in (
                "device",
                "images",
                "training",
                "validation",
                "classes",
                "duplicates",
                "checkpoint",
            ):
                self._set_detail(key, "—")
            self._set_detail(
                "learning_rate", f"{self.learning_rate:g}"
            )
            self.phase_title_var.set("Current step: Ready")
            self.samples_var.set("No batch running")
            self.elapsed_var.set("Elapsed 0:00")
            self.eta_var.set("ETA —")
            self._set_overall_progress(0.0)
            self._set_phase_progress(0, 0, "Waiting to start")
            self.graph_dirty = True
            if clear_log:
                self._clear_log()
                self._append_log(
                    "Ready. Choose the dataset/output if needed, then "
                    "click Start training."
                )

        def _set_controls_running(self, running):
            state = "disabled" if running else "normal"
            for widget in self.input_widgets:
                try:
                    widget.configure(state=state)
                except tk.TclError:
                    pass
            try:
                self.dedupe_combo.configure(
                    state="disabled" if running else "readonly"
                )
            except tk.TclError:
                pass
            self.start_button.configure(state="disabled" if running else "normal")
            self.stop_button.configure(state="normal" if running else "disabled")

        # ----------------------------------------------------------
        # Training lifecycle
        # ----------------------------------------------------------
        def start_training(self):
            if self.running or self.destroyed:
                return
            run_args = self._read_form_args()
            if run_args is None:
                return

            self.total_epochs = int(run_args.epochs)
            self.learning_rate = float(run_args.learning_rate)
            self._reset_run_display(clear_log=False)
            self._append_log("")
            self._append_log("=" * 72)
            self._append_log(
                f"Starting training: dataset={run_args.dataset_dir} · "
                f"output={run_args.output} · epochs={run_args.epochs} · "
                f"batch={run_args.batch_size} · lr={run_args.learning_rate:g}"
            )
            self.cancel_event = threading.Event()
            self.running = True
            self.has_started_run = True
            self.exit_code = 0
            self.close_when_done = False
            self.run_started_at = time.monotonic()
            self._set_controls_running(True)
            self.start_button.configure(text="Training…")
            self._set_status("Starting", "working")
            self.phase_title_var.set("Current step: Starting")
            self._set_phase_indeterminate(True)
            self.phase_text_var.set("Starting worker…")

            self.worker = threading.Thread(
                target=self._training_worker,
                args=(run_args, self.cancel_event),
                name="ChessBotVCNNTrainer",
                daemon=True,
            )
            self.worker.start()

        def _training_worker(self, run_args, cancel_event):
            def post(event):
                self.events.put(event)

            writer = _QueueLogWriter(
                lambda line: post({"type": "log", "message": line})
            )
            try:
                with contextlib.redirect_stdout(writer), (
                    contextlib.redirect_stderr(writer)
                ):
                    summary = train_model(
                        run_args,
                        event_callback=post,
                        cancel_event=cancel_event,
                    )
            except TrainingCancelled:
                writer.flush()
                post({
                    "type": "worker_cancelled",
                    "message": "Training cancelled.",
                })
            except Exception as exc:  # Show errors in the dashboard.
                writer.flush()
                post({
                    "type": "worker_failed",
                    "message": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                })
            else:
                writer.flush()
                post({
                    "type": "worker_completed",
                    "summary": summary,
                })

        def stop_training(self):
            if not self.running or self.cancel_event is None:
                return
            self.cancel_event.set()
            self.stop_button.configure(state="disabled")
            self._set_status("Cancelling", "cancelled")
            self.phase_title_var.set("Current step: Cancelling")
            self.phase_text_var.set("Waiting for the current batch…")
            self._append_log(
                "Stop requested; training will end at the next safe check."
            )

        def _request_close(self):
            if self.running:
                should_close = messagebox.askyesno(
                    "CNN Trainer",
                    "Training is still running. Stop it and close the "
                    "trainer window?",
                )
                if not should_close:
                    return
                self.close_when_done = True
                self.stop_training()
                return
            self._destroy()

        def _destroy(self):
            if self.destroyed:
                return
            if (
                bool(getattr(self.initial_args, "start", False))
                and not self.has_started_run
                and self.exit_code == 0
            ):
                # `--start` callers (notably board-vision) asked for an
                # actual training run, not merely an open window.
                self.exit_code = 130
            self.destroyed = True
            try:
                self.root.destroy()
            except tk.TclError:
                pass

        # ----------------------------------------------------------
        # Event polling and handling
        # ----------------------------------------------------------
        def _poll_events(self):
            if self.destroyed:
                return
            processed = 0
            while processed < 500:
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    break
                self._handle_event(event)
                processed += 1
                if self.destroyed:
                    return

            self._update_time_labels()
            if self.graph_dirty:
                self._draw_graph()
            if not self.destroyed:
                try:
                    self.root.after(80, self._poll_events)
                except tk.TclError:
                    self.destroyed = True

        def _update_time_labels(self):
            if self.run_started_at is None or not self.running:
                # Leave the final elapsed/ETA labels frozen after a run.
                return
            elapsed = time.monotonic() - self.run_started_at
            self.elapsed_var.set(f"Elapsed {_format_duration(elapsed)}")
            if self.training_started_at is None:
                self.eta_var.set("ETA —")
                return
            training_elapsed = (
                time.monotonic() - self.training_started_at
            )
            remaining = _estimate_remaining_seconds(
                training_elapsed, self.overall_fraction
            )
            self.eta_var.set(
                "ETA —"
                if remaining is None
                else f"ETA {_format_duration(remaining)}"
            )

        def _handle_event(self, event):
            event_type = str(event.get("type", ""))

            if event_type == "log":
                self._append_log(event.get("message", ""))
                return

            if event_type == "started":
                self.total_epochs = max(
                    1, int(event.get("epochs") or self.total_epochs)
                )
                self.learning_rate = float(
                    event.get("learning_rate") or self.learning_rate
                )
                self._set_detail("device", event.get("device", "—"))
                self._set_detail(
                    "learning_rate", f"{self.learning_rate:g}"
                )
                self._set_metric(
                    "epoch",
                    f"0/{self.total_epochs}",
                    "Loading dataset",
                )
                self._set_status("Loading dataset", "working")
                self.phase_title_var.set("Current step: Loading dataset")
                self._set_phase_indeterminate(True)
                self.phase_text_var.set("Reading images…")
                return

            if event_type == "phase":
                phase = str(event.get("phase", ""))
                message = str(event.get("message") or "Working…")
                labels = {
                    "loading_dataset": ("Loading dataset", "working"),
                    "deduplicating": ("Removing duplicates", "working"),
                    "preparing": ("Preparing", "working"),
                    "final_evaluation": (
                        "Final validation",
                        "validating",
                    ),
                }
                status, kind = labels.get(phase, ("Working", "working"))
                self._set_status(status, kind)
                self.phase_title_var.set(f"Current step: {status}")
                self.phase_text_var.set(message)
                self.samples_var.set(message)
                if phase != "final_evaluation":
                    self._set_phase_indeterminate(True)
                else:
                    self._set_phase_progress(0, 0, message)
                return

            if event_type == "dataset_ready":
                self._set_detail("device", event.get("device", "—"))
                self._set_detail(
                    "images", event.get("total_images", "—")
                )
                self._set_detail(
                    "training", event.get("training_images", "—")
                )
                self._set_detail(
                    "validation", event.get("validation_images", "—")
                )
                self._set_detail(
                    "classes", event.get("class_count", "—")
                )
                self._set_detail(
                    "duplicates", event.get("duplicates_removed", "—")
                )
                self._set_status("Preparing model", "working")
                self.phase_title_var.set("Current step: Preparing model")
                self._set_phase_progress(
                    0, 0, "Dataset ready; building model…"
                )
                self.samples_var.set(
                    f"Training {event.get('training_images', '—')} images · "
                    f"validation {event.get('validation_images', '—')} images"
                )
                return

            if event_type == "epoch_started":
                if self.training_started_at is None:
                    self.training_started_at = time.monotonic()
                self.current_epoch = int(event.get("epoch") or 0)
                self.total_epochs = int(
                    event.get("total_epochs") or self.total_epochs
                )
                self.current_batch = 0
                self.current_total_batches = int(
                    event.get("total_batches") or 0
                )
                self.live_train = None
                self.live_validation = None
                self._set_status(
                    f"Training epoch {self.current_epoch}", "training"
                )
                self._set_metric(
                    "epoch",
                    f"{self.current_epoch}/{self.total_epochs}",
                    f"Batch 0/{self.current_total_batches}",
                )
                self._set_metric(
                    "training_accuracy",
                    "…",
                    "Running epoch average",
                )
                self.phase_title_var.set(
                    f"Current step: Training epoch {self.current_epoch}"
                )
                self._set_phase_progress(
                    0, self.current_total_batches
                )
                self._set_overall_progress(
                    event.get("overall_fraction")
                )
                self.graph_dirty = True
                return

            if event_type == "batch":
                self.current_epoch = int(event.get("epoch") or 0)
                self.total_epochs = int(
                    event.get("total_epochs") or self.total_epochs
                )
                self.current_batch = int(event.get("batch") or 0)
                self.current_total_batches = int(
                    event.get("total_batches") or 0
                )
                self.training_accuracy = event.get("training_accuracy")
                self.training_loss = event.get("training_loss")
                self.learning_rate = float(
                    event.get("learning_rate") or self.learning_rate
                )
                self.live_train = (
                    float(event.get("epoch_fraction") or 0.0),
                    self.training_accuracy,
                )
                self._set_metric(
                    "epoch",
                    f"{self.current_epoch}/{self.total_epochs}",
                    (
                        f"Batch {self.current_batch}/"
                        f"{self.current_total_batches}"
                    ),
                )
                loss_text = (
                    "—"
                    if self.training_loss is None
                    else f"{float(self.training_loss):.4f}"
                )
                self._set_metric(
                    "training_accuracy",
                    _format_percent(self.training_accuracy),
                    f"Loss {loss_text}",
                )
                self._set_detail(
                    "learning_rate", f"{self.learning_rate:g}"
                )
                self._set_phase_progress(
                    self.current_batch, self.current_total_batches
                )
                self._set_overall_progress(
                    event.get("overall_fraction")
                )
                self.samples_var.set(
                    f"Training samples {event.get('samples_seen', '—')}/"
                    f"{event.get('total_samples', '—')} in epoch "
                    f"{self.current_epoch}"
                )
                self.graph_dirty = True
                return

            if event_type == "validation_started":
                final = bool(event.get("final"))
                epoch_number = int(event.get("epoch") or self.current_epoch)
                title = (
                    "Final validation"
                    if final
                    else f"Validating epoch {epoch_number}"
                )
                self._set_status(title, "validating")
                self.phase_title_var.set(f"Current step: {title}")
                self.current_total_batches = int(
                    event.get("total_batches") or 0
                )
                self._set_phase_progress(
                    0, self.current_total_batches, "Starting validation…"
                )
                self._set_overall_progress(
                    event.get("overall_fraction")
                )
                self._set_metric(
                    "validation_accuracy",
                    _format_percent(self.validation_accuracy)
                    if self.validation_accuracy is not None
                    else "…",
                    "Running validation average",
                )
                self.samples_var.set(
                    f"Validation samples 0/{event.get('total_samples', '—')}"
                )
                return

            if event_type == "validation_batch":
                self.validation_accuracy = event.get(
                    "validation_accuracy"
                )
                self.validation_loss = event.get("validation_loss")
                batch_number = int(event.get("batch") or 0)
                total_batches = int(event.get("total_batches") or 0)
                loss_text = (
                    "—"
                    if self.validation_loss is None
                    else f"{float(self.validation_loss):.4f}"
                )
                self._set_metric(
                    "validation_accuracy",
                    _format_percent(self.validation_accuracy),
                    f"Loss {loss_text} (running)",
                )
                self._set_phase_progress(batch_number, total_batches)
                self._set_overall_progress(
                    event.get("overall_fraction")
                )
                self.samples_var.set(
                    f"Validation samples "
                    f"{event.get('samples_seen', '—')}/"
                    f"{event.get('total_samples', '—')}"
                )
                if not event.get("final"):
                    self.live_validation = (
                        float(event.get("epoch") or self.current_epoch),
                        self.validation_accuracy,
                    )
                    self.graph_dirty = True
                return

            if event_type == "epoch_completed":
                metrics = dict(event.get("metrics") or {})
                epoch_number = int(metrics.get("epoch") or event.get("epoch") or 0)
                existing = {
                    int(item.get("epoch") or 0): index
                    for index, item in enumerate(self.history)
                }
                if epoch_number in existing:
                    self.history[existing[epoch_number]] = metrics
                elif epoch_number:
                    self.history.append(metrics)
                self.history.sort(
                    key=lambda item: int(item.get("epoch") or 0)
                )
                self.current_epoch = epoch_number
                self.training_accuracy = metrics.get(
                    "training_accuracy"
                )
                self.training_loss = metrics.get("training_loss")
                self.validation_accuracy = metrics.get(
                    "validation_accuracy"
                )
                self.validation_loss = metrics.get(
                    "validation_loss"
                )
                self.best_accuracy = event.get("best_accuracy")
                if self.best_accuracy is not None and float(self.best_accuracy) < 0:
                    self.best_accuracy = None
                self.best_epoch = event.get("best_epoch")
                self.learning_rate = float(
                    metrics.get("learning_rate") or self.learning_rate
                )
                self.live_train = None
                self.live_validation = None

                self._set_metric(
                    "epoch",
                    f"{epoch_number}/{self.total_epochs}",
                    "Epoch complete",
                )
                self._set_metric(
                    "training_accuracy",
                    _format_percent(self.training_accuracy),
                    f"Loss {float(self.training_loss or 0.0):.4f}",
                )
                self._set_metric(
                    "validation_accuracy",
                    _format_percent(self.validation_accuracy),
                    f"Loss {float(self.validation_loss or 0.0):.4f}",
                )
                self._set_metric(
                    "best_accuracy",
                    _format_percent(self.best_accuracy),
                    (
                        f"Epoch {self.best_epoch}"
                        if self.best_epoch
                        else "No checkpoint yet"
                    ),
                )
                self._set_detail(
                    "learning_rate", f"{self.learning_rate:g}"
                )
                if self.best_epoch:
                    self._set_detail(
                        "checkpoint", f"Epoch {self.best_epoch}"
                    )
                self._set_phase_progress(1, 1, "Epoch complete · 100.0%")
                self._set_overall_progress(
                    event.get("overall_fraction")
                )
                self.graph_dirty = True
                return

            if event_type == "checkpoint_saved":
                self.best_accuracy = event.get("best_accuracy")
                self.best_epoch = event.get("best_epoch")
                self._set_metric(
                    "best_accuracy",
                    _format_percent(self.best_accuracy),
                    f"Epoch {self.best_epoch} · saved",
                )
                self._set_detail(
                    "checkpoint", f"Epoch {self.best_epoch} · saved"
                )
                self.samples_var.set(
                    f"Saved best model: {event.get('output_model', '')}"
                )
                self.graph_dirty = True
                return

            if event_type == "completed":
                summary = event.get("summary") or {}
                self.best_accuracy = summary.get(
                    "best_accuracy", self.best_accuracy
                )
                self.best_epoch = summary.get(
                    "best_epoch", self.best_epoch
                )
                self.validation_accuracy = summary.get(
                    "final_accuracy", self.validation_accuracy
                )
                self._set_metric(
                    "validation_accuracy",
                    _format_percent(self.validation_accuracy),
                    "Final best-checkpoint validation",
                )
                self._set_metric(
                    "best_accuracy",
                    _format_percent(self.best_accuracy),
                    f"Epoch {self.best_epoch} · saved",
                )
                self._set_overall_progress(1.0)
                self._set_phase_progress(1, 1, "Complete · 100.0%")
                self.graph_dirty = True
                return

            if event_type == "worker_completed":
                self._finish_run("completed", event)
            elif event_type == "worker_cancelled":
                self._finish_run("cancelled", event)
            elif event_type == "worker_failed":
                self._finish_run("failed", event)

        def _finish_run(self, kind, event):
            self.running = False
            self.worker = None
            self._set_phase_indeterminate(False)
            self._set_controls_running(False)
            self.start_button.configure(text="▶  Train again")
            self.stop_button.configure(state="disabled")

            if kind == "completed":
                summary = event.get("summary") or {}
                self.exit_code = 0
                self._set_status("Training complete", "complete")
                self.phase_title_var.set("Current step: Complete")
                self._set_overall_progress(1.0)
                self._set_phase_progress(1, 1, "Complete · 100.0%")
                self.samples_var.set(
                    f"Model saved to {summary.get('output_model', '—')}"
                )
                elapsed = summary.get("elapsed_seconds")
                if elapsed is not None:
                    self.elapsed_var.set(
                        f"Elapsed {_format_duration(elapsed)}"
                    )
                self.eta_var.set("ETA 0:00")
                self._append_log(
                    "Dashboard: training completed successfully."
                )
            elif kind == "cancelled":
                self.exit_code = 130
                self._set_status("Training cancelled", "cancelled")
                self.phase_title_var.set("Current step: Cancelled")
                self.phase_text_var.set("Training stopped")
                self.samples_var.set(
                    "Any best checkpoint saved before cancellation remains "
                    "on disk."
                )
                self.eta_var.set("ETA —")
                self._append_log("Dashboard: training cancelled.")
            else:
                self.exit_code = 1
                self._set_status("Training failed", "failed")
                self.phase_title_var.set("Current step: Failed")
                self.phase_text_var.set("See the training log")
                self.samples_var.set(str(event.get("message", "Training failed")))
                self.eta_var.set("ETA —")
                message_text = str(
                    event.get("message") or "Training failed."
                )
                traceback_text = str(event.get("traceback") or "")
                self._append_log(f"Dashboard error: {message_text}")
                if traceback_text:
                    self._append_log(traceback_text)
                if not self.close_when_done:
                    messagebox.showerror(
                        "CNN Trainer",
                        "Training failed. The detailed error is in the "
                        "training log.\n\n" + message_text,
                    )

            self.graph_dirty = True
            if self.close_when_done:
                self._destroy()

        # ----------------------------------------------------------
        # Live graph
        # ----------------------------------------------------------
        def _on_graph_configure(self, _event=None):
            self.graph_dirty = True

        def _draw_series(
            self,
            points,
            colour,
            plot_rect,
            max_epoch,
            *,
            dashed_from_last=False,
            live_point=None,
        ):
            canvas = self.graph_canvas
            coordinates = []
            xy_points = []
            for epoch_value, accuracy in points:
                if accuracy is None:
                    continue
                x, y = _accuracy_graph_xy(
                    epoch_value, accuracy, plot_rect, max_epoch
                )
                xy_points.append((x, y, float(accuracy)))
                coordinates.extend((x, y))

            if len(xy_points) > 1:
                canvas.create_line(
                    *coordinates,
                    fill=colour,
                    width=3,
                    capstyle="round",
                    joinstyle="round",
                )
            for x, y, _accuracy in xy_points:
                canvas.create_oval(
                    x - 4,
                    y - 4,
                    x + 4,
                    y + 4,
                    fill="#0b111a",
                    outline=colour,
                    width=2,
                )

            if live_point is not None and live_point[1] is not None:
                live_x, live_y = _accuracy_graph_xy(
                    live_point[0], live_point[1], plot_rect, max_epoch
                )
                if dashed_from_last and xy_points:
                    last_x, last_y, _ = xy_points[-1]
                    canvas.create_line(
                        last_x,
                        last_y,
                        live_x,
                        live_y,
                        fill=colour,
                        width=2,
                        dash=(5, 4),
                    )
                canvas.create_oval(
                    live_x - 6,
                    live_y - 6,
                    live_x + 6,
                    live_y + 6,
                    fill="",
                    outline=colour,
                    width=2,
                )
                canvas.create_oval(
                    live_x - 2,
                    live_y - 2,
                    live_x + 2,
                    live_y + 2,
                    fill=colour,
                    outline=colour,
                )
            return xy_points

        def _draw_graph(self):
            if self.destroyed:
                return
            canvas = self.graph_canvas
            try:
                width = int(canvas.winfo_width())
                height = int(canvas.winfo_height())
            except tk.TclError:
                return
            if width < 220 or height < 180:
                return

            canvas.delete("all")
            left = 58
            top = 24
            right = width - 22
            bottom = height - 42
            plot_rect = (left, top, right, bottom)
            history_epochs = [
                int(item.get("epoch") or 0) for item in self.history
            ]
            max_epoch = max(
                [self.total_epochs, self.current_epoch, 1]
                + history_epochs
            )

            # Horizontal percentage grid.
            for percent in (0, 25, 50, 75, 100):
                _x, y = _accuracy_graph_xy(
                    0, percent / 100.0, plot_rect, max_epoch
                )
                canvas.create_line(
                    left,
                    y,
                    right,
                    y,
                    fill="#243144" if percent else "#3b4d66",
                    width=1,
                )
                canvas.create_text(
                    left - 9,
                    y,
                    text=f"{percent}%",
                    fill=colours["muted"],
                    font=("Segoe UI", 8),
                    anchor="e",
                )

            # Epoch ticks; keep the final configured epoch visible.
            tick_step = max(1, int(math.ceil(max_epoch / 10)))
            tick_epochs = list(range(0, max_epoch + 1, tick_step))
            if tick_epochs[-1] != max_epoch:
                tick_epochs.append(max_epoch)
            for epoch_value in tick_epochs:
                x, _y = _accuracy_graph_xy(
                    epoch_value, 0, plot_rect, max_epoch
                )
                if epoch_value:
                    canvas.create_line(
                        x,
                        top,
                        x,
                        bottom,
                        fill="#182334",
                        width=1,
                    )
                canvas.create_line(
                    x, bottom, x, bottom + 5, fill="#3b4d66", width=1
                )
                canvas.create_text(
                    x,
                    bottom + 17,
                    text=str(epoch_value),
                    fill=colours["muted"],
                    font=("Segoe UI", 8),
                    anchor="n",
                )

            canvas.create_line(
                left, top, left, bottom, fill="#3b4d66", width=1
            )
            canvas.create_line(
                left, bottom, right, bottom, fill="#3b4d66", width=1
            )
            canvas.create_text(
                right,
                height - 5,
                text="Epoch",
                fill=colours["faint"],
                font=("Segoe UI", 8),
                anchor="se",
            )
            canvas.create_text(
                left,
                7,
                text="Accuracy",
                fill=colours["faint"],
                font=("Segoe UI", 8, "bold"),
                anchor="nw",
            )

            train_points = [
                (
                    int(item.get("epoch") or 0),
                    item.get("training_accuracy"),
                )
                for item in self.history
            ]
            validation_points = [
                (
                    int(item.get("epoch") or 0),
                    item.get("validation_accuracy"),
                )
                for item in self.history
            ]
            train_xy = self._draw_series(
                train_points,
                colours["train"],
                plot_rect,
                max_epoch,
                dashed_from_last=True,
                live_point=self.live_train,
            )
            validation_xy = self._draw_series(
                validation_points,
                colours["validation"],
                plot_rect,
                max_epoch,
                dashed_from_last=True,
                live_point=self.live_validation,
            )

            if validation_xy:
                best_x, best_y, best_value = max(
                    validation_xy, key=lambda item: item[2]
                )
                canvas.create_oval(
                    best_x - 9,
                    best_y - 9,
                    best_x + 9,
                    best_y + 9,
                    fill="",
                    outline=colours["green"],
                    width=2,
                )
                label_y = best_y + 17 if best_y < top + 24 else best_y - 17
                canvas.create_text(
                    min(max(best_x, left + 36), right - 36),
                    label_y,
                    text=f"Best {best_value * 100:.1f}%",
                    fill=colours["green"],
                    font=("Segoe UI", 8, "bold"),
                    anchor="center",
                )

            if not train_xy and not validation_xy and not self.live_train:
                canvas.create_text(
                    (left + right) / 2,
                    (top + bottom) / 2,
                    text=(
                        "Accuracy curves will appear here while training "
                        "runs"
                    ),
                    fill=colours["faint"],
                    font=("Segoe UI", 11),
                    anchor="center",
                )
            self.graph_dirty = False

    try:
        app = TrainerDashboard(root, initial_args)
    except Exception as exc:
        try:
            root.destroy()
        except Exception:
            pass
        raise TrainerUIUnavailableError(
            f"the dashboard could not be built ({exc})"
        ) from exc

    root.mainloop()
    return int(app.exit_code)


def main(argv=None):
    """Command-line entry point for the dashboard or terminal trainer."""
    args = parse_args(argv)

    if bool(getattr(args, "ui", True)):
        try:
            return int(run_training_ui(args))
        except TrainerUIUnavailableError as exc:
            print(f"CNN trainer dashboard unavailable: {exc}", file=sys.stderr)
            print(
                "Falling back to the terminal trainer.",
                file=sys.stderr,
            )

    try:
        train_model(args)
    except (TrainingCancelled, KeyboardInterrupt):
        print("\nTraining cancelled.")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
