import ctypes
import json
import math
import os
import subprocess
import sys
import threading
import time

import chess
import pygame

# --- Configuration & Setup ---
ENGINE_PATH = r"C:\Users\Blake\Documents\_Engines\Chess\Stockfish\Stockfish Dev\stockfish-windows-x86-64-universal\stockfish\stockfish-windows-x86-64-universal.exe"
SQUARE_SIZE = 80
BOARD_WIDTH = SQUARE_SIZE * 8
EVAL_BAR_WIDTH = 32
EVAL_BAR_GAP = 22
PADDING = 24
TOP_BAR_HEIGHT = 44
INFO_HEIGHT = 152

# Window Dimensions
WIDTH = PADDING + EVAL_BAR_WIDTH + EVAL_BAR_GAP + BOARD_WIDTH + PADDING
HEIGHT = TOP_BAR_HEIGHT + PADDING + BOARD_WIDTH + INFO_HEIGHT

FPS = 60

# Colors
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
LIGHT_SQ = (240, 217, 181)
DARK_SQ = (181, 136, 99)
HIGHLIGHT_COLOR = (205, 210, 106, 150)
LEGAL_MOVE_COLOR = (0, 255, 0, 100)
CAPTURE_MOVE_COLOR = (255, 0, 0, 120)
ENGINE_ARROW_COLOR = (0, 100, 255, 180)
BG_COLOR = (40, 40, 40)
TEXT_COLOR = (220, 220, 220)
TEXT_MUTED = (150, 150, 150)
TEXT_ACCENT = (150, 255, 150)
TEXT_STATUS = (255, 100, 100)
DRAW_COLOR = (200, 200, 120)
BUTTON_COLOR = (70, 70, 70)
BUTTON_HOVER_COLOR = (90, 90, 90)
MENU_BG_COLOR = (50, 50, 50)
MENU_BORDER_COLOR = (100, 100, 100)
DIVIDER_COLOR = (70, 70, 70)
BOARD_BORDER_COLOR = (25, 25, 25)
EVAL_MARKER_COLOR = (255, 190, 30)
EVAL_MARKER_HIGHLIGHT = (255, 240, 140)
GEAR_COLOR = (225, 225, 225)
GEAR_COLOR_HOVER = (255, 255, 255)

pygame.init()
pygame.display.set_caption("ChessBotV Rewrite")
screen = pygame.display.set_mode((WIDTH, HEIGHT))
font_large = pygame.font.SysFont("segoeui", 24, bold=True)
font_small = pygame.font.SysFont("segoeui", 18)
font_tiny = pygame.font.SysFont("segoeui", 15)


# --- Ctypes: Always on Top ---
def set_always_on_top():
    try:
        hwnd = pygame.display.get_wm_info()["window"]
        ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 3)
    except Exception:
        pass


set_always_on_top()


# --- Loaders ---
ENGINE_DEFAULTS = {"hash": 2048, "threads": 4, "skill": 20}


def load_config():
    cfg = dict(ENGINE_DEFAULTS)
    config_files = ["configuration.cfg", "config.cfg"]

    for config_file in config_files:
        try:
            with open(config_file, "r") as f:
                for line in f:
                    if "=" in line:
                        k, v = line.strip().split("=")
                        if k.lower() in cfg:
                            cfg[k.lower()] = int(v)
            print(f"Loaded configuration from {config_file}")
            return cfg
        except FileNotFoundError:
            continue

    print("No configuration file found. Using defaults.")
    return cfg


def save_config():
    """Persist engine settings to configuration.cfg."""
    try:
        with open("configuration.cfg", "w") as f:
            f.write(f"hash={config['hash']}\n")
            f.write(f"threads={config['threads']}\n")
            f.write(f"skill={config['skill']}\n")
    except OSError as e:
        print(f"Failed to save configuration: {e}")


def load_openings():
    try:
        with open("openings.json", "r") as f:
            return json.load(f)
    except FileNotFoundError:
        print("openings.json not found.")
        return {}


# --- Board Themes ---
# Built-in themes live in pieces/<theme>/, while chess.com piece styles live in
# pieces/chesscom/<style>/ (e.g. pieces/chesscom/8_bit/bq.png). Every subfolder
# of pieces/chesscom/ is automatically offered as a selectable theme entry.

_THEME_NAME_OVERRIDES = {
    "chesskid": "ChessKid",
    "8_bit": "8-Bit",
}


def get_piece_dir(theme):
    """Return the folder holding the piece PNGs for a theme id."""
    return os.path.join("pieces", *theme.split("/"))


def format_theme_name(folder_name):
    """Prettify a folder name for display: '8_bit' -> '8-Bit'."""
    if folder_name in _THEME_NAME_OVERRIDES:
        return _THEME_NAME_OVERRIDES[folder_name]
    return folder_name.replace("_", " ").strip().title()


def get_available_themes():
    """Return a list of (theme_id, display_name) for every piece set on disk."""
    themes = []

    # Built-in themes: every folder directly under pieces/ except chesscom.
    # 'classic' is pinned first to match the original menu ordering.
    builtin = []
    if os.path.isdir("pieces"):
        for entry in sorted(os.listdir("pieces")):
            full = os.path.join("pieces", entry)
            if entry == "chesscom" or not os.path.isdir(full):
                continue
            builtin.append((entry, format_theme_name(entry)))
    builtin.sort(key=lambda t: (t[0] != "classic", t[0]))
    themes.extend(builtin)

    # Chess.com styles: every subfolder of pieces/chesscom is one entry.
    chesscom_dir = os.path.join("pieces", "chesscom")
    if os.path.isdir(chesscom_dir):
        for entry in sorted(os.listdir(chesscom_dir)):
            if os.path.isdir(os.path.join(chesscom_dir, entry)):
                display = format_theme_name(entry)
                # Keep the label unique if it clashes with a built-in theme.
                if any(display == name for _, name in themes):
                    display = f"Chess.com {display}"
                themes.append((f"chesscom/{entry}", display))

    # Fallback so the menu is never empty.
    if not themes:
        themes.append(("classic", "Classic"))
    return themes


