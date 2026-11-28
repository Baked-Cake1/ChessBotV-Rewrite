"""Board-detection vision model: CNN keypoint heatmaps + lattice homography.

This is the second vision system beside the square classifier. It answers
*where is the board and what is its geometry?* instead of *what piece is
this?*, so it can replace the OpenCV corner search that struggles with
styled, themed or partially obscured chessboards.

Design (the architecture recommended for ChessBotV):

* **Input** - a square screenshot, resized to ``KEYPOINT_INPUT_SIZE``
  (512x512 by default) and normalised with ImageNet statistics.
* **Backbone** - a small ResNet-style feature extractor with a lightweight
  FPN neck, so both fine grid detail and overall board shape are available.
* **Head** - ``9 x 9`` = 81 keypoint heatmaps, one per board intersection
  (the four outer corners are heatmaps 0, 8, 72 and 80). The whole 9x9
  lattice is predicted rather than only the corners, because the interior
  intersections give redundant geometry: a few weak points can be rejected
  and rebuilt from the rest instead of losing the board.
* **Geometry** - the peak of each heatmap is a lattice intersection; a
  least-squares lattice fit rejects outliers, rebuilds missing points and
  solves the single homography that maps the ideal 9x9 grid onto the
  screenshot. The ``8x8`` square grid follows from that homography, so
  perspective and rotation are handled exactly, not approximated by a box.

Everything except the torch model itself is pure NumPy, so the lattice fit,
annotation and dataset helpers are importable and testable without torch.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------
# Constants shared by board-vision.py, cnn_trainer.py and new.py
# --------------------------------------------------------------------------
KEYPOINT_GRID = 9                      # intersections per side (8 squares + 1)
KEYPOINT_COUNT = KEYPOINT_GRID * KEYPOINT_GRID   # 81 heatmaps
KEYPOINT_INPUT_SIZE = 512              # screenshot fed to the model
KEYPOINT_HEATMAP_SIZE = 128            # output heatmap resolution
KEYPOINT_HEATMAP_STRIDE = KEYPOINT_INPUT_SIZE // KEYPOINT_HEATMAP_SIZE
KEYPOINT_SIGMA = 2.0                   # Gaussian target radius, in heatmap px
KEYPOINT_PEAK_THRESHOLD = 0.25         # below this a keypoint is "missing"
MODEL_TYPE_PIECES = "pieces"
MODEL_TYPE_BOARD = "board"
MODEL_TYPES = (MODEL_TYPE_PIECES, MODEL_TYPE_BOARD)
MODEL_TYPE_LABELS = {
    MODEL_TYPE_PIECES: "Piece detection",
    MODEL_TYPE_BOARD: "Board detection",
}
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)
BOARD_DETECTION_DIRNAME = "board_detection"
BOARD_DETECTION_SAMPLE_DIRNAME = "samples"
BOARD_DETECTION_LABEL_FILENAME = "keypoints.json"
BOARD_DETECTION_CHECKPOINT_SUFFIX = "_board"


def model_type_label(model_type):
    """Human label for a model type string, tolerating odd values."""
    return MODEL_TYPE_LABELS.get(normalise_model_type(model_type), "Piece detection")


def normalise_model_type(value):
    """Free-text/legacy value -> one of :data:`MODEL_TYPES`."""
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower())
    text = text.strip("_")
    if text in (
        "board", "board_detection", "boarddetection", "detection_board",
        "keypoints", "keypoint", "geometry", "board_keypoints",
    ):
        return MODEL_TYPE_BOARD
    return MODEL_TYPE_PIECES


def keypoint_index(row, col):
    """Heatmap index of lattice intersection (row, col), both 0..8."""
    if not (0 <= row < KEYPOINT_GRID and 0 <= col < KEYPOINT_GRID):
        raise ValueError("keypoint row/col must be 0..8")
    return int(row) * KEYPOINT_GRID + int(col)


def keypoint_row_col(index):
    """Inverse of :func:`keypoint_index`."""
    index = int(index)
    return index // KEYPOINT_GRID, index % KEYPOINT_GRID


def board_model_path(model_output_path):
    """``models/<model>.pth`` -> ``models/<model>_board.pth``."""
    path = Path(model_output_path)
    return path.with_name(f"{path.stem}{BOARD_DETECTION_CHECKPOINT_SUFFIX}.pth")


def board_sample_dir(model_dataset_dir):
    """Folder that stores detected screenshots + keypoints.json for a model."""
    return Path(model_dataset_dir) / BOARD_DETECTION_DIRNAME


def format_keypoints_for_json(points):
    """JSON-ready ``{"points": [[x, y], ...81], "size": [w, h]}`` wrapper."""
    return {
        "points": [[round(float(x), 3), round(float(y), 3)] for x, y in points],
        "size": [int(points_size[0]), int(points_size[1])] if (
            points_size := _points_size(points)
        ) else [0, 0],
    }


def _points_size(points):
    if not len(points):
        return None
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return max(xs), max(ys)


def sample_label_path(sample_path):
    """Sidecar JSON holding one sample's keypoints."""
    path = Path(sample_path)
    return path.with_name(f"{path.stem}{BOARD_DETECTION_LABEL_FILENAME}")


