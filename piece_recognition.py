
# piece_recognition.py
import json
import re
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np
import chess

# ---------- CONFIGURATION ----------
SQUARE_DIR = Path("board_debug")
AUTOPLAY_DICTIONARY_PATH = Path("autoplaydictionary.json")

# Reference sprites come from the same chess.com piece sets the main app
# uses: pieces/chesscom/<style>/ (change PIECE_STYLE to use another set).
PIECE_STYLE = "default"
PIECE_DIR = Path("pieces") / "chesscom" / PIECE_STYLE

# Adjust this if your sprites are unusually large or small.
PIECE_SCALE = 0.82

# Lower = stricter matching. Tune after testing real screenshots.
MIN_CONFIDENCE = 0.35
# Even a clear winner is treated as unknown when its absolute image error
# is this large; Piece Detection can then ask the user and learn it.
MAX_BEST_ERROR = 0.30
# A supposedly blank square with more foreground than this (and a
# non-trivial blank-match error) is also unknown rather than blank.
MAX_BLANK_FOREGROUND_FRACTION = 0.10
MIN_BLANK_ERROR_FOR_FOREGROUND_CHECK = 0.03

# Chess.com light/dark square colours present in the board_debug crops.
# Those pixels are background, so they are removed before a crop is scored
# against a piece template.
SQUARE_BG_HEX = ("#ebecd0", "#739552")
# Screenshots rarely reproduce those hex values exactly: browser/display
# scaling, resampling and anti-aliased square edges shift the shade a
# little. A pixel therefore counts as square background when every channel
# is within this tolerance of either target colour.
SQUARE_BG_TOLERANCE = 24
# Neither the board nor the piece sprites legitimately contain magenta,
# so removed background pixels are painted this colour in both the crop
# and the candidate template; they then contribute no matching error.
REMOVED_BG_BGR = (255, 0, 255)

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

# Human-readable labels used by the review UI and autoplaydictionary.json.
PIECE_NAMES = {
    ".": "blank square",
    "K": "white king",
    "Q": "white queen",
    "R": "white rook",
    "B": "white bishop",
    "N": "white knight",
    "P": "white pawn",
    "k": "black king",
    "q": "black queen",
    "r": "black rook",
    "b": "black bishop",
    "n": "black knight",
    "p": "black pawn",
    "?": "unknown",
}
PIECE_CHOICES = (".", "K", "Q", "R", "B", "N", "P", "k", "q", "r", "b", "n", "p")

# Verified square crops become a growing training dataset in board_debug:
# bq_1.png, bq_2.png, blank_1.png, ... (short piece code + running number).
TRAINING_DIR = SQUARE_DIR
TRAINING_EXAMPLE_SIZE = 72
TRAINING_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
TRAINING_CODE_BY_SYMBOL = {**ASSETS, ".": "blank"}
SYMBOL_BY_TRAINING_CODE = {
    code: symbol for symbol, code in TRAINING_CODE_BY_SYMBOL.items()
}
TRAINING_FILENAME_RE = re.compile(
    r"^(" + "|".join(sorted(SYMBOL_BY_TRAINING_CODE, key=len, reverse=True)) + r")_(\d+)$"
)

SOURCE_LABELS = {
    "auto": "auto review",
    "manual": "manual review",
    "unknown": "needs review",
}

# Internal picker results that are not piece symbols.
SKIP_CHOICE = "<skip>"
CANCEL_CHOICE = "<cancel>"


def _hex_to_bgr(hex_colour):
    """Convert an RGB hex colour such as '#ebecd0' to OpenCV BGR order."""
    value = hex_colour.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"Expected a 6-digit hex colour, got {hex_colour!r}")
    red, green, blue = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return np.array((blue, green, red), dtype=np.int16)


SQUARE_BG_BGR = tuple(_hex_to_bgr(colour) for colour in SQUARE_BG_HEX)


def square_background_mask(image):
    """Return True for pixels matching either square colour (with tolerance)."""
    bgr = image[:, :, :3].astype(np.int16)
    mask = np.zeros(image.shape[:2], dtype=bool)

    for colour in SQUARE_BG_BGR:
        # Chebyshev distance: the largest absolute per-channel difference.
        channel_difference = np.max(np.abs(bgr - colour), axis=2)
        mask |= channel_difference <= SQUARE_BG_TOLERANCE

    return mask


