"""Frozen exhaustive Advisor matcher for optimization equivalence tests only."""

def exhaustive_find_move_sequence(board, candidate_grid, max_plies=2):
    """Unique 0/1/2-ply legal sequence shown by an Advisor screenshot.

    Advisor polls only after the player releases the mouse. In a fast
    game that screenshot can already contain both the player's move and
    an instant/premoved reply, so fall back to a unique two-ply sequence
    when no single move explains the observed changed squares.
    """
    if board is None or candidate_grid is None:
        return None
    before_grid = _autoplay_board_grid_from_board(board)
    if candidate_grid == before_grid:
        return []

    matches = _autoplay_matching_legal_moves(board, candidate_grid)
    if len(matches) == 1:
        return matches
    if len(matches) > 1 or int(max_plies or 0) < 2:
        return None

    sequences = []
    for first_move in board.legal_moves:
        clone = board.copy(stack=False)
        clone.push(first_move)
        for second_move in _autoplay_matching_legal_moves(clone, candidate_grid):
            sequences.append([first_move, second_move])
            if len(sequences) > 1:
                return None
    if len(sequences) == 1:
        return sequences[0]

    # A site's checkmate/game-over banner can recolour or cover squares
    # unrelated to the mating move. If no ordinary full-board sequence
    # matches, accept a checkmating sequence only when every square it
    # changes is positively observed and that sequence is unique. This
    # preserves Advisor's required full stop on checkmate without using
    # the looser test for ordinary moves.
    terminal_sequences = []
    for move in board.legal_moves:
        clone = board.copy(stack=False)
        clone.push(move)
        if not clone.is_checkmate():
            continue
        after_grid = _autoplay_board_grid_from_board(clone)
        if _autoplay_transition_matches(candidate_grid, after_grid, before_grid):
            terminal_sequences.append([move])
            if len(terminal_sequences) > 1:
                return None
    if len(terminal_sequences) == 1:
        return terminal_sequences[0]

    for first_move in board.legal_moves:
        first_clone = board.copy(stack=False)
        first_clone.push(first_move)
        if first_clone.is_game_over():
            continue
        for second_move in first_clone.legal_moves:
            second_clone = first_clone.copy(stack=False)
            second_clone.push(second_move)
            if not second_clone.is_checkmate():
                continue
            after_grid = _autoplay_board_grid_from_board(second_clone)
            if _autoplay_transition_matches(
                candidate_grid, after_grid, before_grid
            ):
                terminal_sequences.append([first_move, second_move])
                if len(terminal_sequences) > 1:
                    return None
    return terminal_sequences[0] if len(terminal_sequences) == 1 else None

