
from __future__ import annotations

import argparse
import csv
import hashlib
import random
import re
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


def load_csv_records(csv_path, dataset_dir=DATASET_DIR):
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
    for row in tqdm(rows, desc="Reading dataset CSV"):
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


def deduplicate_records(records, mode="exact-near"):
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

    for record in tqdm(ordered, desc="Checking duplicate images"):
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


def discover_images(dataset_dir=DATASET_DIR):
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
        for path, label, split in tqdm(candidates, desc="Reading class folders"):
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
    for path in tqdm(files, desc="Reading legacy flat dataset"):
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


def evaluate(model, loader, device, criterion):
    model.eval()

    total_loss = 0.0
    total_correct = 0
    total_samples = 0
    all_predictions = []
    all_labels = []

    with torch.inference_mode():
        for images, labels in loader:
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
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    dataset_dir = Path(args.dataset_dir)
    output_model = Path(args.output)
    epochs = max(1, int(args.epochs))

    set_seed(RANDOM_SEED)

    # Avoid excessive CPU thread contention on smaller systems.
    torch.set_num_threads(min(8, torch.get_num_threads()))

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"\nTraining device: {device}")
    if device.type == "cpu":
        print("CPU training enabled. This is expected on your current setup.")

    records_before_dedupe = 0
    duplicate_reports = []
    dataset_layout = "training_validation_folders"
    if args.csv is not None:
        dataset_layout = "csv"
        csv_path = Path(args.csv)
        if not csv_path.is_file():
            raise FileNotFoundError(f"Dataset CSV does not exist: {csv_path}")
        print(f"Using explicitly supplied legacy CSV: {csv_path}")
        records = load_csv_records(csv_path, dataset_dir)
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
        records = load_csv_records(DEFAULT_CSV, LEGACY_DATASET_DIR)
    else:
        print(
            "Using the physical training/validation class folders: "
            f"{dataset_dir}"
        )
        records = discover_images(dataset_dir)

    records_before_dedupe = len(records)
    records, duplicate_reports = deduplicate_records(records, args.dedupe)
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
    elif args.dedupe != "off":
        print(
            "\nNo exact/near duplicate images found before "
            "training/validation use."
        )
    if not records:
        raise RuntimeError("No unique images remain after duplicate removal")

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

    train_dataset = ChessImageDataset(
        train_records, class_to_idx, training=True
    )
    val_dataset = ChessImageDataset(
        val_records, class_to_idx, training=False
    )

    train_loader = DataLoader(
        train_dataset,
        batch_size=BATCH_SIZE,
        shuffle=True,
        num_workers=NUM_WORKERS,
        pin_memory=(device.type == "cuda"),
    )

    val_loader = DataLoader(
        val_dataset,
        batch_size=BATCH_SIZE,
        shuffle=False,
        num_workers=NUM_WORKERS,
        pin_memory=(device.type == "cuda"),
    )


    model = ChessCNN(num_classes=len(class_names)).to(device)
    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=LEARNING_RATE,
        weight_decay=0.0005,
    )

    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer,
        mode="max",
        factor=0.5,
        patience=3,
    )

    best_accuracy = -1.0

    print("\nStarting training...\n")

    for epoch in range(1, epochs + 1):
        model.train()
        running_loss = 0.0
        correct = 0
        total = 0

        progress = tqdm(
            train_loader,
            desc=f"Epoch {epoch:02d}/{epochs}",
            leave=True,
        )

        for images, labels_batch in progress:
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

            progress.set_postfix(
                loss=f"{loss.item():.4f}",
                accuracy=f"{correct / max(total, 1):.1%}",
            )

        train_loss = running_loss / max(total, 1)
        train_accuracy = correct / max(total, 1)

        val_loss, val_accuracy, _, _ = evaluate(
            model, val_loader, device, criterion
        )

        scheduler.step(val_accuracy)

        print(
            f"\nEpoch {epoch:02d}: "
            f"train_loss={train_loss:.4f}, "
            f"train_acc={train_accuracy:.2%}, "
            f"val_loss={val_loss:.4f}, "
            f"val_acc={val_accuracy:.2%}, "
            f"lr={optimizer.param_groups[0]['lr']:.6g}"
        )

        if val_accuracy > best_accuracy:
            best_accuracy = val_accuracy

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
                    "dedupe_mode": args.dedupe,
                    "duplicate_images_removed": len(duplicate_reports),
                    "dataset_images_before_dedupe": records_before_dedupe,
                },
                output_model,
            )

            print(f"  Saved new best model: {output_model}")

    # Validation is the final evaluation set; there is no separate test set.
    # Reload the best checkpoint selected by validation accuracy and report
    # its per-class validation performance.
    checkpoint = torch.load(
        output_model, map_location=device, weights_only=True
    )
    model.load_state_dict(checkpoint["model_state_dict"])

    _, final_accuracy, true_labels, predicted_labels = evaluate(
        model, val_loader, device, criterion
    )

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

    print("\nPer-class validation report:")
    print(
        classification_report(
            true_labels,
            predicted_labels,
            labels=list(range(len(class_names))),
            target_names=target_names,
            zero_division=0,
        )
    )


if __name__ == "__main__":
    main()