def remove_square_background(image):
    """Replace approximate light/dark square-colour pixels with magenta."""
    cleaned = image.copy()
    cleaned[square_background_mask(cleaned)] = REMOVED_BG_BGR
    return cleaned


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
    """Lower is better; normalize the error to a 0–1 range.

    The chess.com square colours are removed from both images first, so
    harmless shade variation in the screenshot's board background does
    not count as a piece-matching error.
    """
    a = remove_square_background(actual).astype(np.float32)
    b = remove_square_background(template).astype(np.float32)

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


def training_code_for_symbol(symbol):
    """Dataset file stem for a symbol ('q' -> 'bq', '.' -> 'blank')."""
    return TRAINING_CODE_BY_SYMBOL.get(symbol)


def symbol_for_training_code(code):
    """Inverse of training_code_for_symbol ('blank' -> '.')."""
    return SYMBOL_BY_TRAINING_CODE.get(code)


def parse_training_filename(path):
    """Return (code, index) for names like bq_12.png, else None."""
    match = TRAINING_FILENAME_RE.match(Path(path).stem)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def _training_directory(directory=None):
    return Path(directory) if directory is not None else Path(TRAINING_DIR)


def next_training_path(code, suffix=".png", directory=None, reserved=None):
    """Next free <code>_<n><suffix> path, continuing existing numbering.

    Numbering is per piece code across every extension, so a second
    dataset never overwrites bq_1.* - it becomes bq_2.*, then bq_3.*, ...
    ``reserved`` (code -> last index handed out) covers allocations made
    earlier in the same run before their files necessarily exist.
    """
    if code not in SYMBOL_BY_TRAINING_CODE:
        raise ValueError(f"Unknown training code: {code!r}")
    directory = _training_directory(directory)
    suffix = suffix or ".png"
    if not suffix.startswith("."):
        suffix = "." + suffix

    highest = 0
    if reserved:
        highest = max(highest, int(reserved.get(code, 0)))
    if directory.is_dir():
        for item in directory.iterdir():
            parsed = parse_training_filename(item)
            if parsed and parsed[0] == code:
                highest = max(highest, parsed[1])

    index = highest + 1
    candidate = directory / f"{code}_{index}{suffix}"
    while candidate.exists():
        index += 1
        candidate = directory / f"{code}_{index}{suffix}"
    if reserved is not None:
        reserved[code] = index
    return candidate


def prepare_training_example(image, size=TRAINING_EXAMPLE_SIZE):
    """Normalise a crop for example comparison (resized/background-free)."""
    if image is None or getattr(image, "size", 0) == 0 or image.ndim != 3:
        return None
    interpolation = (
        cv2.INTER_AREA if max(image.shape[:2]) > size else cv2.INTER_LINEAR
    )
    resized = cv2.resize(
        image[:, :, :3], (size, size), interpolation=interpolation
    )
    cleaned = remove_square_background(resized).astype(np.float32)
    return cv2.GaussianBlur(cleaned, (3, 3), 0)


def _prepare_training_comparison(image, size=TRAINING_EXAMPLE_SIZE):
    """Return a prepared comparison array, accepting pre-prepared input."""
    if image is None or getattr(image, "size", 0) == 0:
        return None
    if image.dtype != np.uint8 and image.shape[:2] == (size, size):
        # Prepared examples are only read during scoring; avoid copying
        # every example for every square as the dataset grows.
        return image if image.dtype == np.float32 else image.astype(np.float32)
    return prepare_training_example(image, size=size)


def load_training_examples(directory=None, size=TRAINING_EXAMPLE_SIZE):
    """Load labelled crops (bq_1.png, blank_2.jpg, ...) grouped by symbol."""
    directory = _training_directory(directory)
    examples = {}
    if not directory.is_dir():
        return examples
    for path in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
        parsed = parse_training_filename(path)
        if not parsed or path.suffix.lower() not in TRAINING_IMAGE_EXTENSIONS:
            continue
        image = cv2.imread(str(path))
        if image is None:
            continue
        prepared = prepare_training_example(image, size=size)
        if prepared is None:
            continue
        symbol = symbol_for_training_code(parsed[0])
        examples.setdefault(symbol, []).append(prepared)
    return examples


