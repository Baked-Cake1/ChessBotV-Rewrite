def _autoplay_candidate_matches(candidate_grid, after_grid, before_grid):
    """True if a partially-observed grid uniquely supports an end grid.

    `?` cells are wildcards (useful while highlights/animations settle),
    but at least one known cell that changed from before->after must
    positively support the move. That prevents an all-unknown changed
    region from being mistaken for a successful move.
    """
    support = 0
    for row in range(8):
        for col in range(8):
            observed = candidate_grid[row][col]
            if observed == "?":
                continue
            if observed != after_grid[row][col]:
                return False
            if after_grid[row][col] != before_grid[row][col]:
                support += 1
    return support > 0


def _autoplay_transition_matches(candidate_grid, after_grid, before_grid):
    """True when every square changed by a move is observed correctly.

    This narrower check is used only for a known checkmating Auto-play
    move. A site's game-over overlay can cover or recolour unrelated
    squares immediately after mate, but the squares that actually
    changed (origin, destination, captured/castling/en-passant squares)
    must still positively match the move before it is accepted.
    """
    changed = 0
    for row in range(8):
        for col in range(8):
            if after_grid[row][col] == before_grid[row][col]:
                continue
            changed += 1
            if candidate_grid[row][col] != after_grid[row][col]:
                return False
    return changed > 0


def _autoplay_matching_legal_moves(board, candidate_grid):
    """All legal moves whose resulting placement matches observations."""
    before_grid = _autoplay_board_grid_from_board(board)
    matches = []
    for move in board.legal_moves:
        clone = board.copy(stack=False)
        clone.push(move)
        after_grid = _autoplay_board_grid_from_board(clone)
        if _autoplay_candidate_matches(candidate_grid, after_grid, before_grid):
            matches.append(move)
    return matches


def _autoplay_find_legal_move(board, candidate_grid):
    """The uniquely matching legal move, or None if ambiguous/unmatched."""
    matches = _autoplay_matching_legal_moves(board, candidate_grid)
    return matches[0] if len(matches) == 1 else None


# --- Fast integer Advisor/Auto-play matchers -----------------------------
#
# The grid functions above are the readable reference implementation. A live
# Advisor/Auto-play poll uses the helpers below instead, because Advisor has
# to explain a screenshot that may already contain two plies: that means
# matching every candidate reply of every candidate first move. Building a
# fresh 8x8 string grid plus a python-chess board copy for each candidate
# cost milliseconds of interpreter time on the poll worker, and the pygame
# thread stuttered while it waited for the GIL.
#
# These helpers carry the same information as flat 64-int tuples (one cell
# code per square, -1 for "unknown") and apply a legal move's placement
# changes directly, so no board copy and no 64-square piece walk is needed
# per candidate. Cell 0 is a8 and cell 63 is h1, matching
# ``_autoplay_board_grid_from_board``.

# Cell codes: 0 empty, 1-6 white P/N/B/R/Q/K, 7-12 the same black pieces,
# -1 unknown. A promoted pawn therefore keeps the code of the piece that
# actually stands on that square in the resulting grid.
_GRID_CELL_CODES = {
    ".": 0,
    "P": 1, "N": 2, "B": 3, "R": 4, "Q": 5, "K": 6,
    "p": 7, "n": 8, "b": 9, "r": 10, "q": 11, "k": 12,
    "?": -1,
}
_WHITE_CELL_CODES = frozenset(range(1, 7))
_ALL_CELL_INDICES = tuple(range(64))
_CELL_BIT = tuple(1 << index for index in range(64))


def _grid_cells(grid):
    """Flatten an 8x8 observation grid to 64 cell codes (-1 = unknown)."""
    codes = _GRID_CELL_CODES
    cells = []
    append = cells.append
    for row in grid:
        for value in row:
            if isinstance(value, str):
                append(codes.get(value, -1))
            else:
                append(-1)
    return tuple(cells)


def _board_cells(board):
    """The same 64 cell codes for a python-chess board."""
    cells = []
    append = cells.append
    for rank_idx in range(7, -1, -1):
        for file_idx in range(8):
            piece = board.piece_at(chess.square(file_idx, rank_idx))
            if piece is None:
                append(0)
            else:
                append(
                    int(piece.piece_type)
                    if piece.color
                    else int(piece.piece_type) + 6
                )
    return tuple(cells)


