import ctypes
import json
import math
import os
import subprocess
import sys
import threading
import time
import webbrowser

import chess
import pygame
import pygame.gfxdraw

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
# Gradient arrow colours (blue range) — used for suggestion arrows with 0.4 opacity
ARROW_GRAD_START = (95, 170, 255)
ARROW_GRAD_END = (0, 70, 210)
# Mate arrow gradient (red) — pulse when forced mate detected
ARROW_MATE_START = (255, 110, 110)
ARROW_MATE_END = (190, 0, 0)
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
EVAL_WIN_WHITE_GLOW = (255, 255, 180, 70)
EVAL_WIN_BLACK_GLOW = (80, 80, 255, 60)
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


# --- Constants ---
RESTART_EXIT_CODE = 42

# --- Loaders ---
ENGINE_DEFAULTS = {"hash": 2048, "threads": 4, "skill": 20, "depth": 25, "multipv": 1, "theme": "classic"}


def load_config():
    cfg = dict(ENGINE_DEFAULTS)
    config_files = ["configuration.cfg", "config.cfg"]

    for config_file in config_files:
        try:
            with open(config_file, "r") as f:
                for line in f:
                    line=line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k=k.strip().lower()
                        v=v.strip()
                        if k in cfg and k != "theme":
                            try:
                                cfg[k] = int(v)
                            except ValueError:
                                pass
                        elif k == "theme":
                            cfg[k] = v
                        elif k == "flipped":
                            cfg[k] = v.lower() in ("1","true","yes")
                        elif k == "eval_side":
                            if v in ("left","right"):
                                cfg[k]=v
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
            f.write(f"depth={config.get('depth', 25)}\n")
            f.write(f"multipv={config.get('multipv', 1)}\n")
            f.write(f"theme={config.get('theme', current_theme)}\n")
    except OSError as e:
        print(f"Failed to save configuration: {e}")


def save_theme_config(theme_id):
    """Persist only theme change (keeps engine settings)."""
    config["theme"]=theme_id
    save_config()


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


def get_preview_images(theme):
    """Return 28x28 preview surfaces for theme, cached."""
    if theme in _theme_preview_cache:
        return _theme_preview_cache[theme]
    pieces = ["bb", "bn", "br", "bk", "bq", "bp", "wb", "wn", "wr", "wk", "wq", "wp"]
    piece_dir = get_piece_dir(theme)
    prev = {}
    for p in pieces:
        path = os.path.join(piece_dir, f"{p}.png")
        try:
            img = pygame.image.load(path).convert_alpha()
            prev[p] = pygame.transform.smoothscale(img, (28, 28))
        except FileNotFoundError:
            surf = pygame.Surface((28, 28), pygame.SRCALPHA)
            pygame.draw.circle(surf, (255, 0, 0) if p.startswith("b") else (0, 255, 0), (14, 14), 9)
            prev[p] = surf
    _theme_preview_cache[theme] = prev
    return prev


# Board theme state — theme is persisted in configuration.cfg
# cache for preview images (theme_id -> {piece_key: surface 28x28})
_theme_preview_cache = {}
available_themes = get_available_themes()
config = load_config()
# Resolve theme from config, fallback to classic/first available
current_theme = config.get("theme", "classic")
if available_themes and all(t[0] != current_theme for t in available_themes):
    current_theme = available_themes[0][0]
    config["theme"]=current_theme

# Pieces Style (board theme) menu layout & scrolling — compact 2-column + preview
# Container was clipping second row of preview pieces (44 + 34 + 28 =106 >92); enlarge to fit
THEME_PREVIEW_H = 116
THEME_PREVIEW_Y = 82
THEME_LIST_TOP = 212
THEME_ITEM_HEIGHT = 36
THEME_ITEM_GAP = 8
THEME_ITEM_WIDTH = 190
THEME_COL_GAP = 12
THEME_LIST_BOTTOM = HEIGHT - 40
theme_scroll_offset = 0.0
theme_scroll_speed = 70.0

# Settings menu state
settings_menu_state = "main"
settings_menu_target = "board_theme"
menu_transition_progress = 0.0
menu_transition_speed = 0.08
menu_transition_direction = "none"

images = load_images(current_theme)
openings = load_openings()
ANALYSIS_DEPTH = config.get("depth", 25)

# Live engine handle (assigned in main()); used by the Engine Settings page.
engine_thread = None

# --- Layout constants (shared between draw_gui and event handling) ---
BOARD_OY = TOP_BAR_HEIGHT + PADDING
# Board X is dynamic (eval bar can be left or right); compute per frame
BOARD_OX_LEFT = PADDING + EVAL_BAR_WIDTH + EVAL_BAR_GAP
BOARD_OX_RIGHT = WIDTH - PADDING - BOARD_WIDTH
EVAL_BAR_X_LEFT = PADDING
EVAL_BAR_X_RIGHT = WIDTH - PADDING - EVAL_BAR_WIDTH
# Runtime animated positions
board_ox_current = BOARD_OX_LEFT
board_ox_target = BOARD_OX_LEFT
eval_bar_x_current = EVAL_BAR_X_LEFT
eval_bar_x_target = EVAL_BAR_X_LEFT
eval_bar_side = "left"  # left or right
board_anim_t = 1.0  # 0->1 lerp
# Flip board (black perspective)
board_flipped = False
board_flip_anim = 0.0  # for smooth flip transition

# --- Engine slider draft state (smooth interpolate + save button) ---
engine_draft = dict(config)  # staged values, only applied on Save
engine_slider_display = {}  # visual lerp towards draft (per key)
engine_slider_velocity = {}
for _k in ENGINE_DEFAULTS:
    if _k != "theme":
        engine_slider_display[_k]=float(engine_draft.get(_k, ENGINE_DEFAULTS[_k]))
        engine_slider_velocity[_k]=0.0
engine_has_changes = False

# --- New game confirmation ---
show_newgame_confirm = False
confirm_anim = 0.0

# --- Legal move pulse ---
legal_pulse_t = 0.0

# --- Eval bar winning highlight pulse ---
eval_highlight_pulse = 0.0

# Backward compat: BOARD_OX constant for old helpers (will be overridden per frame)
BOARD_OX = BOARD_OX_LEFT

# --- Helpers for eval bar click ---
def get_eval_bar_rect():
    return pygame.Rect(int(eval_bar_x_current), BOARD_OY, EVAL_BAR_WIDTH, BOARD_WIDTH)

# --- Menu System ---
settings_menu_open = False
_button_y = (TOP_BAR_HEIGHT - 30) // 2
settings_button_rect = pygame.Rect(
    WIDTH - PADDING - 50, _button_y, 50, 30
)
new_game_button_rect = pygame.Rect(
    settings_button_rect.x - 10 - 50, _button_y, 50, 30
)
flip_button_rect = pygame.Rect(
    new_game_button_rect.x - 10 - 50, _button_y, 50, 30
)

menu_anim_progress = 0.0
menu_anim_opening = False
menu_anim_closing = False
menu_anim_speed = 0.09
menu_anim_close_speed = 0.11
restart_requested = False

# --- Opening Tracking ---
current_opening_name = ""


# --- Board orientation & layout helpers (flip + eval-side) ---
def get_board_ox():
    return board_ox_current

def square_to_screen(sq):
    """Screen pixel top-left for a square, respecting flip and current board ox."""
    f = chess.square_file(sq)
    r = chess.square_rank(sq)
    ox = get_board_ox()
    if board_flipped:
        x = ox + (7 - f) * SQUARE_SIZE
        y = BOARD_OY + r * SQUARE_SIZE
    else:
        x = ox + f * SQUARE_SIZE
        y = BOARD_OY + (7 - r) * SQUARE_SIZE
    return x, y