# --------------------------------------------------------------------------
# Heatmap targets
# --------------------------------------------------------------------------
def gaussian_heatmaps(points, heatmap_size=KEYPOINT_HEATMAP_SIZE,
                      sigma=KEYPOINT_SIGMA, visibility=None):
    """``(81, H, W)`` float32 Gaussian target maps for keypoint ``points``.

    ``points`` are in the *original image* pixel space of the sample and are
    normalised here by the sample image size convention used by
    :func:`prepare_sample_image`. Points flagged invisible (``visibility``
    zero) get an all-zero map, which teaches the model to say "I cannot see
    that intersection" instead of guessing.
    """
    heatmap_size = int(heatmap_size)
    points = np.asarray(points, dtype=np.float32).reshape(KEYPOINT_COUNT, 2)
    maps = np.zeros(
        (KEYPOINT_COUNT, heatmap_size, heatmap_size), dtype=np.float32
    )
    # Points arrive normalised to 0..1 by the sample loader.
    axis = np.arange(heatmap_size, dtype=np.float32)
    full_sigma = max(1e-3, float(sigma))
    for index in range(KEYPOINT_COUNT):
        if visibility is not None and not float(visibility[index]) > 0.5:
            continue
        x = float(points[index, 0]) * heatmap_size
        y = float(points[index, 1]) * heatmap_size
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        gauss = np.exp(
            -((axis[None, :] - x) ** 2 + (axis[:, None] - y) ** 2)
            / (2.0 * full_sigma ** 2)
        ).astype(np.float32)
        peak = gauss.max()
        if peak > 0:
            maps[index] = gauss / peak
    return maps


def normalise_points(points, image_size):
    """Pixel keypoints -> 0..1 coordinates for the fixed-size input."""
    width, height = float(image_size[0]), float(image_size[1])
    points = np.asarray(points, dtype=np.float32).reshape(KEYPOINT_COUNT, 2)
    return np.stack(
        [points[:, 0] / max(width, 1.0), points[:, 1] / max(height, 1.0)],
        axis=1,
    )


def denormalise_points(points, image_size):
    """0..1 coordinates -> pixel keypoints for the original image size."""
    width, height = float(image_size[0]), float(image_size[1])
    points = np.asarray(points, dtype=np.float32).reshape(KEYPOINT_COUNT, 2)
    return np.stack(
        [points[:, 0] * width, points[:, 1] * height], axis=1
    )


# --------------------------------------------------------------------------
# Dataset helpers (torch-free)
# --------------------------------------------------------------------------
def load_keypoint_label(sample_path):
    """Read a sample's sidecar JSON -> ``(points, visibility)`` or ``None``."""
    label_path = sample_label_path(sample_path)
    if not label_path.is_file():
        return None
    try:
        payload = json.loads(label_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    points = payload.get("points")
    if not isinstance(points, list) or len(points) != KEYPOINT_COUNT:
        return None
    try:
        array = np.asarray(points, dtype=np.float32).reshape(KEYPOINT_COUNT, 2)
    except (TypeError, ValueError):
        return None
    visibility = payload.get("visibility")
    if isinstance(visibility, list) and len(visibility) == KEYPOINT_COUNT:
        visible = np.asarray(visibility, dtype=np.float32)
    else:
        visible = np.ones(KEYPOINT_COUNT, dtype=np.float32)
    return array, visible


def save_keypoint_label(sample_path, points, class_meta=None):
    """Write the sidecar JSON; ``points`` are in source-image pixels."""
    label_path = sample_label_path(sample_path)
    label_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "points": [[round(float(x), 3), round(float(y), 3)] for x, y in points],
        "visibility": [1] * KEYPOINT_COUNT,
        "source": Path(sample_path).name,
        "grid": KEYPOINT_GRID,
    }
    if class_meta:
        payload.update(class_meta)
    label_path.write_text(
        json.dumps(payload, indent=1), encoding="utf-8"
    )
    return label_path


def collect_board_samples(dataset_dir):
    """Every labelled sample under ``<dataset>/board_detection/samples``.

    Returns a list of ``{"image": Path, "points": (81,2), "visibility": ...}``
    records for training/validation. Unlabelled files are skipped so a
    half-finished screenshot cannot poison a run.
    """
    folder = board_sample_dir(dataset_dir) / BOARD_DETECTION_SAMPLE_DIRNAME
    records = []
    if not folder.is_dir():
        return records
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.suffix.lower() not in (
            ".png", ".jpg", ".jpeg", ".bmp", ".webp",
        ):
            continue
        loaded = load_keypoint_label(path)
        if loaded is None:
            continue
        records.append(
            {"image": path, "points": loaded[0], "visibility": loaded[1]}
        )
    return records


# --------------------------------------------------------------------------
# Geometry: lattice fit, outlier rejection, homography
# --------------------------------------------------------------------------
def _ideal_lattice():
    """``(81, 2)`` ideal ``(x, y)`` coordinates of the 9x9 grid in 0..1 space.

    Index ``row * 9 + col`` must be ``(col / 8, row / 8)``: x follows the
    column and y the row, matching :func:`keypoint_row_col`, the heatmap
    channel order and every image-space point array this module handles.
    ``meshgrid(..., indexing="ij")`` would stack ``(row, col)`` instead and
    silently transpose the fitted lattice, mapping the top-right corner to
    the bottom-left.
    """
    coords = np.linspace(0.0, 1.0, KEYPOINT_GRID, dtype=np.float64)
    return np.stack(
        np.meshgrid(coords, coords, indexing="xy"), axis=-1
    ).reshape(KEYPOINT_COUNT, 2)