def save_training_example(square, symbol, directory=None, suffix=".png"):
    """Save one verified crop as the next <code>_<n> training example."""
    code = training_code_for_symbol(symbol)
    if code is None:
        raise ValueError(f"Cannot save an unresolved symbol as training data: {symbol!r}")
    directory = _training_directory(directory)
    directory.mkdir(parents=True, exist_ok=True)
    destination = next_training_path(code, suffix=suffix, directory=directory)
    if not cv2.imwrite(str(destination), square):
        raise OSError(f"Could not write training example: {destination}")
    return destination


def add_training_example(square, symbol, training_examples=None,
                         directory=None, suffix=".png"):
    """Save a verified crop and immediately add it to the live example set."""
    destination = save_training_example(
        square, symbol, directory=directory, suffix=suffix
    )
    if training_examples is not None:
        prepared = prepare_training_example(square)
        if prepared is not None:
            training_examples.setdefault(symbol, []).append(prepared)
    return destination


def _training_candidate_scores(square, training_examples):
    """Mean of the best three example differences for each known label."""
    if not training_examples:
        return {}
    query = _prepare_training_comparison(square)
    if query is None:
        return {}
    scores = {}
    for symbol, examples in training_examples.items():
        if symbol not in PIECE_NAMES or symbol == "?" or not examples:
            continue
        differences = []
        for example in examples:
            prepared = _prepare_training_comparison(example)
            if prepared is None or prepared.shape != query.shape:
                continue
            differences.append(
                float(np.mean(np.abs(query - prepared)) / 255.0)
            )
        if differences:
            differences.sort()
            scores[symbol] = float(
                np.mean(differences[:min(3, len(differences))])
            )
    return scores


def export_training_images(grid, squares=None, square_paths=None,
                           training_dir=None, move=True):
    """Rename/copy verified r<row>_c<col> crops into the training dataset.

    Each known square becomes <code>_<n>.<original extension> (for example
    bq_1.png or blank_1.png), continuing numbering from examples already in
    the dataset. Unknown ('?') squares are left in place. By default the
    r*_c* files are renamed (moved); board-vision recreates them next run.
    Returns the destination paths that were written.
    """
    directory = _training_directory(training_dir)
    directory.mkdir(parents=True, exist_ok=True)
    reserved = {}
    exported = []

    for row in range(8):
        for col in range(8):
            symbol = grid[row][col]
            code = training_code_for_symbol(symbol)
            if code is None:
                continue
            if square_paths is not None:
                source = Path(square_paths[row][col])
            else:
                source = square_image_path(row, col)
            destination = next_training_path(
                code,
                suffix=source.suffix or ".png",
                directory=directory,
                reserved=reserved,
            )
            if source.exists():
                if move:
                    source.replace(destination)
                else:
                    shutil.copy2(source, destination)
            elif squares is not None:
                if not cv2.imwrite(str(destination), squares[row][col]):
                    continue
            else:
                continue
            exported.append(destination)
    return exported


def classify_square(square, sprites=None, training_examples=None):
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

    for symbol, sprite in (sprites or {}).items():
        template = render_template(sprite, square)
        candidates[symbol] = difference(square, template)

    # Blend in the growing labelled-example dataset. Real verified crops
    # usually describe the user's exact board/piece rendering better than
    # a generic sprite, so they carry most of the combined weight.
    for symbol, example_error in _training_candidate_scores(
        square, training_examples
    ).items():
        if symbol in candidates:
            candidates[symbol] = 0.65 * example_error + 0.35 * candidates[symbol]
        else:
            candidates[symbol] = example_error

    ranked = sorted(candidates.items(), key=lambda item: item[1])
    symbol, best_error = ranked[0]
    second_error = ranked[1][1] if len(ranked) > 1 else best_error + 1.0

    # Relative separation from the runner-up.
    if len(ranked) == 1:
        confidence = 1.0
    else:
        confidence = max(
            0.0,
            min(1.0, (second_error - best_error) /
                max(second_error, 1e-6))
        )

    # This is a confidence heuristic, not a calibrated probability.
    if best_error > MAX_BEST_ERROR:
        return "?", confidence, ranked

    if symbol == "." and best_error > MIN_BLANK_ERROR_FOR_FOREGROUND_CHECK:
        foreground_fraction = 1.0 - float(
            np.mean(square_background_mask(square))
        )
        if foreground_fraction > MAX_BLANK_FOREGROUND_FRACTION:
            return "?", confidence, ranked

    if symbol != "." and confidence < MIN_CONFIDENCE:
        return "?", confidence, ranked

    return symbol, confidence, ranked