def _changed_cell_indices(before_cells, after_cells):
    """Indices whose symbols differ between two 64-cell tuples."""
    return tuple(
        index
        for index in range(64)
        if before_cells[index] != after_cells[index]
    )


def _cells_match(candidate_cells, after_cells, before_cells):
    """Integer form of :func:`_autoplay_candidate_matches`."""
    support = 0
    for index in range(64):
        observed = candidate_cells[index]
        if observed < 0:
            continue
        if observed != after_cells[index]:
            return False
        if after_cells[index] != before_cells[index]:
            support += 1
    return support > 0


def _transition_cells_match(candidate_cells, after_cells, before_cells,
                            changed_cells=None):
    """Integer form of :func:`_autoplay_transition_matches`."""
    if changed_cells is None:
        changed_cells = _changed_cell_indices(before_cells, after_cells)
    if not changed_cells:
        return False
    for index in changed_cells:
        if candidate_cells[index] != after_cells[index]:
            return False
    return True


def _placement_layout(board):
    """Piece-type/colour bitboards plus chess-square -> cell-index lookup."""
    pairs = []
    for is_white in (True, False):
        for piece_type in range(1, 7):
            mask = board.pieces_mask(piece_type, is_white)
            if mask:
                pairs.append((piece_type, is_white, mask))
    squares = tuple(
        (7 - (square >> 3)) * 8 + (square & 7) for square in range(64)
    )
    return pairs, squares


def _cells_after_move(board, base_cells, move, layout):
    """Cell tuple after ``move``, without copying or pushing a board.

    A legal move can only alter its origin, its destination, the captured
    piece there, the castling rook and (en passant) the passed pawn, so the
    rest of ``base_cells`` is reused unchanged.
    """
    pairs, squares = layout
    cells = list(base_cells)
    from_square = int(move.from_square)
    to_square = int(move.to_square)
    moved = 0
    is_white_mover = True
    bit = 1 << from_square
    for piece_type, is_white, mask in pairs:
        if mask & bit:
            is_white_mover = is_white
            moved = piece_type if is_white else piece_type + 6
            break
    if not moved:
        # Unknown origin (should not happen for a legal move): keep the
        # observed symbol so the comparison stays conservative.
        moved = base_cells[squares[from_square]]
        if moved < 0:
            moved = 0
        is_white_mover = moved in _WHITE_CELL_CODES
    from_index = squares[from_square]
    to_index = squares[to_square]
    cells[from_index] = 0
    cells[to_index] = moved
    if board.is_castling(move):
        if to_square > from_square:
            rook_from, rook_to = from_square + 3, to_square - 1
        else:
            rook_from, rook_to = from_square - 4, to_square + 1
        rook_from_index = squares[rook_from]
        rook_cell = base_cells[rook_from_index]
        if rook_cell > 0:
            cells[rook_from_index] = 0
            cells[squares[rook_to]] = rook_cell
    elif board.is_en_passant(move):
        passed = to_square - 8 if board.turn == chess.WHITE else to_square + 8
        cells[squares[passed]] = 0
    promotion = getattr(move, "promotion", None)
    if promotion:
        cells[to_index] = (
            int(promotion) if is_white_mover else int(promotion) + 6
        )
    return cells


def _advisor_changed_cell_set(board, move, layout):
    """Cell indices a legal reply can alter, mirroring the board mask test."""
    _pairs, squares = layout
    if board.is_castling(move):
        return _ALL_CELL_INDICES
    changed = {squares[int(move.from_square)], squares[int(move.to_square)]}
    if board.is_en_passant(move):
        to_square = int(move.to_square)
        passed = to_square - 8 if board.turn == chess.WHITE else to_square + 8
        changed.add(squares[passed])
    return changed


def _advisor_changed_cell_mask(board, move, layout):
    """Bitmask (over cell indices) of the squares a reply can alter."""
    mask = 0
    for index in _advisor_changed_cell_set(board, move, layout):
        mask |= _CELL_BIT[index]
    return mask


def _advisor_move_changed_mask(board, move):
    """Conservative set of squares a legal reply can alter, without pushing.

    Castling is deliberately unpruned (including Chess960 rook movement).
    En passant also alters the captured pawn's square, not just from/to.
    """
    if board.is_castling(move):
        return chess.BB_ALL
    mask = chess.BB_SQUARES[move.from_square] | chess.BB_SQUARES[move.to_square]
    if board.is_en_passant(move):
        captured = move.to_square - 8 if board.turn == chess.WHITE else move.to_square + 8
        mask |= chess.BB_SQUARES[captured]
    return mask


