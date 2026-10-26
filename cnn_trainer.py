
from __future__ import annotations

import argparse
import csv
import random
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
# Preferred input is board_debug/dataset.csv created by board-vision.py.
# The CSV contains the manual piece label, light/dark/highlighted square
# type, game ID, and whole-game train/validation/test split. A legacy flat folder whose
# labels are inferred from filename prefixes is still supported.
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DATASET_DIR = BASE_DIR / "board_debug"
DEFAULT_CSV = DATASET_DIR / "dataset.csv"
OUTPUT_MODEL = BASE_DIR / "chessbotv_cnn.pth"

IMAGE_SIZE = 64
BATCH_SIZE = 32
EPOCHS = 25
LEARNING_RATE = 0.001
TRAIN_SPLIT = 0.70
VALIDATION_SPLIT = 0.15
TEST_SPLIT = 0.15
BALANCE_TOLERANCE = 2
RANDOM_SEED = 42
NORMALIZE_MEAN = (0.5, 0.5, 0.5)
NORMALIZE_STD = (0.5, 0.5, 0.5)

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

SUPPORTED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".bmp", ".webp"
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
        # Highlighted examples are trained without colour jitter: their
        # changed square colour is part of what the model must learn to
        # ignore, rather than another colour to randomly recolour.
        self.highlighted_transform = transforms.Compose([
            transforms.Resize((IMAGE_SIZE, IMAGE_SIZE)),
            transforms.RandomRotation(8),
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
            square_type = record.get("square_type")
        else:  # backwards-compatible tuple records
            image_path, class_name = record
            square_type = None

        if not self.training:
            transform = self.eval_transform
        elif square_type == "highlighted":
            transform = self.highlighted_transform
        else:
            transform = self.transform

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


def light_dark_balance(records, tolerance=BALANCE_TOLERANCE):
    """Return classes whose light/dark counts differ by more than tolerance.

    Highlighted records are intentionally excluded from this check.
    Legacy prefix-only records have no square type and therefore cannot be
    balance-checked; they simply produce no balance issues here.
    """
    counts = {
        class_name: {"light": 0, "dark": 0, "highlighted": 0}
        for class_name in PREFIX_TO_CLASS.values()
    }
    has_square_types = False
    for record in records:
        if not isinstance(record, dict):
            continue
        label = record.get("label")
        square_type = record.get("square_type")
        if label in counts and square_type in counts[label]:
            counts[label][square_type] += 1
            has_square_types = True

    if not has_square_types:
        return counts, {}

    imbalanced = {
        label: values
        for label, values in counts.items()
        if abs(values["light"] - values["dark"]) > tolerance
    }
    return counts, imbalanced


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

    Expected columns include image_path/filename, label/label_code,
    square_type, and split. The first CSV creation may contain only
    ``unsplit`` rows; split_records() handles those without changing the
    CSV on disk.
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

        square_type = (row.get("square_type") or "").strip().lower()
        if square_type not in ("light", "dark", "highlighted"):
            square_type = None

        split = (row.get("split") or "unsplit").strip().lower()
        if split in ("val", "valid"):
            split = "validation"
        if split not in ("train", "validation", "test"):
            split = "unsplit"

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
            "square_type": square_type,
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

    square_counts = defaultdict(int)
    split_counts = defaultdict(int)
    for record in records:
        square_counts[record.get("square_type") or "unknown"] += 1
        split_counts[record.get("split") or "unsplit"] += 1
    print("\nSquare types:", dict(sorted(square_counts.items())))
    print("CSV splits:", dict(sorted(split_counts.items())))
    game_ids = {record.get("game_id") for record in records if record.get("game_id")}
    print(f"Game IDs: {len(game_ids)}")
    return records


def _split_target_counts(total):
    """Exact integer 70/15/15 target counts for a temporary split."""
    quotas = [
        ("train", total * TRAIN_SPLIT),
        ("validation", total * VALIDATION_SPLIT),
        ("test", total * TEST_SPLIT),
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


def _record_game_id(record):
    """Explicit game/board grouping ID for a trainer record, if present."""
    if not isinstance(record, dict):
        return None
    for field in ("game_id", "board_id", "source_image"):
        value = str(record.get(field, "") or "").strip()
        if value:
            return value
    return None


def _game_split_target_counts(total_games):
    """Whole-game split counts matching board-vision's small-game rules."""
    counts = _split_target_counts(max(0, int(total_games)))
    if total_games >= 2 and counts["validation"] == 0 and counts["train"] > 1:
        counts["train"] -= 1
        counts["validation"] += 1
    if total_games >= 3 and counts["test"] == 0:
        donor = "train" if counts["train"] > 1 else "validation"
        if counts[donor] > 1:
            counts[donor] -= 1
            counts["test"] += 1
    return counts


def _internal_game_split(records):
    """Temporary whole-game split, or None when fewer than 3 games exist."""
    groups = defaultdict(list)
    for record in records:
        game_id = _record_game_id(record)
        if game_id is None:
            return None
        groups[game_id].append(record)
    if len(groups) < 3:
        return None

    ordered_game_ids = sorted(
        groups,
        key=lambda game_id: (
            random.Random(f"{RANDOM_SEED}:{game_id}").random(),
            game_id,
        ),
    )
    targets = _game_split_target_counts(len(ordered_game_ids))
    split_records_by_name = {"train": [], "validation": [], "test": []}
    cursor = 0
    for split_name in ("train", "validation", "test"):
        count = targets[split_name]
        for game_id in ordered_game_ids[cursor:cursor + count]:
            split_records_by_name[split_name].extend(groups[game_id])
        cursor += count

    rng = random.Random(RANDOM_SEED)
    for split in split_records_by_name.values():
        rng.shuffle(split)
    return (
        split_records_by_name["train"],
        split_records_by_name["validation"],
        split_records_by_name["test"],
    )


def _internal_temporary_split(records):
    """Prefer whole-game temporary splits; use image split only if needed."""
    game_split = _internal_game_split(records)
    if game_split is not None:
        return game_split
    return _internal_stratified_split(records)


def _internal_stratified_split(records):
    """Temporary exact-count 70/15/15 split for unsplit/legacy records.

    Allocations start from each (label, square_type) group's proportional
    floor, then remainders are assigned so the global train/validation/test
    totals hit the requested percentages as exactly as integer counts allow.
    """
    groups = defaultdict(list)
    for record in records:
        if isinstance(record, dict):
            key = (record["label"], record.get("square_type") or "")
        else:
            key = (record[1], "")
        groups[key].append(record)

    split_names = ("train", "validation", "test")
    fractions = {
        "train": TRAIN_SPLIT,
        "validation": VALIDATION_SPLIT,
        "test": TEST_SPLIT,
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
            allocations[key]["test"] += count

    split_records_by_name = {name: [] for name in split_names}
    rng = random.Random(RANDOM_SEED)
    for key in sorted(groups):
        group = list(groups[key])
        random.Random(f"{RANDOM_SEED}:{key[0]}:{key[1]}").shuffle(group)
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
        split_records_by_name["test"],
    )


def split_records(records):
    """Use CSV splits when present; otherwise create a temporary split."""
    assigned = {"train": [], "validation": [], "test": []}
    unsplit = []
    for record in records:
        split = record.get("split") if isinstance(record, dict) else None
        if split in assigned:
            assigned[split].append(record)
        else:
            unsplit.append(record)

    if unsplit:
        extra_train, extra_val, extra_test = _internal_temporary_split(unsplit)
        assigned["train"].extend(extra_train)
        assigned["validation"].extend(extra_val)
        assigned["test"].extend(extra_test)
        print(
            "Some/all rows were 'unsplit'; using a temporary whole-game "
            "70/15/15 split when at least 3 game IDs are available "
            "(otherwise an image-level fallback) without changing the CSV."
        )

    if not assigned["train"]:
        raise RuntimeError("No training images after applying dataset splits")
    if not assigned["validation"] or not assigned["test"]:
        print("CSV split had an empty validation/test set; re-splitting temporarily.")
        return _internal_temporary_split(records)
    return assigned["train"], assigned["validation"], assigned["test"]


def filter_highlighted_records(records, highlighted_mode="include"):
    if highlighted_mode == "include":
        return records
    highlighted = [
        record for record in records
        if isinstance(record, dict) and record.get("square_type") == "highlighted"
    ]
    if highlighted_mode == "only":
        return highlighted
    if highlighted_mode == "exclude":
        return [
            record for record in records
            if not (
                isinstance(record, dict)
                and record.get("square_type") == "highlighted"
            )
        ]
    raise ValueError(f"Unsupported highlighted mode: {highlighted_mode}")


def discover_images(dataset_dir=DATASET_DIR):
    dataset_dir = Path(dataset_dir)
    if not dataset_dir.exists():
        raise FileNotFoundError(
            f"Dataset folder does not exist: {dataset_dir}\n"
            "Pass --dataset-dir or edit DATASET_DIR near the top of this script."
        )

    records = []
    skipped_unknown = []
    skipped_bad = []

    files = sorted(
        p for p in dataset_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )

    print(f"Scanning {len(files)} image files...")

    for path in tqdm(files, desc="Reading dataset"):
        # Take the portion before the first underscore.
        prefix = path.stem.split("_", 1)[0].lower()

        if prefix not in PREFIX_TO_CLASS:
            skipped_unknown.append(path.name)
            continue

        if not _verify_image(path):
            skipped_bad.append(path.name)
            continue

        parts = path.stem.lower().split("_")
        square_type = parts[1] if len(parts) > 1 and parts[1] in (
            "light", "dark", "highlighted"
        ) else None
        records.append({
            "path": path,
            "label": PREFIX_TO_CLASS[prefix],
            "label_code": prefix,
            "square_type": square_type,
            "split": "unsplit",
        })

    print(f"\nUsable images: {len(records)}")
    print(f"Unknown filename prefixes: {len(skipped_unknown)}")
    print(f"Unreadable/corrupt images: {len(skipped_bad)}")

    if skipped_unknown:
        print("Unknown examples:", skipped_unknown[:10])

    if skipped_bad:
        print("Unreadable examples:", skipped_bad[:10])

    if not records:
        raise RuntimeError(
            "No usable images found. Check DATASET_DIR, file extensions "
            "and PREFIX_TO_CLASS."
        )

    counts = _print_record_summary(records)

    # The temporary split needs at least two examples in each present class.
    too_small = {
        name: count for name, count in counts.items() if count < 2
    }
    if too_small:
        raise RuntimeError(
            "Each class needs at least two images for a stratified split. "
            f"Classes with too few images: {too_small}"
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


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Train the ChessBotV square CNN")
    parser.add_argument(
        "--dataset-dir", type=Path, default=DATASET_DIR,
        help="Folder containing labelled square images",
    )
    parser.add_argument(
        "--csv", type=Path, default=None,
        help="board-vision dataset.csv (default: <dataset-dir>/dataset.csv if present)",
    )
    parser.add_argument(
        "--output", type=Path, default=OUTPUT_MODEL,
        help="Output .pth model path",
    )
    parser.add_argument("--epochs", type=int, default=EPOCHS)
    parser.add_argument(
        "--highlighted-mode",
        choices=("include", "exclude", "only"),
        default="include",
        help="How highlighted-square examples participate in this run",
    )
    parser.add_argument(
        "--allow-imbalance", action="store_true",
        help="Train even if a class light/dark counts differ by more than 2",
    )
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

    csv_path = Path(args.csv) if args.csv is not None else dataset_dir / "dataset.csv"
    if csv_path.is_file():
        records = load_csv_records(csv_path, dataset_dir)
    elif args.csv is not None:
        raise FileNotFoundError(f"Dataset CSV does not exist: {csv_path}")
    else:
        print("No dataset.csv found; falling back to filename-prefix discovery.")
        records = discover_images(dataset_dir)

    _balance_counts, imbalanced = light_dark_balance(records)
    if imbalanced:
        details = "; ".join(
            f"{label}: light={values['light']}, dark={values['dark']}"
            for label, values in sorted(imbalanced.items())
        )
        message = (
            "Light/dark counts for each class must differ by at most "
            f"{BALANCE_TOLERANCE} (highlighted excluded). Imbalanced: {details}"
        )
        if not args.allow_imbalance:
            raise RuntimeError(message)
        print("WARNING:", message)

    records = filter_highlighted_records(records, args.highlighted_mode)
    if not records:
        raise RuntimeError(
            f"No records remain with --highlighted-mode={args.highlighted_mode}"
        )

    labels = [record["label"] for record in records]
    train_records, val_records, test_records = split_records(records)

    class_names = sorted(set(labels))
    class_to_idx = {
        class_name: index
        for index, class_name in enumerate(class_names)
    }

    print(f"\nTraining images:   {len(train_records)}")
    print(f"Validation images: {len(val_records)}")
    print(f"Test images:       {len(test_records)}")
    print(f"Classes: {len(class_names)}")

    train_dataset = ChessImageDataset(
        train_records, class_to_idx, training=True
    )
    val_dataset = ChessImageDataset(
        val_records, class_to_idx, training=False
    )
    test_dataset = ChessImageDataset(
        test_records, class_to_idx, training=False
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

    test_loader = DataLoader(
        test_dataset,
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
                    "square_types": ["light", "dark", "highlighted"],
                    "highlighted_mode": args.highlighted_mode,
                },
                output_model,
            )

            print(f"  Saved new best model: {output_model}")

    # Evaluate the best validation checkpoint on the held-out test split.
    checkpoint = torch.load(
        output_model, map_location=device, weights_only=True
    )
    model.load_state_dict(checkpoint["model_state_dict"])

    _, final_accuracy, true_labels, predicted_labels = evaluate(
        model, test_loader, device, criterion
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
    print(f"Held-out test accuracy: {final_accuracy:.2%}")
    print(f"Model saved to: {output_model.resolve()}")
    print("=" * 60)

    print("\nPer-class test report:")
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