IDEAL_LATTICE = _ideal_lattice()


def _normalise_points_for_dlt(points):
    """Hartley normalisation: centre on the centroid, scale to mean dist 1."""
    points = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    centroid = points.mean(axis=0)
    shifted = points - centroid
    mean_distance = float(np.mean(np.linalg.norm(shifted, axis=1)))
    scale = math.sqrt(2.0) / mean_distance if mean_distance > 1e-12 else 1.0
    matrix = np.array(
        [
            [scale, 0.0, -scale * centroid[0]],
            [0.0, scale, -scale * centroid[1]],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    return matrix, shifted * scale


def _solve_homography(source, destination):
    """Least-squares homography mapping ``source`` -> ``destination``.

    Both point sets are Hartley-normalised before the DLT and the result is
    denormalised afterwards. Without that step the equations mix raw pixel
    magnitudes (hundreds) with products of two coordinates (tens of
    thousands), and the SVD picks a numerically arbitrary null vector - which
    is why a perfectly clean four-point fit once "explained" only five of 81
    keypoints.
    """
    source = np.asarray(source, dtype=np.float64).reshape(-1, 2)
    destination = np.asarray(destination, dtype=np.float64).reshape(-1, 2)
    if source.shape[0] < 4 or destination.shape[0] != source.shape[0]:
        return None
    source_norm, source_points = _normalise_points_for_dlt(source)
    destination_norm, destination_points = _normalise_points_for_dlt(destination)
    rows = []
    for (x, y), (u, v) in zip(source_points, destination_points):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y, -u])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y, -v])
    matrix = np.asarray(rows, dtype=np.float64)
    try:
        # full_matrices=True is required: with more equations than unknowns
        # NumPy otherwise returns a reduced SVD whose V^H has only ``min(m, n)``
        # rows, and ``vh[-1]`` is then some arbitrary singular vector rather
        # than the 9-element null vector of the system.
        _, singular_values, vh = np.linalg.svd(matrix, full_matrices=True)
    except np.linalg.LinAlgError:
        return None
    if vh.shape[0] < 9:
        return None
    homography = np.asarray(vh[-1], dtype=np.float64).reshape(3, 3)
    try:
        homography = (
            np.linalg.inv(destination_norm) @ homography @ source_norm
        )
    except np.linalg.LinAlgError:
        return None
    norm = np.linalg.norm(homography)
    if not np.isfinite(norm) or norm < 1e-12:
        return None
    homography = homography / norm
    if abs(homography[2, 2]) > 1e-9:
        homography = homography / homography[2, 2]
    if not np.all(np.isfinite(homography)):
        return None
    # A degenerate solution (all points collapsing onto one spot) fits
    # perfectly but describes no board at all, so reject it here instead of
    # letting every caller re-check the same thing.
    if not _homography_spans_area(homography, source):
        return None
    return homography


def _homography_spans_area(homography, source, minimum_ratio=0.02):
    """Whether a solved homography maps the points to a real 2D spread.

    "All keypoints reported the same place" is a common heatmap failure (a
    model that has not learned yet returns a flat map, whose argmax is always
    pixel 0). Such a fit has near-zero area and must not be reported as a
    board.
    """
    source = np.asarray(source, dtype=np.float64).reshape(-1, 2)
    projected = apply_homography(homography, source)
    if not np.all(np.isfinite(projected)):
        return False
    width = float(projected[:, 0].max() - projected[:, 0].min())
    height = float(projected[:, 1].max() - projected[:, 1].min())
    source_width = float(source[:, 0].max() - source[:, 0].min())
    source_height = float(source[:, 1].max() - source[:, 1].min())
    # Compare against the *source* extent in the same units when possible:
    # a unit lattice maps to a board hundreds of pixels wide.
    reference = max(source_width, source_height, 1e-9)
    return max(width, height) > reference * minimum_ratio


def apply_homography(homography, points):
    """Project ``(N, 2)`` points through a 3x3 homography."""
    points = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    ones = np.ones((points.shape[0], 1), dtype=np.float64)
    projected = np.hstack([points, ones]) @ np.asarray(homography).T
    w = projected[:, 2:3]
    w = np.where(np.abs(w) < 1e-12, 1e-12, w)
    return projected[:, :2] / w


def _active_board_side(points, fallback):
    """Rough board side (px) from a set of lattice points, for scaling."""
    points = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if len(points) < 2:
        return float(fallback)
    xs = points[:, 0]
    ys = points[:, 1]
    side = max(float(xs.max() - xs.min()), float(ys.max() - ys.min()))
    return side if side > 1.0 else float(fallback)


