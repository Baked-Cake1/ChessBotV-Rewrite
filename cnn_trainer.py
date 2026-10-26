
from __future__ import annotations

import random
from pathlib import Path

import numpy as np
from PIL import Image, UnidentifiedImageError

import torch
from torch import nn
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms

from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report
from tqdm import tqdm


# ============================================================
# CHESSBOTV CNN TRAINER
# Flat image dataset; labels inferred from filename prefixes.
# Designed for Windows + CPU training.
# ============================================================

# EDIT THIS PATH to point to your folder containing the images.
DATASET_DIR = Path(r"C:\Users\Blake\Documents\ChessBotV Rewrite\board_debug")

OUTPUT_MODEL = Path("chessbotv_cnn.pth")

IMAGE_SIZE = 64
BATCH_SIZE = 32
EPOCHS = 25
LEARNING_RATE = 0.001
VALIDATION_SPLIT = 0.20
RANDOM_SEED = 42

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

        if training:
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
                    mean=(0.5, 0.5, 0.5),
                    std=(0.5, 0.5, 0.5),
                ),
            ])
        else:
            self.transform = transforms.Compose([
                transforms.Resize((IMAGE_SIZE, IMAGE_SIZE)),
                transforms.ToTensor(),
                transforms.Normalize(
                    mean=(0.5, 0.5, 0.5),
                    std=(0.5, 0.5, 0.5),
                ),
            ])

    def __len__(self):
        return len(self.records)

    def __getitem__(self, index):
        image_path, class_name = self.records[index]

        with Image.open(image_path) as image:
            image = image.convert("RGB")
            image_tensor = self.transform(image)

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


def discover_images():
    if not DATASET_DIR.exists():
        raise FileNotFoundError(
            f"Dataset folder does not exist: {DATASET_DIR}\n"
            "Edit DATASET_DIR near the top of this script."
        )

    records = []
    skipped_unknown = []
    skipped_bad = []

    files = sorted(
        p for p in DATASET_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )

    print(f"Scanning {len(files)} image files...")

    for path in tqdm(files, desc="Reading dataset"):
        # Take the portion before the first underscore.
        prefix = path.stem.split("_", 1)[0].lower()

        if prefix not in PREFIX_TO_CLASS:
            skipped_unknown.append(path.name)
            continue

        try:
            with Image.open(path) as image:
                image.verify()
        except (OSError, UnidentifiedImageError, ValueError):
            skipped_bad.append(path.name)
            continue

        records.append((path, PREFIX_TO_CLASS[prefix]))

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

    counts = {}
    for _, class_name in records:
        counts[class_name] = counts.get(class_name, 0) + 1

    print("\nClass distribution:")
    for class_name in sorted(counts):
        print(f"  {class_name:15s}: {counts[class_name]}")

    missing_classes = sorted(
        set(PREFIX_TO_CLASS.values()) - set(counts)
    )
    if missing_classes:
        print("\nWARNING: No images found for:", ", ".join(missing_classes))

    # A stratified split needs at least two examples in each class.
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


def main():
    set_seed(RANDOM_SEED)

    # Avoid excessive CPU thread contention on smaller systems.
    torch.set_num_threads(min(8, torch.get_num_threads()))

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"\nTraining device: {device}")
    if device.type == "cpu":
        print("CPU training enabled. This is expected on your current setup.")

    records = discover_images()

    labels = [class_name for _, class_name in records]

    train_records, val_records = train_test_split(
        records,
        test_size=VALIDATION_SPLIT,
        random_state=RANDOM_SEED,
        stratify=labels,
    )

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

    for epoch in range(1, EPOCHS + 1):
        model.train()
        running_loss = 0.0
        correct = 0
        total = 0

        progress = tqdm(
            train_loader,
            desc=f"Epoch {epoch:02d}/{EPOCHS}",
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

            torch.save(
                {
                    "model_state_dict": model.state_dict(),
                    "class_to_idx": class_to_idx,
                    "class_names": class_names,
                    "image_size": IMAGE_SIZE,
                    "model_name": "ChessCNN",
                },
                OUTPUT_MODEL,
            )

            print(f"  Saved new best model: {OUTPUT_MODEL}")

    # Evaluate the best saved checkpoint rather than the final epoch.
    checkpoint = torch.load(
        OUTPUT_MODEL, map_location=device, weights_only=True
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
    print(f"Best validation accuracy: {final_accuracy:.2%}")
    print(f"Model saved to: {OUTPUT_MODEL.resolve()}")
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
