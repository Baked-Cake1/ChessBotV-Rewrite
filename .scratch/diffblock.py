def _autoplay_changed_visual_squares(previous, current,
                                     mean_threshold=6.0,
                                     changed_fraction=0.035):
    """Screenshot cells whose pixels changed materially between polls.

    One live Advisor/Auto-play poll calls this on every screenshot. The
    previous version ran two ``np.mean`` reductions on each of the 64 cells
    (128 tiny reductions plus their interpreter overhead). The same
    arithmetic is now two ``np.add.reduceat`` passes: exact per-cell sums
    land in an 8x8 block, and each cell is then tested exactly as before.
    """
    if previous is None or current is None:
        return [(row, col) for row in range(8) for col in range(8)]
    if previous.shape != current.shape or previous.ndim != 3:
        return [(row, col) for row in range(8) for col in range(8)]

    import cv2
    import numpy as np

    difference = cv2.absdiff(previous[:, :, :3], current[:, :, :3])
    changed_pixels = np.max(difference, axis=2) > 32
    height = int(difference.shape[0])
    width = int(difference.shape[1])
    if height <= 0 or width <= 0:
        return []

    # Cell bounds are the exclusive pixel bounds the board drawing code
    # uses (one uniform integer boundary per-axis), so per-cell sums can be
    # taken from the whole image in two block reductions.
    row_bounds = [round(row * height / 8) for row in range(9)]
    col_bounds = [round(col * width / 8) for col in range(9)]
    if any(b <= a for a, b in zip(row_bounds, row_bounds[1:])) or any(
        b <= a for a, b in zip(col_bounds, col_bounds[1:])
    ):
        return _autoplay_changed_visual_squares_reference(
            previous, current, mean_threshold, changed_fraction
        )

    diff32 = difference.astype(np.int64).sum(axis=2)
    row_sums = np.add.reduceat(diff32, row_bounds[:-1], axis=0)
    cell_sums = np.add.reduceat(row_sums, col_bounds[:-1], axis=1)

    changed32 = changed_pixels.astype(np.int32)
    row_counts = np.add.reduceat(changed32, row_bounds[:-1], axis=0)
    cell_counts = np.add.reduceat(row_counts, col_bounds[:-1], axis=1)

    row_sizes = np.diff(np.array(row_bounds, dtype=np.int64))
    col_sizes = np.diff(np.array(col_bounds, dtype=np.int64))
    areas = np.outer(row_sizes, col_sizes).astype(np.float64)

    # np.mean(difference[cell]) divides the 3-channel sum by area * 3;
    # np.mean(changed_pixels[cell]) divides the mask sum by area.
    mean_diff = cell_sums.astype(np.float64) / (areas * 3.0)
    fraction = cell_counts.astype(np.float64) / areas

    changed = []
    for row in range(8):
        for col in range(8):
            if (
                mean_diff[row][col] >= mean_threshold
                or fraction[row][col] >= changed_fraction
            ):
                changed.append((row, col))
    return changed


def _autoplay_changed_visual_squares_reference(
    previous, current, mean_threshold=6.0, changed_fraction=0.035
):
    """Per-cell reference form, kept for boards too small to split in 8."""
    if previous is None or current is None:
        return [(row, col) for row in range(8) for col in range(8)]
    if previous.shape != current.shape or previous.ndim != 3:
        return [(row, col) for row in range(8) for col in range(8)]

    import cv2
    import numpy as np

    difference = cv2.absdiff(previous[:, :, :3], current[:, :, :3])
    changed_pixels = np.max(difference, axis=2) > 32
    board_rect = (0, 0, current.shape[1], current.shape[0])
    changed = []
    for row in range(8):
        for col in range(8):
            left, top, right, bottom = _board_cell_bounds(board_rect, row, col)
            cell_diff = difference[top:bottom, left:right]
            cell_changed = changed_pixels[top:bottom, left:right]
            if cell_diff.size == 0:
                continue
            mean_diff = float(np.mean(cell_diff))
            fraction = float(np.mean(cell_changed))
            if mean_diff >= mean_threshold or fraction >= changed_fraction:
                changed.append((row, col))
    return changed
