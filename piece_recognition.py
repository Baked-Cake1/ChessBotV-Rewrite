
# piece_recognition.py
import json
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


def recognize_board(interactive=True):
    """Recognize all 64 crops, optionally opening the verification UIs.

    When interactive, uncertain squares are shown one at a time for manual
    review, then the full board grid is shown for a final hover/click
    verification pass. The final labels are written to
    autoplaydictionary.json either way.
    """
    sprites = load_assets()
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
                    f"Cannot load square crop: {path}"
                )

            symbol, confidence, ranked = classify_square(
                square, sprites
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
        except cv2.error as exc:
            print(f"Could not open the final verification UI. ({exc})")

    # Always write the final verified labels, including any edits made in
    # the full-grid verification window.
    write_autoplay_dictionary(grid)
    print(f"\nWrote {AUTOPLAY_DICTIONARY_PATH} (64 square labels).")

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
    grid, placement = recognize_board(interactive="--no-ui" not in sys.argv)