def _well_conditioned_sample(sample_points, minimum_area=0.01,
                             collinear_ratio=0.02):
    """Whether four ideal-grid points define a usable homography.

    Four intersections in a single row or column (the "inner corners found,
    outer ones missed" failure) are collinear in the ideal grid too, so the
    homography is degenerate. A quad with real area but three points almost
    on one line is equally useless, so both cases are rejected. The four
    board corners pass: their area is 1.0 in the unit lattice.
    """
    quad = np.asarray(sample_points, dtype=np.float64).reshape(4, 2)
    area = abs(
        (quad[1][0] - quad[0][0]) * (quad[3][1] - quad[0][1])
        - (quad[3][0] - quad[0][0]) * (quad[1][1] - quad[0][1])
    ) / 2.0
    if area < minimum_area:
        return False
    # Reject samples where one point sits on the line through two others.
    for skip in range(4):
        triangle = np.delete(quad, skip, axis=0)
        a, b, c = triangle
        scale = max(np.linalg.norm(b - a), np.linalg.norm(c - a), 1e-9)
        distance = abs(
            (b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])
        ) / scale
        if distance / scale < collinear_ratio:
            return False
    return True


def _consensus_seed(points, finite, board_side, min_points,
                    max_error=None, samples=400, seed=1234):
    """Robust seed: the 4-point hypothesis whose consensus is tightest.

    Eight-one keypoints from a heatmap model can contain gross errors, and a
    plain least-squares fit is dragged around by them. Four intersections are
    the minimum for a homography, so every 4-point sample proposes a
    candidate board; the winner is the one that explains the most points and,
    among ties, leaves the smallest median error. The tolerance is returned so
    the caller refits against the *same* fixed threshold instead of
    re-deriving one from whatever survived (which is what makes naive
    trimming diverge).
    """
    indices = np.flatnonzero(finite)
    if indices.size < min_points:
        return None, None
    # The consensus tolerance is deliberately tight: a correct homography
    # leaves sub-pixel residuals on real keypoints, so a few pixels are
    # plenty. A tolerance scaled to the board (2% of ~480px = 10px) is loose
    # enough to admit a dozen points from a *wrong* lattice, which then
    # out-votes the truth.
    absolute = (
        float(max_error) if max_error is not None
        else float(np.clip(0.004 * board_side, 1.5, 4.0))
    )
    generator = np.random.default_rng(seed)
    best_mask = None
    best_key = None
    attempts = 0
    while attempts < int(samples):
        attempts += 1
        sample = generator.choice(indices, size=4, replace=False)
        # Four points from one lattice row/column are collinear in the ideal
        # grid too, so the homography is degenerate. Skip those samples.
        if not _well_conditioned_sample(IDEAL_LATTICE[sample]):
            continue
        homography = _solve_homography(
            IDEAL_LATTICE[sample], points[sample]
        )
        if homography is None or not np.all(np.isfinite(homography)):
            continue
        projected = apply_homography(homography, IDEAL_LATTICE)
        residuals = np.linalg.norm(projected - points, axis=1)
        residuals[~finite] = np.inf
        inliers = residuals <= absolute
        count = int(inliers.sum())
        if count < min_points:
            continue
        median = float(np.median(residuals[inliers]))
        # Prefer coverage, then precision: a hypothesis that explains one
        # extra point but 10x worse is not the board.
        key = (count, -median)
        if best_key is None or key > best_key:
            best_key = key
            best_mask = inliers
    if best_mask is None:
        return None, None
    # Widen for the refinement passes: the seed was found under the tight
    # threshold, but real keypoint noise (a heatmap peak a couple of pixels
    # off on a 4x-upscaled map) should not throw good points away.
    refinement_tolerance = max(absolute * 2.5, 0.012 * board_side)
    return best_mask, refinement_tolerance