def _advisor_find_move_sequence_cells(
    board, candidate_cells, max_plies=2, layout=None, before_cells=None
):
    """Fast path of :func:`_advisor_find_move_sequence`.

    Returns the same list/None result without building a string grid or
    copying a board for the second-ply mismatch pruning.
    """
    if board is None or candidate_cells is None or len(candidate_cells) != 64:
        return None
    if layout is None:
        layout = _placement_layout(board)
    if before_cells is None:
        before_cells = _board_cells(board)
    if candidate_cells == before_cells:
        return []

    # Build first-ply cells once and reuse them for ordinary matching and
    # the separate checkmate-banner recovery pass.
    first_nodes = []
    matches = []
    for move in board.legal_moves:
        cells = _cells_after_move(board, before_cells, move, layout)
        first_nodes.append((move, cells))
        if _cells_match(candidate_cells, cells, before_cells):
            matches.append(move)
    if len(matches) == 1:
        return matches
    if len(matches) > 1 or int(max_plies or 0) < 2:
        return None
    if not first_nodes:
        return None

    probe = board.copy(stack=False)
    try:
        sequences = []
        for first_move, first_cells in first_nodes:
            probe.push(first_move)
            # Every known mismatch must be fixable by this one remaining
            # reply. Unknown cells impose no restriction; the full match
            # below still enforces positive evidence and uniqueness.
            mismatches = 0
            for index in range(64):
                observed = candidate_cells[index]
                if observed >= 0 and observed != first_cells[index]:
                    mismatches |= _CELL_BIT[index]
            for second_move in probe.legal_moves:
                if mismatches & ~_advisor_changed_cell_mask(
                    probe, second_move, layout
                ):
                    continue
                second_cells = _cells_after_move(
                    probe, first_cells, second_move, layout
                )
                if _cells_match(candidate_cells, second_cells, first_cells):
                    sequences.append([first_move, second_move])
                    if len(sequences) > 1:
                        return None
            probe.pop()
    finally:
        del probe
    if len(sequences) == 1:
        return sequences[0]

    # A site's checkmate/game-over banner can recolour or cover squares
    # unrelated to the mating move. If no ordinary full-board sequence
    # matches, accept a checkmating sequence only when every square it
    # changes is positively observed and that sequence is unique. This
    # preserves Advisor's required full stop on checkmate without using
    # the looser test for ordinary moves.
    terminal_sequences = []
    for move, cells in first_nodes:
        probe.push(move)
        try:
            is_mate = probe.is_checkmate()
        finally:
            probe.pop()
        if not is_mate:
            continue
        if _transition_cells_match(candidate_cells, cells, before_cells):
            terminal_sequences.append([move])
            if len(terminal_sequences) > 1:
                return None
    if len(terminal_sequences) == 1:
        return terminal_sequences[0]

    for first_move, first_cells in first_nodes:
        probe.push(first_move)
        try:
            if probe.is_game_over():
                continue
            for second_move in probe.legal_moves:
                # Mate must give check. Avoid copying/pushing non-checks in
                # this fallback; do not use full-grid mismatch pruning here
                # because banners are allowed to obscure unrelated squares.
                if not probe.gives_check(second_move):
                    continue
                second_cells = _cells_after_move(
                    probe, first_cells, second_move, layout
                )
                probe.push(second_move)
                try:
                    is_mate = probe.is_checkmate()
                finally:
                    probe.pop()
                if not is_mate:
                    continue
                if _transition_cells_match(
                    candidate_cells, second_cells, first_cells
                ):
                    terminal_sequences.append([first_move, second_move])
                    if len(terminal_sequences) > 1:
                        return None
        finally:
            probe.pop()
    return terminal_sequences[0] if len(terminal_sequences) == 1 else None


def _advisor_find_move_sequence(board, candidate_grid, max_plies=2):
    """Unique 0/1/2-ply legal sequence shown by an Advisor screenshot.

    Advisor polls only after the player releases the mouse. In a fast
    game that screenshot can already contain both the player's move and
    an instant/premoved reply, so fall back to a unique two-ply sequence
    when no single move explains the observed changed squares.
    """
    if board is None or candidate_grid is None:
        return None
    try:
        candidate_cells = _grid_cells(candidate_grid)
    except Exception:
        return None
    if len(candidate_cells) != 64:
        return None
    return _advisor_find_move_sequence_cells(
        board, candidate_cells, max_plies=max_plies
    )