def square_center(sq):
    x, y = square_to_screen(sq)
    return (x + SQUARE_SIZE//2, y + SQUARE_SIZE//2)

def update_board_layout_anim():
    global board_ox_current, eval_bar_x_current, board_anim_t, board_ox_target, eval_bar_x_target
    # Smooth lerp for board/eval slide
    if abs(board_ox_current - board_ox_target) > 0.5 or abs(eval_bar_x_current - eval_bar_x_target) > 0.5:
        board_anim_t = min(1.0, board_anim_t + 0.12)
        e = cubic_ease_in_out(board_anim_t)
        # Interpolate from start to target — we store start implicitly via current, so lerp
        board_ox_current += (board_ox_target - board_ox_current) * 0.22
        eval_bar_x_current += (eval_bar_x_target - eval_bar_x_current) * 0.22
        if abs(board_ox_current - board_ox_target) < 0.5:
            board_ox_current = board_ox_target
            eval_bar_x_current = eval_bar_x_target
            board_anim_t = 1.0
    else:
        board_anim_t = 1.0

def toggle_eval_side():
    global eval_bar_side, board_ox_target, eval_bar_x_target, board_anim_t
    eval_bar_side = "right" if eval_bar_side == "left" else "left"
    if eval_bar_side == "left":
        board_ox_target = BOARD_OX_LEFT
        eval_bar_x_target = EVAL_BAR_X_LEFT
    else:
        board_ox_target = BOARD_OX_RIGHT
        eval_bar_x_target = EVAL_BAR_X_RIGHT
    board_anim_t = 0.0

def toggle_flip_board():
    global board_flipped
    board_flipped = not board_flipped

def update_slider_display():
    global engine_has_changes
    # smooth interpolate display values toward draft
    for k in ENGINE_ROW_KEYS:
        target = float(engine_draft.get(k, ENGINE_DEFAULTS[k]))
        cur = engine_slider_display.get(k, target)
        if abs(cur - target) > 0.01:
            # spring-like lerp
            cur += (target - cur) * 0.25
            engine_slider_display[k]=cur
        else:
            engine_slider_display[k]=target
    # check dirty
    engine_has_changes = any(engine_draft[k] != config.get(k) for k in ENGINE_ROW_KEYS)

def update_global_anims():
    global legal_pulse_t, eval_highlight_pulse, confirm_anim
    legal_pulse_t += 0.08
    eval_highlight_pulse += 0.06
    if show_newgame_confirm:
        confirm_anim = min(1.0, confirm_anim + 0.12)
    else:
        confirm_anim = max(0.0, confirm_anim - 0.12)

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
        self.board_reset_anims = []

        self.current_eval = 0.0
        self.target_eval = 0.0
        self.eval_interpolation_speed = 0.08

        # Engine arrow animation state (single fallback).
        self.arrow_start = None
        self.arrow_end = None
        self.arrow_target_start = None
        self.arrow_target_end = None
        self.arrow_alpha = 0.0
        self.arrow_target_alpha = 0.0
        self.arrow_fade_speed = 0.15
        self.arrow_morph_speed = 0.18
        # MultiPV arrows: dict id -> {start,end,target_start,target_end,alpha,target_alpha,is_knight,is_mate}
        self.multi_arrows = {}
        self.multi_arrow_fade_speed = 0.15
        self.multi_arrow_morph_speed = 0.18

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

        for anim in self.board_reset_anims[:]:
            anim["progress"] += anim["speed"]
            if anim["progress"] >= 1.0:
                self.board_reset_anims.remove(anim)

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

        # MultiPV arrows update
        for aid, adata in list(self.multi_arrows.items()):
            if abs(adata["alpha"] - adata["target_alpha"]) > 0.5:
                adata["alpha"] += (adata["target_alpha"] - adata["alpha"]) * self.multi_arrow_fade_speed
            else:
                adata["alpha"] = adata["target_alpha"]
            if adata["start"] is not None and adata["end"] is not None and adata["target_start"] is not None and adata["target_end"] is not None:
                ds = adata["target_start"] - adata["start"]
                if ds.length_squared() > 1.0:
                    adata["start"] += ds * self.multi_arrow_morph_speed
                else:
                    adata["start"] = adata["target_start"].copy()
                de = adata["target_end"] - adata["end"]
                if de.length_squared() > 1.0:
                    adata["end"] += de * self.multi_arrow_morph_speed
                else:
                    adata["end"] = adata["target_end"].copy()
            # prune faded out arrows that are no longer wanted
            if adata["target_alpha"] == 0 and adata["alpha"] < 1:
                # keep for a moment, will be removed by set_multipv_arrows if not in targets
                pass

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
        # 0.4 opacity so arrow doesn't disrupt board
        self.arrow_target_alpha = 255 * 0.4

    def set_multipv_arrows(self, targets):
        """targets: dict id -> (start_pos, end_pos, is_knight, is_mate, visible) or None to clear.
        Handles fading and morphing for multiple suggestion arrows."""
        if not targets:
            # fade out all
            for aid in self.multi_arrows:
                self.multi_arrows[aid]["target_alpha"] = 0.0
            return
        # Update / create entries for each target
        for aid, t in targets.items():
            if t is None:
                if aid in self.multi_arrows:
                    self.multi_arrows[aid]["target_alpha"] = 0.0
                continue
            start_pos, end_pos, is_knight, is_mate, visible = t
            if not visible:
                if aid in self.multi_arrows:
                    self.multi_arrows[aid]["target_alpha"] = 0.0
                continue
            new_start = pygame.Vector2(start_pos)
            new_end = pygame.Vector2(end_pos)
            if aid not in self.multi_arrows:
                self.multi_arrows[aid] = {
                    "start": new_start.copy(),
                    "end": new_end.copy(),
                    "target_start": new_start.copy(),
                    "target_end": new_end.copy(),
                    "alpha": 0.0,
                    "target_alpha": 255 * 0.4,
                    "is_knight": is_knight,
                    "is_mate": is_mate,
                }
            else:
                adata = self.multi_arrows[aid]
                # initialize if was faded out
                if adata["alpha"] < 10 or adata["target_start"] is None:
                    adata["start"] = new_start.copy()
                    adata["end"] = new_end.copy()
                adata["target_start"] = new_start
                adata["target_end"] = new_end
                adata["target_alpha"] = 255 * 0.4
                adata["is_knight"] = is_knight
                adata["is_mate"] = is_mate
        # fade out ids no longer in targets
        for aid in list(self.multi_arrows.keys()):
            if aid not in targets:
                self.multi_arrows[aid]["target_alpha"] = 0.0

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
        sx, sy = square_to_screen(start_sq)
        ex, ey = square_to_screen(end_sq)

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
            rx, ry = square_to_screen(rook_start_sq)
            rex, rey = square_to_screen(rook_target_sq)

            self.rook_anim = {
                "piece": rook_piece,
                "start": (rx, ry),
                "end": (rex, rey),
                "progress": 0.0,
                "speed": 0.04,
                "target_sq": rook_target_sq,
            }

    def add_capture(self, piece, sq):
        x, y = square_to_screen(sq)
        self.capture_anims.append(
            {"piece": piece, "pos": (x, y), "progress": 0.0, "speed": 0.03}
        )

    def add_board_reset(self, old_board, new_board):
        self.move_anim = None
        self.rook_anim = None
        self.board_reset_anims.clear()
        from collections import defaultdict
        old_groups = defaultdict(list)
        new_groups = defaultdict(list)
        for sq in chess.SQUARES:
            p = old_board.piece_at(sq)
            if p:
                old_groups[(p.piece_type, p.color)].append(sq)
            p2 = new_board.piece_at(sq)
            if p2:
                new_groups[(p2.piece_type, p2.color)].append(sq)
        for key in set(list(old_groups.keys()) + list(new_groups.keys())):
            olds = old_groups.get(key, [])
            news = new_groups.get(key, [])
            used_old = set()
            pairs = []
            for n_sq in news:
                best = None
                best_dist = None
                nx, ny = chess.square_file(n_sq), chess.square_rank(n_sq)
                for o_sq in olds:
                    if o_sq in used_old:
                        continue
                    ox, oy = chess.square_file(o_sq), chess.square_rank(o_sq)
                    dist = abs(nx - ox) + abs(ny - oy)
                    if best_dist is None or dist < best_dist:
                        best_dist = dist
                        best = o_sq
                if best is not None:
                    used_old.add(best)
                    pairs.append((best, n_sq))
            for o_sq, n_sq in pairs:
                if o_sq == n_sq:
                    continue
                sx, sy = square_to_screen(o_sq)
                ex, ey = square_to_screen(n_sq)
                piece = old_board.piece_at(o_sq)
                self.board_reset_anims.append({
                    "piece": piece,
                    "start": (sx, sy),
                    "end": (ex, ey),
                    "progress": 0.0,
                    "speed": 0.045,
                    "start_sq": o_sq,
                    "target_sq": n_sq,
                })

    def is_board_resetting(self):
        return len(self.board_reset_anims) > 0

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
        menu_anim_progress = min(1.0, menu_anim_progress + menu_anim_speed)
        if menu_anim_progress >= 1.0:
            menu_anim_progress = 1.0
            menu_anim_opening = False
            settings_menu_open = True
    elif menu_anim_closing:
        menu_anim_progress = max(0.0, menu_anim_progress - menu_anim_close_speed)
        if menu_anim_progress <= 0.0:
            menu_anim_progress = 0.0
            menu_anim_closing = False
            settings_menu_open = False
            settings_menu_state = "main"
            menu_transition_progress = 0.0
            menu_transition_direction = "none"
            theme_scroll_offset = 0.0


# --- Stockfish Threading ---
ANALYSIS_DEPTH = 25  # overwritten from config after load


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
            "lines": {},  # multipv number -> {score, mate, pv}
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
                "MultiPV": cfg.get("multipv", 1),
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
                "lines": {},
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
        """Extract depth/nps/score/pv/multipv from a UCI info line. Supports MultiPV."""
        tokens = line.split()
        updates = {}
        score = None
        mate_score = None
        pv_tokens = None
        multipv_num = None

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
                elif tok == "multipv" and i + 1 < len(tokens):
                    multipv_num = int(tokens[i + 1])
                    i += 2
                elif tok == "score" and i + 2 < len(tokens):
                    kind, value = tokens[i + 1], tokens[i + 2]
                    if kind == "cp":
                        score = int(value)
                        mate_score = None
                    elif kind == "mate":
                        mate_score = int(value)
                        score = 10000 if mate_score > 0 else -10000
                    i += 3
                elif tok == "pv":
                    pv_tokens = tokens[i + 1 :]
                    break
                else:
                    i += 1
        except ValueError:
            return

        # Convert to white-relative
        if not white_to_move:
            if score is not None:
                score = -score
            if mate_score is not None:
                mate_score = -mate_score

        moves = []
        if pv_tokens:
            for uci_move in pv_tokens:
                try:
                    moves.append(chess.Move.from_uci(uci_move))
                except ValueError:
                    break

        # Depth/nps are global
        with self.lock:
            if "depth" in updates:
                self.info["depth"] = updates["depth"]
            if "nps" in updates:
                self.info["nps"] = updates["nps"]

            # If multipv was indicated, store per-line
            if multipv_num is not None and moves:
                line_data = {}
                if score is not None:
                    line_data["score"] = score
                if mate_score is not None:
                    line_data["mate"] = mate_score
                else:
                    line_data["mate"] = None
                line_data["pv"] = moves
                # Also store depth for that line
                if "depth" in updates:
                    line_data["depth"] = updates["depth"]
                self.info.setdefault("lines", {})[multipv_num] = line_data
                # Keep #1 as primary for eval bar / single-arrow compat
                if multipv_num == 1:
                    if score is not None:
                        self.info["score"] = score
                    if mate_score is not None:
                        self.info["mate"] = mate_score
                    else:
                        # clear mate if this line is not mate (but keep None)
                        if "mate" in self.info and mate_score is None and score is not None:
                            # only clear if previous was mate but now cp
                            self.info["mate"] = None
                    self.info["pv"] = moves
                return

            # Fallback for non-multipv (single PV) lines
            if score is not None:
                self.info["score"] = score
                if mate_score is not None:
                    self.info["mate"] = mate_score
                else:
                    self.info["mate"] = None
            elif mate_score is not None:
                self.info["mate"] = mate_score
            if moves:
                self.info["pv"] = moves
                # also keep lines[1] in sync for unified rendering
                self.info.setdefault("lines", {})[1] = {"score": self.info.get("score", 0), "mate": self.info.get("mate"), "pv": moves}

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
    # Hide opening if no moves have been made (avoid "Unknown Opening" at start)
    if len(board.move_stack) == 0:
        return ""
    fen_placement = board.fen().split(" ")[0]
    opening_name = openings.get(fen_placement)

    if opening_name:
        current_opening_name = opening_name
    elif not current_opening_name:
        current_opening_name = "Unknown Opening"

    return current_opening_name


def reset_opening():
    global current_opening_name
    current_opening_name = ""


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


# --- Drag & Click Helpers ---
def get_square_from_pos(pos):
    """Return chess square for a mouse pos inside the board, else None."""
    mx, my = pos
    ox = get_board_ox()
    if not (ox <= mx < ox + BOARD_WIDTH and BOARD_OY <= my < BOARD_OY + BOARD_WIDTH):
        return None
    file_vis = (mx - ox) // SQUARE_SIZE
    rank_vis = (my - BOARD_OY) // SQUARE_SIZE
    if board_flipped:
        file = 7 - file_vis
        rank = rank_vis
    else:
        file = file_vis
        rank = 7 - rank_vis
    return chess.square(file, rank)


def build_move_with_promotion(board, from_sq, to_sq):
    """Create a Move handling pawn promotion to queen."""
    move = chess.Move(from_sq, to_sq)
    piece = board.piece_at(from_sq)
    if piece and piece.piece_type == chess.PAWN:
        if chess.square_rank(to_sq) == 0 or chess.square_rank(to_sq) == 7:
            move = chess.Move(from_sq, to_sq, promotion=chess.QUEEN)
    return move


def get_castling_info(board, from_sq, to_sq):
    """Return (is_castling, rook_start, rook_target, rook_piece) for a king move."""
    piece = board.piece_at(from_sq)
    if not piece or piece.piece_type != chess.KING:
        return False, None, None, None
    if (from_sq == chess.E1 and to_sq == chess.G1) or (from_sq == chess.E8 and to_sq == chess.G8):
        rook_start = chess.H1 if from_sq == chess.E1 else chess.H8
        rook_target = chess.F1 if from_sq == chess.E1 else chess.F8
        return True, rook_start, rook_target, board.piece_at(rook_start)
    if (from_sq == chess.E1 and to_sq == chess.C1) or (from_sq == chess.E8 and to_sq == chess.C8):
        rook_start = chess.A1 if from_sq == chess.E1 else chess.A8
        rook_target = chess.D1 if from_sq == chess.E1 else chess.D8
        return True, rook_start, rook_target, board.piece_at(rook_start)
    return False, None, None, None


def try_execute_move(board, anim_mgr, engine_thread, from_sq, to_sq, animate=True):
    """Attempt to push from_sq->to_sq (with promotion/castling/capture).
    Returns True if the move was legal and executed."""
    move = build_move_with_promotion(board, from_sq, to_sq)
    if move not in board.legal_moves:
        return False

    is_castling, rook_start_sq, rook_target_sq, rook_piece = get_castling_info(board, from_sq, to_sq)

    captured_piece = board.piece_at(to_sq)
    # Handle en-passant capture (destination empty but diagonal pawn move)
    if not captured_piece:
        piece = board.piece_at(from_sq)
        if piece and piece.piece_type == chess.PAWN:
            if chess.square_file(from_sq) != chess.square_file(to_sq):
                # en passant square is behind the destination on the same rank as mover
                ep_sq = chess.square(chess.square_file(to_sq), chess.square_rank(from_sq))
                ep_piece = board.piece_at(ep_sq)
                if ep_piece and ep_piece.piece_type == chess.PAWN:
                    captured_piece = ep_piece
                    # Animate capture at the captured pawn's square
                    anim_mgr.add_capture(captured_piece, ep_sq)
                    captured_piece = None  # already handled

    if captured_piece:
        anim_mgr.add_capture(captured_piece, to_sq)

    if animate:
        anim_mgr.add_move(
            board.piece_at(from_sq),
            from_sq,
            to_sq,
            is_castling,
            rook_start_sq,
            rook_target_sq,
            rook_piece,
        )

    board.push(move)
    engine_thread.update_position(board)
    return True


# --- Promotion ---
PROMOTION_CHOICES = [chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT]
promotion_pending = None  # {"from_sq":..., "to_sq":..., "color":...}


def is_promotion_move(board, from_sq, to_sq):
    piece = board.piece_at(from_sq)
    if not piece or piece.piece_type != chess.PAWN:
        return False
    rank = chess.square_rank(to_sq)
    return rank == 0 or rank == 7


def get_promotion_rects():
    total_w = 4 * SQUARE_SIZE + 3 * 10
    start_x = WIDTH // 2 - total_w // 2
    y = BOARD_OY + BOARD_WIDTH // 2 - SQUARE_SIZE // 2
    rects = []
    for i in range(4):
        x = start_x + i * (SQUARE_SIZE + 10)
        rects.append(pygame.Rect(x, y, SQUARE_SIZE, SQUARE_SIZE))
    return rects


def draw_promotion_menu(screen, color):
    overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    overlay.fill((0, 0, 0, 160))
    screen.blit(overlay, (0, 0))
    title = font_large.render("Choose promotion", True, TEXT_COLOR)
    screen.blit(title, (WIDTH // 2 - title.get_width() // 2, BOARD_OY + BOARD_WIDTH // 2 - 90))
    hint = font_tiny.render("Queen, Rook, Bishop, Knight (no pawn/king)", True, TEXT_MUTED)
    screen.blit(hint, (WIDTH // 2 - hint.get_width() // 2, BOARD_OY + BOARD_WIDTH // 2 - 60))
    rects = get_promotion_rects()
    mx, my = pygame.mouse.get_pos()
    for i, piece_type in enumerate(PROMOTION_CHOICES):
        rect = rects[i]
        col = "w" if color == chess.WHITE else "b"
        sym = {chess.QUEEN: "q", chess.ROOK: "r", chess.BISHOP: "b", chess.KNIGHT: "n"}[piece_type]
        pkey = f"{col}{sym}"
        is_hovered = rect.collidepoint(mx, my)
        bg = (90, 90, 120) if is_hovered else (60, 60, 60)
        pygame.draw.rect(screen, bg, rect, border_radius=8)
        pygame.draw.rect(screen, MENU_BORDER_COLOR, rect, 2, border_radius=8)
        if pkey in images:
            # Center piece slightly smaller for padding
            img = images[pkey]
            # images are 80x80 already
            screen.blit(img, rect.topleft)
        if is_hovered:
            pygame.draw.rect(screen, (120, 180, 255), rect, 3, border_radius=8)


def try_execute_promotion(board, anim_mgr, engine_thread, from_sq, to_sq, promo_piece, animate=True):
    move = chess.Move(from_sq, to_sq, promotion=promo_piece)
    if move not in board.legal_moves:
        return False
    is_castling, rook_start_sq, rook_target_sq, rook_piece = get_castling_info(board, from_sq, to_sq)
    captured_piece = board.piece_at(to_sq)
    if captured_piece:
        anim_mgr.add_capture(captured_piece, to_sq)
    if animate:
        anim_mgr.add_move(board.piece_at(from_sq), from_sq, to_sq, is_castling, rook_start_sq, rook_target_sq, rook_piece)
    board.push(move)
    engine_thread.update_position(board)
    return True


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


def _lerp_color(c1, c2, t):
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def draw_arrow(screen, start_pos, end_pos, color, width=7, alpha=255, is_mate=False):
    start = pygame.Vector2(start_pos)
    end = pygame.Vector2(end_pos)

    v = end - start
    length = v.length()
    if length == 0:
        return

    # Subtle pulse for mate arrows (red) — not too noticeable
    pulse = 0.0
    if is_mate:
        pulse = math.sin(pygame.time.get_ticks() * 0.005) * 0.07  # ~7% pulse
        width = width * (1 + pulse * 0.5)
        alpha = max(0, min(255, int(alpha * (1 + pulse * 0.3))))
    v_unit = v / length

    head_length = width * 3
    head_width = width * 2.5

    neck = end - v_unit * (head_length * 0.8)

    perp = pygame.Vector2(-v_unit.y, v_unit.x) * (head_width / 2)

    point1 = end
    point2 = end - v_unit * head_length + perp
    point3 = end - v_unit * head_length - perp

    a = max(0, min(255, int(alpha)))
    if a <= 1:
        return

    # Choose gradient based on mate state
    grad_start = ARROW_MATE_START if is_mate else ARROW_GRAD_START
    grad_end = ARROW_MATE_END if is_mate else ARROW_GRAD_END
    xs = (start.x, end.x, point1.x, point2.x, point3.x)
    ys = (start.y, end.y, point1.y, point2.y, point3.y)
    pad = width + 6
    min_x = int(min(xs)) - pad
    min_y = int(min(ys)) - pad
    max_x = int(max(xs)) + pad
    max_y = int(max(ys)) + pad
    w = max(1, max_x - min_x)
    h = max(1, max_y - min_y)

    surf = pygame.Surface((w, h), pygame.SRCALPHA)
    offset = pygame.Vector2(min_x, min_y)

    # Draw gradient shaft as many small segments — anti-aliased via smooth line
    segs = 24 if is_mate else 20
    for i in range(segs):
        t0 = i / segs
        t1 = (i + 1) / segs
        c = _lerp_color(grad_start, grad_end, t0)
        rgba = (c[0], c[1], c[2], a)
        p0 = start + (neck - start) * t0
        p1 = start + (neck - start) * t1
        pygame.draw.aaline(surf, rgba, p0 - offset, p1 - offset) if hasattr(pygame.draw, 'aaline') else None
        pygame.draw.line(surf, rgba, p0 - offset, p1 - offset, max(1,int(width)))

    # Arrow head in solid end colour with subtle anti-aliased edge
    head_c = grad_end
    head_rgba = (head_c[0], head_c[1], head_c[2], a)
    pygame.draw.polygon(
        surf, head_rgba, [point1 - offset, point2 - offset, point3 - offset]
    )
    # AA outline for head (smooth)
    try:
        pygame.gfxdraw.aapolygon(surf, [point1 - offset, point2 - offset, point3 - offset], head_rgba)
    except Exception:
        pass
    screen.blit(surf, (min_x, min_y))


def _get_knight_corner(start_pos, end_pos):
    """Return the L-corner for a knight move (long leg first)."""
    start = pygame.Vector2(start_pos)
    end = pygame.Vector2(end_pos)
    dx = abs(end.x - start.x)
    dy = abs(end.y - start.y)
    # Go along the longer delta first, like a real knight's path
    if dx > dy:
        # horizontal first: corner shares end.x, start.y
        return pygame.Vector2(end.x, start.y)
    else:
        # vertical first: corner shares start.x, end.y
        return pygame.Vector2(start.x, end.y)


def draw_knight_arrow(screen, start_pos, end_pos, color, width=7, alpha=255, is_mate=False):
    """L-shaped arrow for knight moves: start -> corner -> end with arrowhead."""
    start = pygame.Vector2(start_pos)
    end = pygame.Vector2(end_pos)
    corner = _get_knight_corner(start, end)

    v = end - corner
    length = v.length()
    if length == 0:
        draw_arrow(screen, start_pos, end_pos, color, width, alpha, is_mate)
        return
    if is_mate:
        pulse = math.sin(pygame.time.get_ticks() * 0.005) * 0.07
        width = width * (1 + pulse * 0.5)
        alpha = max(0, min(255, int(alpha * (1 + pulse * 0.3))))

    v_unit = v / length
    head_length = width * 3
    head_width = width * 2.5
    neck = end - v_unit * (head_length * 0.8)
    perp = pygame.Vector2(-v_unit.y, v_unit.x) * (head_width / 2)
    point1 = end
    point2 = end - v_unit * head_length + perp
    point3 = end - v_unit * head_length - perp

    a = max(0, min(255, int(alpha)))
    if a <= 1:
        return
    grad_start = ARROW_MATE_START if is_mate else ARROW_GRAD_START
    grad_end = ARROW_MATE_END if is_mate else ARROW_GRAD_END
    xs = (start.x, corner.x, end.x, point1.x, point2.x, point3.x)
    ys = (start.y, corner.y, end.y, point1.y, point2.y, point3.y)
    pad = width + 6
    min_x = int(min(xs)) - pad
    min_y = int(min(ys)) - pad
    max_x = int(max(xs)) + pad
    max_y = int(max(ys)) + pad
    w = max(1, max_x - min_x)
    h = max(1, max_y - min_y)

    surf = pygame.Surface((w, h), pygame.SRCALPHA)
    offset = pygame.Vector2(min_x, min_y)

    len1 = (corner - start).length()
    len2 = (neck - corner).length()
    total = max(1, len1 + len2)
    segs = 24
    for i in range(segs):
        t0 = i / segs
        t1 = (i + 1) / segs
        def point_at(t):
            if t * total <= len1:
                seg_t = (t * total) / max(1, len1)
                return start + (corner - start) * seg_t
            else:
                seg_t = (t * total - len1) / max(1, len2)
                return corner + (neck - corner) * seg_t
        p0 = point_at(t0)
        p1 = point_at(t1)
        c = _lerp_color(grad_start, grad_end, t0)
        rgba = (c[0], c[1], c[2], a)
        pygame.draw.line(surf, rgba, p0 - offset, p1 - offset, max(1,int(width)))

    head_c = grad_end
    head_rgba = (head_c[0], head_c[1], head_c[2], a)
    pygame.draw.polygon(surf, head_rgba, [point1 - offset, point2 - offset, point3 - offset])
    try:
        pygame.gfxdraw.aapolygon(surf, [point1 - offset, point2 - offset, point3 - offset], head_rgba)
    except Exception:
        pass
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

    # --- Winning side highlight (subtle glow) ---
    winning = None
    if mate is not None:
        winning = "white" if mate > 0 else "black"
    else:
        if current_eval > 180:
            winning = "white"
        elif current_eval < -180:
            winning = "black"
    if winning:
        pulse_a = int(55 + 35 * math.sin(pygame.time.get_ticks() * 0.003))
        if winning == "white" and white_h > 12:
            glow = pygame.Surface((w+4, white_h), pygame.SRCALPHA)
            glow.fill((255, 255, 160, pulse_a//3))
            screen.blit(glow, (x-2, y+black_h))
            # inner bright edge
            pygame.draw.rect(screen, (255, 245, 140, pulse_a), (x, y+black_h, w, 3), border_radius=1)
        elif winning == "black" and black_h > 12:
            glow = pygame.Surface((w+4, black_h), pygame.SRCALPHA)
            glow.fill((90, 90, 255, pulse_a//4))
            screen.blit(glow, (x-2, y))
            pygame.draw.rect(screen, (120, 120, 255, pulse_a), (x, y, w, 3), border_radius=1)

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
    """Draw a detailed cog/gear icon with teeth, hub and spokes."""
    # Outer gear body with teeth — 10 teeth for smoother look
    n_teeth = 10
    r_outer = size
    r_inner = size * 0.72
    pts = []
    for i in range(n_teeth):
        base = (2 * math.pi * i) / n_teeth - math.pi / 2 + rotation
        tooth_w = (2 * math.pi / n_teeth) * 0.48
        # Alternate outer/inner points for zigzag teeth
        ang_outer = base + tooth_w / 2
        ang_inner = base + (2 * math.pi / n_teeth) - tooth_w / 2
        # Root then tip then root
        pts.append((cx + r_inner * math.cos(base), cy + r_inner * math.sin(base)))
        pts.append((cx + r_outer * math.cos(ang_outer - 0.06), cy + r_outer * math.sin(ang_outer - 0.06)))
        pts.append((cx + r_outer * math.cos(ang_outer + 0.06), cy + r_outer * math.sin(ang_outer + 0.06)))
    # Close shape by adding final root
    # Draw filled gear then outline for crisp look
    # Handle alpha color
    if len(color) == 4 and color[3] < 255:
        # Use polygon with alpha
        pygame.draw.polygon(surface, color, pts, 0)
        pygame.draw.polygon(surface, color, pts, max(1, thickness))
    else:
        # Draw gear body filled with muted metallic, outline with color
        base_fill = (color[0], color[1], color[2]) if len(color) == 3 else (color[0], color[1], color[2])
        # Subtle inner fill darker
        pygame.draw.polygon(surface, base_fill, pts, 0)
        pygame.draw.polygon(surface, base_fill, pts, max(1, thickness))
        # Highlight ring
        pygame.draw.circle(surface, base_fill, (cx, cy), int(size * 0.55), max(1, thickness-1) if thickness>1 else 1)
    # Center hub
    hub_r = int(size * 0.30)
    hub_color = (color[0], color[1], color[2]) if len(color) == 3 else (color[0], color[1], color[2])
    pygame.draw.circle(surface, hub_color, (cx, cy), hub_r, max(1, thickness))
    pygame.draw.circle(surface, (30, 30, 30), (cx, cy), max(2, hub_r-2), 0)
    # Spokes — 3 small circles around hub
    for k in range(3):
        ang = (2 * math.pi * k) / 3 + rotation
        sx = cx + int(math.cos(ang) * size * 0.45)
        sy = cy + int(math.sin(ang) * size * 0.45)
        pygame.draw.circle(surface, hub_color, (sx, sy), max(1, int(size * 0.12)), 1)
    # Center dot
    pygame.draw.circle(surface, hub_color, (cx, cy), max(2, int(size*0.14)), 0)


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
    # Only visible when menu is closed — opening animation is handled entirely
    # by draw_settings_menu (single backdrop), avoiding a double-fade / double
    # overlay that previously caused flicker and a competing expanding border.
    if menu_anim_progress > 0:
        return
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


def draw_board_theme_content(screen, offset_x=0, offset_y=0):
    global current_theme, theme_scroll_offset

    mx, my = pygame.mouse.get_pos()
    themes = get_available_themes()

    # --- Preview panel (reserved section) ---
    preview_rect = pygame.Rect(WIDTH // 2 - 200 + offset_x, THEME_PREVIEW_Y + offset_y, 400, THEME_PREVIEW_H)
    pygame.draw.rect(screen, (45, 45, 50), preview_rect, border_radius=8)
    pygame.draw.rect(screen, MENU_BORDER_COLOR, preview_rect, 2, border_radius=8)

    # Determine hovered theme for preview
    # Need to compute grid to know hover, but we can first do quick hover check using same grid logic
    # For preview we need to know hovered theme; we'll compute after we know layout, but preview should be drawn first
    # So temporarily find hovered by scanning with current scroll (compact grid)
    item_h = THEME_ITEM_HEIGHT
    gap = THEME_ITEM_GAP
    top = THEME_LIST_TOP + offset_y
    cols = 2
    rows = (len(themes) + cols - 1) // cols
    viewport_h = THEME_LIST_BOTTOM - THEME_LIST_TOP
    total_h = max(1, rows * item_h + max(0, rows - 1) * gap)
    max_scroll = max(0, total_h - viewport_h)
    theme_scroll_offset = max(0.0, min(theme_scroll_offset, max_scroll))
    viewport = pygame.Rect(WIDTH // 2 - 200 + offset_x, top, 400, viewport_h)

    hovered_theme = None
    hovered_display = None
    for i, (tid, dname) in enumerate(themes):
        row = i // cols
        col = i % cols
        x = viewport.x + col * (THEME_ITEM_WIDTH + THEME_COL_GAP)
        y = top + row * (item_h + gap) - int(theme_scroll_offset)
        r = pygame.Rect(x, y, THEME_ITEM_WIDTH, item_h)
        if viewport.collidepoint(mx, my) and r.collidepoint(mx, my) and menu_transition_direction == "none":
            hovered_theme = tid
            hovered_display = dname
            break

    preview_theme = hovered_theme if hovered_theme is not None else current_theme
    preview_display = hovered_display if hovered_display is not None else next((d for tid, d in themes if tid == current_theme), current_theme)
    # Title in preview
    title_s = font_tiny.render("Preview — Hover a style to see pieces", True, TEXT_MUTED)
    screen.blit(title_s, (preview_rect.centerx - title_s.get_width() // 2, preview_rect.y + 6))
    name_s = font_small.render(preview_display, True, TEXT_ACCENT if hovered_theme else TEXT_COLOR)
    screen.blit(name_s, (preview_rect.centerx - name_s.get_width() // 2, preview_rect.y + 22))
    # Draw 12 pieces in 2 rows of 6
    prev_imgs = get_preview_images(preview_theme)
    # order: white K Q R B N P / black K Q R B N P
    order = ["wk", "wq", "wr", "wb", "wn", "wp", "bk", "bq", "br", "bb", "bn", "bp"]
    # Layout
    piece_sz = 28
    gap_px = 6
    per_row = 6
    row_w = per_row * piece_sz + (per_row - 1) * gap_px
    start_x = preview_rect.centerx - row_w // 2
    for idx, pkey in enumerate(order):
        row = idx // per_row
        col = idx % per_row
        px = start_x + col * (piece_sz + gap_px)
        py = preview_rect.y + 44 + row * (piece_sz + gap_px)
        # small square bg for each piece
        bg = (60, 60, 60) if (row + col) % 2 == 0 else (70, 70, 70)
        # alternate light/dark like board for preview
        pygame.draw.rect(screen, bg, (px, py, piece_sz, piece_sz), border_radius=3)
        pygame.draw.rect(screen, (90, 90, 90), (px, py, piece_sz, piece_sz), 1, border_radius=3)
        if pkey in prev_imgs:
            screen.blit(prev_imgs[pkey], (px, py))

    # --- Compact 2-column grid ---
    screen.set_clip(viewport)
    for i, (theme_id, display_name) in enumerate(themes):
        row = i // cols
        col = i % cols
        x = viewport.x + col * (THEME_ITEM_WIDTH + THEME_COL_GAP)
        y = top + row * (item_h + gap) - int(theme_scroll_offset)
        theme_rect = pygame.Rect(x, y, THEME_ITEM_WIDTH, item_h)
        if theme_rect.bottom < top or theme_rect.top > top + viewport_h:
            continue

        is_current = theme_id == current_theme
        is_hovered = (
            theme_rect.collidepoint(mx, my)
            and viewport.collidepoint(mx, my)
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
        pygame.draw.rect(screen, MENU_BORDER_COLOR, theme_rect, 1, border_radius=4)

        # Use small font for compact
        theme_text = font_small.render(display_name, True, TEXT_COLOR)
        theme_text_rect = theme_text.get_rect(center=theme_rect.center)
        screen.blit(theme_text, theme_text_rect)
        # current indicator dot
        if is_current:
            pygame.draw.circle(screen, (120, 200, 120), (theme_rect.right - 10, theme_rect.centery), 4)
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

        hint = font_tiny.render("Scroll for more styles", True, TEXT_MUTED)
        screen.blit(
            hint, (WIDTH // 2 - hint.get_width() // 2, THEME_LIST_BOTTOM + offset_y + 6)
        )


def scroll_theme_list(delta):
    """Scroll the board theme list (positive delta scrolls towards the end)."""
    global theme_scroll_offset
    themes = get_available_themes()
    cols = 2
    rows = (len(themes) + cols - 1) // cols
    total_h = rows * THEME_ITEM_HEIGHT + max(0, rows - 1) * THEME_ITEM_GAP
    viewport_h = THEME_LIST_BOTTOM - THEME_LIST_TOP
    max_scroll = max(0, total_h - viewport_h)
    theme_scroll_offset = max(0.0, min(theme_scroll_offset + delta, max_scroll))


# --- Engine Settings Page (Sliders) ---
ENGINE_ROW_KEYS = ["threads", "skill", "hash", "depth", "multipv"]
ENGINE_ROW_LABELS = {"threads": "Threads", "skill": "Skill Level", "hash": "Hash", "depth": "Depth Limit", "multipv": "MultiPV"}
ENGINE_ROW_LIMITS = {"threads": (1, 1024), "skill": (0, 20), "hash": (16, 32768), "depth": (5, 40), "multipv": (1, 5)}
ENGINE_PARAM_NAMES = {"threads": "Threads", "skill": "Skill Level", "hash": "Hash", "multipv": "MultiPV"}
HASH_STEPS = [16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768]

ENGINE_ROW_TOP = 120
ENGINE_ROW_HEIGHT = 64
ENGINE_ROW_GAP = 14
ENGINE_ROW_WIDTH = 480
ENGINE_SLIDER_TRACK_W = 180
ENGINE_SLIDER_TRACK_H = 6
ENGINE_SLIDER_THUMB_R = 10

engine_slider_dragging = None


def get_engine_slider_rects(i, offset_x=0, offset_y=0):
    y = ENGINE_ROW_TOP + i * (ENGINE_ROW_HEIGHT + ENGINE_ROW_GAP) + offset_y
    row_rect = pygame.Rect(
        WIDTH // 2 - ENGINE_ROW_WIDTH // 2 + offset_x,
        y,
        ENGINE_ROW_WIDTH,
        ENGINE_ROW_HEIGHT,
    )
    track_w = ENGINE_SLIDER_TRACK_W
    track_h = ENGINE_SLIDER_TRACK_H
    track_x = row_rect.x + 150
    track_y = row_rect.centery - track_h // 2
    track_rect = pygame.Rect(track_x, track_y, track_w, track_h)
    value_rect = pygame.Rect(track_rect.right + 12, row_rect.y, 70, ENGINE_ROW_HEIGHT)
    return row_rect, track_rect, value_rect


def get_engine_reset_rect(offset_x=0, offset_y=0):
    reset_w, reset_h = 140, 40
    reset_y = ENGINE_ROW_TOP + 5 * (ENGINE_ROW_HEIGHT + ENGINE_ROW_GAP) + 12 + offset_y
    return pygame.Rect(
        WIDTH // 2 - 150 + offset_x, reset_y, reset_w, reset_h
    )

def get_engine_save_rect(offset_x=0, offset_y=0):
    save_w, save_h = 140, 40
    save_y = ENGINE_ROW_TOP + 5 * (ENGINE_ROW_HEIGHT + ENGINE_ROW_GAP) + 12 + offset_y
    return pygame.Rect(
        WIDTH // 2 + 10 + offset_x, save_y, save_w, save_h
    )

def set_engine_slider_value(key, mouse_x, track_rect):
    low, high = ENGINE_ROW_LIMITS[key]
    t = (mouse_x - track_rect.x) / max(1, track_rect.width)
    t = max(0.0, min(1.0, t))
    if key == "hash":
        idx = int(round(t * (len(HASH_STEPS) - 1)))
        new_value = HASH_STEPS[idx]
    else:
        new_value = int(round(low + t * (high - low)))
    if new_value != engine_draft.get(key):
        engine_draft[key] = new_value
        # do not apply instantly — wait for Save
        # display will smoothly lerp via update_slider_display()

def reset_engine_settings():
    # Reset draft to defaults, not yet saved
    for k in ENGINE_ROW_KEYS:
        if k in ENGINE_DEFAULTS:
            engine_draft[k]=ENGINE_DEFAULTS[k]

def apply_engine_settings():
    global ANALYSIS_DEPTH, engine_has_changes
    changed=False
    for k in ENGINE_ROW_KEYS:
        if engine_draft.get(k) != config.get(k):
            config[k]=engine_draft[k]
            changed=True
            if k == "depth":
                ANALYSIS_DEPTH = engine_draft[k]
            if engine_thread is not None and k in ENGINE_PARAM_NAMES:
                engine_thread.set_pending_parameters({ENGINE_PARAM_NAMES[k]: engine_draft[k]})
    if changed:
        save_config()
    engine_has_changes=False


def draw_engine_settings_content(screen, offset_x=0, offset_y=0):
    mx, my = pygame.mouse.get_pos()

    for i, key in enumerate(ENGINE_ROW_KEYS):
        row_rect, track_rect, value_rect = get_engine_slider_rects(i, offset_x, offset_y)

        pygame.draw.rect(screen, (55, 55, 55), row_rect, border_radius=6)
        pygame.draw.rect(screen, MENU_BORDER_COLOR, row_rect, 2, border_radius=6)

        label = font_small.render(ENGINE_ROW_LABELS[key], True, TEXT_COLOR)
        screen.blit(label, (row_rect.x + 14, row_rect.centery - label.get_height() // 2))

        # Slider track background with AA
        pygame.draw.rect(screen, (45, 45, 45), track_rect, border_radius=3)
        # Smooth interpolated fill based on display lerp, not raw draft snap
        low, high = ENGINE_ROW_LIMITS[key]
        # display value is float lerped toward draft
        disp_val = engine_slider_display.get(key, float(engine_draft.get(key, low)))
        if key == "hash":
            # map hash display to index for smooth visual (use float for lerp)
            try:
                idx_disp = HASH_STEPS.index(engine_draft[key]) if engine_draft[key] in HASH_STEPS else 0
                # for smooth, lerp between steps based on display float? we approximate via disp_val index ratio
                # find nearest step index for disp_val
                # instead directly lerp display hash value proportionally (log scale)
                # use display's actual value mapped to index via closest
                # simpler: t from display's actual hash value normalized log-like via index interpolation
                # compute t as display mapped continuously
                # we interpolate display hash value linearly between steps for visual smoothness
                # get t via display value's proportional position
                # find surrounding steps
                dv = disp_val
                # convert dv to t via linear search between steps
                if dv <= HASH_STEPS[0]:
                    t = 0.0
                elif dv >= HASH_STEPS[-1]:
                    t = 1.0
                else:
                    for s_i in range(len(HASH_STEPS)-1):
                        lo_h = HASH_STEPS[s_i]
                        hi_h = HASH_STEPS[s_i+1]
                        if lo_h <= dv <= hi_h:
                            inner = (dv - lo_h) / max(1, hi_h - lo_h)
                            t = (s_i + inner) / max(1, len(HASH_STEPS)-1)
                            break
                    else:
                        t = idx_disp / max(1, len(HASH_STEPS)-1)
            except Exception:
                t = 0.5
        else:
            t = (disp_val - low) / max(1, high - low)
            t = max(0.0, min(1.0, t))
        fill_w = int(track_rect.width * t)
        fill_rect = pygame.Rect(track_rect.x, track_rect.y, max(4, fill_w), track_rect.height)
        fill_color = (100, 200, 120) if key == "depth" else (80, 140, 255)
        pygame.draw.rect(screen, fill_color, fill_rect, border_radius=3)
        # Thumb with shadow and AA
        thumb_x = track_rect.x + int(t * track_rect.width)
        thumb_y = track_rect.centery
        is_dragging = engine_slider_dragging == key
        thumb_hover = is_dragging or (abs(mx - thumb_x) < 14 and abs(my - thumb_y) < 14) or track_rect.collidepoint(mx, my)
        # shadow
        pygame.draw.circle(screen, (0,0,0,70), (thumb_x, thumb_y+2), ENGINE_SLIDER_THUMB_R+1)
        thumb_color = (130, 180, 255) if is_dragging else (210, 210, 210) if thumb_hover else (185, 185, 185)
        pygame.draw.circle(screen, thumb_color, (thumb_x, thumb_y), ENGINE_SLIDER_THUMB_R)
        # AA outline
        try:
            pygame.gfxdraw.aacircle(screen, thumb_x, thumb_y, ENGINE_SLIDER_THUMB_R, (30,30,30))
        except Exception:
            pass
        pygame.draw.circle(screen, (30, 30, 30), (thumb_x, thumb_y), ENGINE_SLIDER_THUMB_R, 1)

        # Value text shows draft (staged) value, highlight if dirty
        is_dirty = engine_draft.get(key) != config.get(key)
        if key == "hash":
            value_text = f"{engine_draft[key]} MB"
        else:
            value_text = str(engine_draft[key])
        vc = TEXT_ACCENT if is_dirty else (TEXT_ACCENT if thumb_hover or is_dragging else TEXT_COLOR)
        value_surf = font_small.render(value_text, True, vc)
        screen.blit(value_surf, (value_rect.centerx - value_surf.get_width() // 2, value_rect.centery - value_surf.get_height() // 2))
        # small dot if dirty
        if is_dirty:
            pygame.draw.circle(screen, (255, 200, 80), (value_rect.right - 6, value_rect.centery - 14), 3)

    reset_rect = get_engine_reset_rect(offset_x, offset_y)
    save_rect = get_engine_save_rect(offset_x, offset_y)
    reset_hovered = reset_rect.collidepoint(mx, my)
    save_hovered = save_rect.collidepoint(mx, my)
    # Reset button
    pygame.draw.rect(screen, (95, 95, 95) if reset_hovered else (75, 75, 75), reset_rect, border_radius=8)
    pygame.draw.rect(screen, MENU_BORDER_COLOR, reset_rect, 2, border_radius=8)
    reset_text = font_small.render("Reset", True, TEXT_COLOR)
    screen.blit(reset_text, reset_text.get_rect(center=reset_rect.center))
    # Save button — modern, disabled look if no changes
    if engine_has_changes:
        save_bg = (55, 145, 85) if save_hovered else (45, 125, 75)
        save_border = (80, 180, 110)
        save_text_col = TEXT_COLOR
    else:
        save_bg = (60, 60, 60)
        save_border = (80, 80, 80)
        save_text_col = (130,130,130)
    pygame.draw.rect(screen, save_bg, save_rect, border_radius=8)
    pygame.draw.rect(screen, save_border, save_rect, 2, border_radius=8)
    save_text = font_small.render("Save", True, save_text_col)
    screen.blit(save_text, save_text.get_rect(center=save_rect.center))

    note = font_tiny.render("Adjust sliders then press Save to apply", True, TEXT_MUTED)
    screen.blit(note, (WIDTH // 2 - note.get_width() // 2, reset_rect.bottom + 10))
    if engine_has_changes:
        dirty_note = font_tiny.render("• Unsaved changes", True, (255, 220, 120))
        screen.blit(dirty_note, (WIDTH // 2 - dirty_note.get_width()//2, reset_rect.bottom + 28))


def get_github_button_rect(offset_x=0, about_y=0):
    w, h = 220, 36
    x = WIDTH // 2 - w // 2 + offset_x
    y = about_y + 64
    return pygame.Rect(x, y, w, h)


def get_restart_button_rect(offset_x=0, about_y=0):
    w, h = 220, 36
    x = WIDTH // 2 - w // 2 + offset_x
    y = about_y + 108
    return pygame.Rect(x, y, w, h)

def draw_polished_close_button(screen, rect, hovered):
    # Modern pill with X icon: subtle gradient, hover glow, AA circle
    bg = (200, 60, 60) if hovered else (155, 55, 55)
    # outer glow when hovered
    if hovered:
        glow = pygame.Surface((rect.width+12, rect.height+12), pygame.SRCALPHA)
        pygame.draw.rect(glow, (255,80,80,45), glow.get_rect(), border_radius=12)
        screen.blit(glow, (rect.x-6, rect.y-6))
    pygame.draw.rect(screen, bg, rect, border_radius=14)
    pygame.draw.rect(screen, (255,255,255,35) if hovered else MENU_BORDER_COLOR, rect, 1, border_radius=14)
    # X icon — two diagonal lines with AA
    cx, cy = rect.center
    length = 8
    thick = 2
    col = (255,255,255) if hovered else (235,235,235)
    # draw X with lines
    pygame.draw.line(screen, col, (cx-length, cy-length), (cx+length, cy+length), thick)
    pygame.draw.line(screen, col, (cx+length, cy-length), (cx-length, cy+length), thick)
    # AA refinement via aalines
    try:
        pygame.draw.aaline(screen, col, (cx-length, cy-length), (cx+length, cy+length))
        pygame.draw.aaline(screen, col, (cx+length, cy-length), (cx-length, cy+length))
    except Exception:
        pass

def draw_polished_back_button(screen, rect, hovered):
    bg = (75, 75, 85) if hovered else (60, 60, 70)
    if hovered:
        glow = pygame.Surface((rect.width+10, rect.height+10), pygame.SRCALPHA)
        pygame.draw.rect(glow, (120,140,255,35), glow.get_rect(), border_radius=12)
        screen.blit(glow, (rect.x-5, rect.y-5))
    pygame.draw.rect(screen, bg, rect, border_radius=14)
    pygame.draw.rect(screen, (140,140,180) if hovered else MENU_BORDER_COLOR, rect, 1, border_radius=14)
    # Arrow: left chevron
    cx, cy = rect.center
    col = (220,220,255) if hovered else TEXT_COLOR
    # draw chevron with two lines
    pygame.draw.line(screen, col, (cx+5, cy-6), (cx-4, cy), 2)
    pygame.draw.line(screen, col, (cx-4, cy), (cx+5, cy+6), 2)
    try:
        pygame.draw.aaline(screen, col, (cx+5, cy-6), (cx-4, cy))
        pygame.draw.aaline(screen, col, (cx-4, cy), (cx+5, cy+6))
    except Exception:
        pass

def draw_flip_button(screen, mouse_pos):
    if menu_anim_progress != 0:
        return
    hovered = flip_button_rect.collidepoint(mouse_pos)
    bg = BUTTON_HOVER_COLOR if hovered else BUTTON_COLOR
    pygame.draw.rect(screen, bg, flip_button_rect, border_radius=6)
    pygame.draw.rect(screen, MENU_BORDER_COLOR, flip_button_rect, 2, border_radius=6)
    # Flip icon: two opposing arrows
    cx, cy = flip_button_rect.center
    col = GEAR_COLOR_HOVER if hovered else GEAR_COLOR
    # simple flip icon: vertical line with arrows
    pygame.draw.line(screen, col, (cx, cy-7), (cx, cy+7), 2)
    pygame.draw.polygon(screen, col, [(cx-5, cy-4), (cx, cy-9), (cx+1, cy-5)])
    pygame.draw.polygon(screen, col, [(cx+5, cy+4), (cx, cy+9), (cx-1, cy+5)])
    # label tiny
    txt = font_tiny.render("Flip", True, col)
    # keep compact: only icon, tooltip on hover
    if hovered:
        tip = font_tiny.render("Flip board", True, TEXT_COLOR)
        tip_bg = pygame.Surface((tip.get_width()+10, tip.get_height()+6), pygame.SRCALPHA)
        tip_bg.fill((30,30,30,220))
        pygame.draw.rect(tip_bg, MENU_BORDER_COLOR, tip_bg.get_rect(),1, border_radius=4)
        screen.blit(tip_bg, (flip_button_rect.x -2, flip_button_rect.y - 22))
        screen.blit(tip, (flip_button_rect.x +5 -2, flip_button_rect.y - 19))

def get_flip_button_rect():
    return flip_button_rect


def draw_settings_menu(screen):
    global menu_anim_progress, settings_menu_state, menu_transition_progress
    global menu_transition_direction

    if menu_anim_progress <= 0.01:
        return

    update_menu_transition()

    mx, my = pygame.mouse.get_pos()
    transition_idle = menu_transition_direction == "none" and menu_anim_progress >= 0.99

    # --- Unified eased open/close: single backdrop + slide for all content ---
    eased = cubic_ease_in_out(max(0.0, min(1.0, menu_anim_progress)))
    # Backdrop — single source of truth (removed duplicate from draw_settings_button)
    backdrop_alpha = int(eased * 180)
    overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    overlay.fill((0, 0, 0, backdrop_alpha))
    screen.blit(overlay, (0, 0))
    # Content entrance: slide up + subtle scale via eased offset
    open_slide = int((1 - eased) * 22)  # 22px slide fully resolves at eased=1

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
        "board_theme": "Pieces Style",
        "engine_settings": "Engine Settings",
    }
    title_text = font_large.render(
        page_titles.get(settings_menu_state, "Settings"), True, TEXT_COLOR
    )
    title_rect = title_text.get_rect(center=(WIDTH // 2 + offset_x, 50 + open_slide))
    screen.blit(title_text, title_rect)

    close_button_rect = pygame.Rect(WIDTH - 60, 20 + open_slide, 44, 32)
    close_hovered = transition_idle and close_button_rect.collidepoint(mx, my)
    draw_polished_close_button(screen, close_button_rect, close_hovered)

    if settings_menu_state != "main":
        back_button_rect = pygame.Rect(20, 20 + open_slide, 44, 32)
        back_hovered = transition_idle and back_button_rect.collidepoint(mx, my)
        draw_polished_back_button(screen, back_button_rect, back_hovered)

    if settings_menu_state == "main":
        menu_items = [
            "Engine Settings",
            "Pieces Style",
            "Sound Settings",
            "Game Controls",
            "Analysis Settings",
        ]

        item_height = 50
        total_menu_height = len(menu_items) * item_height + (len(menu_items) - 1) * 20
        start_y = (HEIGHT - total_menu_height) // 2

        for i, item in enumerate(menu_items):
            # Staggered entrance: each item slides in slightly after the previous
            item_delay = i * 0.06
            item_eased = max(0.0, min(1.0, (eased - item_delay) / 0.82)) if eased > item_delay else 0.0
            item_slide = int((1 - (cubic_ease_in_out(item_eased) if item_eased > 0 else 0)) * 14) if eased < 0.99 else 0
            # Combine global open_slide with per-item stagger for polished cascade
            y_slide = open_slide + item_slide if eased < 1.0 else 0
            item_rect = pygame.Rect(
                WIDTH // 2 - 200 + offset_x,
                start_y + i * (item_height + 20) + y_slide,
                400,
                item_height,
            )

            is_interactive = item in ("Pieces Style", "Engine Settings")
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

        about_y = start_y + len(menu_items) * (item_height + 20) + 40 + open_slide

        about_title = font_large.render("About", True, TEXT_COLOR)
        about_title_rect = about_title.get_rect(
            center=(WIDTH // 2 + offset_x, about_y)
        )
        screen.blit(about_title, about_title_rect)

        version_text = font_small.render(
            "ChessBotV Rewrite Version 1.5-beta2", True, TEXT_COLOR
        )
        version_rect = version_text.get_rect(
            center=(WIDTH // 2 + offset_x, about_y + 40)
        )
        screen.blit(version_text, version_rect)

        # Github button
        github_rect = get_github_button_rect(offset_x, about_y)
        github_hovered = transition_idle and github_rect.collidepoint(mx, my)
        pygame.draw.rect(screen, (45, 85, 155) if github_hovered else (55, 75, 145), github_rect, border_radius=6)
        pygame.draw.rect(screen, MENU_BORDER_COLOR, github_rect, 2, border_radius=6)
        github_text = font_small.render("Github", True, TEXT_COLOR)
        screen.blit(github_text, github_text.get_rect(center=github_rect.center))

        # Restart button
        restart_rect = get_restart_button_rect(offset_x, about_y)
        restart_hovered = transition_idle and restart_rect.collidepoint(mx, my)
        pygame.draw.rect(screen, (140, 50, 50) if restart_hovered else (100, 50, 50), restart_rect, border_radius=6)
        pygame.draw.rect(screen, MENU_BORDER_COLOR, restart_rect, 2, border_radius=6)
        restart_text = font_small.render("Restart", True, TEXT_COLOR)
        screen.blit(restart_text, restart_text.get_rect(center=restart_rect.center))

    elif settings_menu_state == "board_theme":
        draw_board_theme_content(screen, offset_x, open_slide)

    elif settings_menu_state == "engine_settings":
        draw_engine_settings_content(screen, offset_x, open_slide)


def handle_menu_click(mx, my):
    global menu_anim_closing, settings_menu_state, menu_transition_direction
    global current_theme, images, theme_scroll_offset, settings_menu_target
    global engine_slider_dragging, restart_requested

    if menu_anim_progress < 1.0 or menu_transition_direction != "none":
        return True

    close_button_rect = pygame.Rect(WIDTH - 60, 20, 44, 32)
    if close_button_rect.collidepoint(mx, my):
        menu_anim_closing = True
        return True

    if settings_menu_state != "main":
        back_button_rect = pygame.Rect(20, 20, 44, 32)
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
        cols = 2
        for i, (theme_id, _display) in enumerate(themes):
            row = i // cols
            col = i % cols
            x = WIDTH // 2 - 200 + col * (THEME_ITEM_WIDTH + THEME_COL_GAP)
            y = top + row * (item_height + gap) - int(theme_scroll_offset)
            theme_rect = pygame.Rect(x, y, THEME_ITEM_WIDTH, item_height)
            if (
                viewport.collidepoint(mx, my)
                and theme_rect.collidepoint(mx, my)
            ):
                if theme_id != current_theme:
                    current_theme = theme_id
                    images = load_images(current_theme)
                    save_theme_config(current_theme)
                return True

    elif settings_menu_state == "engine_settings":
        for i, key in enumerate(ENGINE_ROW_KEYS):
            _row_rect, track_rect, _value_rect = get_engine_slider_rects(i)
            hit_rect = track_rect.inflate(20, 24)
            if hit_rect.collidepoint(mx, my):
                engine_slider_dragging = key
                set_engine_slider_value(key, mx, track_rect)
                return True

        if get_engine_reset_rect().collidepoint(mx, my):
            reset_engine_settings()
            return True
        if get_engine_save_rect().collidepoint(mx, my):
            if engine_has_changes:
                apply_engine_settings()
            return True

    elif settings_menu_state == "main":
        menu_items = [
            "Engine Settings",
            "Pieces Style",
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
                if item == "Pieces Style":
                    theme_scroll_offset = 0.0
                    settings_menu_target = "board_theme"
                    menu_transition_direction = "left_out"
                elif item == "Engine Settings":
                    settings_menu_target = "engine_settings"
                    menu_transition_direction = "left_out"
                return True

        # About section buttons — compute same about_y as draw
        about_y = start_y + len(menu_items) * (item_height + 20) + 40
        if get_github_button_rect(0, about_y).collidepoint(mx, my):
            try:
                webbrowser.open("https://github.com/Baked-Cake1/ChessBotV-Rewrite")
            except Exception:
                pass
            return True
        if get_restart_button_rect(0, about_y).collidepoint(mx, my):
            restart_requested = True
            menu_anim_closing = True
            return True

    return True


def draw_confirm_dialog(screen):
    if not show_newgame_confirm:
        return None, None
    # backdrop
    e = cubic_ease_in_out(confirm_anim)
    overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    overlay.fill((0,0,0, int(160*e)))
    screen.blit(overlay, (0,0))
    # dialog box
    w, h = 360, 160
    x = WIDTH//2 - w//2
    y = HEIGHT//2 - h//2 - int((1-e)*20)
    bg = pygame.Surface((w, h), pygame.SRCALPHA)
    bg.fill((55,55,65, int(255*e)))
    pygame.draw.rect(bg, (80,80,90), bg.get_rect(), 2, border_radius=12)
    # we need to blit bg with proper alpha
    screen.blit(bg, (x, y))
    # title
    title = font_large.render("New Game?", True, TEXT_COLOR)
    screen.blit(title, (x + w//2 - title.get_width()//2, y+18))
    hint = font_small.render("You have moves in progress. Continue?", True, TEXT_MUTED)
    screen.blit(hint, (x + w//2 - hint.get_width()//2, y+52))
    # buttons
    yes_rect = pygame.Rect(x+30, y+95, 140, 40)
    no_rect = pygame.Rect(x+w-170, y+95, 140, 40)
    mx, my = pygame.mouse.get_pos()
    yes_hover = yes_rect.collidepoint(mx, my)
    no_hover = no_rect.collidepoint(mx, my)
    pygame.draw.rect(screen, (70, 150, 90) if yes_hover else (55, 125, 75), yes_rect, border_radius=8)
    pygame.draw.rect(screen, (90, 190, 120) if yes_hover else MENU_BORDER_COLOR, yes_rect, 2, border_radius=8)
    pygame.draw.rect(screen, (95,95,105) if no_hover else (70,70,80), no_rect, border_radius=8)
    pygame.draw.rect(screen, MENU_BORDER_COLOR, no_rect, 2, border_radius=8)
    yes_t = font_small.render("Yes, reset", True, TEXT_COLOR)
    no_t = font_small.render("Cancel", True, TEXT_COLOR)
    screen.blit(yes_t, yes_t.get_rect(center=yes_rect.center))
    screen.blit(no_t, no_t.get_rect(center=no_rect.center))
    return yes_rect, no_rect

def draw_gui(
    screen, board, engine_thread, anim_mgr, check_effect, selected_sq, mouse_pos,
    drag_sq=None, drag_offset=None, dragging=False
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

    # 1. Evaluation bar — dynamic position with winning highlight and smooth slide
    eval_bar_x = int(eval_bar_x_current)
    eval_bar_y = BOARD_OY
    eval_bar_h = BOARD_WIDTH

    with engine_thread.lock:
        score = engine_thread.info["score"]
        mate = engine_thread.info["mate"]
        depth = engine_thread.info["depth"]
        nps = engine_thread.info["nps"]
        pv = engine_thread.info["pv"]

    if depth >= ANALYSIS_DEPTH:
        nps = 0

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
    # click hint when hovered
    if get_eval_bar_rect().collidepoint(mouse_pos):
        hint = font_tiny.render("Click to swap side", True, TEXT_MUTED)
        # show above bar
        tx = eval_bar_x + EVAL_BAR_WIDTH//2 - hint.get_width()//2
        ty = eval_bar_y - 18
        if ty > 2:
            screen.blit(hint, (tx, ty))

    # 2. Board — supports flip and smooth evaluation-side slide
    board_ox = int(get_board_ox())
    for r in range(8):
        for f in range(8):
            color = LIGHT_SQ if (r + f) % 2 == 0 else DARK_SQ
            sq_rect = pygame.Rect(
                board_ox + f * SQUARE_SIZE,
                BOARD_OY + r * SQUARE_SIZE,
                SQUARE_SIZE,
                SQUARE_SIZE,
            )
            pygame.draw.rect(screen, color, sq_rect)
            # AA border for squares (subtle)
            try:
                pygame.gfxdraw.rectangle(screen, sq_rect, color)
            except Exception:
                pass
            # map visual to logical square considering flip
            if board_flipped:
                vis_file = 7 - f
                vis_rank = r
            else:
                vis_file = f
                vis_rank = 7 - r
            sq = chess.square(vis_file, vis_rank)
            if sq == selected_sq:
                s = pygame.Surface((SQUARE_SIZE, SQUARE_SIZE), pygame.SRCALPHA)
                s.fill(HIGHLIGHT_COLOR)
                screen.blit(s, sq_rect.topleft)

    # Board border with AA
    pygame.draw.rect(
        screen,
        BOARD_BORDER_COLOR,
        (board_ox - 2, BOARD_OY - 2, BOARD_WIDTH + 4, BOARD_WIDTH + 4),
        2, border_radius=2
    )
    try:
        pygame.gfxdraw.rectangle(screen, pygame.Rect(board_ox-2, BOARD_OY-2, BOARD_WIDTH+4, BOARD_WIDTH+4), BOARD_BORDER_COLOR)
    except Exception:
        pass

    regular_moves, capture_moves = get_legal_squares(board, selected_sq)

    # Animated legal move green circles — pulsating scale + alpha
    pulse_scale = 1.0 + 0.12 * math.sin(legal_pulse_t)
    pulse_alpha = 90 + int(18 * math.sin(legal_pulse_t))

    for sq in regular_moves:
        cx, cy = square_center(sq)
        # AA circle with pulse
        radius = int(15 * pulse_scale)
        col = (0, 255, 0, pulse_alpha)
        # use AA via gfxdraw
        try:
            pygame.gfxdraw.aacircle(screen, int(cx), int(cy), radius, (0,255,0))
            pygame.gfxdraw.filled_circle(screen, int(cx), int(cy), radius, col)
        except Exception:
            pygame.draw.circle(screen, LEGAL_MOVE_COLOR, (int(cx), int(cy)), radius)

    for sq in capture_moves:
        cx, cy = square_center(sq)
        radius = int(15 * pulse_scale)
        cap_col = (255, 0, 0, 110)
        try:
            pygame.gfxdraw.aacircle(screen, int(cx), int(cy), radius, (255,0,0))
            pygame.gfxdraw.filled_circle(screen, int(cx), int(cy), radius, cap_col)
            pygame.gfxdraw.aacircle(screen, int(cx), int(cy), radius, (255,255,255,200))
        except Exception:
            pygame.draw.circle(screen, CAPTURE_MOVE_COLOR, (int(cx), int(cy)), radius)
        pygame.draw.circle(screen, (255, 255, 255, 200), (int(cx), int(cy)), radius, 2)

    # Engine arrows — MultiPV support: multiple arrows with numbers (#1 best)
    # Gather lines dict (multipv -> {pv, score, mate})
    lines = {}
    with engine_thread.lock:
        # copy lines to avoid holding lock during board checks
        _lines_copy = dict(engine_thread.info.get("lines", {}))
        _pv_copy = list(engine_thread.info.get("pv", []))
        _mate_copy = engine_thread.info.get("mate")
        _score_copy = engine_thread.info.get("score", 0)
    # Fallback if engine hasn't yet populated lines (older parsing or no info yet)
    if not _lines_copy and _pv_copy:
        _lines_copy = {1: {"pv": _pv_copy, "mate": _mate_copy, "score": _score_copy}}
    # Build targets for animation manager
    multipv_targets = {}
    multipv_visible = False
    if not game_over and _lines_copy:
        for num in sorted(_lines_copy.keys()):
            ldata = _lines_copy[num]
            pv_moves = ldata.get("pv", [])
            if not pv_moves:
                continue
            mv = pv_moves[0]
            # Only display if move is legal in current position (engine may have stale line)
            if mv not in board.legal_moves:
                continue
            start_x, start_y = square_center(mv.from_square)
            end_x, end_y = square_center(mv.to_square)
            is_knight = False
            piece = board.piece_at(mv.from_square)
            if piece and piece.piece_type == chess.KNIGHT:
                is_knight = True
            is_mate_line = ldata.get("mate") is not None
            multipv_targets[num] = (start_x, start_y, end_x, end_y, is_knight, is_mate_line, True)
            multipv_visible = True
        # If we have at least one valid line, use multi-arrow system
        if multipv_targets:
            # translate to format expected by set_multipv_arrows: id -> (start,end,is_knight,is_mate,visible)
            fmt_targets = {}
            for aid, (sx, sy, ex, ey, ik, im, vis) in multipv_targets.items():
                fmt_targets[aid] = ((sx, sy), (ex, ey), ik, im, vis)
            anim_mgr.set_multipv_arrows(fmt_targets)
            # also keep single-arrow for compat (fade out)
            anim_mgr.set_arrow_target(None, None, False)
        else:
            anim_mgr.set_multipv_arrows({})
            anim_mgr.set_arrow_target(None, None, False)
    else:
        anim_mgr.set_multipv_arrows({})
        anim_mgr.set_arrow_target(None, None, False)
    # Legacy single-arrow fallback not needed when multipv active
    # Check/Checkmate glow, UNDER the pieces
    draw_check_effect_under(screen, check_effect, board_ox, BOARD_OY)

    # 3. Pieces — flip-aware
    moving_sq = anim_mgr.move_anim["target_sq"] if anim_mgr.move_anim else -1
    is_castling = anim_mgr.move_anim["is_castling"] if anim_mgr.move_anim else False
    rook_target_sq = (
        anim_mgr.move_anim["rook_target_sq"] if anim_mgr.move_anim else None
    )
    rook_start_sq = anim_mgr.rook_anim["target_sq"] if anim_mgr.rook_anim else None

    reset_targets = {a["target_sq"] for a in anim_mgr.board_reset_anims} if anim_mgr.board_reset_anims else set()

    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if not piece:
            continue
        if sq == moving_sq:
            continue
        if (
            is_castling
            and (sq == rook_target_sq or sq == rook_start_sq)
            and piece
            and piece.piece_type == chess.ROOK
        ):
            continue
        if dragging and sq == drag_sq:
            continue
        if sq in reset_targets:
            continue
        pkey = get_piece_key(piece)
        x, y = square_to_screen(sq)
        if pkey in images:
            screen.blit(images[pkey], (x, y))

    # 3b. Drag highlight for drop square
    if dragging and drag_sq is not None:
        drop_sq = get_square_from_pos(mouse_pos)
        if drop_sq is not None and drop_sq != drag_sq:
            tmp_move = build_move_with_promotion(board, drag_sq, drop_sq)
            if tmp_move in board.legal_moves:
                x, y = square_to_screen(drop_sq)
                hl_rect = pygame.Rect(x, y, SQUARE_SIZE, SQUARE_SIZE)
                s = pygame.Surface((SQUARE_SIZE, SQUARE_SIZE), pygame.SRCALPHA)
                s.fill((255, 255, 100, 80))
                screen.blit(s, hl_rect.topleft)

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

    # Board reset animations — multiple pieces sliding to start squares
    for anim in anim_mgr.board_reset_anims:
        pkey = get_piece_key(anim["piece"])
        if pkey in images:
            t = cubic_ease_in_out(anim["progress"])
            sx, sy = anim["start"]
            ex, ey = anim["end"]
            curr_x = sx + (ex - sx) * t
            curr_y = sy + (ey - sy) * t
            screen.blit(images[pkey], (curr_x, curr_y))

    # Suggestion arrows — MultiPV: render all with numbers #1 best. Falls back to single arrow.
    def _multipv_colors(num, is_mate):
        if is_mate:
            return ARROW_MATE_START, ARROW_MATE_END
        palette = {
            1: (ARROW_GRAD_START, ARROW_GRAD_END),  # blue best
            2: ((80, 200, 200), (0, 120, 120)),  # teal
            3: ((180, 120, 255), (80, 0, 180)),  # purple
            4: ((255, 180, 80), (210, 80, 0)),  # orange
            5: ((120, 220, 120), (0, 120, 0)),  # green
        }
        return palette.get(num, palette[1])

    # Prefer MultiPV arrows if any are active
    has_multi = any(v.get("alpha", 0) > 1 and v.get("start") is not None for v in getattr(anim_mgr, "multi_arrows", {}).values())
    if has_multi:
        # Draw in reverse order so #1 is on top
        for aid in sorted(anim_mgr.multi_arrows.keys(), reverse=True):
            adata = anim_mgr.multi_arrows[aid]
            if adata["alpha"] <= 1 or adata["start"] is None or adata["end"] is None:
                continue
            c_start, c_end = _multipv_colors(aid, adata.get("is_mate", False))
            # temporarily override globals for gradient
            # we pass colors via custom draw that uses gradient override — we monkey-patch globals briefly
            # instead, we call draw with is_mate flag and custom palette handling inside draw_arrow?
            # For simplicity, set globals to desired palette for this draw call
            orig_start, orig_end = ARROW_GRAD_START, ARROW_GRAD_END
            orig_mate_start, orig_mate_end = ARROW_MATE_START, ARROW_MATE_END
            try:
                # if this arrow is mate, it will use mate colors; otherwise use normal
                # we override normal palette to multipv color
                globals()["ARROW_GRAD_START"], globals()["ARROW_GRAD_END"] = c_start, c_end
                # keep mate colors as is (or also override if is_mate)
                if adata.get("is_knight"):
                    draw_knight_arrow(screen, adata["start"], adata["end"], ENGINE_ARROW_COLOR, 7, adata["alpha"], is_mate=adata.get("is_mate", False))
                else:
                    draw_arrow(screen, adata["start"], adata["end"], ENGINE_ARROW_COLOR, 7, adata["alpha"], is_mate=adata.get("is_mate", False))
            finally:
                globals()["ARROW_GRAD_START"], globals()["ARROW_GRAD_END"] = orig_start, orig_end
                globals()["ARROW_MATE_START"], globals()["ARROW_MATE_END"] = orig_mate_start, orig_mate_end
            # Number badge at arrow origin (slightly offset)
            bx, by = int(adata["start"].x), int(adata["start"].y)
            # badge background
            badge_r = 11
            badge_col = (30, 30, 35, 230) if aid != 1 else (25, 60, 160, 240)
            # outer glow for best
            if aid == 1:
                glow = pygame.Surface((badge_r*2+6, badge_r*2+6), pygame.SRCALPHA)
                pygame.draw.circle(glow, (80, 140, 255, 80), (badge_r+3, badge_r+3), badge_r+3)
                screen.blit(glow, (bx - badge_r -3, by - badge_r -3))
            pygame.draw.circle(screen, (20,20,25), (bx, by), badge_r+1)
            pygame.draw.circle(screen, badge_col[:3], (bx, by), badge_r)
            pygame.draw.circle(screen, (220,220,240), (bx, by), badge_r, 1)
            try:
                pygame.gfxdraw.aacircle(screen, bx, by, badge_r, (220,220,240))
            except Exception:
                pass
            num_txt = font_small.render(f"#{aid}", True, (255,255,255))
            # use tiny font for badge to fit
            if num_txt.get_width() > badge_r*2:
                num_txt = font_tiny.render(f"#{aid}", True, (255,255,255))
            screen.blit(num_txt, num_txt.get_rect(center=(bx, by)))
    else:
        # Fallback single arrow (when MultiPV=1 or engine hasn't sent multipv yet)
        if (
            getattr(anim_mgr, "arrow_alpha", 0) > 1
            and getattr(anim_mgr, "arrow_start", None) is not None
            and getattr(anim_mgr, "arrow_end", None) is not None
        ):
            # need to know if it's knight/mate for single arrow; we approximate via checking if any multi arrows not present
            # For single arrow mode, we use stored mate status from best line
            single_is_mate = mate is not None
            # also check knight status: we can store last arrow knight flag in manager (fallback false)
            single_is_knight = False
            # Try to detect knight from current positions: if arrow targets were set for knight, we would have stored
            # But single arrow path doesn't have that info, so we just check move piece type if we have pv
            try:
                if pv and pv[0] in board.legal_moves:
                    p = board.piece_at(pv[0].from_square)
                    if p and p.piece_type == chess.KNIGHT:
                        single_is_knight = True
            except Exception:
                pass
            if single_is_knight:
                draw_knight_arrow(screen, anim_mgr.arrow_start, anim_mgr.arrow_end, ENGINE_ARROW_COLOR, 7, anim_mgr.arrow_alpha, is_mate=single_is_mate)
            else:
                draw_arrow(screen, anim_mgr.arrow_start, anim_mgr.arrow_end, ENGINE_ARROW_COLOR, 7, anim_mgr.arrow_alpha, is_mate=single_is_mate)

    # Check/Checkmate brackets, rings, and mate border, OVER the pieces
    draw_check_effect_over(screen, check_effect, board_ox, BOARD_OY)

    # 5b. Dragged piece — follows cursor on top of everything
    if dragging and drag_sq is not None and drag_offset is not None:
        piece = board.piece_at(drag_sq)
        if piece:
            pkey = get_piece_key(piece)
            if pkey in images:
                mx, my = mouse_pos
                ox, oy = drag_offset
                draw_x = mx - ox
                draw_y = my - oy
                screen.blit(images[pkey], (draw_x, draw_y))

    # Promotion choice overlay (must be over board but under menu)
    if promotion_pending is not None:
        draw_promotion_menu(screen, promotion_pending["color"])

    # 6. Info panel — uses dynamic board ox for slide animation
    info_top = BOARD_OY + BOARD_WIDTH

    if game_over and status_text:
        status_surf = font_large.render(status_text, True, status_color)
        sx = board_ox + (BOARD_WIDTH - status_surf.get_width()) // 2
        screen.blit(status_surf, (sx, info_top + 8))
    else:
        opening_name = update_opening_name(board)
        if opening_name:
            opening_text = font_large.render(opening_name, True, TEXT_COLOR)
            screen.blit(opening_text, (board_ox, info_top + 8))

        if status_text:
            status_surf = font_large.render(status_text, True, status_color)
            screen.blit(
                status_surf,
                (
                    board_ox + BOARD_WIDTH - status_surf.get_width(),
                    info_top + 8,
                ),
            )

    divider_y = info_top + 46
    pygame.draw.line(
        screen,
        DIVIDER_COLOR,
        (board_ox, divider_y),
        (board_ox + BOARD_WIDTH, divider_y),
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
    col1_x = board_ox
    col2_x = board_ox + 180
    col3_x = board_ox + 340

    screen.blit(stats_label, (col1_x, stats_y))
    screen.blit(stats_value, (col1_x + stats_label.get_width() + 8, stats_y))
    screen.blit(depth_label, (col2_x, stats_y))
    screen.blit(depth_value, (col2_x + depth_label.get_width() + 8, stats_y))
    screen.blit(nps_label, (col3_x, stats_y))
    screen.blit(nps_value, (col3_x + nps_label.get_width() + 8, stats_y))

    rec_y = info_top + 88
    if game_over:
        opening_name_go = update_opening_name(board)
        if opening_name_go:
            rec_label = font_small.render("Opening", True, TEXT_MUTED)
            rec_value = font_small.render(opening_name_go, True, TEXT_COLOR)
            screen.blit(rec_label, (board_ox, rec_y))
            screen.blit(rec_value, (board_ox + rec_label.get_width() + 8, rec_y))
    else:
        best_move_str = str(pv[0]) if pv else "—"
        rec_label = font_small.render("Rec. Move", True, TEXT_MUTED)
        rec_value = font_small.render(best_move_str, True, TEXT_ACCENT)
        screen.blit(rec_label, (board_ox, rec_y))
        screen.blit(rec_value, (board_ox + rec_label.get_width() + 8, rec_y))
        # MultiPV list: show #2..#N below rec move when MultiPV>1
        try:
            multipv_val = config.get("multipv", 1)
            if multipv_val > 1 and not game_over:
                with engine_thread.lock:
                    lines_copy = dict(engine_thread.info.get("lines", {}))
                # only show if we have more than 1 line
                if len(lines_copy) > 1:
                    # Colors matching arrows
                    mp_colors = {
                        2: (80, 200, 200),
                        3: (180, 120, 255),
                        4: (255, 180, 80),
                        5: (120, 220, 120),
                    }
                    y_off = rec_y + 22
                    for idx in range(2, min(multipv_val, 5) + 1):
                        ldata = lines_copy.get(idx)
                        if not ldata or not ldata.get("pv"):
                            continue
                        mv_str = str(ldata["pv"][0])
                        # eval string
                        if ldata.get("mate") is not None:
                            ev_str = f"M{ldata['mate']}"
                        elif ldata.get("score") is not None:
                            ev_str = f"{ldata['score']/100.0:+.2f}"
                        else:
                            ev_str = ""
                        # badge #N
                        badge_txt = font_tiny.render(f"#{idx}", True, mp_colors.get(idx, TEXT_MUTED))
                        move_txt = font_tiny.render(mv_str, True, TEXT_COLOR)
                        eval_txt = font_tiny.render(ev_str, True, TEXT_MUTED)
                        bx = board_ox + (idx-2)*130
                        # prevent overflow
                        if bx + 120 > board_ox + BOARD_WIDTH:
                            break
                        screen.blit(badge_txt, (bx, y_off))
                        screen.blit(move_txt, (bx + badge_txt.get_width() + 6, y_off))
                        screen.blit(eval_txt, (bx + badge_txt.get_width() + 6 + move_txt.get_width() + 6, y_off))
        except Exception:
            pass

    # Buttons (top bar) + flip + confirmation + menu overlay
    draw_new_game_button(screen, mouse_pos)
    draw_settings_button(screen, mouse_pos)
    draw_flip_button(screen, mouse_pos)

    # New game confirmation dialog (over buttons, under menu)
    if show_newgame_confirm:
        draw_confirm_dialog(screen)

    if menu_anim_progress > 0.01:
        draw_settings_menu(screen)

    pygame.display.flip()


def main():
    global menu_anim_progress, menu_anim_opening, menu_anim_closing
    global menu_transition_direction, engine_thread, promotion_pending, restart_requested, engine_slider_dragging
    global show_newgame_confirm, confirm_anim, board_flipped, board_ox_current, eval_bar_x_current, board_ox_target, eval_bar_x_target, board_anim_t, eval_bar_side
    global engine_draft, engine_slider_display, engine_has_changes

    clock = pygame.time.Clock()
    board = chess.Board()
    anim_mgr = AnimationManager()
    check_effect = CheckEffect()
    engine_thread = EngineThread(ENGINE_PATH, config)

    selected_sq = None
    dragging = False
    drag_sq = None
    drag_offset = None
    running = True

    def do_new_game():
        old_board = board.copy()
        new_board = chess.Board()
        anim_mgr.add_board_reset(old_board, new_board)
        board.reset()
        reset_opening()
        return False, None, None, None  # will reset selected/drag in caller

    while running:
        anim_mgr.update()
        update_board_layout_anim()
        update_slider_display()
        update_global_anims()

        is_check = board.is_check()
        is_mate = board.is_checkmate()
        king_sq = (
            board.king(board.turn) if (is_check or is_mate) else None
        )
        check_effect.update(is_check, is_mate, king_sq)

        update_menu_animation()
        if restart_requested:
            if menu_anim_progress == 0 and not menu_anim_opening and not menu_anim_closing:
                running = False
            elif menu_anim_progress > 0 and not menu_anim_closing and not menu_anim_opening:
                menu_anim_closing = True
        mouse_pos = pygame.mouse.get_pos()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                mx, my = event.pos

                # Confirmation dialog has priority over everything
                if show_newgame_confirm:
                    # compute dialog buttons positions (same as draw_confirm_dialog)
                    w, h = 360, 160
                    x = WIDTH//2 - w//2
                    y = HEIGHT//2 - h//2
                    yes_rect = pygame.Rect(x+30, y+95, 140, 40)
                    no_rect = pygame.Rect(x+w-170, y+95, 140, 40)
                    if yes_rect.collidepoint(mx, my):
                        # confirmed — reset board
                        old_board = board.copy()
                        new_board = chess.Board()
                        anim_mgr.add_board_reset(old_board, new_board)
                        board.reset()
                        reset_opening()
                        selected_sq = None
                        dragging = False
                        drag_sq = None
                        drag_offset = None
                        anim_mgr.capture_anims = []
                        anim_mgr.current_eval = 0.0
                        anim_mgr.target_eval = 0.0
                        anim_mgr.arrow_target_alpha = 0.0
                        check_effect.reset()
                        engine_thread.update_position(board)
                        promotion_pending = None
                        show_newgame_confirm = False
                    elif no_rect.collidepoint(mx, my):
                        show_newgame_confirm = False
                    else:
                        # click outside — dismiss
                        # keep dialog open? we close on outside click for convenience
                        show_newgame_confirm = False
                    continue

                menu_active = (
                    menu_anim_progress > 0
                    or menu_anim_opening
                    or menu_anim_closing
                )

                if not menu_active:
                    if promotion_pending is not None:
                        rects = get_promotion_rects()
                        handled = False
                        for idx, rect in enumerate(rects):
                            if rect.collidepoint(mx, my):
                                promo_piece = PROMOTION_CHOICES[idx]
                                try_execute_promotion(board, anim_mgr, engine_thread, promotion_pending["from_sq"], promotion_pending["to_sq"], promo_piece, animate=True)
                                selected_sq = None
                                dragging = False
                                drag_sq = None
                                drag_offset = None
                                promotion_pending = None
                                handled = True
                                break
                        if handled:
                            continue
                        outside = True
                        for rect in rects:
                            if rect.collidepoint(mx, my):
                                outside = False
                                break
                        continue

                    # Flip button
                    if flip_button_rect.collidepoint(mx, my):
                        toggle_flip_board()
                        continue
                    # Eval bar click — swap side with smooth animation
                    if get_eval_bar_rect().collidepoint(mx, my):
                        toggle_eval_side()
                        continue

                    if new_game_button_rect.collidepoint(mx, my):
                        if len(board.move_stack) > 0:
                            show_newgame_confirm = True
                            confirm_anim = 0.0
                        else:
                            old_board = board.copy()
                            new_board = chess.Board()
                            anim_mgr.add_board_reset(old_board, new_board)
                            board.reset()
                            reset_opening()
                            selected_sq = None
                            dragging = False
                            drag_sq = None
                            drag_offset = None
                            anim_mgr.capture_anims = []
                            anim_mgr.current_eval = 0.0
                            anim_mgr.target_eval = 0.0
                            anim_mgr.arrow_target_alpha = 0.0
                            check_effect.reset()
                            engine_thread.update_position(board)
                            promotion_pending = None
                        continue

                    elif settings_button_rect.collidepoint(mx, my):
                        menu_anim_opening = True

                    elif not (anim_mgr.move_anim or anim_mgr.rook_anim or anim_mgr.board_reset_anims):
                        if not board.is_game_over(claim_draw=True):
                            sq = get_square_from_pos(event.pos)
                            if sq is not None:
                                piece = board.piece_at(sq)
                                if piece and piece.color == board.turn:
                                    selected_sq = sq
                                    dragging = True
                                    drag_sq = sq
                                    sx, sy = square_to_screen(sq)
                                    drag_offset = (mx - sx, my - sy)
                                else:
                                    if selected_sq is not None:
                                        if is_promotion_move(board, selected_sq, sq):
                                            test_move = chess.Move(selected_sq, sq, promotion=chess.QUEEN)
                                            if test_move in board.legal_moves:
                                                promotion_pending = {"from_sq": selected_sq, "to_sq": sq, "color": board.turn}
                                                dragging = False
                                                drag_sq = None
                                                drag_offset = None
                                                continue
                                        if try_execute_move(board, anim_mgr, engine_thread, selected_sq, sq, animate=True):
                                            selected_sq = None
                                        else:
                                            if piece and piece.color == board.turn:
                                                selected_sq = sq
                                            else:
                                                selected_sq = None
                                        dragging = False
                                        drag_sq = None
                                        drag_offset = None
                else:
                    handle_menu_click(mx, my)

            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                if engine_slider_dragging is not None:
                    engine_slider_dragging = None
                mx, my = event.pos
                menu_active = (
                    menu_anim_progress > 0
                    or menu_anim_opening
                    or menu_anim_closing
                )
                if dragging and not menu_active and not (anim_mgr.move_anim or anim_mgr.rook_anim or anim_mgr.board_reset_anims):
                    drop_sq = get_square_from_pos(event.pos)
                    if drop_sq is not None and drag_sq is not None and drop_sq != drag_sq:
                        if is_promotion_move(board, drag_sq, drop_sq):
                            test_move = chess.Move(drag_sq, drop_sq, promotion=chess.QUEEN)
                            if test_move in board.legal_moves:
                                promotion_pending = {"from_sq": drag_sq, "to_sq": drop_sq, "color": board.turn}
                                dragging = False
                                drag_sq = None
                                drag_offset = None
                                continue
                        if try_execute_move(board, anim_mgr, engine_thread, drag_sq, drop_sq, animate=False):
                            selected_sq = None
                        else:
                            piece = board.piece_at(drop_sq)
                            if piece and piece.color == board.turn:
                                selected_sq = drop_sq
                    dragging = False
                    drag_sq = None
                    drag_offset = None
                elif dragging:
                    dragging = False
                    drag_sq = None
                    drag_offset = None

            elif event.type == pygame.MOUSEMOTION:
                if engine_slider_dragging is not None:
                    try:
                        idx = ENGINE_ROW_KEYS.index(engine_slider_dragging)
                        _, track_rect, _ = get_engine_slider_rects(idx)
                        set_engine_slider_value(engine_slider_dragging, event.pos[0], track_rect)
                    except ValueError:
                        engine_slider_dragging = None

            elif event.type == pygame.MOUSEWHEEL:
                if (
                    settings_menu_open
                    and settings_menu_state == "board_theme"
                    and menu_transition_direction == "none"
                ):
                    scroll_theme_list(event.y * theme_scroll_speed)

            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    if show_newgame_confirm:
                        show_newgame_confirm=False
                        continue
                    if promotion_pending is not None:
                        promotion_pending = None
                        continue
                    if menu_anim_progress > 0 and not (
                        menu_anim_opening or menu_anim_closing
                    ):
                        if menu_transition_direction != "none":
                            pass
                        elif settings_menu_state != "main":
                            menu_transition_direction = "right_out"
                        else:
                            menu_anim_closing = True
                    else:
                        selected_sq = None
                elif event.key == pygame.K_f and menu_anim_progress == 0 and not show_newgame_confirm:
                    toggle_flip_board()
                elif event.key == pygame.K_e and menu_anim_progress == 0 and not show_newgame_confirm:
                    toggle_eval_side()

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
            drag_sq=drag_sq,
            drag_offset=drag_offset,
            dragging=dragging,
        )
        clock.tick(FPS)

    engine_thread.quit()
    pygame.quit()
    if restart_requested:
        sys.exit(RESTART_EXIT_CODE)
    sys.exit(0)


if __name__ == "__main__":
    main()