def lattice_fit(points, visibility=None, max_error=None, min_points=6):
    """Robust 9x9 lattice fit for 81 predicted keypoint positions.

    Implements the "make it recoverable" step: seed the fit from *all*
    finite points (so six confident keypoints cannot drag the board
    sideways), then repeatedly refit and drop points that disagree with the
    consensus. The rejection threshold scales with the board size, the
    worst offenders are removed on every pass, and the surviving points get
    the final fit. Missing/unreliable keypoints are reconstructed by
    projecting the ideal lattice through that homography.

    Returns a dict with ``homography`` (ideal -> image), ``points`` (the
    reconstructed 81 points), ``inliers`` (bool mask), ``residuals`` and
    ``confidence`` (0..1, blending keypoint coverage with fit tightness).
    ``None`` means fewer than ``min_points`` points survived.
    """
    points = np.asarray(points, dtype=np.float64).reshape(KEYPOINT_COUNT, 2)
    finite = np.all(np.isfinite(points), axis=1)
    if finite.sum() < min_points:
        return None
    if visibility is None:
        confident = np.ones(KEYPOINT_COUNT, dtype=bool)
    else:
        confident = np.asarray(visibility, dtype=np.float64).reshape(-1) > 0.5
    # Points the model called "not visible" may still be finite (the raw
    # argmax of a flat heatmap has *some* position). Seed from everything
    # and let the residual test decide, but never trust a low-confidence
    # point over a confident one: those are penalised when scoring.
    usable = finite.copy()
    active = finite.copy()

    board_side = _active_board_side(points[finite], 1.0)
    if max_error is None:
        max_error = max(0.02 * board_side, 2.0)

    # Seed from the best 4-point consensus before refitting, and keep that
    # tolerance fixed for the rest of the fit. Re-deriving it from the current
    # inliers is what makes naive trimming diverge: each bad point that slips
    # in widens the threshold, which admits more bad points.
    seed_mask, tolerance = _consensus_seed(
        points, finite, board_side, min_points, max_error=max_error
    )
    if seed_mask is not None:
        active = seed_mask
    else:
        tolerance = max(0.02 * board_side, 2.0)

    for _iteration in range(8):
        if active.sum() < min_points:
            break
        homography = _solve_homography(IDEAL_LATTICE[active], points[active])
        if homography is None:
            break
        projected = apply_homography(homography, IDEAL_LATTICE)
        residuals = np.linalg.norm(projected - points, axis=1)
        residuals[~usable] = np.inf
        keep = (residuals <= tolerance) & usable
        if keep.sum() < min_points:
            break
        if np.array_equal(keep, active):
            break
        active = keep

    if active.sum() < min_points:
        return None
    homography = _solve_homography(IDEAL_LATTICE[active], points[active])
    if homography is None:
        return None
    projected = apply_homography(homography, IDEAL_LATTICE)
    residuals = np.linalg.norm(projected - points, axis=1)
    residuals[~finite] = np.inf

    # Guard against a degenerate "perfect" fit: when the points collapse onto
    # a line the residuals are all ~0 and confidence would be 1.0, yet the
    # lattice describes no board. Measure the second singular value of the
    # centred inlier point cloud - a real 9x9 lattice is strongly 2D.
    inlier_points = points[active]
    if len(inlier_points) >= 3:
        centred = inlier_points - inlier_points.mean(axis=0)
        singular = np.linalg.svd(centred, compute_uv=False)
        if (
            singular.size < 2
            or float(singular[1]) < 0.02 * max(float(singular[0]), 1e-9)
        ):
            return None

    inlier_residuals = residuals[active]
    mean_error = (
        float(np.mean(inlier_residuals)) if inlier_residuals.size else 0.0
    )
    # Confidence blends how much of the lattice the fit explains with how
    # tightly it does so, and discounts keypoints the model was unsure of.
    coverage = float(active.sum()) / float(KEYPOINT_COUNT)
    confident_fraction = float(np.mean(confident[active])) if active.any() else 0.0
    tightness = 1.0 / (1.0 + mean_error / max(0.01 * board_side, 1.0))
    confidence = float(
        np.clip(
            coverage * (0.5 + 0.5 * confident_fraction) * tightness,
            0.0, 1.0,
        )
    )
    return {
        "homography": homography,
        "points": projected,
        "inliers": active,
        "residuals": residuals,
        "confidence": confidence,
        "mean_error": mean_error,
        "inlier_count": int(active.sum()),
    }


def heatmaps_to_points(heatmaps, threshold=KEYPOINT_PEAK_THRESHOLD):
    """Extract ``(81, 2)`` peak positions (0..1) and per-point confidence.

    Peaks are refined with a small quadratic interpolation around the argmax
    so the model's 128x128 heatmaps are not limited to 4-pixel quantisation
    when upscaled to a 512x512 screenshot.
    """
    heatmaps = np.asarray(heatmaps, dtype=np.float32)
    if heatmaps.shape[0] != KEYPOINT_COUNT:
        heatmaps = heatmaps.reshape(KEYPOINT_COUNT, *heatmaps.shape[-2:])
    _, height, width = heatmaps.shape
    points = np.zeros((KEYPOINT_COUNT, 2), dtype=np.float64)
    confidence = np.zeros(KEYPOINT_COUNT, dtype=np.float64)
    for index in range(KEYPOINT_COUNT):
        heatmap = heatmaps[index]
        peak = int(np.argmax(heatmap))
        row, col = divmod(peak, width)
        confidence[index] = float(heatmap[row, col])
        y = float(row)
        x = float(col)
        # Quadratic sub-pixel refinement using direct neighbours.
        if 0 < row < height - 1:
            above, below = heatmap[row - 1, col], heatmap[row + 1, col]
            denominator = above - 2.0 * heatmap[row, col] + below
            if abs(denominator) > 1e-6:
                y += float(np.clip(0.5 * (above - below) / denominator, -0.5, 0.5))
        if 0 < col < width - 1:
            left, right = heatmap[row, col - 1], heatmap[row, col + 1]
            denominator = left - 2.0 * heatmap[row, col] + right
            if abs(denominator) > 1e-6:
                x += float(np.clip(0.5 * (left - right) / denominator, -0.5, 0.5))
        points[index] = (
            (x + 0.5) / float(width),
            (y + 0.5) / float(height),
        )
    visibility = confidence >= float(threshold)
    return points, confidence, visibility


def corners_from_points(points):
    """Board corners ``(tl, tr, br, bl)`` from 81 lattice points."""
    points = np.asarray(points, dtype=np.float64).reshape(KEYPOINT_COUNT, 2)
    return (
        points[keypoint_index(0, 0)],
        points[keypoint_index(0, KEYPOINT_GRID - 1)],
        points[keypoint_index(KEYPOINT_GRID - 1, KEYPOINT_GRID - 1)],
        points[keypoint_index(KEYPOINT_GRID - 1, 0)],
    )


def board_rect_from_points(points):
    """Axis-aligned ``(x, y, w, h)`` box containing the fitted board."""
    points = np.asarray(points, dtype=np.float64).reshape(KEYPOINT_COUNT, 2)
    xs = points[:, 0]
    ys = points[:, 1]
    x0, y0 = float(xs.min()), float(ys.min())
    x1, y1 = float(xs.max()), float(ys.max())
    return (x0, y0, max(1.0, x1 - x0), max(1.0, y1 - y0))