def piece_name(symbol):
    """Return the human-readable label for a board symbol."""
    return PIECE_NAMES.get(symbol, "unknown")


def square_image_path(row, col):
    """Return the board_debug crop path for one square."""
    return SQUARE_DIR / f"r{row}_c{col}.png"


def square_coordinate(row, col):
    """Return algebraic notation assuming row 0 is rank 8, col 0 is file a."""
    return f"{'abcdefgh'[col]}{8 - row}"


def square_status_text(row, col, grid, sources, confidences=None):
    """Describe one square for the verification grid's hover display."""
    symbol = grid[row][col]
    source = sources[row][col]
    text = (
        f"{square_coordinate(row, col)} (row {row}, col {col}): "
        f"{piece_name(symbol)} - {SOURCE_LABELS.get(source, source)}"
    )
    if confidences is not None and source in ("auto", "unknown"):
        text += f" (confidence {confidences[row][col]:.2f})"
    return text


def fen_placement(grid):
    """Convert a recognized symbol grid into the FEN piece-placement field."""
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

    return "/".join(fen_rows)


def build_autoplay_dictionary(grid):
    """Map every square-image path to its final human-readable piece label."""
    return {
        square_image_path(row, col).as_posix(): piece_name(grid[row][col])
        for row in range(8)
        for col in range(8)
    }


