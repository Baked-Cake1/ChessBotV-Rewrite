# piece_detection.py
"""Ask the user to identify an unknown square and learn from the answer.

Board Detection calls :func:`ask_and_save_training_example` whenever the
combined sprite/training-example classifier cannot confidently identify a
square. The existing piece picker opens with the title "Piece Detection",
the selected crop is saved as the next ``<code>_<n>`` training example
(for example ``bq_3.png``), and the prepared example is appended to the
caller's in-memory example set before this module returns. The caller can
therefore re-classify the remaining unknown squares immediately, during
the same detection run.
"""

import re
import sys
from pathlib import Path

import cv2

import piece_recognition as pr


PIECE_DETECTION_WINDOW = "Piece Detection"


def ask_piece(square, row=0, col=0, initial="?", title=PIECE_DETECTION_WINDOW,
              detail=None, ranked=None, confidence=None, progress=None):
    """Open the piece picker for one crop and return the selected symbol.

    Returns ``None`` to skip only this square, or
    ``piece_recognition.CANCEL_CHOICE`` when Esc asks to stop asking
    altogether. A last-resort console prompt is used if OpenCV cannot
    open a window, so a CLI invocation can still save the example
    instead of losing the label.
    """
    if detail:
        print(detail)
    current = initial if initial in pr.PIECE_CHOICES else "?"
    try:
        selected = pr.choose_piece_ui(
            square,
            row,
            col,
            ranked=ranked,
            confidence=confidence,
            current=current,
            progress=progress,
            mode="review",
            title=title,
        )
    except cv2.error:
        choices = ", ".join(
            f"{symbol} = {pr.PIECE_NAMES[symbol]}"
            for symbol in pr.PIECE_CHOICES
        )
        print("Piece Detection could not identify this square.")
        print(choices)
        try:
            answer = input("Piece symbol (blank = .): ").strip()
        except (EOFError, KeyboardInterrupt):
            return pr.CANCEL_CHOICE
        if answer == "":
            return "." if current == "?" else current
        selected = answer

    if selected == pr.CANCEL_CHOICE:
        return selected
    if selected in pr.PIECE_CHOICES:
        return selected
    return None


def ask_and_save_training_example(square, training_examples=None,
                                  row=0, col=0, initial="?",
                                  title=PIECE_DETECTION_WINDOW,
                                  detail=None, ranked=None, confidence=None,
                                  progress=None, directory=None, suffix=".png"):
    """Ask for a square's label, save it, and apply it live.

    Returns ``(symbol, saved_path)``. A skip returns ``(None, None)``;
    Esc returns ``(CANCEL_CHOICE, None)`` so the caller can stop asking.
    Nothing is written in either case. ``training_examples`` is the
    mutable symbol -> prepared-examples mapping used by
    ``piece_recognition.classify_square``; the new verified example is
    appended to it immediately after the file is saved.
    """
    symbol = ask_piece(
        square,
        row=row,
        col=col,
        initial=initial,
        title=title,
        detail=detail,
        ranked=ranked,
        confidence=confidence,
        progress=progress,
    )
    if symbol == pr.CANCEL_CHOICE:
        return symbol, None
    if symbol not in pr.PIECE_CHOICES:
        return None, None

    saved_path = pr.add_training_example(
        square,
        symbol,
        training_examples=training_examples,
        directory=directory,
        suffix=suffix,
    )
    return symbol, saved_path


def _row_col_from_filename(path):
    """Best-effort (row, col) from a standalone r<row>_c<col> filename."""
    match = re.search(r"r(\d+)_c(\d+)$", Path(path).stem)
    if not match:
        return 0, 0
    row = max(0, min(7, int(match.group(1))))
    col = max(0, min(7, int(match.group(2))))
    return row, col


def main(argv=None):
    """Run Piece Detection on one square image and save it as training data."""
    args = list(sys.argv[1:] if argv is None else argv)
    paths = [arg for arg in args if arg and not arg.startswith("-")]

    if not paths:
        print("Usage: python piece_detection.py <square-image>")
        print("Board Detection opens Piece Detection automatically when it")
        print("does not know a piece; standalone use needs one square crop.")
        return None

    image_path = Path(paths[0]).expanduser()
    square = cv2.imread(str(image_path))
    if square is None:
        raise FileNotFoundError(f"Could not load image: {image_path}")
    row, col = _row_col_from_filename(image_path)

    suffix = image_path.suffix
    if suffix.lower() not in pr.TRAINING_IMAGE_EXTENSIONS:
        suffix = ".png"
    examples = pr.load_training_examples()
    symbol, saved_path = ask_and_save_training_example(
        square,
        training_examples=examples,
        row=row,
        col=col,
        detail="Choose the piece shown on this square.",
        suffix=suffix,
    )
    if symbol not in pr.PIECE_CHOICES:
        print("Piece Detection cancelled/skipped; nothing was saved.")
        return None
    print(f"Identified as {pr.PIECE_NAMES[symbol]} ({symbol}).")
    print(f"Saved training example: {saved_path}")
    return symbol


if __name__ == "__main__":
    main()