def square_corners(points, row, col):
    """Four corners of square ``(row, col)`` as a ``(4, 2)`` array.

    Squares come straight from the 9x9 lattice, so they follow the board's
    perspective instead of assuming an axis-aligned grid.
    """
    if not (0 <= row < KEYPOINT_GRID - 1 and 0 <= col < KEYPOINT_GRID - 1):
        raise ValueError("square row/col must be 0..7")
    points = np.asarray(points, dtype=np.float64).reshape(KEYPOINT_COUNT, 2)
    return np.asarray(
        [
            points[keypoint_index(row, col)],
            points[keypoint_index(row, col + 1)],
            points[keypoint_index(row + 1, col + 1)],
            points[keypoint_index(row + 1, col)],
        ]
    )


def warp_board_from_points(points, image, output_size):
    """Perspective-warp the board described by 81 points into a square image.

    The homography is fitted from *all* 81 intersections (not four corners),
    so a single noisy corner nudges the warp instead of defining it. Returns
    a ``(output_size, output_size, 3)`` uint8 array, or ``None``.
    """
    points = np.asarray(points, dtype=np.float64).reshape(KEYPOINT_COUNT, 2)
    image = np.asarray(image)
    if image.ndim != 3 or not np.all(np.isfinite(points)):
        return None
    coordinates = np.linspace(0.0, 1.0, KEYPOINT_GRID, dtype=np.float64)
    grid = np.stack(np.meshgrid(coordinates, coordinates, indexing="ij"), -1)
    ideal_pixels = grid.reshape(KEYPOINT_COUNT, 2) * float(output_size)
    ideal_pixels = np.clip(ideal_pixels, 0.0, float(output_size) - 1e-3)
    homography = _solve_homography(points, ideal_pixels)
    if homography is None:
        return None
    return warp_perspective(image, homography, (output_size, output_size))


def warp_perspective(image, homography, output_size):
    """Bilinear inverse-warp of ``image`` through ``homography``.

    A small NumPy implementation so the board-detection pipeline does not
    need OpenCV. ``homography`` maps *image* coordinates to *output*
    coordinates; the inverse sampling grid is built directly from it.
    """
    image = np.asarray(image)
    if image.ndim == 2:
        image = np.stack([image] * 3, axis=-1)
    elif image.ndim == 3 and image.shape[2] == 4:
        image = image[:, :, :3]
    height, width = image.shape[:2]
    out_w, out_h = int(output_size[0]), int(output_size[1])
    try:
        inverse = np.linalg.inv(np.asarray(homography, dtype=np.float64))
    except np.linalg.LinAlgError:
        return None
    xs, ys = np.meshgrid(
        np.arange(out_w, dtype=np.float64),
        np.arange(out_h, dtype=np.float64),
    )
    denominator = (
        inverse[2, 0] * xs + inverse[2, 1] * ys + inverse[2, 2]
    )
    denominator = np.where(np.abs(denominator) < 1e-12, 1e-12, denominator)
    src_x = (
        inverse[0, 0] * xs + inverse[0, 1] * ys + inverse[0, 2]
    ) / denominator
    src_y = (
        inverse[1, 0] * xs + inverse[1, 1] * ys + inverse[1, 2]
    ) / denominator
    return _bilinear_sample(image, src_x, src_y)


def _bilinear_sample(image, src_x, src_y):
    """Sample ``image`` at float coordinates with bilinear interpolation."""
    height, width = image.shape[:2]
    x0 = np.floor(src_x).astype(np.int64)
    y0 = np.floor(src_y).astype(np.int64)
    fx = (src_x - x0)[..., None]
    fy = (src_y - y0)[..., None]
    inside = (x0 >= 0) & (y0 >= 0) & (x0 < width - 1) & (y0 < height - 1)
    x0c = np.clip(x0, 0, width - 2)
    y0c = np.clip(y0, 0, height - 2)
    top_left = image[y0c, x0c].astype(np.float64)
    top_right = image[y0c, x0c + 1].astype(np.float64)
    bottom_left = image[y0c + 1, x0c].astype(np.float64)
    bottom_right = image[y0c + 1, x0c + 1].astype(np.float64)
    sampled = (
        top_left * (1 - fx) * (1 - fy)
        + top_right * fx * (1 - fy)
        + bottom_left * (1 - fx) * fy
        + bottom_right * fx * fy
    )
    sampled[~inside] = 0.0
    return np.clip(sampled, 0, 255).astype(np.uint8)