def load_images(theme="classic"):
    pieces = ["bb", "bn", "br", "bk", "bq", "bp", "wb", "wn", "wr", "wk", "wq", "wp"]
    piece_dir = get_piece_dir(theme)
    images = {}
    for p in pieces:
        path = os.path.join(piece_dir, f"{p}.png")
        try:
            img = pygame.image.load(path).convert_alpha()
            images[p] = pygame.transform.smoothscale(img, (SQUARE_SIZE, SQUARE_SIZE))
        except FileNotFoundError:
            surf = pygame.Surface((SQUARE_SIZE, SQUARE_SIZE), pygame.SRCALPHA)
            pygame.draw.circle(
                surf,
                (255, 0, 0) if p.startswith("b") else (0, 255, 0),
                (SQUARE_SIZE // 2, SQUARE_SIZE // 2),
                SQUARE_SIZE // 3,
            )
            images[p] = surf
    return images


# Board theme state
current_theme = "classic"
available_themes = get_available_themes()
if available_themes and all(t[0] != current_theme for t in available_themes):
    # Default theme folder missing -> use the first theme found on disk.
    current_theme = available_themes[0][0]

# Board theme menu layout & scrolling
THEME_LIST_TOP = 120
THEME_ITEM_HEIGHT = 50
THEME_ITEM_GAP = 20
THEME_LIST_BOTTOM = HEIGHT - 44
theme_scroll_offset = 0.0
theme_scroll_speed = 70.0

# Settings menu state
settings_menu_state = "main"
settings_menu_target = "board_theme"
menu_transition_progress = 0.0
menu_transition_speed = 0.08
menu_transition_direction = "none"

images = load_images(current_theme)
config = load_config()
openings = load_openings()

# Live engine handle (assigned in main()); used by the Engine Settings page.
engine_thread = None

# --- Layout constants (shared between draw_gui and event handling) ---
BOARD_OX = PADDING + EVAL_BAR_WIDTH + EVAL_BAR_GAP
BOARD_OY = TOP_BAR_HEIGHT + PADDING

# --- Menu System ---
settings_menu_open = False
_button_y = (TOP_BAR_HEIGHT - 30) // 2
settings_button_rect = pygame.Rect(
    WIDTH - PADDING - 50, _button_y, 50, 30
)
new_game_button_rect = pygame.Rect(
    settings_button_rect.x - 10 - 50, _button_y, 50, 30
)

menu_anim_progress = 0.0
menu_anim_opening = False
menu_anim_closing = False
menu_anim_speed = 0.05

# --- Opening Tracking ---
current_opening_name = ""


# --- Math & Animations ---
def cubic_ease_in_out(t):
    if t < 0.5:
        return 4 * t * t * t
    else:
        f = (2 * t) - 2
        return 0.5 * f * f * f + 1


class AnimationManager:
    def __init__(self):
        self.move_anim = None
        self.rook_anim = None
        self.capture_anims = []

        self.current_eval = 0.0
        self.target_eval = 0.0
        self.eval_interpolation_speed = 0.08

        # Engine arrow animation state.
        self.arrow_start = None
        self.arrow_end = None
        self.arrow_target_start = None
        self.arrow_target_end = None
        self.arrow_alpha = 0.0
        self.arrow_target_alpha = 0.0
        self.arrow_fade_speed = 0.15
        self.arrow_morph_speed = 0.18

    def update(self):
        if self.move_anim:
            self.move_anim["progress"] += self.move_anim["speed"]
            if self.move_anim["progress"] >= 1.0:
                self.move_anim = None

        if self.rook_anim:
            self.rook_anim["progress"] += self.rook_anim["speed"]
            if self.rook_anim["progress"] >= 1.0:
                self.rook_anim = None

        for anim in self.capture_anims[:]:
            anim["progress"] += anim["speed"]
            if anim["progress"] >= 1.0:
                self.capture_anims.remove(anim)

        if abs(self.current_eval - self.target_eval) > 0.01:
            self.current_eval += (
                self.target_eval - self.current_eval
            ) * self.eval_interpolation_speed
        else:
            self.current_eval = self.target_eval

        if abs(self.arrow_alpha - self.arrow_target_alpha) > 0.5:
            self.arrow_alpha += (
                self.arrow_target_alpha - self.arrow_alpha
            ) * self.arrow_fade_speed
        else:
            self.arrow_alpha = self.arrow_target_alpha

        if (
            self.arrow_start is not None
            and self.arrow_end is not None
            and self.arrow_target_start is not None
            and self.arrow_target_end is not None
        ):
            ds = self.arrow_target_start - self.arrow_start
            if ds.length_squared() > 1.0:
                self.arrow_start += ds * self.arrow_morph_speed
            else:
                self.arrow_start = self.arrow_target_start.copy()

            de = self.arrow_target_end - self.arrow_end
            if de.length_squared() > 1.0:
                self.arrow_end += de * self.arrow_morph_speed
            else:
                self.arrow_end = self.arrow_target_end.copy()

    def set_arrow_target(self, start_pos, end_pos, visible):
        if not visible:
            self.arrow_target_alpha = 0.0
            return

        new_start = pygame.Vector2(start_pos)
        new_end = pygame.Vector2(end_pos)

        if self.arrow_target_start is None or self.arrow_alpha < 10:
            self.arrow_start = new_start.copy()
            self.arrow_end = new_end.copy()

        self.arrow_target_start = new_start
        self.arrow_target_end = new_end
        self.arrow_target_alpha = 255.0

    def add_move(
        self,
        piece,
        start_sq,
        end_sq,
        is_castling=False,
        rook_start_sq=None,
        rook_target_sq=None,
        rook_piece=None,
    ):
        sx = (chess.square_file(start_sq) * SQUARE_SIZE) + BOARD_OX
        sy = ((7 - chess.square_rank(start_sq)) * SQUARE_SIZE) + BOARD_OY
        ex = (chess.square_file(end_sq) * SQUARE_SIZE) + BOARD_OX
        ey = ((7 - chess.square_rank(end_sq)) * SQUARE_SIZE) + BOARD_OY

        self.move_anim = {
            "piece": piece,
            "start": (sx, sy),
            "end": (ex, ey),
            "progress": 0.0,
            "speed": 0.04,
            "target_sq": end_sq,
            "is_castling": is_castling,
            "rook_target_sq": rook_target_sq,
        }

        if (
            is_castling
            and rook_start_sq is not None
            and rook_target_sq is not None
            and rook_piece is not None
        ):
            rx = (chess.square_file(rook_start_sq) * SQUARE_SIZE) + BOARD_OX
            ry = ((7 - chess.square_rank(rook_start_sq)) * SQUARE_SIZE) + BOARD_OY
            rex = (chess.square_file(rook_target_sq) * SQUARE_SIZE) + BOARD_OX
            rey = ((7 - chess.square_rank(rook_target_sq)) * SQUARE_SIZE) + BOARD_OY

            self.rook_anim = {
                "piece": rook_piece,
                "start": (rx, ry),
                "end": (rex, rey),
                "progress": 0.0,
                "speed": 0.04,
                "target_sq": rook_target_sq,
            }

    def add_capture(self, piece, sq):
        x = (chess.square_file(sq) * SQUARE_SIZE) + BOARD_OX
        y = ((7 - chess.square_rank(sq)) * SQUARE_SIZE) + BOARD_OY
        self.capture_anims.append(
            {"piece": piece, "pos": (x, y), "progress": 0.0, "speed": 0.03}
        )

    def set_target_eval(self, eval_score):
        self.target_eval = eval_score


class CheckEffect:
    """Rich check / checkmate animation.

    - Under pieces: soft radial red glow on the king square
    - Over pieces:  pulsing corner brackets + expanding ripple rings
    - Checkmate extras: faster/more rings, brighter brackets, and a
      pulsing red border around the entire board.
    """

    def __init__(self):
        self.active = False
        self.is_mate = False
        self.king_sq = None
        self.origin_sq = None
        self.time = 0.0
        self.spawn_timer = 0.0
        self.rings = []

    def reset(self):
        self.active = False
        self.is_mate = False
        self.king_sq = None
        self.origin_sq = None
        self.time = 0.0
        self.spawn_timer = 0.0
        self.rings.clear()

    def pulse(self):
        return (math.sin(self.time * 5.5) + 1.0) / 2.0

    def update(self, is_check, is_mate, king_sq):
        if (is_check or is_mate) and king_sq is not None:
            if not self.active:
                self.active = True
                self.spawn_timer = 0.0
                self.rings.append(
                    {
                        "progress": 0.0,
                        "max_radius": SQUARE_SIZE * (2.2 if is_mate else 1.6),
                    }
                )
            self.is_mate = is_mate
            self.king_sq = king_sq
            self.origin_sq = king_sq
            self.time += 0.016

            self.spawn_timer += 0.016
            interval = 0.42 if is_mate else 0.75
            if self.spawn_timer >= interval:
                self.spawn_timer = 0.0
                self.rings.append(
                    {
                        "progress": 0.0,
                        "max_radius": SQUARE_SIZE * (2.2 if is_mate else 1.6),
                    }
                )

            for ring in self.rings[:]:
                ring["progress"] += 0.022
                if ring["progress"] >= 1.0:
                    self.rings.remove(ring)
        else:
            self.king_sq = None
            if self.active:
                for ring in self.rings[:]:
                    ring["progress"] += 0.022
                    if ring["progress"] >= 1.0:
                        self.rings.remove(ring)
                if not self.rings:
                    self.active = False
                    self.origin_sq = None


# --- Cached surfaces for check effect rendering ---
_check_under_surf = None
_check_over_surf = None
_check_border_surf = None


def _get_check_under_surf():
    global _check_under_surf
    if _check_under_surf is None:
        _check_under_surf = pygame.Surface(
            (SQUARE_SIZE, SQUARE_SIZE), pygame.SRCALPHA
        )
    else:
        _check_under_surf.fill((0, 0, 0, 0))
    return _check_under_surf


def _get_check_over_surf():
    global _check_over_surf
    if _check_over_surf is None:
        _check_over_surf = pygame.Surface(
            (BOARD_WIDTH, BOARD_WIDTH), pygame.SRCALPHA
        )
    else:
        _check_over_surf.fill((0, 0, 0, 0))
    return _check_over_surf


def _get_check_border_surf():
    global _check_border_surf
    size = BOARD_WIDTH + 12
    if _check_border_surf is None:
        _check_border_surf = pygame.Surface((size, size), pygame.SRCALPHA)
    else:
        _check_border_surf.fill((0, 0, 0, 0))
    return _check_border_surf


def draw_check_effect_under(screen, effect, board_ox, board_oy):
    """Soft radial red glow on the king square, drawn beneath the pieces."""
    if not effect.active or effect.king_sq is None:
        return
    kx = board_ox + chess.square_file(effect.king_sq) * SQUARE_SIZE
    ky = board_oy + (7 - chess.square_rank(effect.king_sq)) * SQUARE_SIZE

    p = effect.pulse()
    if effect.is_mate:
        base_a = int(110 + 100 * p)
    else:
        base_a = int(70 + 80 * p)

    glow = _get_check_under_surf()
    half = SQUARE_SIZE // 2
    for r in range(half, 0, -2):
        t = 1.0 - (r / half)
        a = int(base_a * (0.35 + 0.65 * t))
        pygame.draw.circle(glow, (255, 40, 40, a), (half, half), r)
    screen.blit(glow, (kx, ky))


def draw_check_effect_over(screen, effect, board_ox, board_oy):
    """Corner brackets, ripple rings, and mate board border, over pieces."""
    if not effect.active:
        return

    overlay = _get_check_over_surf()
    p = effect.pulse()

    if effect.origin_sq is not None:
        ox = (
            chess.square_file(effect.origin_sq) * SQUARE_SIZE
            + SQUARE_SIZE // 2
        )
        oy = (
            (7 - chess.square_rank(effect.origin_sq)) * SQUARE_SIZE
            + SQUARE_SIZE // 2
        )
        for ring in effect.rings:
            rp = ring["progress"]
            radius = int(SQUARE_SIZE * 0.35 + rp * ring["max_radius"])
            ring_a = int(220 * (1 - rp))
            ring_w = max(1, int((5 if effect.is_mate else 3) * (1 - rp * 0.6)))
            rc = (
                (255, 60, 60, ring_a)
                if effect.is_mate
                else (255, 130, 110, ring_a)
            )
            pygame.draw.circle(overlay, rc, (ox, oy), radius, ring_w)

    if effect.king_sq is not None:
        kx = chess.square_file(effect.king_sq) * SQUARE_SIZE
        ky = (7 - chess.square_rank(effect.king_sq)) * SQUARE_SIZE
        b = 22
        s = SQUARE_SIZE
        if effect.is_mate:
            ba = min(255, int(200 + 55 * p))
            bc = (255, 80, 80, ba)
            w = 3
        else:
            ba = min(255, int(150 + 60 * p))
            bc = (255, 130, 110, ba)
            w = 2

        pygame.draw.line(overlay, bc, (kx + 1, ky + 1), (kx + 1 + b, ky + 1), w)
        pygame.draw.line(overlay, bc, (kx + 1, ky + 1), (kx + 1, ky + 1 + b), w)
        pygame.draw.line(
            overlay, bc, (kx + s - 2, ky + 1), (kx + s - 2 - b, ky + 1), w
        )
        pygame.draw.line(
            overlay, bc, (kx + s - 2, ky + 1), (kx + s - 2, ky + 1 + b), w
        )
        pygame.draw.line(
            overlay, bc, (kx + 1, ky + s - 2), (kx + 1 + b, ky + s - 2), w
        )
        pygame.draw.line(
            overlay, bc, (kx + 1, ky + s - 2), (kx + 1, ky + s - 2 - b), w
        )
        pygame.draw.line(
            overlay,
            bc,
            (kx + s - 2, ky + s - 2),
            (kx + s - 2 - b, ky + s - 2),
            w,
        )
        pygame.draw.line(
            overlay,
            bc,
            (kx + s - 2, ky + s - 2),
            (kx + s - 2, ky + s - 2 - b),
            w,
        )

    screen.blit(overlay, (board_ox, board_oy))

    if effect.is_mate:
        border_a = int(140 + 90 * p)
        border = _get_check_border_surf()
        pygame.draw.rect(
            border,
            (255, 45, 45, border_a),
            (0, 0, BOARD_WIDTH + 12, BOARD_WIDTH + 12),
            4,
            border_radius=3,
        )
        screen.blit(border, (board_ox - 6, board_oy - 6))


def get_game_status(board):
    """Return (text, color, is_terminal) describing the current game state.

    Handles checkmate, stalemate, all automatic draws, and claimable draws
    (threefold repetition, fifty-move rule) — the game ends automatically
    for any of these so play cannot continue in a finished position.
    """
    if board.is_checkmate():
        return ("Checkmate!", TEXT_STATUS, True)
    if board.is_stalemate():
        return ("Draw — Stalemate", DRAW_COLOR, True)
    if board.is_insufficient_material():
        return ("Draw — Insufficient Material", DRAW_COLOR, True)
    if board.is_seventyfive_moves():
        return ("Draw — 75-Move Rule", DRAW_COLOR, True)
    if board.is_fivefold_repetition():
        return ("Draw — Fivefold Repetition", DRAW_COLOR, True)
    if board.is_fifty_moves():
        return ("Draw — 50-Move Rule", DRAW_COLOR, True)
    if board.is_repetition(3):
        return ("Draw — Threefold Repetition", DRAW_COLOR, True)
    if board.is_check():
        return ("Check!", TEXT_STATUS, False)
    return (None, None, False)


def update_menu_animation():
    global menu_anim_progress, menu_anim_opening, menu_anim_closing, settings_menu_open
    global settings_menu_state, menu_transition_progress, menu_transition_direction
    global theme_scroll_offset

    if menu_anim_opening:
        menu_anim_progress += menu_anim_speed
        if menu_anim_progress >= 1.0:
            menu_anim_progress = 1.0
            menu_anim_opening = False
            settings_menu_open = True
    elif menu_anim_closing:
        menu_anim_progress -= menu_anim_speed
        if menu_anim_progress <= 0.0:
            menu_anim_progress = 0.0
            menu_anim_closing = False
            settings_menu_open = False
            settings_menu_state = "main"
            menu_transition_progress = 0.0
            menu_transition_direction = "none"
            theme_scroll_offset = 0.0


# --- Stockfish Threading ---
ANALYSIS_DEPTH = 25


class EngineThread:
    """Runs Stockfish in a background thread and reports live search info.

    Talks to the engine over raw UCI so every "info" line can be parsed for
    real depth, nps, score and pv while the search is still running. One
    search per position (the old version ran two full searches per update:
    one for the evaluation and a second one for the best move), and once a
    search finishes the thread idles instead of restarting the same search
    - so depth only ever climbs, and resets to 0 when a move is played.
    """

    def __init__(self, path, cfg):
        self.running = True
        self.board = chess.Board()
        self.lock = threading.Lock()
        # The main thread also talks to the engine ("stop" when a move is
        # played), so stdin writes need their own lock to never interleave.
        self.send_lock = threading.Lock()
        # Bumped every time update_position publishes a board; lets the
        # loop tell a completed search apart from one aborted by "stop".
        self.search_token = 0

        self.info = {
            "score": 0,
            "depth": 0,
            "nps": 0,
            "pv": [],
            "mate": None,
        }

        # Parameters waiting to be applied before the next search so we
        # never send "setoption" while the engine is mid-search.
        self.pending_params = {}

        try:
            self.proc = subprocess.Popen(
                [path],
                universal_newlines=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
            )
            self._handshake(cfg)
            self.thread = threading.Thread(target=self._analyze_loop, daemon=True)
            self.thread.start()
            self.engine_active = True
        except Exception as e:
            print(f"Engine not found or error: {e}")
            self.engine_active = False

    # --- Low-level UCI I/O ---

    def _send(self, cmd):
        with self.send_lock:
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()

    def _read_line(self):
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("engine closed its output stream")
        return line.strip()

    def _wait_for(self, token):
        while True:
            if self._read_line().startswith(token):
                return

    def _apply_params(self, params):
        for name, value in params.items():
            self._send(f"setoption name {name} value {value}")
        self._send("isready")
        self._wait_for("readyok")

    def _handshake(self, cfg):
        self._send("uci")
        self._wait_for("uciok")
        self._apply_params(
            {
                "Threads": cfg.get("threads", 4),
                "Hash": cfg.get("hash", 2048),
                "Skill Level": cfg.get("skill", 20),
            }
        )

    def update_position(self, board):
        # Ask the engine to wrap up any running search BEFORE publishing
        # the new board. UCI commands are processed in order, so this
        # "stop" can only end a search of the OLD position - it can never
        # cancel the fresh search the loop is about to start. When the
        # engine is idle it simply ignores the stop.
        try:
            self._send("stop")
        except Exception:
            pass
        with self.lock:
            self.board = board.copy()
            self.search_token += 1

    def _reset_info(self):
        """Zero the readout; called when the loop picks up a new position."""
        with self.lock:
            self.info = {
                "score": 0,
                "depth": 0,
                "nps": 0,
                "pv": [],
                "mate": None,
            }

    def set_pending_parameters(self, params):
        """Queue UCI options to apply before the next engine search."""
        with self.lock:
            self.pending_params.update(params)

    def _analyze_loop(self):
        last_searched_fen = None
        last_searched_token = None
        while self.running:
            with self.lock:
                current_board = self.board.copy()
                pending = dict(self.pending_params)
                self.pending_params.clear()
                token = self.search_token

            if pending:
                try:
                    self._apply_params(pending)
                except Exception as e:
                    print(f"Failed to apply engine parameters: {e}")

            if current_board.is_game_over(claim_draw=True):
                time.sleep(0.5)
                continue

            fen = current_board.fen()
            if fen == last_searched_fen and token == last_searched_token:
                # This exact position was already searched to full depth.
                # Keep the final depth/nps/score on screen and idle until a
                # move changes the position - re-searching the same position
                # made depth climb to 25, drop back to 1 and climb again in
                # an endless sawtooth.
                time.sleep(0.1)
                continue

            # Fresh position (a move was played): zero the readout so
            # depth/nps restart from 0 and climb during the new search.
            self._reset_info()

            try:
                self._search_position(fen, current_board.turn == chess.WHITE)
            except Exception as e:
                print(f"Analysis error: {e}")
                self.engine_active = False
                break

            # Store the token as it was when the search STARTED: if a move
            # arrives mid-search, update_position bumps search_token and the
            # "stop" it sent ends this search early, so this position must
            # not be treated as fully analyzed (the loop re-searches it).
            last_searched_fen = fen
            last_searched_token = token

    def _search_position(self, fen, white_to_move):
        """Run one depth-limited search, streaming live info into self.info."""
        # position + go go out as a single atomic write so a "stop" from
        # the main thread can never squeeze in between them.
        self._send(f"position fen {fen}\ngo depth {ANALYSIS_DEPTH}")

        while True:
            line = self._read_line()
            if line.startswith("bestmove"):
                return
            if line.startswith("info") and not line.startswith("info string"):
                self._parse_info_line(line, white_to_move)

    def _parse_info_line(self, line, white_to_move):
        """Extract depth/nps/score/pv from a UCI info line."""
        tokens = line.split()
        updates = {}
        score = None
        pv_tokens = None

        try:
            i = 1
            while i < len(tokens):
                tok = tokens[i]
                if tok == "depth" and i + 1 < len(tokens):
                    updates["depth"] = int(tokens[i + 1])
                    i += 2
                elif tok == "nps" and i + 1 < len(tokens):
                    updates["nps"] = int(tokens[i + 1])
                    i += 2
                elif tok == "score" and i + 2 < len(tokens):
                    kind, value = tokens[i + 1], tokens[i + 2]
                    if kind == "cp":
                        score = int(value)
                    elif kind == "mate":
                        updates["mate"] = int(value)
                        score = 10000 if updates["mate"] > 0 else -10000
                    i += 3
                elif tok == "pv":
                    pv_tokens = tokens[i + 1 : i + 9]
                    break
                else:
                    i += 1
        except ValueError:
            return

        # UCI scores are relative to the side to move; convert to
        # white-relative so the eval bar means what its W/B labels say.
        if not white_to_move:
            if score is not None:
                score = -score
            if "mate" in updates:
                updates["mate"] = -updates["mate"]

        if score is not None:
            updates["score"] = score

        if pv_tokens:
            moves = []
            for uci_move in pv_tokens:
                try:
                    moves.append(chess.Move.from_uci(uci_move))
                except ValueError:
                    break
            if moves:
                updates["pv"] = moves

        if not updates:
            return

        with self.lock:
            self.info.update(updates)

    def quit(self):
        self.running = False
        try:
            self._send("quit")
        except Exception:
            pass
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


# --- Main App ---
def get_piece_key(piece):
    if not piece:
        return None
    color = "w" if piece.color == chess.WHITE else "b"
    symbol = piece.symbol().lower()
    return f"{color}{symbol}"


def update_opening_name(board):
    global current_opening_name
    fen_placement = board.fen().split(" ")[0]
    opening_name = openings.get(fen_placement)

    if opening_name:
        current_opening_name = opening_name
    elif not current_opening_name:
        current_opening_name = "Unknown Opening"

    return current_opening_name


def get_legal_squares(board, selected_sq):
    if selected_sq is None:
        return set(), set()

    regular_moves = set()
    capture_moves = set()

    for move in board.legal_moves:
        if move.from_square == selected_sq:
            if board.piece_at(move.to_square):
                capture_moves.add(move.to_square)
            else:
                regular_moves.add(move.to_square)

    return regular_moves, capture_moves


def format_nps(nps):
    if nps >= 1000000000000:
        return f"{nps / 1000000000000:.1f}T"
    elif nps >= 1000000000:
        return f"{nps / 1000000000:.1f}B"
    elif nps >= 1000000:
        return f"{nps / 1000000:.1f}M"
    elif nps >= 1000:
        return f"{nps / 1000:.0f}K"
    else:
        return str(nps)


def draw_arrow(screen, start_pos, end_pos, color, width=8, alpha=255):
    start = pygame.Vector2(start_pos)
    end = pygame.Vector2(end_pos)

    v = end - start
    length = v.length()
    if length == 0:
        return

    v_unit = v / length

    head_length = width * 3
    head_width = width * 2.5

    neck = end - v_unit * (head_length * 0.8)

    perp = pygame.Vector2(-v_unit.y, v_unit.x) * (head_width / 2)

    point1 = end
    point2 = end - v_unit * head_length + perp
    point3 = end - v_unit * head_length - perp

    if alpha >= 254:
        rgb = color[:3]
        pygame.draw.line(screen, rgb, start, neck, width)
        pygame.draw.polygon(screen, rgb, [point1, point2, point3])
        return

    a = max(0, min(255, int(alpha)))
    if a <= 1:
        return

    xs = (start.x, end.x, point1.x, point2.x, point3.x)
    ys = (start.y, end.y, point1.y, point2.y, point3.y)
    pad = width + 4
    min_x = int(min(xs)) - pad
    min_y = int(min(ys)) - pad
    max_x = int(max(xs)) + pad
    max_y = int(max(ys)) + pad
    w = max(1, max_x - min_x)
    h = max(1, max_y - min_y)

    surf = pygame.Surface((w, h), pygame.SRCALPHA)
    rgba = (color[0], color[1], color[2], a)
    offset = pygame.Vector2(min_x, min_y)

    pygame.draw.line(surf, rgba, start - offset, neck - offset, width)
    pygame.draw.polygon(
        surf, rgba, [point1 - offset, point2 - offset, point3 - offset]
    )
    screen.blit(surf, (min_x, min_y))


def draw_gradient_rect(screen, rect, start_color, end_color, vertical=True):
    surface = pygame.Surface((rect.width, rect.height), pygame.SRCALPHA)

    if vertical:
        for y in range(rect.height):
            t = y / rect.height
            r = int(start_color[0] + (end_color[0] - start_color[0]) * t)
            g = int(start_color[1] + (end_color[1] - start_color[1]) * t)
            b = int(start_color[2] + (end_color[2] - start_color[2]) * t)
            a = start_color[3] if len(start_color) > 3 else 255
            pygame.draw.line(surface, (r, g, b, a), (0, y), (rect.width, y))
    else:
        for x in range(rect.width):
            t = x / rect.width
            r = int(start_color[0] + (end_color[0] - start_color[0]) * t)
            g = int(start_color[1] + (end_color[1] - start_color[1]) * t)
            b = int(start_color[2] + (end_color[2] - start_color[2]) * t)
            a = start_color[3] if len(start_color) > 3 else 255
            pygame.draw.line(surface, (r, g, b, a), (x, 0), (x, rect.height))

    screen.blit(surface, rect.topleft)


def draw_eval_bar(screen, x, y, w, h, current_eval, mate):
    """Draw a polished, lichess-style evaluation bar."""
    pygame.draw.rect(screen, (12, 12, 12), (x - 3, y - 3, w + 6, h + 6))
    pygame.draw.rect(screen, (60, 60, 60), (x - 2, y - 2, w + 4, h + 4))

    if mate is not None:
        white_h = h if mate > 0 else 0
    else:
        clamped = max(min(current_eval, 1000), -1000)
        fill_percent = (clamped + 1000) / 2000
        white_h = int(h * fill_percent)

    black_h = h - white_h

    if black_h > 0:
        for i in range(black_h):
            t = i / max(1, black_h)
            v = int(55 - 40 * t)
            pygame.draw.line(screen, (v, v, v), (x, y + i), (x + w - 1, y + i))

    if white_h > 0:
        wy = y + black_h
        for i in range(white_h):
            t = i / max(1, white_h)
            v = int(230 + 25 * t)
            pygame.draw.line(screen, (v, v, v), (x, wy + i), (x + w - 1, wy + i))

    for frac, strong in ((0.25, False), (0.5, True), (0.75, False)):
        ty = y + int(h * frac)
        tick_len = 6 if strong else 3
        color = (150, 150, 150) if strong else (85, 85, 85)
        pygame.draw.line(screen, color, (x, ty), (x + tick_len, ty), 1)
        pygame.draw.line(screen, color, (x + w - tick_len, ty), (x + w - 1, ty), 1)

    if mate is None:
        marker_y = y + black_h
        if marker_y < y + 1:
            marker_y = y + 1
        elif marker_y > y + h - 2:
            marker_y = y + h - 2
    else:
        marker_y = (y + h - 3) if mate > 0 else (y + 1)

    pygame.draw.rect(
        screen,
        EVAL_MARKER_COLOR,
        (x - 5, marker_y - 2, w + 10, 5),
        border_radius=2,
    )
    pygame.draw.rect(
        screen,
        EVAL_MARKER_HIGHLIGHT,
        (x - 5, marker_y - 1, w + 10, 2),
        border_radius=1,
    )

    if black_h >= 22:
        b_label = font_tiny.render("B", True, (215, 215, 215))
        screen.blit(b_label, (x + (w - b_label.get_width()) // 2, y + 4))
    if white_h >= 22:
        w_label = font_tiny.render("W", True, (30, 30, 30))
        screen.blit(
            w_label, (x + (w - w_label.get_width()) // 2, y + h - 20)
        )

    pygame.draw.rect(screen, (85, 85, 85), (x, y, w, h), 1)


def draw_gear_icon(surface, cx, cy, size, color, thickness=2, rotation=0.0):
    """Draw a proper cog/gear icon with teeth and a center hole.

    Args:
        surface:   pygame surface to draw on (supports alpha)
        cx, cy:    center position in pixels
        size:      outer radius in pixels
        color:     RGB or RGBA outline color
        thickness: line width in pixels
        rotation:  optional rotation offset in radians
    """
    n_teeth = 8
    r_outer = size
    r_inner = size * 0.70
    tooth_frac = 0.55        # fraction of the sector that is a tooth
    transition_frac = 0.30   # fraction of the tooth used for the sloped sides

    pts = []
    for i in range(n_teeth):
        base = (2 * math.pi * i) / n_teeth - math.pi / 2 + rotation
        tooth = (2 * math.pi / n_teeth) * tooth_frac
        transition = tooth * transition_frac

        # 4 points per tooth: root-left, top-left, top-right, root-right
        pts.append(
            (
                cx + r_inner * math.cos(base),
                cy + r_inner * math.sin(base),
            )
        )
        pts.append(
            (
                cx + r_outer * math.cos(base + transition),
                cy + r_outer * math.sin(base + transition),
            )
        )
        pts.append(
            (
                cx + r_outer * math.cos(base + tooth - transition),
                cy + r_outer * math.sin(base + tooth - transition),
            )
        )
        pts.append(
            (
                cx + r_inner * math.cos(base + tooth),
                cy + r_inner * math.sin(base + tooth),
            )
        )

    pygame.draw.polygon(surface, color, pts, thickness)

    # Center hub hole
    hole_r = max(2, int(size * 0.34))
    pygame.draw.circle(surface, color, (cx, cy), hole_r, thickness)


def draw_new_game_button(screen, mouse_pos):
    if menu_anim_progress == 0:
        color = (
            BUTTON_HOVER_COLOR
            if new_game_button_rect.collidepoint(mouse_pos)
            else BUTTON_COLOR
        )
        pygame.draw.rect(screen, color, new_game_button_rect, border_radius=4)
        pygame.draw.rect(
            screen, MENU_BORDER_COLOR, new_game_button_rect, 2, border_radius=4
        )

        text = font_small.render("New", True, TEXT_COLOR)
        text_rect = text.get_rect(center=new_game_button_rect.center)
        screen.blit(text, text_rect)


def draw_settings_button(screen, mouse_pos):
    global menu_anim_progress

    if menu_anim_progress == 0:
        hovered = settings_button_rect.collidepoint(mouse_pos)
        color = BUTTON_HOVER_COLOR if hovered else BUTTON_COLOR
        pygame.draw.rect(screen, color, settings_button_rect, border_radius=4)
        pygame.draw.rect(
            screen, MENU_BORDER_COLOR, settings_button_rect, 2, border_radius=4
        )

        gear_color = GEAR_COLOR_HOVER if hovered else GEAR_COLOR
        draw_gear_icon(
            screen,
            settings_button_rect.centerx,
            settings_button_rect.centery,
            9,
            gear_color,
            thickness=2,
        )
    else:
        t = cubic_ease_in_out(menu_anim_progress)

        overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        overlay.fill((30, 30, 30, int(240 * t)))
        screen.blit(overlay, (0, 0))

        half_w = int((WIDTH / 2) * t)
        half_h = int((HEIGHT / 2) * t)
        border_rect = (
            WIDTH // 2 - half_w,
            HEIGHT // 2 - half_h,
            half_w * 2,
            half_h * 2,
        )
        pygame.draw.rect(screen, MENU_BORDER_COLOR, border_rect, 3)

        if menu_anim_progress < 0.5:
            alpha = int(255 * (1 - (menu_anim_progress * 2)))
            gear_surface = pygame.Surface((50, 30), pygame.SRCALPHA)
            gear_surface.fill((0, 0, 0, 0))
            draw_gear_icon(
                gear_surface,
                25,
                15,
                9,
                (*GEAR_COLOR, alpha),
                thickness=2,
            )
            screen.blit(gear_surface, settings_button_rect.topleft)


def update_menu_transition():
    global menu_transition_progress, menu_transition_direction, settings_menu_state
    global settings_menu_target

    if menu_transition_direction != "none":
        menu_transition_progress += menu_transition_speed

        if menu_transition_progress >= 1.0:
            menu_transition_progress = 0.0

            if menu_transition_direction == "left_out":
                settings_menu_state = settings_menu_target
                menu_transition_direction = "right_in"
            elif menu_transition_direction == "right_out":
                settings_menu_state = "main"
                menu_transition_direction = "left_in"
            elif menu_transition_direction == "right_in":
                menu_transition_direction = "none"
            elif menu_transition_direction == "left_in":
                menu_transition_direction = "none"


def draw_board_theme_content(screen, offset_x=0):
    global current_theme, theme_scroll_offset

    mx, my = pygame.mouse.get_pos()
    themes = get_available_themes()

    item_height = THEME_ITEM_HEIGHT
    gap = THEME_ITEM_GAP
    top = THEME_LIST_TOP
    viewport_h = THEME_LIST_BOTTOM - top
    total_h = max(1, len(themes) * item_height + (len(themes) - 1) * gap)
    max_scroll = max(0, total_h - viewport_h)
    theme_scroll_offset = max(0.0, min(theme_scroll_offset, max_scroll))

    viewport = pygame.Rect(WIDTH // 2 - 200 + offset_x, top, 400, viewport_h)

    screen.set_clip(viewport)
    for i, (theme_id, display_name) in enumerate(themes):
        theme_rect = pygame.Rect(
            viewport.x,
            top + i * (item_height + gap) - int(theme_scroll_offset),
            400,
            item_height,
        )
        if theme_rect.bottom < top or theme_rect.top > top + viewport_h:
            continue

        is_current = theme_id == current_theme
        is_hovered = (
            theme_rect.collidepoint(mx, my)
            and menu_transition_direction == "none"
        )

        if is_current:
            bg_color = (80, 80, 120)
            if is_hovered:
                bg_color = (100, 100, 150)
        else:
            bg_color = (60, 60, 60)
            if is_hovered:
                bg_color = (80, 80, 80)

        pygame.draw.rect(screen, bg_color, theme_rect, border_radius=4)
        pygame.draw.rect(
            screen, MENU_BORDER_COLOR, theme_rect, 2, border_radius=4
        )

        theme_text = font_large.render(display_name, True, TEXT_COLOR)
        theme_text_rect = theme_text.get_rect(center=theme_rect.center)
        screen.blit(theme_text, theme_text_rect)
    screen.set_clip(None)

    # Scrollbar + hint (only when the list overflows the viewport)
    if max_scroll > 0:
        track_rect = pygame.Rect(viewport.right + 8, top, 4, viewport_h)
        pygame.draw.rect(screen, (70, 70, 70), track_rect, border_radius=2)
        thumb_h = max(30, int(viewport_h * viewport_h / total_h))
        thumb_y = top + int(
            (viewport_h - thumb_h) * (theme_scroll_offset / max_scroll)
        )
        pygame.draw.rect(
            screen,
            (150, 150, 150),
            (track_rect.x, thumb_y, track_rect.w, thumb_h),
            border_radius=2,
        )

        hint = font_tiny.render("Scroll for more themes", True, TEXT_MUTED)
        screen.blit(
            hint, (WIDTH // 2 - hint.get_width() // 2, THEME_LIST_BOTTOM + 6)
        )


def scroll_theme_list(delta):
    """Scroll the board theme list (positive delta scrolls towards the end)."""
    global theme_scroll_offset
    themes = get_available_themes()
    total_h = len(themes) * THEME_ITEM_HEIGHT + (len(themes) - 1) * THEME_ITEM_GAP
    viewport_h = THEME_LIST_BOTTOM - THEME_LIST_TOP
    max_scroll = max(0, total_h - viewport_h)
    theme_scroll_offset = max(0.0, min(theme_scroll_offset + delta, max_scroll))


# --- Engine Settings Page ---
ENGINE_ROW_KEYS = ["threads", "skill", "hash"]
ENGINE_ROW_LABELS = {"threads": "Threads", "skill": "Skill Level", "hash": "Hash"}
ENGINE_ROW_LIMITS = {"threads": (1, 1024), "skill": (0, 20), "hash": (16, 32768)}
ENGINE_PARAM_NAMES = {"threads": "Threads", "skill": "Skill Level", "hash": "Hash"}
HASH_STEPS = [16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768]

ENGINE_ROW_TOP = 130
ENGINE_ROW_HEIGHT = 64
ENGINE_ROW_GAP = 18
ENGINE_ROW_WIDTH = 460
ENGINE_BTN_W = 44
ENGINE_BTN_H = 38
ENGINE_VALUE_W = 110


def step_hash_value(current, direction):
    """Move through the HASH_STEPS ladder, keeping custom config values."""
    steps = sorted(set(HASH_STEPS) | {current})
    idx = steps.index(current)
    return steps[max(0, min(idx + direction, len(steps) - 1))]


def get_engine_row_rects(i, offset_x=0):
    """Rects (row, minus, value, plus) for engine setting row i."""
    y = ENGINE_ROW_TOP + i * (ENGINE_ROW_HEIGHT + ENGINE_ROW_GAP)
    row_rect = pygame.Rect(
        WIDTH // 2 - ENGINE_ROW_WIDTH // 2 + offset_x,
        y,
        ENGINE_ROW_WIDTH,
        ENGINE_ROW_HEIGHT,
    )
    plus_rect = pygame.Rect(
        row_rect.right - 18 - ENGINE_BTN_W,
        row_rect.centery - ENGINE_BTN_H // 2,
        ENGINE_BTN_W,
        ENGINE_BTN_H,
    )
    value_rect = pygame.Rect(
        plus_rect.x - 8 - ENGINE_VALUE_W,
        row_rect.y,
        ENGINE_VALUE_W,
        ENGINE_ROW_HEIGHT,
    )
    minus_rect = pygame.Rect(
        value_rect.x - 8 - ENGINE_BTN_W,
        row_rect.centery - ENGINE_BTN_H // 2,
        ENGINE_BTN_W,
        ENGINE_BTN_H,
    )
    return row_rect, minus_rect, value_rect, plus_rect


def get_engine_reset_rect(offset_x=0):
    reset_w, reset_h = 220, 40
    reset_y = ENGINE_ROW_TOP + 3 * (ENGINE_ROW_HEIGHT + ENGINE_ROW_GAP) + 14
    return pygame.Rect(
        WIDTH // 2 - reset_w // 2 + offset_x, reset_y, reset_w, reset_h
    )


def adjust_engine_setting(key, direction):
    """Change one engine setting, persist it, and queue it on the engine."""
    low, high = ENGINE_ROW_LIMITS[key]
    if key == "hash":
        new_value = step_hash_value(config[key], direction)
    else:
        new_value = max(low, min(high, config[key] + direction))

    if new_value == config[key]:
        return

    config[key] = new_value
    save_config()
    if engine_thread is not None:
        engine_thread.set_pending_parameters({ENGINE_PARAM_NAMES[key]: new_value})


def reset_engine_settings():
    config.update(ENGINE_DEFAULTS)
    save_config()
    if engine_thread is not None:
        engine_thread.set_pending_parameters(
            {
                "Threads": config["threads"],
                "Hash": config["hash"],
                "Skill Level": config["skill"],
            }
        )


def draw_engine_settings_content(screen, offset_x=0):
    mx, my = pygame.mouse.get_pos()

    for i, key in enumerate(ENGINE_ROW_KEYS):
        row_rect, minus_rect, value_rect, plus_rect = get_engine_row_rects(
            i, offset_x
        )

        pygame.draw.rect(screen, (55, 55, 55), row_rect, border_radius=4)
        pygame.draw.rect(screen, MENU_BORDER_COLOR, row_rect, 2, border_radius=4)

        label = font_large.render(ENGINE_ROW_LABELS[key], True, TEXT_COLOR)
        screen.blit(
            label,
            (row_rect.x + 18, row_rect.centery - label.get_height() // 2),
        )

        low, high = ENGINE_ROW_LIMITS[key]
        value_text = f"{config[key]} MB" if key == "hash" else str(config[key])
        value_surf = font_small.render(value_text, True, TEXT_COLOR)
        screen.blit(
            value_surf,
            (
                value_rect.centerx - value_surf.get_width() // 2,
                value_rect.centery - value_surf.get_height() // 2,
            ),
        )

        for rect, symbol, enabled in (
            (minus_rect, "-", config[key] > low),
            (plus_rect, "+", config[key] < high),
        ):
            if enabled:
                btn_bg = (
                    (95, 95, 95) if rect.collidepoint(mx, my) else (75, 75, 75)
                )
            else:
                btn_bg = (48, 48, 48)
            pygame.draw.rect(screen, btn_bg, rect, border_radius=4)
            pygame.draw.rect(screen, MENU_BORDER_COLOR, rect, 2, border_radius=4)
            sym_color = TEXT_COLOR if enabled else (110, 110, 110)
            sym = font_small.render(symbol, True, sym_color)
            screen.blit(sym, sym.get_rect(center=rect.center))

    reset_rect = get_engine_reset_rect(offset_x)
    reset_hovered = reset_rect.collidepoint(mx, my)
    pygame.draw.rect(
        screen,
        (95, 95, 95) if reset_hovered else (75, 75, 75),
        reset_rect,
        border_radius=4,
    )
    pygame.draw.rect(screen, MENU_BORDER_COLOR, reset_rect, 2, border_radius=4)
    reset_text = font_small.render("Reset to Defaults", True, TEXT_COLOR)
    screen.blit(reset_text, reset_text.get_rect(center=reset_rect.center))

    note = font_tiny.render(
        "Changes apply to the engine instantly and are saved", True, TEXT_MUTED
    )
    screen.blit(
        note, (WIDTH // 2 - note.get_width() // 2, reset_rect.bottom + 12)
    )


def draw_settings_menu(screen):
    global menu_anim_progress, settings_menu_state, menu_transition_progress
    global menu_transition_direction

    if menu_anim_progress < 1.0:
        return

    update_menu_transition()

    mx, my = pygame.mouse.get_pos()
    transition_idle = menu_transition_direction == "none"

    offset_x = 0
    if menu_transition_direction == "left_out":
        offset_x = -int(400 * cubic_ease_in_out(menu_transition_progress))
    elif menu_transition_direction == "right_out":
        offset_x = int(400 * cubic_ease_in_out(menu_transition_progress))
    elif menu_transition_direction == "right_in":
        offset_x = int(400 * (1 - cubic_ease_in_out(menu_transition_progress)))
    elif menu_transition_direction == "left_in":
        offset_x = -int(400 * (1 - cubic_ease_in_out(menu_transition_progress)))

    page_titles = {
        "main": "Settings",
        "board_theme": "Board Theme",
        "engine_settings": "Engine Settings",
    }
    title_text = font_large.render(
        page_titles.get(settings_menu_state, "Settings"), True, TEXT_COLOR
    )
    title_rect = title_text.get_rect(center=(WIDTH // 2 + offset_x, 50))
    screen.blit(title_text, title_rect)

    close_button_rect = pygame.Rect(WIDTH - 60, 20, 40, 30)
    close_hovered = transition_idle and close_button_rect.collidepoint(mx, my)
    pygame.draw.rect(
        screen,
        (150, 50, 50) if close_hovered else (100, 50, 50),
        close_button_rect,
        border_radius=4,
    )
    pygame.draw.rect(
        screen, MENU_BORDER_COLOR, close_button_rect, 2, border_radius=4
    )
    close_text = font_small.render("X", True, TEXT_COLOR)
    screen.blit(close_text, close_text.get_rect(center=close_button_rect.center))

    if settings_menu_state != "main":
        back_button_rect = pygame.Rect(20, 20, 40, 30)
        back_hovered = transition_idle and back_button_rect.collidepoint(mx, my)
        pygame.draw.rect(
            screen,
            BUTTON_HOVER_COLOR if back_hovered else BUTTON_COLOR,
            back_button_rect,
            border_radius=4,
        )
        pygame.draw.rect(
            screen, MENU_BORDER_COLOR, back_button_rect, 2, border_radius=4
        )
        back_text = font_small.render("<", True, TEXT_COLOR)
        screen.blit(back_text, back_text.get_rect(center=back_button_rect.center))

    if settings_menu_state == "main":
        menu_items = [
            "Engine Settings",
            "Board Theme",
            "Sound Settings",
            "Game Controls",
            "Analysis Settings",
        ]

        item_height = 50
        total_menu_height = len(menu_items) * item_height + (len(menu_items) - 1) * 20
        start_y = (HEIGHT - total_menu_height) // 2

        for i, item in enumerate(menu_items):
            item_rect = pygame.Rect(
                WIDTH // 2 - 200 + offset_x,
                start_y + i * (item_height + 20),
                400,
                item_height,
            )

            is_interactive = item in ("Board Theme", "Engine Settings")
            is_hovered = (
                is_interactive
                and transition_idle
                and item_rect.collidepoint(mx, my)
            )

            if is_hovered:
                bg = (80, 80, 120)
            else:
                bg = (60, 60, 60)

            pygame.draw.rect(screen, bg, item_rect, border_radius=4)
            pygame.draw.rect(
                screen, MENU_BORDER_COLOR, item_rect, 1, border_radius=4
            )

            text_color = TEXT_COLOR if is_interactive else (150, 150, 150)
            item_text = font_large.render(item, True, text_color)
            item_text_rect = item_text.get_rect(center=item_rect.center)
            screen.blit(item_text, item_text_rect)

        about_y = start_y + len(menu_items) * (item_height + 20) + 40

        about_title = font_large.render("About", True, TEXT_COLOR)
        about_title_rect = about_title.get_rect(
            center=(WIDTH // 2 + offset_x, about_y)
        )
        screen.blit(about_title, about_title_rect)

        version_text = font_small.render(
            "ChessBotV Rewrite Version 1.2.1-beta", True, TEXT_COLOR
        )
        version_rect = version_text.get_rect(
            center=(WIDTH // 2 + offset_x, about_y + 40)
        )
        screen.blit(version_text, version_rect)

    elif settings_menu_state == "board_theme":
        draw_board_theme_content(screen, offset_x)

    elif settings_menu_state == "engine_settings":
        draw_engine_settings_content(screen, offset_x)


def handle_menu_click(mx, my):
    global menu_anim_closing, settings_menu_state, menu_transition_direction
    global current_theme, images, theme_scroll_offset, settings_menu_target

    if menu_anim_progress < 1.0 or menu_transition_direction != "none":
        return True

    close_button_rect = pygame.Rect(WIDTH - 60, 20, 40, 30)
    if close_button_rect.collidepoint(mx, my):
        menu_anim_closing = True
        return True

    if settings_menu_state != "main":
        back_button_rect = pygame.Rect(20, 20, 40, 30)
        if back_button_rect.collidepoint(mx, my):
            menu_transition_direction = "right_out"
            return True

    if settings_menu_state == "board_theme":
        themes = get_available_themes()
        item_height = THEME_ITEM_HEIGHT
        gap = THEME_ITEM_GAP
        top = THEME_LIST_TOP
        viewport = pygame.Rect(
            WIDTH // 2 - 200, top, 400, THEME_LIST_BOTTOM - top
        )
        for i, (theme_id, _display) in enumerate(themes):
            theme_rect = pygame.Rect(
                WIDTH // 2 - 200,
                top + i * (item_height + gap) - int(theme_scroll_offset),
                400,
                item_height,
            )
            if (
                viewport.collidepoint(mx, my)
                and theme_rect.collidepoint(mx, my)
            ):
                if theme_id != current_theme:
                    current_theme = theme_id
                    images = load_images(current_theme)
                return True

    elif settings_menu_state == "engine_settings":
        for i, key in enumerate(ENGINE_ROW_KEYS):
            _row_rect, minus_rect, _value_rect, plus_rect = get_engine_row_rects(i)
            if minus_rect.collidepoint(mx, my):
                adjust_engine_setting(key, -1)
                return True
            if plus_rect.collidepoint(mx, my):
                adjust_engine_setting(key, 1)
                return True

        if get_engine_reset_rect().collidepoint(mx, my):
            reset_engine_settings()
            return True

    elif settings_menu_state == "main":
        menu_items = [
            "Engine Settings",
            "Board Theme",
            "Sound Settings",
            "Game Controls",
            "Analysis Settings",
        ]
        item_height = 50
        total_menu_height = len(menu_items) * item_height + (len(menu_items) - 1) * 20
        start_y = (HEIGHT - total_menu_height) // 2

        for i, item in enumerate(menu_items):
            item_rect = pygame.Rect(
                WIDTH // 2 - 200,
                start_y + i * (item_height + 20),
                400,
                item_height,
            )
            if item_rect.collidepoint(mx, my):
                if item == "Board Theme":
                    theme_scroll_offset = 0.0
                    settings_menu_target = "board_theme"
                    menu_transition_direction = "left_out"
                elif item == "Engine Settings":
                    settings_menu_target = "engine_settings"
                    menu_transition_direction = "left_out"
                return True

    return True


def draw_gui(
    screen, board, engine_thread, anim_mgr, check_effect, selected_sq, mouse_pos
):
    screen.fill(BG_COLOR)

    # Compute status once for the whole frame
    status_text, status_color, game_over = get_game_status(board)

    # Top bar divider
    pygame.draw.line(
        screen,
        DIVIDER_COLOR,
        (PADDING, TOP_BAR_HEIGHT - 1),
        (WIDTH - PADDING, TOP_BAR_HEIGHT - 1),
        1,
    )

    # 1. Evaluation bar
    eval_bar_x = PADDING
    eval_bar_y = BOARD_OY
    eval_bar_h = BOARD_WIDTH

    with engine_thread.lock:
        score = engine_thread.info["score"]
        mate = engine_thread.info["mate"]
        depth = engine_thread.info["depth"]
        nps = engine_thread.info["nps"]
        pv = engine_thread.info["pv"]

    anim_mgr.set_target_eval(score)
    draw_eval_bar(
        screen,
        eval_bar_x,
        eval_bar_y,
        EVAL_BAR_WIDTH,
        eval_bar_h,
        anim_mgr.current_eval,
        mate,
    )

    # 2. Board
    for r in range(8):
        for f in range(8):
            color = LIGHT_SQ if (r + f) % 2 == 0 else DARK_SQ
            sq_rect = pygame.Rect(
                BOARD_OX + f * SQUARE_SIZE,
                BOARD_OY + r * SQUARE_SIZE,
                SQUARE_SIZE,
                SQUARE_SIZE,
            )
            pygame.draw.rect(screen, color, sq_rect)

            sq = chess.square(f, 7 - r)
            if sq == selected_sq:
                s = pygame.Surface((SQUARE_SIZE, SQUARE_SIZE), pygame.SRCALPHA)
                s.fill(HIGHLIGHT_COLOR)
                screen.blit(s, sq_rect.topleft)

    pygame.draw.rect(
        screen,
        BOARD_BORDER_COLOR,
        (BOARD_OX - 2, BOARD_OY - 2, BOARD_WIDTH + 4, BOARD_WIDTH + 4),
        2,
    )

    regular_moves, capture_moves = get_legal_squares(board, selected_sq)

    for sq in regular_moves:
        x = BOARD_OX + chess.square_file(sq) * SQUARE_SIZE + SQUARE_SIZE // 2
        y = BOARD_OY + (7 - chess.square_rank(sq)) * SQUARE_SIZE + SQUARE_SIZE // 2
        pygame.draw.circle(screen, LEGAL_MOVE_COLOR, (x, y), 15)

    for sq in capture_moves:
        x = BOARD_OX + chess.square_file(sq) * SQUARE_SIZE + SQUARE_SIZE // 2
        y = BOARD_OY + (7 - chess.square_rank(sq)) * SQUARE_SIZE + SQUARE_SIZE // 2
        pygame.draw.circle(screen, CAPTURE_MOVE_COLOR, (x, y), 15)
        pygame.draw.circle(screen, (255, 255, 255, 200), (x, y), 15, 2)

    # Engine arrow — hidden once the game is over
    arrow_visible = False
    if not game_over and pv and len(pv) > 0:
        best_move = pv[0]
        if best_move in board.legal_moves:
            start_x = (
                BOARD_OX
                + chess.square_file(best_move.from_square) * SQUARE_SIZE
                + SQUARE_SIZE // 2
            )
            start_y = (
                BOARD_OY
                + (7 - chess.square_rank(best_move.from_square)) * SQUARE_SIZE
                + SQUARE_SIZE // 2
            )
            end_x = (
                BOARD_OX
                + chess.square_file(best_move.to_square) * SQUARE_SIZE
                + SQUARE_SIZE // 2
            )
            end_y = (
                BOARD_OY
                + (7 - chess.square_rank(best_move.to_square)) * SQUARE_SIZE
                + SQUARE_SIZE // 2
            )
            anim_mgr.set_arrow_target(
                (start_x, start_y), (end_x, end_y), True
            )
            arrow_visible = True

    if not arrow_visible or game_over:
        anim_mgr.set_arrow_target(None, None, False)

    if (
        anim_mgr.arrow_alpha > 1
        and anim_mgr.arrow_start is not None
        and anim_mgr.arrow_end is not None
    ):
        draw_arrow(
            screen,
            anim_mgr.arrow_start,
            anim_mgr.arrow_end,
            ENGINE_ARROW_COLOR,
            8,
            anim_mgr.arrow_alpha,
        )

    # Check/Checkmate glow, UNDER the pieces
    draw_check_effect_under(screen, check_effect, BOARD_OX, BOARD_OY)

    # 3. Pieces
    moving_sq = anim_mgr.move_anim["target_sq"] if anim_mgr.move_anim else -1
    is_castling = anim_mgr.move_anim["is_castling"] if anim_mgr.move_anim else False
    rook_target_sq = (
        anim_mgr.move_anim["rook_target_sq"] if anim_mgr.move_anim else None
    )
    rook_start_sq = anim_mgr.rook_anim["target_sq"] if anim_mgr.rook_anim else None

    for r in range(8):
        for f in range(8):
            sq = chess.square(f, 7 - r)
            piece = board.piece_at(sq)

            if sq == moving_sq:
                continue

            if (
                is_castling
                and (sq == rook_target_sq or sq == rook_start_sq)
                and piece
                and piece.piece_type == chess.ROOK
            ):
                continue

            if piece:
                pkey = get_piece_key(piece)
                x = BOARD_OX + f * SQUARE_SIZE
                y = BOARD_OY + r * SQUARE_SIZE
                if pkey in images:
                    screen.blit(images[pkey], (x, y))

    # 4. Capture animations
    for anim in anim_mgr.capture_anims:
        pkey = get_piece_key(anim["piece"])
        if pkey in images:
            img = images[pkey].copy()
            scale = 1.0 + (anim["progress"] * 0.8)
            new_size = int(SQUARE_SIZE * scale)
            scaled_img = pygame.transform.smoothscale(img, (new_size, new_size))
            alpha = int(255 * (1.0 - anim["progress"]))
            scaled_img.set_alpha(alpha)
            offset = (new_size - SQUARE_SIZE) // 2
            screen.blit(
                scaled_img, (anim["pos"][0] - offset, anim["pos"][1] - offset)
            )

    # 5. Move animations
    if anim_mgr.move_anim:
        pkey = get_piece_key(anim_mgr.move_anim["piece"])
        if pkey in images:
            t = cubic_ease_in_out(anim_mgr.move_anim["progress"])
            sx, sy = anim_mgr.move_anim["start"]
            ex, ey = anim_mgr.move_anim["end"]
            curr_x = sx + (ex - sx) * t
            curr_y = sy + (ey - sy) * t
            screen.blit(images[pkey], (curr_x, curr_y))

    if anim_mgr.rook_anim:
        pkey = get_piece_key(anim_mgr.rook_anim["piece"])
        if pkey in images:
            t = cubic_ease_in_out(anim_mgr.rook_anim["progress"])
            sx, sy = anim_mgr.rook_anim["start"]
            ex, ey = anim_mgr.rook_anim["end"]
            curr_x = sx + (ex - sx) * t
            curr_y = sy + (ey - sy) * t
            screen.blit(images[pkey], (curr_x, curr_y))

    # Check/Checkmate brackets, rings, and mate border, OVER the pieces
    draw_check_effect_over(screen, check_effect, BOARD_OX, BOARD_OY)

    # 6. Info panel
    info_top = BOARD_OY + BOARD_WIDTH

    if game_over and status_text:
        # Show result prominently, centered
        status_surf = font_large.render(status_text, True, status_color)
        sx = BOARD_OX + (BOARD_WIDTH - status_surf.get_width()) // 2
        screen.blit(status_surf, (sx, info_top + 8))
    else:
        # Show opening name on left; "Check!" on right when applicable
        opening_text = font_large.render(
            update_opening_name(board), True, TEXT_COLOR
        )
        screen.blit(opening_text, (BOARD_OX, info_top + 8))

        if status_text:
            status_surf = font_large.render(status_text, True, status_color)
            screen.blit(
                status_surf,
                (
                    BOARD_OX + BOARD_WIDTH - status_surf.get_width(),
                    info_top + 8,
                ),
            )

    divider_y = info_top + 46
    pygame.draw.line(
        screen,
        DIVIDER_COLOR,
        (BOARD_OX, divider_y),
        (BOARD_OX + BOARD_WIDTH, divider_y),
        1,
    )

    mate_str = f"M{mate}" if mate else ""
    eval_str = mate_str if mate else f"{score / 100.0:+.2f}"

    stats_label = font_small.render("Eval", True, TEXT_MUTED)
    stats_value = font_small.render(eval_str, True, TEXT_COLOR)
    depth_label = font_small.render("Depth", True, TEXT_MUTED)
    depth_value = font_small.render(str(depth), True, TEXT_COLOR)
    nps_label = font_small.render("NPS", True, TEXT_MUTED)
    nps_value = font_small.render(format_nps(nps), True, TEXT_COLOR)

    stats_y = info_top + 56
    col1_x = BOARD_OX
    col2_x = BOARD_OX + 180
    col3_x = BOARD_OX + 340

    screen.blit(stats_label, (col1_x, stats_y))
    screen.blit(stats_value, (col1_x + stats_label.get_width() + 8, stats_y))
    screen.blit(depth_label, (col2_x, stats_y))
    screen.blit(depth_value, (col2_x + depth_label.get_width() + 8, stats_y))
    screen.blit(nps_label, (col3_x, stats_y))
    screen.blit(nps_value, (col3_x + nps_label.get_width() + 8, stats_y))

    # Bottom row: rec move during play, opening name once game is over
    rec_y = info_top + 88
    if game_over:
        rec_label = font_small.render("Opening", True, TEXT_MUTED)
        rec_value = font_small.render(
            update_opening_name(board), True, TEXT_COLOR
        )
        screen.blit(rec_label, (BOARD_OX, rec_y))
        screen.blit(rec_value, (BOARD_OX + rec_label.get_width() + 8, rec_y))
    else:
        best_move_str = str(pv[0]) if pv else "—"
        rec_label = font_small.render("Rec. Move", True, TEXT_MUTED)
        rec_value = font_small.render(best_move_str, True, TEXT_ACCENT)
        screen.blit(rec_label, (BOARD_OX, rec_y))
        screen.blit(rec_value, (BOARD_OX + rec_label.get_width() + 8, rec_y))

    # Buttons & menu overlay
    draw_new_game_button(screen, mouse_pos)
    draw_settings_button(screen, mouse_pos)

    if menu_anim_progress >= 1.0:
        draw_settings_menu(screen)

    pygame.display.flip()


def main():
    global menu_anim_progress, menu_anim_opening, menu_anim_closing
    global menu_transition_direction, engine_thread

    clock = pygame.time.Clock()
    board = chess.Board()
    anim_mgr = AnimationManager()
    check_effect = CheckEffect()
    engine_thread = EngineThread(ENGINE_PATH, config)

    selected_sq = None
    running = True

    while running:
        anim_mgr.update()

        # Update check effect each frame with the current board state
        is_check = board.is_check()
        is_mate = board.is_checkmate()
        king_sq = (
            board.king(board.turn) if (is_check or is_mate) else None
        )
        check_effect.update(is_check, is_mate, king_sq)

        update_menu_animation()
        mouse_pos = pygame.mouse.get_pos()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                mx, my = event.pos

                menu_active = (
                    menu_anim_progress > 0
                    or menu_anim_opening
                    or menu_anim_closing
                )

                if not menu_active:
                    if new_game_button_rect.collidepoint(mx, my):
                        board.reset()
                        selected_sq = None
                        anim_mgr.move_anim = None
                        anim_mgr.rook_anim = None
                        anim_mgr.capture_anims = []
                        anim_mgr.current_eval = 0.0
                        anim_mgr.target_eval = 0.0
                        anim_mgr.arrow_target_alpha = 0.0
                        check_effect.reset()
                        engine_thread.update_position(board)

                    elif settings_button_rect.collidepoint(mx, my):
                        menu_anim_opening = True

                    elif not (anim_mgr.move_anim or anim_mgr.rook_anim):
                        if not board.is_game_over(claim_draw=True):
                            if (
                                BOARD_OX <= mx < BOARD_OX + BOARD_WIDTH
                                and BOARD_OY <= my < BOARD_OY + BOARD_WIDTH
                            ):
                                file = (mx - BOARD_OX) // SQUARE_SIZE
                                rank = 7 - ((my - BOARD_OY) // SQUARE_SIZE)
                                sq = chess.square(file, rank)

                                if selected_sq is None:
                                    if (
                                        board.piece_at(sq)
                                        and board.piece_at(sq).color == board.turn
                                    ):
                                        selected_sq = sq
                                else:
                                    move = chess.Move(selected_sq, sq)
                                    if (
                                        board.piece_at(selected_sq)
                                        and board.piece_at(selected_sq).piece_type
                                        == chess.PAWN
                                    ):
                                        if (
                                            chess.square_rank(sq) == 0
                                            or chess.square_rank(sq) == 7
                                        ):
                                            move = chess.Move(
                                                selected_sq, sq, promotion=chess.QUEEN
                                            )

                                    if move in board.legal_moves:
                                        is_castling = False
                                        rook_start_sq = None
                                        rook_target_sq = None
                                        rook_piece = None

                                        if (
                                            board.piece_at(selected_sq)
                                            and board.piece_at(selected_sq).piece_type
                                            == chess.KING
                                        ):
                                            if (
                                                selected_sq == chess.E1 and sq == chess.G1
                                            ) or (
                                                selected_sq == chess.E8 and sq == chess.G8
                                            ):
                                                is_castling = True
                                                rook_start_sq = (
                                                    chess.H1
                                                    if selected_sq == chess.E1
                                                    else chess.H8
                                                )
                                                rook_target_sq = (
                                                    chess.F1
                                                    if selected_sq == chess.E1
                                                    else chess.F8
                                                )
                                                rook_piece = board.piece_at(rook_start_sq)
                                            elif (
                                                selected_sq == chess.E1 and sq == chess.C1
                                            ) or (
                                                selected_sq == chess.E8 and sq == chess.C8
                                            ):
                                                is_castling = True
                                                rook_start_sq = (
                                                    chess.A1
                                                    if selected_sq == chess.E1
                                                    else chess.A8
                                                )
                                                rook_target_sq = (
                                                    chess.D1
                                                    if selected_sq == chess.E1
                                                    else chess.D8
                                                )
                                                rook_piece = board.piece_at(rook_start_sq)

                                        captured_piece = board.piece_at(sq)
                                        if captured_piece:
                                            anim_mgr.add_capture(captured_piece, sq)

                                        anim_mgr.add_move(
                                            board.piece_at(selected_sq),
                                            selected_sq,
                                            sq,
                                            is_castling,
                                            rook_start_sq,
                                            rook_target_sq,
                                            rook_piece,
                                        )

                                        board.push(move)
                                        engine_thread.update_position(board)
                                        selected_sq = None
                                    else:
                                        if (
                                            board.piece_at(sq)
                                            and board.piece_at(sq).color == board.turn
                                        ):
                                            selected_sq = sq
                                        else:
                                            selected_sq = None
                else:
                    handle_menu_click(mx, my)

            elif event.type == pygame.MOUSEWHEEL:
                if (
                    settings_menu_open
                    and settings_menu_state == "board_theme"
                    and menu_transition_direction == "none"
                ):
                    scroll_theme_list(event.y * theme_scroll_speed)

            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    if menu_anim_progress > 0 and not (
                        menu_anim_opening or menu_anim_closing
                    ):
                        if menu_transition_direction != "none":
                            pass
                        elif settings_menu_state != "main":
                            menu_transition_direction = "right_out"
                        else:
                            menu_anim_closing = True

                elif event.key == pygame.K_SPACE and menu_anim_progress == 0:
                    if (
                        not (anim_mgr.move_anim or anim_mgr.rook_anim)
                        and not board.is_game_over(claim_draw=True)
                    ):
                        with engine_thread.lock:
                            pv = engine_thread.info["pv"]
                        # Guard against a stale pv (a move was just played
                        # but the new search has not started yet): never
                        # push an illegal move onto the board.
                        if pv and pv[0] in board.legal_moves:
                            best_move = pv[0]
                            captured_piece = board.piece_at(best_move.to_square)
                            if captured_piece:
                                anim_mgr.add_capture(
                                    captured_piece, best_move.to_square
                                )

                            is_castling = False
                            rook_start_sq = None
                            rook_target_sq = None
                            rook_piece = None

                            if (
                                board.piece_at(best_move.from_square)
                                and board.piece_at(best_move.from_square).piece_type
                                == chess.KING
                            ):
                                if (
                                    best_move.from_square == chess.E1
                                    and best_move.to_square == chess.G1
                                ) or (
                                    best_move.from_square == chess.E8
                                    and best_move.to_square == chess.G8
                                ):
                                    is_castling = True
                                    rook_start_sq = (
                                        chess.H1
                                        if best_move.from_square == chess.E1
                                        else chess.H8
                                    )
                                    rook_target_sq = (
                                        chess.F1
                                        if best_move.from_square == chess.E1
                                        else chess.F8
                                    )
                                    rook_piece = board.piece_at(rook_start_sq)
                                elif (
                                    best_move.from_square == chess.E1
                                    and best_move.to_square == chess.C1
                                ) or (
                                    best_move.from_square == chess.E8
                                    and best_move.to_square == chess.C8
                                ):
                                    is_castling = True
                                    rook_start_sq = (
                                        chess.A1
                                        if best_move.from_square == chess.E1
                                        else chess.A8
                                    )
                                    rook_target_sq = (
                                        chess.D1
                                        if best_move.from_square == chess.E1
                                        else chess.D8
                                    )
                                    rook_piece = board.piece_at(rook_start_sq)

                            anim_mgr.add_move(
                                board.piece_at(best_move.from_square),
                                best_move.from_square,
                                best_move.to_square,
                                is_castling,
                                rook_start_sq,
                                rook_target_sq,
                                rook_piece,
                            )
                            board.push(best_move)
                            engine_thread.update_position(board)

        draw_gui(
            screen,
            board,
            engine_thread,
            anim_mgr,
            check_effect,
            selected_sq,
            mouse_pos,
        )
        clock.tick(FPS)

    engine_thread.quit()
    pygame.quit()
    sys.exit()


if __name__ == "__main__":
    main()