
# piece_recognition.py
from pathlib import Path

import cv2
import numpy as np
import chess

# ---------- CONFIGURATION ----------
SQUARE_DIR = Path("board_debug")

# Reference sprites come from the same chess.com piece sets the main app
# uses: pieces/chesscom/<style>/ (change PIECE_STYLE to use another set).
PIECE_STYLE = "default"
PIECE_DIR = Path("pieces") / "chesscom" / PIECE_STYLE

# Adjust this if your sprites are unusually large or small.
PIECE_SCALE = 0.82

# Lower = stricter matching. Tune after testing real screenshots.
MIN_CONFIDENCE = 0.35

# FEN symbol -> sprite file stem in PIECE_DIR: colour letter ('w'/'b')
# plus piece letter, e.g. "wr" = white rook -> pieces/chesscom/default/wr.png.
ASSETS = {
    "K": "wk",
    "Q": "wq",
    "R": "wr",
    "B": "wb",
    "N": "wn",
    "P": "wp",
    "k": "bk",
    "q": "bq",
    "r": "br",
    "b": "bb",
    "n": "bn",
    "p": "bp",
}


def load_sprite(path):
    """Load a transparent PNG as BGRA."""
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)

    if img is None:
        raise FileNotFoundError(f"Cannot load sprite: {path}")

    if img.ndim != 3 or img.shape[2] != 4:
        raise ValueError(
            f"{path} needs an alpha channel (transparent PNG)."
        )

    return img


def render_template(sprite, square, scale=PIECE_SCALE):
    """Render a piece centered on a real square background."""
    h, w = square.shape[:2]

    # Estimate the square's background using its corner pixels.
    corners = np.array([
        square[2, 2],
        square[2, w - 3],
        square[h - 3, 2],
        square[h - 3, w - 3],
    ])
    bg = np.median(corners, axis=0).astype(np.uint8)

    canvas = np.empty_like(square)
    canvas[:] = bg

    sh, sw = sprite.shape[:2]
    target_w = max(1, int(w * scale))
    target_h = max(1, int(h * scale))

    # Preserve the sprite's aspect ratio.
    ratio = min(target_w / sw, target_h / sh)
    nw = max(1, int(sw * ratio))
    nh = max(1, int(sh * ratio))

    sprite = cv2.resize(
        sprite, (nw, nh), interpolation=cv2.INTER_AREA
    )

    x = (w - nw) // 2
    y = (h - nh) // 2

    rgb = sprite[:, :, :3].astype(np.float32)
    alpha = sprite[:, :, 3:4].astype(np.float32) / 255.0

    roi = canvas[y:y + nh, x:x + nw].astype(np.float32)
    blended = rgb * alpha + roi * (1.0 - alpha)

    canvas[y:y + nh, x:x + nw] = np.clip(
        blended, 0, 255
    ).astype(np.uint8)

    return canvas


def difference(actual, template):
    """Lower is better; normalize the error to a 0–1 range."""
    a = actual.astype(np.float32)
    b = template.astype(np.float32)

    # A slight blur reduces sensitivity to tiny rendering differences.
    a = cv2.GaussianBlur(a, (3, 3), 0)
    b = cv2.GaussianBlur(b, (3, 3), 0)

    return float(np.mean(np.abs(a - b)) / 255.0)


def load_assets():
    sprites = {}

    for symbol, code in ASSETS.items():
        path = PIECE_DIR / f"{code}.png"
        sprites[symbol] = load_sprite(path)

    return sprites


def classify_square(square, sprites):
    """
    Return (symbol, confidence, scores).
    '.' means empty.
    """
    h, w = square.shape[:2]

    # Empty squares are candidates too.
    corners = np.array([
        square[2, 2],
        square[2, w - 3],
        square[h - 3, 2],
        square[h - 3, w - 3],
    ])
    bg = np.median(corners, axis=0).astype(np.uint8)

    empty_template = np.empty_like(square)
    empty_template[:] = bg

    candidates = {
        ".": difference(square, empty_template)
    }

    for symbol, sprite in sprites.items():
        template = render_template(sprite, square)
        candidates[symbol] = difference(square, template)

    ranked = sorted(candidates.items(), key=lambda item: item[1])
    symbol, best_error = ranked[0]
    second_error = ranked[1][1]

    # Relative separation from the runner-up.
    confidence = max(
        0.0,
        min(1.0, (second_error - best_error) /
            max(second_error, 1e-6))
    )

    # This is a confidence heuristic, not a calibrated probability.
    if symbol != "." and confidence < MIN_CONFIDENCE:
        return "?", confidence, ranked

    return symbol, confidence, ranked


def recognize_board():
    sprites = load_assets()
    grid = []
    review = []

    for row in range(8):
        grid_row = []

        for col in range(8):
            path = SQUARE_DIR / f"r{row}_c{col}.png"
            square = cv2.imread(str(path))

            if square is None:
                raise FileNotFoundError(
                    f"Cannot load square crop: {path}"
                )

            symbol, confidence, ranked = classify_square(
                square, sprites
            )
            grid_row.append(symbol)

            print(
                f"row={row} col={col}: "
                f"{symbol} confidence={confidence:.2f}"
            )

            if symbol == "?":
                review.append((row, col, ranked[:3]))

        grid.append(grid_row)

    print("\nRecognized board (rank 8 down to rank 1):")
    for row in grid:
        print(" ".join(row))

    # Convert recognized pieces into FEN placement.
    fen_rows = []
    for row in grid:
        fen_row = ""
        empty = 0

        for symbol in row:
            if symbol in (".", "?"):
                empty += 1
            else:
                if empty:
                    fen_row += str(empty)
                    empty = 0
                fen_row += symbol

        if empty:
            fen_row += str(empty)

        fen_rows.append(fen_row)

    placement = "/".join(fen_rows)
    print("\nFEN placement:", placement)

    if review:
        print("\nSquares needing manual review:")
        for row, col, ranked in review:
            print(f"  row {row}, col {col}: {ranked}")

    return grid, placement


if __name__ == "__main__":
    grid, placement = recognize_board()