# --------------------------------------------------------------------------
# Annotation (BGR arrays, used by the GUI preview)
# --------------------------------------------------------------------------
def annotate_keypoints(image, points, inliers=None, colour=(90, 170, 255),
                       missing_colour=(120, 120, 120), thickness=1):
    """Draw the fitted lattice over a BGR screenshot copy.

    Inlier keypoints are bright and rejected ones grey, and the 8x8 square
    grid is traced through the lattice so a perspective board looks as
    straight on screen as it does after the warp.
    """
    annotated = np.array(image, copy=True)
    if annotated.ndim != 3:
        return annotated
    points = np.asarray(points, dtype=np.float64).reshape(KEYPOINT_COUNT, 2)
    if inliers is None:
        inliers = np.ones(KEYPOINT_COUNT, dtype=bool)
    inliers = np.asarray(inliers, dtype=bool).reshape(-1)
    height, width = annotated.shape[:2]

    def draw_line(start, end, line_colour):
        x0, y0 = int(round(start[0])), int(round(start[1]))
        x1, y1 = int(round(end[0])), int(round(end[1]))
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        for step in range(steps + 1):
            fraction = step / steps
            x = int(round(x0 + (x1 - x0) * fraction))
            y = int(round(y0 + (y1 - y0) * fraction))
            if 0 <= y < height and 0 <= x < width:
                annotated[y, x] = line_colour

    for row in range(KEYPOINT_GRID):
        for col in range(KEYPOINT_GRID):
            index = keypoint_index(row, col)
            if col + 1 < KEYPOINT_GRID:
                neighbour = keypoint_index(row, col + 1)
                if inliers[index] and inliers[neighbour]:
                    draw_line(points[index], points[neighbour], colour)
            if row + 1 < KEYPOINT_GRID:
                neighbour = keypoint_index(row + 1, col)
                if inliers[index] and inliers[neighbour]:
                    draw_line(points[index], points[neighbour], colour)
            x, y = int(round(points[index][0])), int(round(points[index][1]))
            if 0 <= y < height and 0 <= x < width:
                dot_colour = colour if inliers[index] else missing_colour
                annotated[max(0, y - 1):y + 2, max(0, x - 1):x + 2] = dot_colour
    for corner in corners_from_points(points):
        x, y = int(round(corner[0])), int(round(corner[1]))
        if 0 <= y < height and 0 <= x < width:
            annotated[max(0, y - 4):y + 5, max(0, x - 4):x + 5] = (60, 220, 120)
    return annotated


# --------------------------------------------------------------------------
# Torch model (imported lazily so NumPy-only callers stay light)
# --------------------------------------------------------------------------
def build_board_detection_model(num_keypoints=KEYPOINT_COUNT):
    """Feature extractor + FPN neck + heatmap head, as recommended.

    The backbone is a compact ResNet18-style stack written out explicitly
    rather than pulled from ``torchvision`` so a checkpoint stays loadable on
    any machine with plain torch, and so the parameter names are stable.
    """
    import torch
    import torch.nn as nn
    import torch.nn.functional as F

    class ConvBlock(nn.Module):
        def __init__(self, in_channels, out_channels, stride=1):
            super().__init__()
            self.conv = nn.Conv2d(
                in_channels, out_channels, 3, stride=stride, padding=1,
                bias=False,
            )
            self.norm = nn.BatchNorm2d(out_channels)
            self.act = nn.ReLU(inplace=True)

        def forward(self, x):
            return self.act(self.norm(self.conv(x)))

    class ResidualBlock(nn.Module):
        def __init__(self, channels, stride=1):
            super().__init__()
            self.conv1 = nn.Conv2d(
                channels, channels, 3, stride=stride, padding=1, bias=False
            )
            self.norm1 = nn.BatchNorm2d(channels)
            self.conv2 = nn.Conv2d(
                channels, channels, 3, padding=1, bias=False
            )
            self.norm2 = nn.BatchNorm2d(channels)
            self.act = nn.ReLU(inplace=True)
            self.downsample = None
            if stride != 1:
                self.downsample = nn.Sequential(
                    nn.Conv2d(channels, channels, 1, stride=stride, bias=False),
                    nn.BatchNorm2d(channels),
                )

        def forward(self, x):
            identity = x if self.downsample is None else self.downsample(x)
            out = self.act(self.norm1(self.conv1(x)))
            out = self.norm2(self.conv2(out))
            return self.act(out + identity)

    class BoardDetectionCNN(nn.Module):
        """Nine-by-nine keypoint heatmap network."""

        def __init__(self, keypoints=num_keypoints):
            super().__init__()
            self.stem = nn.Sequential(
                ConvBlock(3, 32, stride=2),        # 256
                ResidualBlock(32),
            )
            self.stage2 = nn.Sequential(
                ConvBlock(32, 64, stride=2),       # 128
                ResidualBlock(64),
            )
            self.stage3 = nn.Sequential(
                ConvBlock(64, 128, stride=2),      # 64
                ResidualBlock(128),
            )
            self.stage4 = nn.Sequential(
                ConvBlock(128, 256, stride=2),     # 32
                ResidualBlock(256),
            )
            # Lightweight FPN: fuse 1/4, 1/8, 1/16 and 1/32 features.
            self.lateral2 = nn.Conv2d(64, 64, 1)
            self.lateral3 = nn.Conv2d(128, 64, 1)
            self.lateral4 = nn.Conv2d(256, 64, 1)
            self.fuse = nn.Sequential(
                ConvBlock(64, 64),
                ConvBlock(64, 64),
            )
            self.head = nn.Sequential(
                ConvBlock(64, 64),
                nn.Conv2d(64, keypoints, 1),
            )

        def forward(self, x):
            c2 = self.stage2(self.stem(x))          # 1/4
            c3 = self.stage3(c2)                    # 1/8
            c4 = self.stage4(c3)                    # 1/16
            target_size = c2.shape[-2:]
            fused = self.lateral2(c2)
            for lateral, source in (
                (self.lateral3, c3),
                (self.lateral4, c4),
            ):
                upscaled = F.interpolate(
                    lateral(source), size=target_size,
                    mode="bilinear", align_corners=False,
                )
                fused = fused + upscaled
            features = self.fuse(fused)
            heatmaps = self.head(features)
            return F.interpolate(
                heatmaps, size=(
                    KEYPOINT_HEATMAP_SIZE, KEYPOINT_HEATMAP_SIZE
                ),
                mode="bilinear", align_corners=False,
            )

    return BoardDetectionCNN(num_keypoints)