def write_autoplay_dictionary(grid, path=AUTOPLAY_DICTIONARY_PATH):
    """Write the square-image -> piece dictionary as JSON and return it."""
    data = build_autoplay_dictionary(grid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return data


# ---------- REVIEW / VERIFICATION UI ----------

CHOICE_ORIGIN = (425, 118)
CHOICE_BUTTON_W = 270
CHOICE_BUTTON_H = 44
CHOICE_GAP_X = 18
CHOICE_GAP_Y = 12

GRID_CELL_SIZE = 92
GRID_HEADER_H = 96
GRID_WIDTH = GRID_CELL_SIZE * 8
GRID_HEIGHT = GRID_HEADER_H + GRID_CELL_SIZE * 8


def piece_choice_rects(origin=CHOICE_ORIGIN):
    """Return [(choice, (x, y, w, h)), ...] for the piece-picker buttons."""
    rects = []
    choices = PIECE_CHOICES + (SKIP_CHOICE,)
    for index, choice in enumerate(choices):
        x = origin[0] + (index % 2) * (CHOICE_BUTTON_W + CHOICE_GAP_X)
        y = origin[1] + (index // 2) * (CHOICE_BUTTON_H + CHOICE_GAP_Y)
        rects.append((choice, (x, y, CHOICE_BUTTON_W, CHOICE_BUTTON_H)))
    return rects


def choice_for_point(x, y, rects):
    """Return the picker choice containing (x, y), or None."""
    for choice, (rx, ry, rw, rh) in rects:
        if rx <= x < rx + rw and ry <= y < ry + rh:
            return choice
    return None


def grid_cell_at(x, y):
    """Return (row, col) for a point in the verification grid, or None."""
    if x < 0 or y < GRID_HEADER_H:
        return None
    col = int(x // GRID_CELL_SIZE)
    row = int((y - GRID_HEADER_H) // GRID_CELL_SIZE)
    if 0 <= row < 8 and 0 <= col < 8:
        return row, col
    return None


def _draw_text(canvas, text, origin, scale=0.6, colour=(255, 255, 255),
               thickness=1, outline=False):
    """Small putText wrapper, optionally with a black outline for contrast."""
    if outline:
        cv2.putText(
            canvas, text, origin, cv2.FONT_HERSHEY_SIMPLEX,
            scale, (0, 0, 0), thickness + 2, cv2.LINE_AA,
        )
    cv2.putText(
        canvas, text, origin, cv2.FONT_HERSHEY_SIMPLEX,
        scale, colour, thickness, cv2.LINE_AA,
    )


def _draw_square_preview(canvas, square, box):
    """Draw a square crop centred inside box=(x, y, w, h), keeping aspect."""
    x, y, w, h = box
    cv2.rectangle(canvas, (x, y), (x + w - 1, y + h - 1), (15, 15, 15), -1)

    image = square[:, :, :3]
    img_h, img_w = image.shape[:2]
    scale = min(w / img_w, h / img_h)
    new_w = max(1, round(img_w * scale))
    new_h = max(1, round(img_h * scale))
    interpolation = cv2.INTER_NEAREST if scale >= 1.0 else cv2.INTER_AREA
    resized = cv2.resize(image, (new_w, new_h), interpolation=interpolation)

    paste_x = x + (w - new_w) // 2
    paste_y = y + (h - new_h) // 2
    canvas[paste_y:paste_y + new_h, paste_x:paste_x + new_w] = resized
    cv2.rectangle(canvas, (x, y), (x + w - 1, y + h - 1), (180, 180, 180), 1)


def _button_label(choice):
    return "Skip / unsure" if choice == SKIP_CHOICE else piece_name(choice).title()


def choose_piece_ui(square, row, col, ranked=None, confidence=None,
                    current=None, progress=None, mode="review",
                    title="Piece review"):
    """Ask the user what piece (or blank) is in one square crop.

    Returns a symbol from PIECE_CHOICES, None to skip/leave unchanged, or
    CANCEL_CHOICE when the user presses Esc to stop reviewing.
    """
    width, height = 1020, 660
    rects = piece_choice_rects()
    state = {"mouse": (-1, -1), "selected": None}
    top_guess = ranked[0][0] if ranked else None

    def on_mouse(event, x, y, flags, param):
        state["mouse"] = (x, y)
        if event == cv2.EVENT_LBUTTONDOWN:
            choice = choice_for_point(x, y, rects)
            if choice is not None:
                state["selected"] = choice

    cv2.namedWindow(title, cv2.WINDOW_NORMAL)
    try:
        cv2.resizeWindow(title, width, height)
        cv2.setMouseCallback(title, on_mouse)

        while True:
            selected = state["selected"]
            if selected is not None:
                return None if selected == SKIP_CHOICE else selected

            canvas = np.full((height, width, 3), 38, np.uint8)
            if progress is not None:
                heading = f"Manual review {progress[0]} of {progress[1]}"
            elif mode == "edit":
                heading = "Edit this square"
            else:
                heading = "What piece is in this square?"
            _draw_text(canvas, heading, (24, 34), scale=0.72, thickness=2)
            _draw_text(
                canvas,
                f"{square_image_path(row, col).as_posix()} - "
                f"{square_coordinate(row, col)} (row {row}, col {col})",
                (24, 62), scale=0.55,
            )
            if ranked:
                guesses = ", ".join(
                    f"{piece_name(symbol)} ({error:.3f})"
                    for symbol, error in ranked[:3]
                )
                detail = f"Auto guesses: {guesses}"
                if confidence is not None:
                    detail += f" - confidence {confidence:.2f}"
            elif current is not None:
                detail = f"Current: {piece_name(current)}"
            else:
                detail = "Choose the piece shown, or Blank Square."
            _draw_text(canvas, detail, (24, 90), scale=0.5, colour=(210, 230, 255))

            _draw_square_preview(canvas, square, (28, 118, 360, 360))

            mouse_x, mouse_y = state["mouse"]
            for choice, (x, y, w, h) in rects:
                if choice == SKIP_CHOICE:
                    fill, text_colour = (90, 80, 130), (255, 255, 255)
                elif choice == ".":
                    fill, text_colour = (95, 95, 95), (255, 255, 255)
                elif choice.isupper():
                    fill, text_colour = (235, 235, 235), (0, 0, 0)
                else:
                    fill, text_colour = (28, 28, 28), (255, 255, 255)

                border, border_width = (165, 165, 165), 1
                if choice == top_guess:
                    border, border_width = (40, 220, 40), 3
                if choice == current:
                    border, border_width = (255, 255, 0), 3
                if x <= mouse_x < x + w and y <= mouse_y < y + h:
                    border, border_width = (0, 255, 255), 3

                cv2.rectangle(canvas, (x, y), (x + w - 1, y + h - 1), fill, -1)
                cv2.rectangle(
                    canvas, (x, y), (x + w - 1, y + h - 1), border, border_width
                )
                _draw_text(
                    canvas, _button_label(choice), (x + 14, y + 29),
                    scale=0.56, colour=text_colour,
                )

            _draw_text(
                canvas,
                "Click a piece, or type K/Q/R/B/N/P: Shift = white, lower-case = black, . = blank.",
                (24, 608), scale=0.46, colour=(220, 220, 220),
            )
            _draw_text(
                canvas,
                "Enter accepts the top auto guess/current piece. S skips this square. Esc stops review.",
                (24, 634), scale=0.46, colour=(220, 220, 220),
            )
            cv2.imshow(title, canvas)

            key = cv2.waitKey(20) & 0xFF
            if key == 27:
                return CANCEL_CHOICE
            if key in (13, 10):
                if top_guess in PIECE_CHOICES:
                    return top_guess
                if current in PIECE_CHOICES:
                    return current
            if 0 <= key < 128:
                char = chr(key)
                if char in ("s", "S"):
                    return None
                if char == "0":
                    return "."
                if char in PIECE_CHOICES:
                    return char
    finally:
        cv2.destroyWindow(title)


def manual_review_squares(grid, sources, squares, review):
    """Show each uncertain square and ask what piece it contains.

    Mutates grid/sources for answered squares and returns the items that
    are still unresolved (skipped, or not reached after Esc).
    """
    remaining = []

    for index, item in enumerate(review):
        row, col, ranked = item[0], item[1], item[2]
        confidence = item[3] if len(item) > 3 else None
        selected = choose_piece_ui(
            squares[row][col], row, col,
            ranked=ranked,
            confidence=confidence,
            current=grid[row][col],
            progress=(index + 1, len(review)),
            mode="review",
        )

        if selected == CANCEL_CHOICE:
            remaining.extend(review[index:])
            break
        if selected is None:
            remaining.append(item)
            continue

        grid[row][col] = selected
        sources[row][col] = "manual"
        print(
            f"Manual review row={row} col={col}: {piece_name(selected)}"
        )

    return remaining


def verification_grid_ui(grid, sources, squares, confidences=None,
                         on_change=None, title="Verify recognized board"):
    """Show the full board; hover explains a square, click edits it."""
    state = {"mouse": (-1, -1), "clicked": None}

    def on_mouse(event, x, y, flags, param):
        state["mouse"] = (x, y)
        if event == cv2.EVENT_LBUTTONDOWN:
            cell = grid_cell_at(x, y)
            if cell is not None:
                state["clicked"] = cell

    cv2.namedWindow(title, cv2.WINDOW_NORMAL)
    try:
        cv2.resizeWindow(title, GRID_WIDTH, GRID_HEIGHT)
        cv2.setMouseCallback(title, on_mouse)

        badges = {
            "auto": ("A", (60, 190, 60)),
            "manual": ("M", (0, 150, 255)),
            "unknown": ("?", (60, 60, 230)),
        }

        while True:
            if state["clicked"] is not None:
                row, col = state["clicked"]
                state["clicked"] = None
                selected = choose_piece_ui(
                    squares[row][col], row, col,
                    current=grid[row][col],
                    mode="edit",
                    title="Edit square",
                )
                if selected in PIECE_CHOICES:
                    grid[row][col] = selected
                    sources[row][col] = "manual"
                    print(
                        f"Edited row={row} col={col}: "
                        f"{piece_name(selected)} (manual review)"
                    )
                    if on_change is not None:
                        on_change()

            hover = grid_cell_at(*state["mouse"])
            canvas = np.full((GRID_HEIGHT, GRID_WIDTH, 3), 28, np.uint8)

            for row in range(8):
                for col in range(8):
                    x = col * GRID_CELL_SIZE
                    y = GRID_HEADER_H + row * GRID_CELL_SIZE
                    tile = squares[row][col][:, :, :3]
                    interpolation = (
                        cv2.INTER_NEAREST
                        if max(tile.shape[:2]) < GRID_CELL_SIZE
                        else cv2.INTER_AREA
                    )
                    resized = cv2.resize(
                        tile, (GRID_CELL_SIZE, GRID_CELL_SIZE),
                        interpolation=interpolation,
                    )
                    canvas[y:y + GRID_CELL_SIZE, x:x + GRID_CELL_SIZE] = resized
                    cv2.rectangle(
                        canvas, (x, y),
                        (x + GRID_CELL_SIZE - 1, y + GRID_CELL_SIZE - 1),
                        (75, 75, 75), 1,
                    )

                    symbol = grid[row][col]
                    code = ASSETS.get(symbol, "blank" if symbol == "." else "?")
                    (text_w, _), _ = cv2.getTextSize(
                        code, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1
                    )
                    cv2.rectangle(
                        canvas,
                        (x + 3, y + GRID_CELL_SIZE - 22),
                        (x + text_w + 9, y + GRID_CELL_SIZE - 3),
                        (0, 0, 0), -1,
                    )
                    _draw_text(
                        canvas, code, (x + 6, y + GRID_CELL_SIZE - 7),
                        scale=0.45,
                    )

                    letter, colour = badges.get(
                        sources[row][col], ("?", (60, 60, 230))
                    )
                    cv2.circle(canvas, (x + 14, y + 14), 11, colour, -1)
                    _draw_text(
                        canvas, letter, (x + 9, y + 19),
                        scale=0.42, colour=(0, 0, 0), thickness=1,
                    )

            if hover is not None:
                row, col = hover
                x = col * GRID_CELL_SIZE
                y = GRID_HEADER_H + row * GRID_CELL_SIZE
                cv2.rectangle(
                    canvas, (x, y),
                    (x + GRID_CELL_SIZE - 1, y + GRID_CELL_SIZE - 1),
                    (0, 255, 255), 3,
                )
                status = square_status_text(
                    row, col, grid, sources, confidences
                )
            else:
                status = "Hover a square to see its piece and review source."

            _draw_text(canvas, status, (14, 30), scale=0.58, thickness=1)
            _draw_text(
                canvas,
                "Click any square to edit it. Changes save immediately. S = save, Q/Esc = close.",
                (14, 58), scale=0.46, colour=(215, 215, 215),
            )
            _draw_text(
                canvas,
                "A = auto review, M = manual review, ? = needs review.",
                (14, 82), scale=0.46, colour=(215, 215, 215),
            )
            cv2.imshow(title, canvas)

            key = cv2.waitKey(20) & 0xFF
            if key in (27, ord("q"), ord("Q")):
                break
            if key in (ord("s"), ord("S")) and on_change is not None:
                on_change()
    finally:
        cv2.destroyWindow(title)

    return grid


def recognize_board(interactive=True, export_training=None,
                    use_training_examples=True):
    """Recognize all 64 crops, optionally opening the verification UIs.

    Existing labelled training crops are used alongside the canonical
    sprites. When interactive, uncertain squares are shown one at a time
    for manual review, then the full board grid is shown for a final
    hover/click verification pass. After successful verification (or when
    ``export_training`` is forced), every known crop is renamed into the
    growing <code>_<n> training dataset. Final labels are always written
    to autoplaydictionary.json.
    """
    sprites = load_assets()
    training_examples = {}
    if use_training_examples:
        try:
            training_examples = load_training_examples()
            example_count = sum(len(items) for items in training_examples.values())
            if example_count:
                print(
                    f"Loaded {example_count} labelled training example(s) "
                    f"across {len(training_examples)} label(s)."
                )
        except Exception as exc:
            print(f"Could not load training examples; using sprites only. ({exc})")
            training_examples = {}
    grid = []
    sources = []
    confidences = []
    squares = []
    review = []
    ranked_by_square = {}

    for row in range(8):
        grid_row = []
        source_row = []
        confidence_row = []
        square_row = []

        for col in range(8):
            path = square_image_path(row, col)
            square = cv2.imread(str(path))

            if square is None:
                raise FileNotFoundError(
                    f"Cannot load square crop: {path}. Run board-vision.py "
                    "to recreate the r<row>_c<col> crops for this dataset."
                )

            symbol, confidence, ranked = classify_square(
                square,
                sprites,
                training_examples=training_examples,
            )
            grid_row.append(symbol)
            source_row.append("unknown" if symbol == "?" else "auto")
            confidence_row.append(confidence)
            square_row.append(square)

            print(
                f"row={row} col={col}: "
                f"{symbol} confidence={confidence:.2f}"
            )

            if symbol == "?":
                top_ranked = ranked[:3]
                review.append((row, col, top_ranked, confidence))
                ranked_by_square[(row, col)] = top_ranked

        grid.append(grid_row)
        sources.append(source_row)
        confidences.append(confidence_row)
        squares.append(square_row)

    verification_completed = False

    if interactive:
        if review:
            print(
                f"\nOpening manual review for {len(review)} uncertain "
                "square(s)..."
            )
            try:
                review = manual_review_squares(
                    grid, sources, squares, review
                )
                write_autoplay_dictionary(grid)
                print(
                    "Saved manual-review labels to "
                    f"{AUTOPLAY_DICTIONARY_PATH}"
                )
            except cv2.error as exc:
                print(
                    "Could not open the manual-review UI; uncertain "
                    f"squares remain marked '?'. ({exc})"
                )
        else:
            print("\nNo squares need manual review.")

        print(
            "Opening final board verification: hover a square for its "
            "piece/source, click it to edit."
        )
        try:
            verification_grid_ui(
                grid,
                sources,
                squares,
                confidences=confidences,
                on_change=lambda: write_autoplay_dictionary(grid),
            )
            verification_completed = True
        except cv2.error as exc:
            print(f"Could not open the final verification UI. ({exc})")

    # Always write the final verified labels, including any edits made in
    # the full-grid verification window.
    write_autoplay_dictionary(grid)
    print(f"\nWrote {AUTOPLAY_DICTIONARY_PATH} (64 square labels).")

    # Grow the training dataset only from a board the user has verified
    # with every square marked, unless a caller explicitly forces/skips
    # the export. A forced export still leaves any '?' crops in place.
    unresolved_count = sum(symbol == "?" for row in grid for symbol in row)
    if export_training is None:
        should_export = verification_completed and unresolved_count == 0
    else:
        should_export = bool(export_training)
    if should_export:
        exported = export_training_images(grid, squares=squares)
        print(
            f"Added {len(exported)} verified square image(s) to the "
            f"training dataset in {TRAINING_DIR}."
        )
        if exported:
            print("Examples: " + ", ".join(path.name for path in exported[:8]))
            print("Run board-vision.py again to recreate r*_c* crops for the next dataset.")
    elif interactive:
        if verification_completed and unresolved_count:
            print(
                "Training-dataset export skipped: "
                f"{unresolved_count} square(s) are still marked '?'."
            )
        else:
            print(
                "Training-dataset export skipped: final board verification "
                "did not complete."
            )

    print("\nRecognized board (rank 8 down to rank 1):")
    for row in grid:
        print(" ".join(row))

    placement = fen_placement(grid)
    print("\nFEN placement:", placement)

    unresolved = [
        (row, col)
        for row in range(8)
        for col in range(8)
        if grid[row][col] == "?"
    ]
    if unresolved:
        print("\nSquares still needing manual review:")
        for row, col in unresolved:
            ranked = ranked_by_square.get((row, col), [])
            suffix = f": {ranked}" if ranked else ""
            print(f"  row {row}, col {col}{suffix}")

    return grid, placement


if __name__ == "__main__":
    export_training = None
    if "--export-training" in sys.argv:
        export_training = True
    elif "--no-export" in sys.argv:
        export_training = False
    grid, placement = recognize_board(
        interactive="--no-ui" not in sys.argv,
        export_training=export_training,
        use_training_examples="--no-training" not in sys.argv,
    )
