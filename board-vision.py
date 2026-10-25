
import cv2
from pathlib import Path

IMAGE_PATH = Path("chess_screenshot.png")
OUTPUT_DIR = Path("board_debug")

def main():
    image = cv2.imread(str(IMAGE_PATH))

    if image is None:
        raise FileNotFoundError(
            f"Could not load screenshot: {IMAGE_PATH.resolve()}"
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Select the OUTER EDGE of the chessboard.")
    print("Press ENTER or SPACE to confirm; C to cancel.")

    x, y, w, h = map(
        int,
        cv2.selectROI(
            "Select chessboard",
            image,
            showCrosshair=True,
            fromCenter=False
        )
    )
    cv2.destroyAllWindows()

    if w == 0 or h == 0:
        print("Board selection cancelled.")
        return

    # Crop the selected board.
    board = image[y:y + h, x:x + w].copy()

    # The standard board is square. Correct the crop to a square
    # if the selection has a small accidental width/height mismatch.
    size = min(w, h)
    board = cv2.resize(board, (size, size))

    # Save the board crop.
    cv2.imwrite(str(OUTPUT_DIR / "board.png"), board)

    square_size = size / 8
    debug = board.copy()

    # Split the board into 64 equal squares.
    for row in range(8):
        for col in range(8):
            left = round(col * square_size)
            top = round(row * square_size)
            right = round((col + 1) * square_size)
            bottom = round((row + 1) * square_size)

            square = board[top:bottom, left:right]

            # Save each tile for later piece-classifier training.
            name = f"r{row}_c{col}.png"
            cv2.imwrite(str(OUTPUT_DIR / name), square)

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

    cv2.imwrite(str(OUTPUT_DIR / "grid_debug.png"), debug)

    print(f"Board size: {size} x {size}")
    print(f"Square size: {square_size:.2f} pixels")
    print(f"Saved 64 square images to: {OUTPUT_DIR.resolve()}")
    print("Inspect grid_debug.png to verify the crop.")

    cv2.imshow("Detected board grid", debug)
    cv2.waitKey(0)
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