# --------------------------------------------------------------------------
# Inference helpers shared by the GUI and the trainer's evaluation
# --------------------------------------------------------------------------
def preprocess_screenshot(image, size=KEYPOINT_INPUT_SIZE):
    """BGR uint8 screenshot -> normalised ``(1, 3, S, S)`` float32 batch.

    Uses Pillow for the resize so the letterboxing/pixel mapping matches
    :func:`prepare_sample_image`, which the training data went through.
    """
    from PIL import Image

    array = np.asarray(image)
    if array.ndim == 2:
        array = np.stack([array] * 3, axis=-1)
    if array.shape[2] == 4:
        array = array[:, :, :3]
    # Callers pass OpenCV's BGR convention.
    rgb = array[:, :, ::-1]
    prepared, _ = prepare_sample_image(Image.fromarray(rgb), size)
    return prepared[None, ...]


def prepare_sample_image(pil_image, size=KEYPOINT_INPUT_SIZE):
    """Resize + pad to ``size`` and return ``(tensor_array, scale_metadata)``.

    The screenshot is letterboxed (aspect ratio preserved, black bars) and
    the metadata records the scale/offset so predicted 0..1 coordinates can
    be mapped back to the original screenshot exactly.
    """
    pil_image = pil_image.convert("RGB")
    width, height = pil_image.size
    scale = min(size / max(width, 1), size / max(height, 1))
    new_w = max(1, int(round(width * scale)))
    new_h = max(1, int(round(height * scale)))
    resized = pil_image.resize((new_w, new_h))
    canvas = np.zeros((size, size, 3), dtype=np.float32)
    offset_x = (size - new_w) // 2
    offset_y = (size - new_h) // 2
    canvas[offset_y:offset_y + new_h, offset_x:offset_x + new_w] = (
        np.asarray(resized, dtype=np.float32) / 255.0
    )
    mean = np.asarray(IMAGENET_MEAN, dtype=np.float32)
    std = np.asarray(IMAGENET_STD, dtype=np.float32)
    normalised = (canvas - mean) / std
    tensor = np.transpose(normalised, (2, 0, 1)).astype(np.float32)
    metadata = {
        "scale": float(scale),
        "offset": (int(offset_x), int(offset_y)),
        "size": (int(size), int(size)),
        "original_size": (int(width), int(height)),
    }
    return tensor, metadata


def map_points_to_image(points, metadata):
    """Undo the letterbox: 0..1 model space -> original screenshot pixels."""
    points = np.asarray(points, dtype=np.float64).reshape(KEYPOINT_COUNT, 2)
    size = float(metadata["size"][0])
    scale = float(metadata["scale"]) or 1.0
    offset_x, offset_y = metadata["offset"]
    x = (points[:, 0] * size - offset_x) / scale
    y = (points[:, 1] * size - offset_y) / scale
    return np.stack([x, y], axis=1)


def detect_board_with_model(image, model, device=None, threshold=KEYPOINT_PEAK_THRESHOLD):
    """Run a loaded board-detection model on one BGR screenshot.

    Returns a detection dict (``points``, ``homography``, ``corners``,
    ``board_rect``, ``confidence`` ...) or ``None`` when the lattice cannot
    be fitted. The four outer corners are still reported for code that only
    understands a rectangle, but they come from the fitted lattice.
    """
    import torch

    if model is None:
        return None
    batch = preprocess_screenshot(image)
    if device is None:
        device = next(
            (parameter.device for parameter in model.parameters()),
            torch.device("cpu"),
        )
    tensor = torch.from_numpy(batch).to(device)
    with torch.no_grad():
        heatmaps = model(tensor)
    heatmaps = heatmaps[0].detach().cpu().numpy()
    normalised, confidence, visibility = heatmaps_to_points(
        heatmaps, threshold=threshold
    )
    # Fit once, in image pixels, so residuals/thresholds are in pixels.
    height, width = np.asarray(image).shape[:2]
    pixel_points = normalised * np.asarray([width, height], dtype=np.float64)
    pixel_fit = lattice_fit(pixel_points, visibility)
    if pixel_fit is None:
        return None
    fitted = pixel_fit["points"]
    corners = corners_from_points(fitted)
    rect = board_rect_from_points(fitted)
    return {
        "points": fitted,
        "raw_points": pixel_points,
        "homography": pixel_fit["homography"],
        "corners": corners,
        "board_rect": rect,
        "confidence": float(pixel_fit["confidence"]),
        "mean_error": float(pixel_fit["mean_error"]),
        "inliers": pixel_fit["inliers"],
        "inlier_count": int(pixel_fit["inlier_count"]),
        "keypoint_confidence": confidence,
        "visibility": visibility,
        "image_size": (int(width), int(height)),
        "method": "board_model",
    }


def square_side(points):
    """Approximate board side in pixels, from the outer lattice ring."""
    rect = board_rect_from_points(points)
    return float(min(rect[2], rect[3]))
