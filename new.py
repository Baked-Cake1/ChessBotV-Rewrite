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

# SDL normally discards the mouse click that activates an inactive window.
# Enable click-through so that first click both focuses this (often topmost)
# window and interacts with it. This must be set before pygame initializes SDL.
os.environ["SDL_MOUSE_FOCUS_CLICKTHROUGH"] = "1"

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
# Board (square-colour) themes — separate from piece styles ("theme").
BOARD_THEMES = {
    "classic": {"display": "Classic", "light": (240, 217, 181), "dark": (181, 136, 99)},
    "chesscom": {"display": "Chess.com", "light": (235, 236, 208), "dark": (115, 149, 82)},  # #ebecd0 / #739552
}
BOARD_THEME_ORDER = ["classic", "chesscom"]
current_board_theme = "classic"
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
# Eval-bar shell/gradient palette. The bar stays neutral (black/ivory) so it
# works with every board theme, while the slim amber boundary marker remains
# the one strong accent colour.
EVAL_BAR_FRAME_COLOR = (112, 118, 132)
EVAL_BAR_FRAME_DARK = (17, 19, 24)
EVAL_BLACK_GRADIENT = ((58, 63, 74), (12, 14, 19))
EVAL_WHITE_GRADIENT = ((255, 253, 247), (196, 203, 216))
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
# NOTE: the Win32 calls below MUST be prototyped with pointer-sized HWND
# arguments. The previous version passed plain Python ints through
# ctypes.windll, which marshals them as 32-bit C ints — on 64-bit Windows
# HWND_TOPMOST ((-1) as a handle) therefore arrived as 0x00000000FFFFFFFF
# instead of 0xFFFFFFFFFFFFFFFF, SetWindowPos rejected it, and the failure
# was silently swallowed, so the window never actually became topmost.
_GWL_EXSTYLE = -20
_WS_EX_TOPMOST = 0x00000008
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020


def _clear_win_error():
    fn = getattr(ctypes, "set_last_error", None)
    if fn is not None:
        fn(0)


def _win_error():
    fn = getattr(ctypes, "get_last_error", None)
    return fn() if fn is not None else 0


def _user32():
    """user32 with correctly-typed prototypes, or None off Windows."""
    if os.name != "nt":
        return None
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_uint,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    # GetWindowLongPtrW only exists as an export on 64-bit Windows; 32-bit
    # builds expose the same thing as GetWindowLongW.
    get_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
    set_long = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
    get_long.argtypes = [wintypes.HWND, ctypes.c_int]
    get_long.restype = ctypes.c_ssize_t
    set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    set_long.restype = ctypes.c_ssize_t
    user32._get_window_long = get_long
    user32._set_window_long = set_long
    return user32


def _get_window_hwnd(user32):
    """The pygame window's HWND as a c_void_p, or None."""
    hwnd = None
    try:
        hwnd = pygame.display.get_wm_info().get("window")
    except Exception:
        hwnd = None
    if not hwnd and user32 is not None:
        # Fallback: find our top-level window by its caption.
        try:
            caption = pygame.display.get_caption()[0] or "ChessBotV Rewrite"
        except Exception:
            caption = "ChessBotV Rewrite"
        hwnd = user32.FindWindowW(None, caption)
    if not hwnd:
        return None
    return hwnd if isinstance(hwnd, ctypes.c_void_p) else ctypes.c_void_p(hwnd)


def _is_window_topmost(user32, hwnd):
    """True/False from WS_EX_TOPMOST, or None if it cannot be read."""
    try:
        _clear_win_error()
        ex_style = user32._get_window_long(hwnd, _GWL_EXSTYLE)
        if ex_style == 0 and _win_error() != 0:
            return None
        return bool(ex_style & _WS_EX_TOPMOST)
    except Exception:
        return None


def set_always_on_top(enabled=True):
    """Pin (or unpin) the window above other windows (Windows HWND_TOPMOST).

    Returns True only if the window really ended up in the requested
    state (verified via WS_EX_TOPMOST where possible)."""
    if os.name != "nt":
        return False
    try:
        user32 = _user32()
        hwnd = _get_window_hwnd(user32)
        if user32 is None or hwnd is None:
            print("Always on Top: could not get the window handle (HWND).")
            return False

        insert_after = ctypes.c_void_p(-1 if enabled else -2)  # HWND_TOPMOST / HWND_NOTOPMOST
        flags = _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE
        _clear_win_error()
        ok = bool(user32.SetWindowPos(hwnd, insert_after, 0, 0, 0, 0, flags))
        err = _win_error()
        state = _is_window_topmost(user32, hwnd)
        if ok and (state is None or state == bool(enabled)):
            return True

        # Fallback: flip WS_EX_TOPMOST directly, then re-apply the Z-order.
        ex_style = user32._get_window_long(hwnd, _GWL_EXSTYLE)
        new_style = (ex_style | _WS_EX_TOPMOST) if enabled else (ex_style & ~_WS_EX_TOPMOST)
        _clear_win_error()
        user32._set_window_long(hwnd, _GWL_EXSTYLE, new_style)
        style_err = _win_error()
        _clear_win_error()
        ok2 = bool(user32.SetWindowPos(
            hwnd, insert_after, 0, 0, 0, 0, flags | _SWP_FRAMECHANGED
        ))
        err2 = _win_error()
        state = _is_window_topmost(user32, hwnd)
        if state is not None:
            if state == bool(enabled):
                return True
        elif ok2:
            return True
        print(
            "Always on Top: failed to "
            f"{'pin' if enabled else 'unpin'} window "
            f"(SetWindowPos errors {err}/{err2}, style error {style_err}, topmost={state})."
        )
        return False
    except Exception as exc:
        print(f"Always on Top: {exc!r}")
        return False


# --- Safe engine resource limits ---
def _total_physical_mem_mb():
    """Total physical RAM in MB, or None if it cannot be determined."""
    try:
        if os.name == "nt":
            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MemoryStatusEx()
            stat.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullTotalPhys // (1024 * 1024))
    except Exception:
        pass
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024))
    except Exception:
        return None


def _safe_hash_mb(total_mb):
    """Largest power-of-two hash (MB) that stays within a safe share of RAM:
    at most half of physical memory, always leaving the OS/apps headroom,
    so a big hash cannot page the machine into a slowdown or OOM crash."""
    if not total_mb:
        return 2048
    if total_mb > 2048:
        budget = min(total_mb // 2, max(1024, total_mb - 2048))
    else:
        budget = max(256, total_mb // 2)
    step = 16
    while step * 2 <= budget:
        step *= 2
    return step


TOTAL_RAM_MB = _total_physical_mem_mb()
SAFE_HASH_MB = _safe_hash_mb(TOTAL_RAM_MB)
MAX_THREADS = max(1, os.cpu_count() or 1)


# --- Constants ---
RESTART_EXIT_CODE = 42

# --- Loaders ---
ENGINE_DEFAULTS = {"hash": 2048, "threads": 4, "skill": 20, "depth": 25, "multipv": 1, "theme": "classic", "board_theme": "classic", "always_on_top": 1, "auto_move": 0, "auto_move_color": "both", "auto_move_depth": 0}
_STRING_CONFIG_KEYS = ("theme", "board_theme", "auto_move_color")


def _snap_hash_down(value):
    step = 16
    while step * 2 <= value:
        step *= 2
    return step


def _clamp_config_limits(cfg):
    """Never let a saved config exceed this machine's safe limits."""
    try:
        cfg["threads"] = max(1, min(int(cfg.get("threads", 4)), MAX_THREADS))
    except Exception:
        cfg["threads"] = min(4, MAX_THREADS)
    try:
        h = int(cfg.get("hash", 2048))
    except Exception:
        h = 2048
    if h > SAFE_HASH_MB:
        h = _snap_hash_down(SAFE_HASH_MB)
    cfg["hash"] = max(16, h)
    color = str(cfg.get("auto_move_color", "both")).strip().lower()
    cfg["auto_move_color"] = color if color in ("white", "black", "both") else "both"
    cfg["always_on_top"] = 1 if cfg.get("always_on_top", 1) else 0
    # Auto-Move's on/off state is deliberately session-only. Even if an
    # older configuration file still contains ``auto_move=1``, startup must
    # ignore it and leave the feature off until the user enables it again.
    cfg["auto_move"] = 0
    try:
        cfg["auto_move_depth"] = max(0, min(int(cfg.get("auto_move_depth", 0)), 40))
    except Exception:
        cfg["auto_move_depth"] = 0
    return cfg


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
                        if k in cfg and k not in _STRING_CONFIG_KEYS:
                            try:
                                cfg[k] = int(v)
                            except ValueError:
                                pass
                        elif k in _STRING_CONFIG_KEYS:
                            cfg[k] = v
                        elif k == "flipped":
                            cfg[k] = v.lower() in ("1","true","yes")
                        elif k == "eval_side":
                            if v in ("left","right"):
                                cfg[k]=v
            print(f"Loaded configuration from {config_file}")
            return _clamp_config_limits(cfg)
        except FileNotFoundError:
            continue

    print("No configuration file found. Using defaults.")
    return _clamp_config_limits(cfg)


def save_config():
    """Persist preferences to configuration.cfg.

    Auto-Move's enabled state is intentionally *not* written: it is a
    per-session safety choice and always starts off on the next launch.
    Its colour/depth preferences may still be remembered.
    """
    try:
        with open("configuration.cfg", "w") as f:
            f.write(f"hash={config['hash']}\n")
            f.write(f"threads={config['threads']}\n")
            f.write(f"skill={config['skill']}\n")
            f.write(f"depth={config.get('depth', 25)}\n")
            f.write(f"multipv={config.get('multipv', 1)}\n")
            f.write(f"theme={config.get('theme', current_theme)}\n")
            f.write(f"board_theme={config.get('board_theme', current_board_theme)}\n")
            f.write(f"always_on_top={1 if config.get('always_on_top', 1) else 0}\n")
            f.write(f"auto_move_color={config.get('auto_move_color', 'both')}\n")
            f.write(f"auto_move_depth={int(config.get('auto_move_depth', 0))}\n")
    except OSError as e:
        print(f"Failed to save configuration: {e}")


def save_theme_config(theme_id):
    """Persist only theme change (keeps engine settings)."""
    config["theme"]=theme_id
    save_config()


# Runtime mirrors of the persisted toggles (initialised after config load).
always_on_top_enabled = True
auto_move_enabled = False
auto_move_color = "both"  # "white" | "black" | "both"
auto_move_depth = 0  # 0 means "use the configured Depth Limit"
auto_move_arm_reset = False  # main() consumes this to re-arm Auto-Move
auto_move_status_text = ""  # human-readable arming state for the Analysis page
show_auto_color_dialog = False
auto_color_dialog_anim = 0.0

# --- Board Detection (Analysis page) ---
show_board_detection_dialog = False
board_detection_dialog_anim = 0.0
board_detection_windows = []
board_detection_selected = 0
board_detection_page = 0
board_detection_running = False
board_detection_status = ""
board_detection_error = ""
board_detection_result = None
board_detection_result_ready = False
board_detection_last_summary = ""
_board_detection_thread = None
_win32_capture_libs_cache = None
board_detection_picker_purpose = "detect"  # "detect" | "autoplay"

# --- Auto-play (Analysis page) ---
# One mutable session dict keeps the external-window sync state together:
# the chosen window/side, the located board rectangle, the last accepted
# board-only screenshot/grid, and any move awaiting verification after a
# real mouse drag.
AUTOPLAY_DEPTH = 20
AUTOPLAY_POLL_SECONDS = 0.4
AUTOPLAY_DRAG_SECONDS = 0.38
AUTOPLAY_STOP_VK = 0x77  # F8: global stop while the chess window has focus
show_autoplay_side_dialog = False
autoplay_side_dialog_anim = 0.0
show_autoplay_gameover_dialog = False
autoplay_gameover_dialog_anim = 0.0
_autoplay = {
    "enabled": False,
    "running": False,
    "state": "idle",
    "status": "",
    "error": "",
    "window": None,
    "pending_window": None,
    "side": "white",
    "scan_id": 0,
    "scan_running": False,
    "scan_ready": False,
    "scan_result": None,
    "board_rect": None,
    "window_rect": None,
    "window_size": None,
    "screen_board_rect": None,
    "last_image": None,
    "last_grid": None,
    "expected_fen": None,
    "next_poll_at": 0.0,
    "pending_move": None,
    "pending_before_image": None,
    "pending_before_grid": None,
    "pending_started_at": 0.0,
    "drag_attempts": 0,
    "game_result": None,
    "stop_key_down": False,
}


def apply_always_on_top(enabled, persist=False):
    global always_on_top_enabled
    desired = bool(enabled)
    ok = set_always_on_top(desired)
    if ok or os.name != "nt":
        # Off Windows there is no HWND to pin; keep the desired state.
        always_on_top_enabled = desired
    # On Windows, if the call really failed the toggle keeps showing the
    # actual (previous) state instead of pretending it worked.
    config["always_on_top"] = 1 if always_on_top_enabled else 0
    if persist:
        save_config()
    return always_on_top_enabled


def set_auto_move(enabled, color=None, persist=True):
    """Enable/disable Auto-Move (optionally choosing its colour) and ask
    main() to restart its arming sequence.

    The enabled flag itself is runtime-only. Calling ``save_config`` here
    is safe because that function deliberately omits it; only harmless
    preferences such as colour and target depth are written to disk.
    """
    global auto_move_enabled, auto_move_color, auto_move_arm_reset
    if color is not None:
        c = str(color).strip().lower()
        auto_move_color = c if c in ("white", "black", "both") else "both"
    auto_move_enabled = bool(enabled)
    config["auto_move"] = 0
    config["auto_move_color"] = auto_move_color
    auto_move_arm_reset = True
    if persist:
        save_config()
    return auto_move_enabled


def get_auto_move_depth():
    """Return the depth at which internal Auto-Move should play.

    ``auto_move_depth == 0`` is the documented sentinel for the current
    Engine Settings Depth Limit. A positive choice is also capped at that
    limit because the analysis search never goes deeper than it.
    """
    try:
        limit = int(globals().get("ANALYSIS_DEPTH", config.get("depth", 25)))
    except Exception:
        limit = 25
    limit = max(1, limit)
    try:
        chosen = int(auto_move_depth)
    except Exception:
        chosen = 0
    return limit if chosen <= 0 else min(chosen, limit)


def set_auto_move_depth(depth, persist=True):
    """Set Auto-Move's target depth (0..40; 0 means Depth Limit)."""
    global auto_move_depth
    try:
        auto_move_depth = max(0, min(int(depth), 40))
    except Exception:
        auto_move_depth = 0
    config["auto_move_depth"] = auto_move_depth
    if persist:
        save_config()
    return auto_move_depth


def normalise_board_theme(value):
    v = str(value or "classic").strip().lower()
    if v in ("chess.com", "chesscom", "chess_com"):
        return "chesscom"
    return v if v in BOARD_THEMES else "classic"


def apply_board_theme(theme_id, persist=False):
    """Switch the light/dark square colours (not the piece style)."""
    global LIGHT_SQ, DARK_SQ, current_board_theme
    theme_id = normalise_board_theme(theme_id)
    current_board_theme = theme_id
    LIGHT_SQ = BOARD_THEMES[theme_id]["light"]
    DARK_SQ = BOARD_THEMES[theme_id]["dark"]
    config["board_theme"] = theme_id
    if persist:
        save_config()
    return theme_id


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
# Full-size piece surfaces are also kept in RAM, keyed by theme. main()
# preloads every installed set once at startup so later theme/board use
# never has to wait on PNG decoding/scaling again.
_piece_image_cache = {}


def get_piece_images(theme):
    """Return a theme's full-size piece images, loading them only once."""
    if theme not in _piece_image_cache:
        _piece_image_cache[theme] = load_images(theme)
    return _piece_image_cache[theme]


def preload_piece_images(themes=None):
    """Decode and cache every installed piece set and its previews."""
    if themes is None:
        themes = [theme_id for theme_id, _name in get_available_themes()]
    loaded = 0
    for theme in themes:
        try:
            get_piece_images(theme)
            get_preview_images(theme)
        except Exception as exc:
            # One unused/corrupt optional piece set must not prevent the
            # application from starting; it can still be reported clearly.
            print(f"Piece image preload skipped {theme!r}: {exc}")
            continue
        loaded += 1
    if loaded:
        print(f"Preloaded {loaded} piece image set(s) into memory.")
    return loaded


available_themes = get_available_themes()
config = load_config()
# Resolve theme from config, fallback to classic/first available
current_theme = config.get("theme", "classic")
if available_themes and all(t[0] != current_theme for t in available_themes):
    current_theme = available_themes[0][0]
    config["theme"]=current_theme
# Board square colours from config ("Classic" unless saved as Chess.com)
apply_board_theme(config.get("board_theme", "classic"))
# Window / analysis toggles from config
_desired_on_top = bool(config.get("always_on_top", 1))
if _desired_on_top:
    # Reflect the real result on Windows; elsewhere keep the preference.
    always_on_top_enabled = set_always_on_top(True) or os.name != "nt"
else:
    set_always_on_top(False)
    always_on_top_enabled = False
# Do not restore Auto-Move from disk. It always starts off; the user must
# explicitly switch it on during this session. Colour/depth are ordinary
# preferences and can be restored without enabling the feature.
auto_move_enabled = False
config["auto_move"] = 0
auto_move_color = config.get("auto_move_color", "both")
try:
    auto_move_depth = max(0, min(int(config.get("auto_move_depth", 0)), 40))
except Exception:
    auto_move_depth = 0
auto_move_arm_reset = True

# Pieces Style menu layout & scrolling — two columns of visual style cards
# below a larger live preview. Cards are tall enough to show sample pieces,
# the style name and its source instead of being text-only buttons.
THEME_PREVIEW_H = 126
THEME_PREVIEW_Y = 82
THEME_LIST_TOP = 238
THEME_ITEM_HEIGHT = 62
THEME_ITEM_GAP = 10
THEME_ITEM_WIDTH = 190
THEME_COL_GAP = 12
THEME_LIST_BOTTOM = HEIGHT - 40
theme_scroll_offset = 0.0
theme_scroll_speed = 70.0

# Settings menu state — pages form a small hierarchy:
#   main -> engine_settings / analysis
#   main -> board -> pieces_style / board_themes
settings_menu_state = "main"
settings_menu_target = "pieces_style"
settings_menu_from = "main"
menu_transition_progress = 0.0
menu_transition_speed = 0.07
menu_transition_direction = "none"  # "none" | "forward" | "back"
SETTINGS_PAGE_TITLES = {
    "main": "Settings",
    "board": "Board",
    "pieces_style": "Pieces Styles",
    "board_themes": "Board Themes",
    "engine_settings": "Engine Settings",
    "analysis": "Analysis",
}
SETTINGS_PAGE_PARENT = {
    "main": None,
    "engine_settings": "main",
    "analysis": "main",
    "board": "main",
    "pieces_style": "board",
    "board_themes": "board",
}
SETTINGS_MAIN_ITEMS = [
    "Engine Settings",
    "Board",
    "Game Controls",
    "Analysis Settings",
]
# Toggle knob animation (0 = off, 1 = on)
always_on_top_anim = 1.0 if always_on_top_enabled else 0.0
# Visual-only polish state for the Always on Top row. The enabled flag and
# always_on_top_anim above remain the behavioural/animation source of truth.
always_on_top_hover_amount = 0.0
always_on_top_icon_phase = 0.0
auto_move_anim = 1.0 if auto_move_enabled else 0.0
autoplay_anim = 0.0
# Smoothed depth progress-bar fill (0..1 of the target depth)
depth_bar_fraction = 0.0
# One-shot opening animation for the in-window title. It advances once per
# frame in update_global_anims() and is never reset during the session.
title_slide_progress = 0.0
TITLE_SLIDE_SPEED = 1.0 / 48.0  # about 0.8 seconds at 60 FPS

images = get_piece_images(current_theme)
openings = load_openings()
ANALYSIS_DEPTH = config.get("depth", 25)

# Live engine handle (assigned in main()); used by the Engine Settings page.
engine_thread = None

# --- Layout constants (shared between draw_gui and event handling) ---
BOARD_OY = TOP_BAR_HEIGHT + PADDING
# Board X is dynamic (eval bar can be left or right); compute per frame
BOARD_OX_LEFT = PADDING + EVAL_BAR_WIDTH + EVAL_BAR_GAP
# Eval bar on the right: the board sits flush left (the eval bar takes the
# strip on the right). This was WIDTH-PADDING-BOARD_WIDTH, which equals
# BOARD_OX_LEFT — so the board never moved and the bar overlapped it.
BOARD_OX_RIGHT = PADDING
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
    if _k not in _STRING_CONFIG_KEYS:
        engine_slider_display[_k]=float(engine_draft.get(_k, ENGINE_DEFAULTS[_k]))
        engine_slider_velocity[_k]=0.0
engine_has_changes = False

# --- New game confirmation ---
show_newgame_confirm = False
confirm_anim = 0.0

# --- Game-over dialog (win / draw) ---
show_game_over = False
game_over_anim = 0.0
game_over_seen_key = None  # terminal position the dialog was shown/dismissed for

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
# Small interactive polish for the settings cog: it eases up in brightness /
# size and turns slowly while hovered. These are visual-only and never
# change settings_button_rect (the actual click target).
settings_gear_rotation = 0.0
settings_gear_hover_amount = 0.0
# Matching visual-only hover/sparkle state for the New Game button.
new_game_hover_amount = 0.0
new_game_sparkle_phase = 0.0
# Flip button polish: hover easing plus a 0..1 icon turn that eases toward
# the current board orientation so its ivory/charcoal arrows swap smoothly.
flip_button_hover_amount = 0.0
flip_icon_turn = 0.0
# Settings > About > Restart button polish. The icon turns slowly on hover
# and spins faster after a restart has been requested while the menu closes.
restart_button_hover_amount = 0.0
restart_icon_rotation = 0.0
# Settings > About > Github button polish. Hover eases the sapphire body in
# while the medallion's orbit dot and external-link arrow animate.
github_button_hover_amount = 0.0
github_button_pulse_phase = 0.0
# Settings header Close/X button polish. Hover eases in separately from a
# short fold animation that runs while the Settings menu is closing.
close_button_hover_amount = 0.0
close_button_fold_amount = 0.0
close_button_icon_phase = 0.0
# Settings main-menu Engine Settings button polish. These values animate
# only its gear/telemetry visuals; navigation and engine drafts are unchanged.
engine_settings_button_hover_amount = 0.0
engine_settings_button_gear_rotation = 0.0
engine_settings_button_pulse_phase = 0.0
# Settings main-menu Board button polish. The phase drives only the
# miniature-board scan/piece bob; Board navigation and themes are unchanged.
board_button_hover_amount = 0.0
board_button_pulse_phase = 0.0
# Settings main-menu Analysis Settings button polish. Its phase drives the
# scope sweep/status glints only; Auto-Move, detection and Auto-play state
# remains owned by their existing runtime variables.
analysis_settings_button_hover_amount = 0.0
analysis_settings_button_pulse_phase = 0.0

menu_anim_progress = 0.0
menu_anim_opening = False
menu_anim_closing = False
menu_anim_speed = 0.075
menu_anim_close_speed = 0.09
# Visual-only choreography for the whole Settings shell. ``progress`` above
# remains the behavioural open/closed amount; these values remember which
# way the shell is travelling, where its reveal rings should anchor, and how
# far its gear/ring accents have turned. They never alter click handling:
# menu interactions still wait for a fully settled, transition-idle page.
settings_menu_anim_direction = "none"  # "none" | "opening" | "closing"
settings_menu_anim_origin = settings_button_rect.center
settings_menu_anim_phase = 0.0
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
    global legal_pulse_t, eval_highlight_pulse, confirm_anim, game_over_anim
    global always_on_top_anim, auto_move_anim, auto_color_dialog_anim
    global board_detection_dialog_anim, autoplay_anim
    global autoplay_side_dialog_anim, autoplay_gameover_dialog_anim
    global title_slide_progress
    if title_slide_progress < 1.0:
        title_slide_progress = min(1.0, title_slide_progress + TITLE_SLIDE_SPEED)
    legal_pulse_t += 0.08
    eval_highlight_pulse += 0.06
    if show_newgame_confirm:
        confirm_anim = min(1.0, confirm_anim + 0.12)
    else:
        confirm_anim = max(0.0, confirm_anim - 0.12)
    if show_game_over:
        game_over_anim = min(1.0, game_over_anim + 0.10)
    else:
        game_over_anim = max(0.0, game_over_anim - 0.12)
    if show_auto_color_dialog:
        auto_color_dialog_anim = min(1.0, auto_color_dialog_anim + 0.12)
    else:
        auto_color_dialog_anim = max(0.0, auto_color_dialog_anim - 0.12)
    if show_board_detection_dialog:
        board_detection_dialog_anim = min(1.0, board_detection_dialog_anim + 0.12)
    else:
        board_detection_dialog_anim = max(0.0, board_detection_dialog_anim - 0.12)
    if show_autoplay_side_dialog:
        autoplay_side_dialog_anim = min(1.0, autoplay_side_dialog_anim + 0.12)
    else:
        autoplay_side_dialog_anim = max(0.0, autoplay_side_dialog_anim - 0.12)
    if show_autoplay_gameover_dialog:
        autoplay_gameover_dialog_anim = min(1.0, autoplay_gameover_dialog_anim + 0.12)
    else:
        autoplay_gameover_dialog_anim = max(0.0, autoplay_gameover_dialog_anim - 0.12)
    autoplay_shown_on = bool(
        _autoplay.get("enabled")
        or show_autoplay_side_dialog
        or (
            show_board_detection_dialog
            and board_detection_picker_purpose == "autoplay"
        )
    )
    for name, on in (
        ("always_on_top_anim", always_on_top_enabled),
        ("auto_move_anim", auto_move_enabled or show_auto_color_dialog),
        ("autoplay_anim", autoplay_shown_on),
    ):
        cur = globals()[name]
        target = 1.0 if on else 0.0
        globals()[name] = target if abs(cur - target) < 0.02 else cur + (target - cur) * 0.25

# --- Math & Animations ---
def cubic_ease_in(t):
    """Cubic ease-in: starts slowly, then accelerates into place."""
    t = max(0.0, min(1.0, float(t)))
    return t * t * t


def cubic_ease_out(t):
    """Cubic ease-out: arrives quickly, then settles gently."""
    t = max(0.0, min(1.0, float(t)))
    return 1.0 - (1.0 - t) ** 3


def cubic_ease_out_back(t, overshoot=1.25):
    """Cubic ease-out with a restrained overshoot just before settling."""
    t = max(0.0, min(1.0, float(t)))
    c1 = float(overshoot)
    c3 = c1 + 1.0
    return 1.0 + c3 * (t - 1.0) ** 3 + c1 * (t - 1.0) ** 2


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

    def add_rook_only(self, rook_piece, rook_start_sq, rook_target_sq):
        """Animate only the rook (drag-castling: the king was dragged by
        hand, so it must not slide again, but the rook should still move
        smoothly instead of teleporting)."""
        if rook_piece is None or rook_start_sq is None or rook_target_sq is None:
            return
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


def get_game_result(board):
    """Return (title, subtitle, score, colour) for a finished game, else None."""
    if board.is_checkmate():
        # Side to move is checkmated, so the other side wins.
        if board.turn == chess.BLACK:
            return ("White wins!", "by checkmate", "1 – 0", (140, 230, 160))
        return ("Black wins!", "by checkmate", "0 – 1", (140, 230, 160))
    if board.is_stalemate():
        return ("Draw", "by stalemate", "½ – ½", DRAW_COLOR)
    if board.is_insufficient_material():
        return ("Draw", "insufficient material", "½ – ½", DRAW_COLOR)
    if board.is_seventyfive_moves():
        return ("Draw", "by the 75-move rule", "½ – ½", DRAW_COLOR)
    if board.is_fivefold_repetition():
        return ("Draw", "by fivefold repetition", "½ – ½", DRAW_COLOR)
    if board.is_fifty_moves():
        return ("Draw", "by the 50-move rule", "½ – ½", DRAW_COLOR)
    if board.is_repetition(3):
        return ("Draw", "by threefold repetition", "½ – ½", DRAW_COLOR)
    return None


def begin_settings_menu_open():
    """Start the Settings shell's opening choreography from the gear."""
    global menu_anim_opening, menu_anim_closing
    global settings_menu_anim_direction, settings_menu_anim_origin
    global settings_menu_anim_phase

    if menu_anim_opening or (menu_anim_progress >= 1.0 and not menu_anim_closing):
        return False
    menu_anim_opening = True
    menu_anim_closing = False
    settings_menu_anim_direction = "opening"
    settings_menu_anim_origin = settings_button_rect.center
    settings_menu_anim_phase = 0.0
    return True


def begin_settings_menu_close(origin=None):
    """Start the Settings shell's dedicated closing choreography.

    ``origin`` is only the visual anchor for the contracting reveal rings;
    the Close/X passes its own centre, while Esc/restart/Auto-play exits
    return toward the top-bar Settings gear by default.
    """
    global menu_anim_opening, menu_anim_closing
    global settings_menu_anim_direction, settings_menu_anim_origin
    global settings_menu_anim_phase

    if menu_anim_closing or menu_anim_progress <= 0.0:
        return False
    menu_anim_opening = False
    menu_anim_closing = True
    settings_menu_anim_direction = "closing"
    settings_menu_anim_phase = 0.0
    try:
        origin_x, origin_y = origin
        settings_menu_anim_origin = (int(origin_x), int(origin_y))
    except Exception:
        settings_menu_anim_origin = settings_button_rect.center
    return True


def _settings_menu_presentation(progress=None, opening=None, closing=None):
    """Visual shell values for one Settings open/close frame.

    Opening uses a fast-arriving cubic-out presence: the backdrop and
    reveal rings lead slightly, then the content scales/fades up into
    place. Closing is not merely that curve played backwards at the raw
    progress level; its presence holds for a beat and then accelerates
    away with a cubic-in exit, a smaller scale change and a shorter sink.
    At rest this returns the identity transform, so settled rendering and
    every click rect remain exactly as before.
    """
    try:
        raw = float(menu_anim_progress if progress is None else progress)
    except Exception:
        raw = 0.0
    raw = max(0.0, min(1.0, raw))

    opening_flag = bool(menu_anim_opening if opening is None else opening)
    closing_flag = bool(menu_anim_closing if closing is None else closing)
    if opening_flag and closing_flag:
        if settings_menu_anim_direction == "closing":
            opening_flag = False
        else:
            closing_flag = False

    if closing_flag:
        direction = "closing"
    elif opening_flag:
        direction = "opening"
    elif raw < 1.0:
        direction = (
            settings_menu_anim_direction
            if settings_menu_anim_direction in ("opening", "closing")
            else "opening"
        )
    else:
        direction = "none"

    if direction == "closing":
        presence = 1.0 - cubic_ease_in(1.0 - raw)
        backdrop = presence
        reveal = presence
        content_alpha = int(round(255 * presence))
        content_scale = 0.972 + 0.028 * presence
        content_y_offset = int(round((1.0 - presence) * 20))
    elif direction == "opening":
        presence = cubic_ease_out(raw)
        backdrop = cubic_ease_out(min(1.0, raw * 1.08))
        reveal = cubic_ease_out(min(1.0, raw * 1.22))
        content_alpha = int(
            round(255 * min(1.0, 0.16 + 0.84 * presence))
        )
        content_scale = 0.962 + 0.038 * presence
        content_y_offset = int(round((1.0 - presence) * 30))
    else:
        presence = 1.0
        backdrop = 1.0
        reveal = 1.0
        content_alpha = 255
        content_scale = 1.0
        content_y_offset = 0

    try:
        origin_x, origin_y = settings_menu_anim_origin
        origin = (int(origin_x), int(origin_y))
    except Exception:
        origin = settings_button_rect.center

    return {
        "raw": raw,
        "direction": direction,
        "animating": direction != "none",
        "presence": max(0.0, min(1.0, presence)),
        "backdrop_alpha": int(round(180 * backdrop)),
        "reveal": max(0.0, min(1.0, reveal)),
        "content_alpha": max(0, min(255, content_alpha)),
        "content_scale": max(0.1, min(1.0, content_scale)),
        "content_y_offset": content_y_offset,
        "origin": origin,
        "phase": settings_menu_anim_phase,
    }


def _settings_main_item_slide(index, count, presentation):
    """Per-row cascade offset for the Settings main menu.

    Rows arrive top-to-bottom with a slight overshoot. On close the same
    local-presence ordering makes the last-arriving lower rows leave
    first, while the first row remains anchored for a moment longer.
    """
    if not presentation.get("animating"):
        return 0
    try:
        index = int(index)
        count = max(1, int(count))
    except Exception:
        return 0
    index = max(0, min(count - 1, index))
    # Use raw shell progress for the cascade itself. The shell's eased
    # presence is deliberately front-loaded for scale/fade, and using it
    # here too would make all four rows arrive in the first few frames.
    raw = max(0.0, min(1.0, float(presentation.get("raw", 1.0))))

    if presentation.get("direction") == "closing":
        local = max(
            0.0,
            min(1.0, (raw - index * 0.05) / 0.85),
        )
        eased = cubic_ease_in_out(local)
        return int(round((1.0 - eased) * 18))

    local = max(
        0.0,
        min(1.0, (raw - index * 0.06) / 0.78),
    )
    eased = cubic_ease_out_back(local, overshoot=1.25)
    return int(round((1.0 - eased) * 20))


def _draw_settings_menu_reveal(surface, presentation):
    """Draw the shell's expanding/contracting rings and gear echo."""
    if not presentation.get("animating"):
        return

    presence = max(0.0, min(1.0, float(presentation.get("presence", 1.0))))
    reveal = max(0.0, min(1.0, float(presentation.get("reveal", presence))))
    strength = 1.0 - presence
    if strength <= 0.01:
        return

    origin = presentation.get("origin", settings_button_rect.center)
    try:
        origin_x, origin_y = int(origin[0]), int(origin[1])
    except Exception:
        origin_x, origin_y = settings_button_rect.center
    phase = float(presentation.get("phase", settings_menu_anim_phase))

    overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)

    # Two soft sonar-like rings expand from (or collect back toward) the
    # anchor while the content itself performs the scale/fade. Keeping
    # these as rings rather than a faux panel avoids a stray rectangular
    # edge crossing pages whose content already spans the whole window.
    max_radius = math.hypot(
        max(origin_x, WIDTH - origin_x),
        max(origin_y, HEIGHT - origin_y),
    )
    for radius_factor, alpha_factor in ((1.0, 54), (0.68, 30)):
        radius = int(round(max_radius * reveal * radius_factor))
        if radius > 10:
            pygame.draw.circle(
                overlay,
                (120, 165, 255, int(alpha_factor * strength)),
                (origin_x, origin_y),
                radius,
                2,
            )

    # Short rotating ticks around the anchor give the first/last frames a
    # mechanical link to the Settings cog without moving any click target.
    tick_radius = 23.0 + 3.0 * math.sin(phase)
    tick_alpha = int(132 * strength)
    if tick_alpha > 0:
        for tick_index in range(8):
            angle = phase + tick_index * (math.tau / 8.0)
            start = (
                origin_x + (tick_radius - 3.0) * math.cos(angle),
                origin_y + (tick_radius - 3.0) * math.sin(angle),
            )
            end = (
                origin_x + (tick_radius + 3.0) * math.cos(angle),
                origin_y + (tick_radius + 3.0) * math.sin(angle),
            )
            pygame.draw.aaline(
                overlay,
                (175, 205, 255, tick_alpha),
                start,
                end,
            )
    surface.blit(overlay, (0, 0))

    # The actual top-bar cog is hidden while Settings owns the screen, so
    # echo it beneath the transforming content. It spins out on open and
    # becomes the landing point that the real button replaces on close.
    gear_alpha = int(205 * strength)
    if gear_alpha > 4:
        gear_surface = pygame.Surface((58, 58), pygame.SRCALPHA)
        gear_colour = _lerp_color(
            GEAR_COLOR,
            GEAR_COLOR_HOVER,
            max(0.0, min(1.0, settings_gear_hover_amount)),
        )
        draw_gear_icon(
            gear_surface,
            29,
            29,
            11.0 + 3.5 * strength,
            gear_colour,
            thickness=2,
            rotation=settings_gear_rotation,
        )
        gear_surface.fill(
            (255, 255, 255, gear_alpha),
            special_flags=pygame.BLEND_RGBA_MULT,
        )
        surface.blit(
            gear_surface,
            gear_surface.get_rect(center=settings_button_rect.center),
        )


def _composite_settings_menu_content(destination, content, presentation):
    """Fade/scale one offscreen Settings frame onto the real screen."""
    alpha = max(0, min(255, int(presentation.get("content_alpha", 255))))
    if alpha <= 0:
        return

    scale = max(0.1, min(1.0, float(presentation.get("content_scale", 1.0))))
    scaled = content
    if scale < 0.999:
        scaled_size = (
            max(1, int(round(WIDTH * scale))),
            max(1, int(round(HEIGHT * scale))),
        )
        # The shell only travels through the final few percent of scale,
        # so the fast scaler is visually indistinguishable here while
        # leaving ample frame budget for the board underneath. Likewise,
        # SDL's global alpha modulation avoids a second full-window
        # multiply pass; the surface's per-pixel alpha still applies.
        scaled = pygame.transform.scale(content, scaled_size)
    if alpha < 255:
        scaled.set_alpha(alpha)

    centre = (
        WIDTH // 2,
        HEIGHT // 2 + int(presentation.get("content_y_offset", 0)),
    )
    destination.blit(scaled, scaled.get_rect(center=centre))


def update_menu_animation():
    global menu_anim_progress, menu_anim_opening, menu_anim_closing, settings_menu_open
    global settings_menu_state, settings_menu_target, settings_menu_from
    global menu_transition_progress, menu_transition_direction
    global settings_menu_anim_direction, settings_menu_anim_phase
    global settings_gear_rotation
    global theme_scroll_offset

    if menu_anim_opening:
        settings_menu_anim_direction = "opening"
        settings_menu_anim_phase = (
            settings_menu_anim_phase + 0.14
        ) % (2.0 * math.pi)
        settings_gear_rotation = (
            settings_gear_rotation + 0.045
        ) % (2.0 * math.pi)
        menu_anim_progress = min(1.0, menu_anim_progress + menu_anim_speed)
        if menu_anim_progress >= 1.0:
            menu_anim_progress = 1.0
            menu_anim_opening = False
            settings_menu_anim_direction = "none"
            settings_menu_open = True
    elif menu_anim_closing:
        settings_menu_anim_direction = "closing"
        settings_menu_anim_phase = (
            settings_menu_anim_phase - 0.16
        ) % (2.0 * math.pi)
        settings_gear_rotation = (
            settings_gear_rotation - 0.055
        ) % (2.0 * math.pi)
        menu_anim_progress = max(0.0, menu_anim_progress - menu_anim_close_speed)
        if menu_anim_progress <= 0.0:
            menu_anim_progress = 0.0
            menu_anim_closing = False
            settings_menu_anim_direction = "none"
            settings_menu_open = False
            settings_menu_state = "main"
            settings_menu_target = "main"
            settings_menu_from = "main"
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
            "fen": self.board.fen(),
            "token": self.search_token,
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
            # Invalidate the previous search immediately. Without this, a
            # just-imported Auto-play position could briefly inherit the
            # old position's depth/PV even though engine.board is new.
            self.info = {
                "score": 0,
                "depth": 0,
                "nps": 0,
                "pv": [],
                "mate": None,
                "lines": {},
                "fen": self.board.fen(),
                "token": self.search_token,
            }

    def _reset_info(self, fen=None, token=None):
        """Zero the readout; called when the loop picks up a new position."""
        with self.lock:
            if fen is None:
                fen = self.board.fen()
            if token is None:
                token = self.search_token
            self.info = {
                "score": 0,
                "depth": 0,
                "nps": 0,
                "pv": [],
                "mate": None,
                "lines": {},
                "fen": fen,
                "token": token,
            }

    def set_pending_parameters(self, params):
        """Queue UCI options to apply before the next engine search."""
        with self.lock:
            self.pending_params.update(params)

    def recalculate_current(self):
        """Force the current board to be searched again.

        Re-publishing the same board is intentional: it stops any running
        search, bumps ``search_token`` and clears the old readout. Without
        the token bump, applying a new MultiPV value would leave the loop
        idle because the FEN still matches ``last_searched_fen``.
        Pending UCI options remain queued and are applied by the loop
        immediately before this fresh search starts.
        """
        with self.lock:
            current = self.board.copy()
        self.update_position(current)

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
            self._reset_info(fen, token)

            try:
                self._search_position(fen, current_board.turn == chess.WHITE, token=token)
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

    def _search_position(self, fen, white_to_move, token=None):
        """Run one depth-limited search, streaming live info into self.info."""
        # position + go go out as a single atomic write so a "stop" from
        # the main thread can never squeeze in between them.
        self._send(f"position fen {fen}\ngo depth {ANALYSIS_DEPTH}")

        while True:
            line = self._read_line()
            if line.startswith("bestmove"):
                return
            if line.startswith("info") and not line.startswith("info string"):
                self._parse_info_line(line, white_to_move, token=token)

    def _parse_info_line(self, line, white_to_move, token=None):
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
            # A stop/update may have happened while this line was being
            # parsed. Never let the old search repopulate the new readout.
            if token is not None and token != self.search_token:
                return
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
    elif is_castling:
        # Dragged king: no king slide (it followed the cursor), but the
        # rook still animates to its castled square.
        anim_mgr.add_rook_only(rook_piece, rook_start_sq, rook_target_sq)

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


_CASTLING_ROOK_PAIRS = {
    (chess.E1, chess.G1): (chess.H1, chess.F1),
    (chess.E1, chess.C1): (chess.A1, chess.D1),
    (chess.E8, chess.G8): (chess.H8, chess.F8),
    (chess.E8, chess.C8): (chess.A8, chess.D8),
}


def add_move_animation_for_move(anim_mgr, board_before, move, reverse=False):
    """Queue the normal piece-slide animation for a history step.

    Call BEFORE push/pop, with the board still in its pre-step state.
    reverse=False replays the move (→); reverse=True plays it backwards
    (←), including the rook hop for castling in either direction."""
    if not reverse:
        piece = board_before.piece_at(move.from_square)
        if not piece:
            return
        # capture flourish, mirroring try_execute_move (incl. en passant)
        captured = board_before.piece_at(move.to_square)
        cap_sq = move.to_square
        if not captured and board_before.is_en_passant(move):
            cap_sq = chess.square(chess.square_file(move.to_square), chess.square_rank(move.from_square))
            captured = board_before.piece_at(cap_sq)
        if captured:
            anim_mgr.add_capture(captured, cap_sq)
        is_castling, rs, rt, rp = get_castling_info(board_before, move.from_square, move.to_square)
        anim_mgr.add_move(piece, move.from_square, move.to_square, is_castling, rs, rt, rp)
    else:
        piece = board_before.piece_at(move.to_square)
        if not piece:
            return
        rook_pair = _CASTLING_ROOK_PAIRS.get((move.from_square, move.to_square))
        if rook_pair and piece.piece_type == chess.KING:
            rook_from, rook_to = rook_pair[1], rook_pair[0]  # castled -> home
            rook_piece = board_before.piece_at(rook_from)
            anim_mgr.add_move(piece, move.to_square, move.from_square, True, rook_from, rook_to, rook_piece)
        else:
            anim_mgr.add_move(piece, move.to_square, move.from_square)


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
    if length < 1:
        return

    # Subtle pulse for mate arrows (red) — not too noticeable
    if is_mate:
        pulse = math.sin(pygame.time.get_ticks() * 0.005) * 0.07  # ~7% pulse
        width = width * (1 + pulse * 0.5)
        alpha = max(0, min(255, int(alpha * (1 + pulse * 0.3))))
    v_unit = v / length

    head_length = width * 3
    head_width = width * 2.5
    shaft_half = width / 2.0
    perp_unit = pygame.Vector2(-v_unit.y, v_unit.x)
    head_base = end - v_unit * head_length
    # Point well inside the head triangle: the gradient shaft runs to here,
    # so it is always buried in (and welded to) the filled head below.
    joint = end - v_unit * (head_length * 0.45)

    a = max(0, min(255, int(alpha)))
    if a <= 1:
        return

    grad_start = ARROW_MATE_START if is_mate else ARROW_GRAD_START
    grad_end = ARROW_MATE_END if is_mate else ARROW_GRAD_END

    # Shaft and head as ONE silhouette. The previous version stopped the
    # shaft 80% into a separately-drawn triangle; integer truncation of the
    # two shapes could leave a hairline seam where they met. A single
    # polygon cannot have a gap between "body" and "head".
    silhouette = [
        start + perp_unit * shaft_half,
        head_base + perp_unit * shaft_half,
        head_base + perp_unit * (head_width / 2),
        end,
        head_base - perp_unit * (head_width / 2),
        head_base - perp_unit * shaft_half,
        start - perp_unit * shaft_half,
    ]
    xs = [p.x for p in silhouette] + [start.x, end.x]
    ys = [p.y for p in silhouette] + [start.y, end.y]
    pad = int(width) + 6
    min_x = int(min(xs)) - pad
    min_y = int(min(ys)) - pad
    max_x = int(max(xs)) + pad
    max_y = int(max(ys)) + pad
    w = max(1, max_x - min_x)
    h = max(1, max_y - min_y)

    # Draw everything fully opaque onto a scratch surface, then apply the
    # requested alpha once at the end. Overlapping shapes therefore merge
    # with no double-alpha seam at the head/shaft junction.
    surf = pygame.Surface((w, h), pygame.SRCALPHA)
    offset = pygame.Vector2(min_x, min_y)
    pts = [(int(p.x - offset.x), int(p.y - offset.y)) for p in silhouette]
    pygame.draw.polygon(surf, (*grad_end, 255), pts)
    try:
        pygame.gfxdraw.filled_polygon(surf, pts, (*grad_end, 255))
        pygame.gfxdraw.aapolygon(surf, pts, (*grad_end, 255))
    except Exception:
        pass

    # Gradient along the shaft, running into the head up to `joint`.
    joint_frac = max(0.0, min(1.0, (joint - start).length() / length))
    segs = 24 if is_mate else 20
    line_w = max(1, int(width))
    for i in range(segs):
        t0 = i / segs
        t1 = (i + 1) / segs
        c = _lerp_color(grad_start, grad_end, t0 * joint_frac)
        rgba = (*c, 255)
        p0 = start + (joint - start) * t0
        p1 = start + (joint - start) * t1
        pygame.draw.line(surf, rgba, p0 - offset, p1 - offset, line_w)
        pygame.draw.circle(surf, rgba, (int(p0.x - offset.x), int(p0.y - offset.y)), max(1, int(shaft_half)))

    # Weld discs: rounded tail and a solid joint where shaft meets head.
    pygame.draw.circle(surf, (*grad_start, 255), (int(start.x - offset.x), int(start.y - offset.y)), max(1, int(shaft_half)))
    pygame.draw.circle(surf, (*grad_end, 255), (int(head_base.x - offset.x), int(head_base.y - offset.y)), max(1, int(shaft_half)))
    pygame.draw.circle(surf, (*grad_end, 255), (int(joint.x - offset.x), int(joint.y - offset.y)), max(1, int(shaft_half)))

    if a < 255:
        surf.fill((255, 255, 255, a), special_flags=pygame.BLEND_RGBA_MULT)
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
    shaft_half = width / 2.0
    head_base = end - v_unit * head_length
    # As in draw_arrow: run the shaft into the head and weld it there,
    # rather than butting two separately-rounded shapes together.
    joint = end - v_unit * (head_length * 0.45)
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
    pad = int(width) + 6
    min_x = int(min(xs)) - pad
    min_y = int(min(ys)) - pad
    max_x = int(max(xs)) + pad
    max_y = int(max(ys)) + pad
    w = max(1, max_x - min_x)
    h = max(1, max_y - min_y)

    # Opaque scratch surface + single alpha multiply at the end (see
    # draw_arrow) so the corner and the head junction have no seam.
    surf = pygame.Surface((w, h), pygame.SRCALPHA)
    offset = pygame.Vector2(min_x, min_y)

    head_pts = [(int(p.x - offset.x), int(p.y - offset.y)) for p in (point1, point2, point3)]
    pygame.draw.polygon(surf, (*grad_end, 255), head_pts)
    try:
        pygame.gfxdraw.filled_polygon(surf, head_pts, (*grad_end, 255))
        pygame.gfxdraw.aapolygon(surf, head_pts, (*grad_end, 255))
    except Exception:
        pass

    len1 = (corner - start).length()
    len2 = (joint - corner).length()
    total = max(1, len1 + len2)
    line_w = max(1, int(width))
    segs = 28
    for i in range(segs):
        t0 = i / segs
        t1 = (i + 1) / segs
        def point_at(t, _len1=len1, _len2=len2, _total=total):
            if t * _total <= _len1:
                seg_t = (t * _total) / max(1, _len1)
                return start + (corner - start) * seg_t
            seg_t = (t * _total - _len1) / max(1, _len2)
            return corner + (joint - corner) * seg_t
        p0 = point_at(t0)
        p1 = point_at(t1)
        c = _lerp_color(grad_start, grad_end, t0)
        rgba = (*c, 255)
        pygame.draw.line(surf, rgba, p0 - offset, p1 - offset, line_w)
        pygame.draw.circle(surf, rgba, (int(p0.x - offset.x), int(p0.y - offset.y)), max(1, int(shaft_half)))

    # Weld discs at the tail, the L-bend, and the head junction.
    corner_c = _lerp_color(grad_start, grad_end, len1 / total)
    for pos, col in ((start, grad_start), (corner, corner_c), (head_base, grad_end), (joint, grad_end)):
        pygame.draw.circle(surf, (*col, 255), (int(pos.x - offset.x), int(pos.y - offset.y)), max(1, int(shaft_half)))

    if a < 255:
        surf.fill((255, 255, 255, a), special_flags=pygame.BLEND_RGBA_MULT)
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


def _eval_bar_white_fraction(current_eval, mate):
    """White's share of the eval bar on the existing ±10-pawn scale."""
    if mate is not None:
        return 1.0 if mate > 0 else 0.0
    try:
        clamped = max(-1000.0, min(1000.0, float(current_eval)))
    except Exception:
        clamped = 0.0
    return (clamped + 1000.0) / 2000.0


def draw_eval_bar(screen, x, y, w, h, current_eval, mate):
    """Draw a rounded, layered Lichess-style evaluation bar.

    The evaluation mapping and click geometry are unchanged; this is a
    visual upgrade: a soft shadow/halo, framed glass-like track, anchored
    black/ivory gradients, a clear centre point, circular side labels and
    a slim amber pointer at the exact black/white boundary.
    """
    x, y, w, h = int(x), int(y), int(w), int(h)
    if w <= 0 or h <= 0:
        return

    bar_rect = pygame.Rect(x, y, w, h)
    radius = max(4, min(10, w // 3))
    white_h = max(0, min(h, int(round(h * _eval_bar_white_fraction(current_eval, mate)))))
    black_h = h - white_h

    winning = None
    if mate is not None:
        winning = "white" if mate > 0 else "black"
    elif current_eval > 180:
        winning = "white"
    elif current_eval < -180:
        winning = "black"

    pulse = 0.5 + 0.5 * math.sin(pygame.time.get_ticks() * 0.003)

    # A very soft coloured halo behind a clearly winning side. Several
    # expanded rounded rects approximate a blur without an expensive filter.
    if winning:
        halo_rgb = (255, 214, 92) if winning == "white" else (105, 130, 255)
        halo = pygame.Surface((w + 20, h + 20), pygame.SRCALPHA)
        base_alpha = 15 + int(12 * pulse)
        for inset, alpha_scale in ((1, 0.45), (4, 0.7), (7, 1.0)):
            pygame.draw.rect(
                halo,
                (*halo_rgb, int(base_alpha * alpha_scale)),
                (inset, inset, w + 20 - inset * 2, h + 20 - inset * 2),
                border_radius=radius + 8 - inset,
            )
        screen.blit(halo, (x - 10, y - 10))

    # Drop shadow and the dark outer shell.
    shadow = pygame.Surface((w + 10, h + 12), pygame.SRCALPHA)
    pygame.draw.rect(
        shadow,
        (0, 0, 0, 82),
        (2, 5, w + 6, h + 4),
        border_radius=radius + 3,
    )
    screen.blit(shadow, (x - 5, y - 3))
    pygame.draw.rect(
        screen,
        EVAL_BAR_FRAME_DARK,
        bar_rect.inflate(6, 6),
        border_radius=radius + 3,
    )

    # Build the inner bar on its own surface so all shading can be clipped
    # to the same rounded silhouette. Gradients are anchored to the whole
    # bar (not each changing segment), so colours do not swim as eval moves.
    bar = pygame.Surface((w, h), pygame.SRCALPHA)
    black_top, black_bottom = EVAL_BLACK_GRADIENT
    white_top, white_bottom = EVAL_WHITE_GRADIENT
    denom = max(1, h - 1)
    for i in range(h):
        t = i / denom
        colour = (
            _lerp_color(black_top, black_bottom, t)
            if i < black_h
            else _lerp_color(white_top, white_bottom, t)
        )
        pygame.draw.line(bar, (*colour, 255), (0, i), (w - 1, i))

    # All translucent decoration goes on an overlay first. Drawing an
    # alpha colour directly onto the opaque gradient with pygame.draw can
    # replace the gradient's alpha instead of blending with it, which made
    # a winning White segment look dark during visual testing. Blitting
    # this overlay below performs the intended alpha blend.
    overlay = pygame.Surface((w, h), pygame.SRCALPHA)

    # A restrained warm/cool wash over the winning segment.
    if winning == "white" and white_h > 0:
        pygame.draw.rect(
            overlay,
            (255, 214, 92, 18 + int(14 * pulse)),
            (0, black_h, w, white_h),
        )
    elif winning == "black" and black_h > 0:
        pygame.draw.rect(
            overlay,
            (105, 130, 255, 17 + int(13 * pulse)),
            (0, 0, w, black_h),
        )

    # Glass shading: a bright spine down the left, a darker right edge and
    # shallow inner shadows at both ends give the flat fill some depth.
    pygame.draw.rect(overlay, (255, 255, 255, 30), (2, 2, 4, h - 4), border_radius=2)
    pygame.draw.rect(overlay, (255, 255, 255, 12), (7, 2, 3, h - 4), border_radius=2)
    pygame.draw.rect(overlay, (0, 0, 0, 34), (w - 5, 2, 3, h - 4), border_radius=2)
    pygame.draw.rect(overlay, (0, 0, 0, 34), (0, 0, w, 3))
    pygame.draw.rect(overlay, (0, 0, 0, 28), (0, h - 3, w, 3))
    pygame.draw.line(overlay, (255, 255, 255, 34), (3, 1), (w - 4, 1))

    # Fine quarter ticks, coloured against whichever segment they sit on.
    for frac, strong in ((0.25, False), (0.5, True), (0.75, False)):
        ty = int(round((h - 1) * frac))
        tick_len = 8 if strong else 5
        tick_colour = (
            (255, 255, 255, 112) if ty < black_h else (22, 25, 31, 96)
        )
        tick_width = 2 if strong else 1
        pygame.draw.line(overlay, tick_colour, (1, ty), (tick_len, ty), tick_width)
        pygame.draw.line(
            overlay,
            tick_colour,
            (w - tick_len - 1, ty),
            (w - 2, ty),
            tick_width,
        )

    # The exact centre gets a faint amber rule and diamond, separate from
    # the moving boundary marker below.
    centre_y = h // 2
    pygame.draw.line(
        overlay,
        (*EVAL_MARKER_COLOR, 92),
        (3, centre_y),
        (w - 4, centre_y),
        1,
    )
    cx = w // 2
    diamond = [
        (cx, centre_y - 4),
        (cx + 4, centre_y),
        (cx, centre_y + 4),
        (cx - 4, centre_y),
    ]
    pygame.draw.polygon(overlay, (*EVAL_MARKER_COLOR, 178), diamond)
    pygame.draw.polygon(overlay, (62, 43, 0, 190), diamond, 1)

    bar.blit(overlay, (0, 0))

    # Clip every layer above to the bar's rounded silhouette.
    mask = pygame.Surface((w, h), pygame.SRCALPHA)
    mask.fill((0, 0, 0, 0))
    pygame.draw.rect(mask, (255, 255, 255, 255), mask.get_rect(), border_radius=radius)
    bar.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
    screen.blit(bar, bar_rect.topleft)

    # Metallic inner frame and a hairline around the dark shell.
    pygame.draw.rect(
        screen,
        (151, 159, 173),
        bar_rect.inflate(4, 4),
        1,
        border_radius=radius + 2,
    )
    pygame.draw.rect(
        screen,
        EVAL_BAR_FRAME_COLOR,
        bar_rect,
        2,
        border_radius=radius,
    )

    # Slim amber boundary pointer with little side tips. It sits above the
    # frame so the precise black/white meeting point remains unmistakable.
    marker_y = max(y + 3, min(y + black_h, y + h - 4))
    marker_glow = pygame.Surface((w + 24, 19), pygame.SRCALPHA)
    glow_alpha = 24 + int(18 * pulse)
    pygame.draw.rect(
        marker_glow,
        (*EVAL_MARKER_COLOR, glow_alpha // 2),
        (1, 3, w + 22, 13),
        border_radius=7,
    )
    pygame.draw.rect(
        marker_glow,
        (*EVAL_MARKER_COLOR, glow_alpha),
        (5, 5, w + 14, 9),
        border_radius=5,
    )
    screen.blit(marker_glow, (x - 12, marker_y - 9))

    left_tip = [(x - 10, marker_y), (x - 4, marker_y - 4), (x - 4, marker_y + 4)]
    right_tip = [
        (x + w + 9, marker_y),
        (x + w + 3, marker_y - 4),
        (x + w + 3, marker_y + 4),
    ]
    pygame.draw.polygon(screen, EVAL_MARKER_COLOR, left_tip)
    pygame.draw.polygon(screen, EVAL_MARKER_COLOR, right_tip)

    marker_rect = pygame.Rect(x - 6, marker_y - 3, w + 12, 7)
    pygame.draw.rect(screen, EVAL_MARKER_COLOR, marker_rect, border_radius=4)
    pygame.draw.rect(
        screen,
        EVAL_MARKER_HIGHLIGHT,
        (marker_rect.x + 2, marker_y - 2, marker_rect.width - 4, 2),
        border_radius=1,
    )
    pygame.draw.rect(screen, (74, 50, 0), marker_rect, 1, border_radius=4)
    marker_cx = x + w // 2
    pygame.draw.polygon(
        screen,
        (74, 50, 0),
        [
            (marker_cx, marker_y - 2),
            (marker_cx + 3, marker_y),
            (marker_cx, marker_y + 2),
            (marker_cx - 3, marker_y),
        ],
    )

    # Circular side chips replace the old floating letters. They only appear
    # when their segment is tall enough not to crowd the boundary marker.
    def draw_side_label(cy, text, foreground, background, border):
        centre = (x + w // 2, cy)
        pygame.draw.circle(screen, (14, 15, 19), (centre[0], centre[1] + 1), 11)
        pygame.draw.circle(screen, background, centre, 11)
        pygame.draw.circle(screen, border, centre, 11, 1)
        label = font_tiny.render(text, True, foreground)
        screen.blit(label, label.get_rect(center=centre))

    if black_h >= 30:
        draw_side_label(
            y + 17,
            "B",
            (22, 24, 30),
            (238, 241, 247),
            (151, 159, 173),
        )
    if white_h >= 30:
        draw_side_label(
            y + h - 17,
            "W",
            (242, 244, 248),
            (25, 27, 34),
            (151, 159, 173),
        )
    if mate is not None and h >= 90:
        # A central M makes a forced-mate bar feel distinct from a merely
        # large numerical advantage.
        if mate > 0:
            draw_side_label(
                y + h // 2,
                "M",
                EVAL_MARKER_HIGHLIGHT,
                (25, 27, 34),
                EVAL_MARKER_COLOR,
            )
        else:
            draw_side_label(
                y + h // 2,
                "M",
                (25, 27, 34),
                (238, 241, 247),
                EVAL_MARKER_COLOR,
            )


_gear_icon_base_cache = {}
_gear_icon_cache = {}


def _gear_polar_point(cx, cy, radius, angle):
    return (cx + radius * math.cos(angle), cy + radius * math.sin(angle))


def _render_gear_icon_base(rgb, thickness=2, compact=False):
    """Render one detailed gear at high resolution.

    The settings cog is only about 22 px wide on screen. Drawing all of its
    bevels and bolt details directly at that size produces a noisy blob, so
    the master icon is rendered on a 144 px canvas once and smooth-scaled /
    rotated from cache by :func:`draw_gear_icon`. ``compact`` keeps the
    bevels, rim, bolts and hub but omits hairline spokes/slots that would
    turn into speckle at toolbar size.
    """
    cache_key = (tuple(rgb), int(thickness), bool(compact))
    cached = _gear_icon_base_cache.get(cache_key)
    if cached is not None:
        return cached

    canvas = 144
    cx = cy = canvas // 2
    outer_r = 54
    root_r = 40 if compact else 42
    icon = pygame.Surface((canvas, canvas), pygame.SRCALPHA)

    base_rgb = tuple(max(0, min(255, int(v))) for v in rgb[:3])
    highlight = _lerp_color(base_rgb, (255, 255, 255), 0.22)
    light = _lerp_color(base_rgb, (174, 184, 201), 0.28)
    mid = _lerp_color(base_rgb, (104, 113, 129), 0.48)
    dark = _lerp_color(base_rgb, (45, 50, 61), 0.62)
    deep = _lerp_color(base_rgb, (20, 23, 29), 0.78)
    recess = _lerp_color(base_rgb, (48, 54, 66), 0.72)
    recess_light = _lerp_color(base_rgb, (82, 90, 105), 0.58)

    # Eight chunky trapezoidal teeth read better at toolbar size than the
    # old ten narrow zig-zags. A root midpoint between teeth gives the
    # valley a gently scalloped, machined profile.
    teeth = 8
    pitch = 2.0 * math.pi / teeth
    body_pts = []
    tooth_faces = []
    for i in range(teeth):
        angle = -math.pi / 2.0 + i * pitch
        root_left = _gear_polar_point(cx, cy, root_r, angle - pitch * 0.34)
        tip_left = _gear_polar_point(cx, cy, outer_r, angle - pitch * 0.18)
        tip_mid = _gear_polar_point(cx, cy, outer_r, angle)
        tip_right = _gear_polar_point(cx, cy, outer_r, angle + pitch * 0.18)
        root_mid = _gear_polar_point(cx, cy, root_r, angle)
        root_right = _gear_polar_point(cx, cy, root_r, angle + pitch * 0.34)
        valley = _gear_polar_point(cx, cy, root_r - 1, angle + pitch * 0.5)
        body_pts.extend((root_left, tip_left, tip_right, root_right, valley))
        tooth_faces.append(
            (
                [root_left, tip_left, tip_mid, root_mid],
                [root_mid, tip_mid, tip_right, root_right],
                tip_left,
                tip_mid,
                tip_right,
            )
        )

    # Soft offset silhouette first; the opaque body then leaves only a
    # narrow lower-right shadow visible.
    shadow_pts = [(px + 3, py + 4) for px, py in body_pts]
    pygame.draw.polygon(icon, (7, 9, 12, 118), shadow_pts)
    pygame.draw.polygon(icon, (*mid, 255), body_pts)

    # Alternate bevel faces on every tooth: lit on one flank, shaded on the
    # other, with a fine bright edge along the first half of each tip.
    for left_face, right_face, tip_left, tip_mid, tip_right in tooth_faces:
        pygame.draw.polygon(icon, (*light, 255), left_face)
        pygame.draw.polygon(icon, (*dark, 255), right_face)
        pygame.draw.line(icon, (*highlight, 255), tip_left, tip_mid, 3)
        pygame.draw.line(icon, (*deep, 255), tip_mid, tip_right, 2)

    outline_w = max(4 if compact else 3, int(thickness) * 2)
    pygame.draw.polygon(icon, (*deep, 255), body_pts, outline_w)

    # Concentric machined rim and recessed face.
    pygame.draw.circle(icon, (*dark, 255), (cx, cy), root_r - 3)
    pygame.draw.circle(icon, (*light, 255), (cx, cy), root_r - 4, 3)
    pygame.draw.circle(icon, (*recess, 255), (cx, cy), 33)
    pygame.draw.circle(icon, (*recess_light, 255), (cx, cy), 29)
    pygame.draw.circle(icon, (*deep, 255), (cx, cy), 33, 2)
    pygame.draw.circle(icon, (*highlight, 255), (cx, cy), 29, 1)
    try:
        pygame.gfxdraw.aacircle(icon, cx, cy, 33, (*deep, 255))
        pygame.gfxdraw.aacircle(icon, cx, cy, 29, (*highlight, 255))
    except Exception:
        pass

    # Six spokes ending in little slotted bolt heads. These details are
    # deliberately drawn large here; smooth downscaling turns them into a
    # crisp suggestion of hardware rather than random pixels. The compact
    # toolbar variant keeps clean bolt heads but drops the hairlines.
    for k in range(6):
        angle = -math.pi / 2.0 + k * math.pi / 3.0 + 0.24
        if not compact:
            inner = _gear_polar_point(cx, cy, 13, angle)
            outer = _gear_polar_point(cx, cy, 27, angle)
            pygame.draw.line(icon, (*dark, 255), inner, outer, 8)
            pygame.draw.line(icon, (*light, 255), inner, outer, 4)

        bx, by = _gear_polar_point(cx, cy, 23, angle)
        pygame.draw.circle(icon, (*deep, 255), (int(bx) + 1, int(by) + 1), 5)
        pygame.draw.circle(icon, (*highlight, 255), (int(bx), int(by)), 5)
        pygame.draw.circle(icon, (*deep, 255), (int(bx), int(by)), 5, 1)
        if compact:
            pygame.draw.circle(icon, (*deep, 255), (int(bx), int(by)), 2)
        else:
            slot_angle = angle + 0.65
            slot_a = (
                bx - 3.4 * math.cos(slot_angle),
                by - 3.4 * math.sin(slot_angle),
            )
            slot_b = (
                bx + 3.4 * math.cos(slot_angle),
                by + 3.4 * math.sin(slot_angle),
            )
            pygame.draw.line(icon, (*deep, 255), slot_a, slot_b, 2)

    # Tiny engraved indexing dots between the bolts (large variant only).
    if not compact:
        for k in range(6):
            angle = -math.pi / 2.0 + (k + 0.5) * math.pi / 3.0 + 0.24
            dx, dy = _gear_polar_point(cx, cy, 25, angle)
            pygame.draw.circle(icon, (*deep, 255), (int(dx), int(dy)), 2)

    # Layered centre hub: outer collar, bright face, dark socket and pin,
    # finished with a short screwdriver slot. The compact hub is slightly
    # larger so the cog still reads instantly in the 50x30 toolbar button.
    hub_r = 18 if compact else 16
    hub_face_r = 15 if compact else 13
    socket_r = 10 if compact else 9
    pin_r = 5 if compact else 4
    pygame.draw.circle(icon, (*deep, 255), (cx, cy + 1), hub_r)
    pygame.draw.circle(icon, (*mid, 255), (cx, cy), hub_r)
    pygame.draw.circle(icon, (*light, 255), (cx, cy), hub_face_r)
    pygame.draw.circle(icon, (*recess, 255), (cx, cy), socket_r)
    pygame.draw.circle(icon, (*highlight, 255), (cx, cy), pin_r)
    pygame.draw.circle(icon, (*deep, 255), (cx, cy), hub_r, 2)
    slot_len = 7 if compact else 6
    slot_angle = -0.58
    slot_a = (
        cx - slot_len * math.cos(slot_angle),
        cy - slot_len * math.sin(slot_angle),
    )
    slot_b = (
        cx + slot_len * math.cos(slot_angle),
        cy + slot_len * math.sin(slot_angle),
    )
    pygame.draw.line(icon, (*deep, 255), slot_a, slot_b, 3)
    pygame.draw.line(
        icon,
        (*highlight, 255),
        (slot_a[0], slot_a[1] + 2),
        (slot_b[0], slot_b[1] + 2),
        1,
    )

    # One restrained specular arc/dot in the upper-left gives the metal a
    # light source without adding another bright outline everywhere.
    pygame.draw.arc(
        icon,
        (*highlight, 255),
        pygame.Rect(cx - 30, cy - 30, 60, 60),
        2.25,
        4.05,
        3,
    )
    if not compact:
        pygame.draw.circle(icon, (*highlight, 255), (cx - 19, cy - 21), 3)

    _gear_icon_base_cache[cache_key] = icon
    return icon


def _get_gear_icon(color, size, thickness=2, rotation=0.0):
    """Return a cached downscaled/rotated copy of the master gear."""
    rgb = tuple(max(0, min(255, int(v))) for v in color[:3])
    alpha = max(0, min(255, int(color[3]))) if len(color) > 3 else 255
    size_key = max(1, int(round(float(size) * 4.0)))
    effective_size = size_key / 4.0
    rotation_key = int(round(math.degrees(float(rotation)))) % 360
    cache_key = (rgb, alpha, int(thickness), size_key, rotation_key)
    cached = _gear_icon_cache.get(cache_key)
    if cached is not None:
        return cached

    base = _render_gear_icon_base(
        rgb, thickness=thickness, compact=effective_size < 16
    )
    # The master gear's outside radius is 54 px on its 144 px canvas.
    scale = effective_size / 54.0
    if rotation_key:
        # Pygame rotates counter-clockwise; negate to preserve this
        # function's historic screen-coordinate (clockwise) convention.
        icon = pygame.transform.rotozoom(base, -rotation_key, scale)
    else:
        target = max(2, int(round(base.get_width() * scale)))
        icon = pygame.transform.smoothscale(base, (target, target))
    if alpha < 255:
        icon.set_alpha(alpha)
    _gear_icon_cache[cache_key] = icon
    return icon


def draw_gear_icon(surface, cx, cy, size, color, thickness=2, rotation=0.0):
    """Draw a polished, supersampled cog with beveled teeth and hub."""
    if size <= 0:
        return
    icon = _get_gear_icon(color, size, thickness=thickness, rotation=rotation)
    surface.blit(icon, icon.get_rect(center=(int(cx), int(cy))))


def _draw_new_game_icon(surface, cx, cy, radius, hover_amount, sparkle_phase):
    """Draw the New button's circled-plus badge and tiny hover sparkle."""
    cx, cy, radius = int(cx), int(cy), max(5, int(radius))

    # Layered badge: offset shadow, pale face, inner ring and dark rim.
    pygame.draw.circle(surface, (17, 45, 27), (cx, cy + 1), radius)
    pygame.draw.circle(surface, (233, 250, 237), (cx, cy), radius)
    pygame.draw.circle(
        surface, (196, 238, 207), (cx, cy), max(3, radius - 2)
    )
    pygame.draw.circle(surface, (24, 82, 43), (cx, cy), radius, 1)
    try:
        pygame.gfxdraw.aacircle(
            surface, cx, cy, radius, (24, 82, 43, 255)
        )
        pygame.gfxdraw.aacircle(
            surface,
            cx,
            cy,
            max(3, radius - 2),
            (112, 190, 132, 255),
        )
    except Exception:
        pass

    # A fine upper-left glint makes the badge read as a little enamelled
    # disc rather than a flat white circle.
    glint_x = cx - max(2, radius // 3)
    glint_y = cy - max(2, radius // 3)
    pygame.draw.circle(surface, (255, 255, 255), (glint_x, glint_y), 1)

    # Plus sign with a one-pixel lower shadow for a lightly embossed feel.
    arm = max(3, radius - 3)
    for y_offset, colour in ((1, (151, 205, 165)), (0, (24, 96, 49))):
        pygame.draw.line(
            surface,
            colour,
            (cx - arm, cy + y_offset),
            (cx + arm, cy + y_offset),
            2,
        )
        pygame.draw.line(
            surface,
            colour,
            (cx, cy - arm + y_offset),
            (cx, cy + arm + y_offset),
            2,
        )

    # A four-point sparkle perched on the badge's upper-right rim. It is
    # just a dot at rest and gently grows/pulses during hover.
    sparkle_cx = cx + radius - 1
    sparkle_cy = cy - radius + 1
    sparkle_r = 1.6 + 1.5 * hover_amount + (
        0.35 * math.sin(sparkle_phase) if hover_amount > 0.02 else 0.0
    )
    if sparkle_r >= 2.0:
        waist = sparkle_r * 0.30
        points = [
            (sparkle_cx, sparkle_cy - sparkle_r),
            (sparkle_cx + waist, sparkle_cy - waist),
            (sparkle_cx + sparkle_r, sparkle_cy),
            (sparkle_cx + waist, sparkle_cy + waist),
            (sparkle_cx, sparkle_cy + sparkle_r),
            (sparkle_cx - waist, sparkle_cy + waist),
            (sparkle_cx - sparkle_r, sparkle_cy),
            (sparkle_cx - waist, sparkle_cy - waist),
        ]
        pygame.draw.polygon(surface, (255, 239, 157), points)
        pygame.draw.polygon(surface, (122, 96, 22), points, 1)
    else:
        pygame.draw.circle(
            surface, (255, 239, 157), (sparkle_cx, sparkle_cy), 1
        )


def draw_new_game_button(screen, mouse_pos):
    """Draw the polished emerald New Game top-bar button."""
    global new_game_hover_amount, new_game_sparkle_phase
    if menu_anim_progress != 0:
        return

    rect = new_game_button_rect
    hovered = rect.collidepoint(mouse_pos)
    try:
        pressed = bool(hovered and pygame.mouse.get_pressed()[0])
    except Exception:
        pressed = False

    hover_target = 1.0 if hovered else 0.0
    new_game_hover_amount += (hover_target - new_game_hover_amount) * 0.22
    if abs(hover_target - new_game_hover_amount) < 0.01:
        new_game_hover_amount = hover_target
    hover_amount = max(0.0, min(1.0, new_game_hover_amount))
    if hovered:
        new_game_sparkle_phase = (
            new_game_sparkle_phase + 0.12
        ) % (2.0 * math.pi)

    visual_rect = rect.move(0, 1 if pressed else 0)

    # Emerald halo behind the primary action on hover.
    if hover_amount > 0.01:
        glow = pygame.Surface(
            (visual_rect.width + 16, visual_rect.height + 16),
            pygame.SRCALPHA,
        )
        for inset, alpha in ((1, 17), (4, 27), (7, 40)):
            pygame.draw.rect(
                glow,
                (74, 205, 120, int(alpha * hover_amount)),
                (
                    inset,
                    inset,
                    glow.get_width() - inset * 2,
                    glow.get_height() - inset * 2,
                ),
                border_radius=13 - inset,
            )
        screen.blit(glow, (visual_rect.x - 8, visual_rect.y - 8))

    # Ground shadow, tightened in the pressed state.
    pygame.draw.rect(
        screen,
        (14, 27, 19),
        visual_rect.move(0, 1 if pressed else 2),
        border_radius=9,
    )

    if pressed:
        top_colour = (45, 86, 59)
        bottom_colour = (31, 61, 42)
        border_colour = (105, 190, 129)
    elif hovered:
        top_colour = (94, 143, 109)
        bottom_colour = (52, 94, 67)
        border_colour = (152, 226, 172)
    else:
        top_colour = (73, 112, 87)
        bottom_colour = (42, 71, 54)
        border_colour = (116, 164, 132)

    # Rounded emerald-metal body. As with the settings button, translucent
    # sheen/shading is composited via an overlay to preserve the gradient's
    # alpha correctly, then everything is clipped with a rounded mask.
    body = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    denom = max(1, visual_rect.height - 1)
    for row in range(visual_rect.height):
        colour = _lerp_color(top_colour, bottom_colour, row / denom)
        pygame.draw.line(
            body,
            (*colour, 255),
            (0, row),
            (visual_rect.width - 1, row),
        )

    overlay = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    pygame.draw.polygon(
        overlay,
        (255, 255, 255, int(11 + 18 * hover_amount)),
        [
            (-8, visual_rect.height),
            (visual_rect.width * 0.43, 0),
            (visual_rect.width * 0.66, 0),
            (visual_rect.width * 0.12, visual_rect.height),
        ],
    )
    pygame.draw.line(
        overlay,
        (255, 255, 255, int(76 + 46 * hover_amount)),
        (6, 1),
        (visual_rect.width - 7, 1),
        1,
    )
    pygame.draw.line(
        overlay,
        (255, 255, 255, 24),
        (2, 5),
        (2, visual_rect.height - 6),
        1,
    )
    pygame.draw.rect(
        overlay,
        (0, 0, 0, 46 if pressed else 31),
        (0, visual_rect.height - 4, visual_rect.width, 4),
    )
    pygame.draw.rect(
        overlay,
        (0, 0, 0, 25),
        (visual_rect.width - 3, 4, 3, visual_rect.height - 8),
        border_radius=2,
    )
    body.blit(overlay, (0, 0))

    mask = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    mask.fill((0, 0, 0, 0))
    pygame.draw.rect(
        mask,
        (255, 255, 255, 255),
        mask.get_rect(),
        border_radius=9,
    )
    body.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
    screen.blit(body, visual_rect.topleft)

    pygame.draw.rect(screen, border_colour, visual_rect, 2, border_radius=9)
    pygame.draw.rect(
        screen,
        _lerp_color(border_colour, (255, 255, 255), 0.30),
        visual_rect.inflate(-3, -3),
        1,
        border_radius=7,
    )

    # Brightening bottom accent, drawn behind the badge/text.
    if hover_amount > 0.04:
        accent_colour = _lerp_color(
            bottom_colour, (142, 237, 171), hover_amount
        )
        accent_w = int(26 * hover_amount)
        if accent_w >= 2:
            pygame.draw.line(
                screen,
                accent_colour,
                (visual_rect.centerx - accent_w // 2, visual_rect.bottom - 3),
                (visual_rect.centerx + accent_w // 2, visual_rect.bottom - 3),
                2,
            )

    # Badge + label are laid out as one centred group. The fallback steps
    # keep the group inside the fixed 50 px button with wider system fonts.
    text_colour = _lerp_color(
        (226, 246, 231), (255, 255, 255), hover_amount
    )
    text = font_small.render("New", True, text_colour)
    icon_radius = 7
    gap = 4
    group_w = icon_radius * 2 + gap + text.get_width()
    if group_w > visual_rect.width - 5:
        icon_radius = 6
        gap = 2
        group_w = icon_radius * 2 + gap + text.get_width()
    if group_w > visual_rect.width - 4:
        text = font_tiny.render("New", True, text_colour)
        group_w = icon_radius * 2 + gap + text.get_width()

    group_x = visual_rect.centerx - group_w // 2
    icon_cx = group_x + icon_radius
    _draw_new_game_icon(
        screen,
        icon_cx,
        visual_rect.centery,
        icon_radius,
        hover_amount,
        new_game_sparkle_phase,
    )

    text_rect = text.get_rect(
        midleft=(group_x + icon_radius * 2 + gap, visual_rect.centery)
    )
    shadow_text = font_small.render("New", True, (17, 45, 27))
    if shadow_text.get_width() != text.get_width():
        shadow_text = font_tiny.render("New", True, (17, 45, 27))
    screen.blit(shadow_text, text_rect.move(0, 1))
    screen.blit(text, text_rect)

    # Tooltip below the top bar, matching Settings/Flip. Suppressed while
    # the New Game confirmation or game-over dialog owns the screen.
    if (
        hovered
        and not pressed
        and not show_newgame_confirm
        and not show_game_over
    ):
        tip = font_tiny.render("Start a new game", True, TEXT_COLOR)
        tip_w = tip.get_width() + 16
        tip_h = 24
        tip_x = max(
            4,
            min(visual_rect.centerx - tip_w // 2, WIDTH - tip_w - 4),
        )
        tip_y = min(visual_rect.bottom + 7, HEIGHT - tip_h - 4)
        tip_rect = pygame.Rect(tip_x, tip_y, tip_w, tip_h)
        pygame.draw.rect(screen, (22, 28, 24), tip_rect, border_radius=7)
        pygame.draw.rect(
            screen, (152, 226, 172), tip_rect, 1, border_radius=7
        )
        pointer_x = max(
            tip_rect.x + 8,
            min(visual_rect.centerx, tip_rect.right - 8),
        )
        pygame.draw.polygon(
            screen,
            (22, 28, 24),
            [
                (pointer_x - 5, tip_rect.y + 1),
                (pointer_x + 5, tip_rect.y + 1),
                (pointer_x, max(visual_rect.bottom + 1, tip_rect.y - 5)),
            ],
        )
        screen.blit(tip, tip.get_rect(center=tip_rect.center))


def draw_settings_button(screen, mouse_pos):
    # Only visible when menu is closed — opening animation is handled entirely
    # by draw_settings_menu (single backdrop), avoiding a double-fade / double
    # overlay that previously caused flicker and a competing expanding border.
    global settings_gear_rotation, settings_gear_hover_amount
    if menu_anim_progress > 0:
        return

    rect = settings_button_rect
    hovered = rect.collidepoint(mouse_pos)
    try:
        pressed = bool(hovered and pygame.mouse.get_pressed()[0])
    except Exception:
        pressed = False

    # Ease the hover treatment and turn the cog slowly while it is under
    # the cursor. Rotation stops where it is when the pointer leaves —
    # snapping back to 0° would look like a visual glitch.
    hover_target = 1.0 if hovered else 0.0
    settings_gear_hover_amount += (
        hover_target - settings_gear_hover_amount
    ) * 0.22
    if abs(hover_target - settings_gear_hover_amount) < 0.01:
        settings_gear_hover_amount = hover_target
    hover_amount = max(0.0, min(1.0, settings_gear_hover_amount))
    if hovered:
        settings_gear_rotation = (
            settings_gear_rotation + (0.003 if pressed else 0.010)
        ) % (2.0 * math.pi)

    visual_rect = rect.move(0, 1 if pressed else 0)

    # Cool blue halo that fades in behind the button on hover.
    if hover_amount > 0.01:
        glow = pygame.Surface(
            (visual_rect.width + 16, visual_rect.height + 16), pygame.SRCALPHA
        )
        for inset, alpha in ((1, 18), (4, 28), (7, 42)):
            pygame.draw.rect(
                glow,
                (105, 145, 255, int(alpha * hover_amount)),
                (
                    inset,
                    inset,
                    glow.get_width() - inset * 2,
                    glow.get_height() - inset * 2,
                ),
                border_radius=13 - inset,
            )
        screen.blit(glow, (visual_rect.x - 8, visual_rect.y - 8))

    # Ground shadow — tighter when pressed.
    pygame.draw.rect(
        screen,
        (16, 18, 22),
        visual_rect.move(0, 1 if pressed else 2),
        border_radius=9,
    )

    # Vertical metal gradient, built on a small surface so the glass sheen
    # and shading can be clipped to the rounded button silhouette.
    if pressed:
        top_colour = (58, 64, 78)
        bottom_colour = (40, 45, 57)
        border_colour = (116, 145, 215)
    elif hovered:
        top_colour = (104, 116, 139)
        bottom_colour = (63, 72, 92)
        border_colour = (157, 183, 255)
    else:
        top_colour = (83, 89, 102)
        bottom_colour = (51, 55, 65)
        border_colour = (126, 134, 150)

    body = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    denom = max(1, visual_rect.height - 1)
    for row in range(visual_rect.height):
        colour = _lerp_color(top_colour, bottom_colour, row / denom)
        pygame.draw.line(
            body,
            (*colour, 255),
            (0, row),
            (visual_rect.width - 1, row),
        )

    # Translucent detail belongs on an overlay, then alpha-blends over the
    # gradient (direct alpha drawing on ``body`` can replace its alpha).
    overlay = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    sheen_alpha = int(12 + 18 * hover_amount)
    pygame.draw.polygon(
        overlay,
        (255, 255, 255, sheen_alpha),
        [
            (-8, visual_rect.height),
            (visual_rect.width * 0.46, 0),
            (visual_rect.width * 0.68, 0),
            (visual_rect.width * 0.14, visual_rect.height),
        ],
    )
    pygame.draw.line(
        overlay,
        (255, 255, 255, int(72 + 48 * hover_amount)),
        (6, 1),
        (visual_rect.width - 7, 1),
        1,
    )
    pygame.draw.line(
        overlay,
        (255, 255, 255, 22),
        (2, 5),
        (2, visual_rect.height - 6),
        1,
    )
    pygame.draw.rect(
        overlay,
        (0, 0, 0, 44 if pressed else 30),
        (0, visual_rect.height - 4, visual_rect.width, 4),
    )
    pygame.draw.rect(
        overlay,
        (0, 0, 0, 24),
        (visual_rect.width - 3, 4, 3, visual_rect.height - 8),
        border_radius=2,
    )
    body.blit(overlay, (0, 0))

    mask = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    mask.fill((0, 0, 0, 0))
    pygame.draw.rect(
        mask,
        (255, 255, 255, 255),
        mask.get_rect(),
        border_radius=9,
    )
    body.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
    screen.blit(body, visual_rect.topleft)

    pygame.draw.rect(screen, border_colour, visual_rect, 2, border_radius=9)
    pygame.draw.rect(
        screen,
        _lerp_color(border_colour, (255, 255, 255), 0.30),
        visual_rect.inflate(-3, -3),
        1,
        border_radius=7,
    )

    # A short accent under the cog gives the hover state a finished,
    # deliberate endpoint without adding text inside the 50 px button.
    if hover_amount > 0.04:
        accent_colour = _lerp_color(
            bottom_colour, (126, 178, 255), hover_amount
        )
        accent_w = int(22 * hover_amount)
        if accent_w >= 2:
            pygame.draw.line(
                screen,
                accent_colour,
                (visual_rect.centerx - accent_w // 2, visual_rect.bottom - 3),
                (visual_rect.centerx + accent_w // 2, visual_rect.bottom - 3),
                2,
            )

    gear_colour = _lerp_color(
        GEAR_COLOR, GEAR_COLOR_HOVER, hover_amount
    )
    gear_size = 11.1 + 0.6 * hover_amount - (0.3 if pressed else 0.0)
    draw_gear_icon(
        screen,
        visual_rect.centerx,
        visual_rect.centery,
        gear_size,
        gear_colour,
        thickness=2,
        rotation=settings_gear_rotation,
    )

    # Matching top-bar tooltip language with Flip/eval hints: place it
    # below the button (never clipped by the top edge) and clamp sideways.
    if hovered and not pressed:
        tip = font_tiny.render("Settings", True, TEXT_COLOR)
        tip_w = tip.get_width() + 16
        tip_h = 24
        tip_x = max(
            4,
            min(
                visual_rect.centerx - tip_w // 2,
                WIDTH - tip_w - 4,
            ),
        )
        tip_y = min(visual_rect.bottom + 7, HEIGHT - tip_h - 4)
        tip_rect = pygame.Rect(tip_x, tip_y, tip_w, tip_h)
        pygame.draw.rect(screen, (22, 22, 28), tip_rect, border_radius=7)
        pygame.draw.rect(
            screen, (157, 183, 255), tip_rect, 1, border_radius=7
        )
        pointer_x = max(
            tip_rect.x + 8,
            min(visual_rect.centerx, tip_rect.right - 8),
        )
        pygame.draw.polygon(
            screen,
            (22, 22, 28),
            [
                (pointer_x - 5, tip_rect.y + 1),
                (pointer_x + 5, tip_rect.y + 1),
                (pointer_x, max(visual_rect.bottom + 1, tip_rect.y - 5)),
            ],
        )
        screen.blit(tip, tip.get_rect(center=tip_rect.center))


def navigate_to(page, forward=True):
    """Start a page transition. Both pages are drawn sliding together
    (see draw_settings_menu) instead of one sliding fully out before the
    next slides in."""
    global menu_transition_progress, menu_transition_direction
    global settings_menu_target, settings_menu_from
    if menu_transition_direction != "none" or page == settings_menu_state:
        return
    settings_menu_from = settings_menu_state
    settings_menu_target = page
    menu_transition_progress = 0.0
    menu_transition_direction = "forward" if forward else "back"


def navigate_back():
    parent = SETTINGS_PAGE_PARENT.get(settings_menu_state)
    if parent:
        navigate_to(parent, forward=False)


def settings_breadcrumb(page):
    """['Settings', 'Board', 'Board Themes'] style path for a page."""
    path = []
    cur = page
    while cur:
        path.append(SETTINGS_PAGE_TITLES.get(cur, cur))
        cur = SETTINGS_PAGE_PARENT.get(cur)
    return list(reversed(path))


def update_menu_transition():
    global menu_transition_progress, menu_transition_direction, settings_menu_state
    global settings_menu_target, settings_menu_from

    if menu_transition_direction != "none":
        menu_transition_progress += menu_transition_speed
        if menu_transition_progress >= 1.0:
            settings_menu_state = settings_menu_target
            settings_menu_from = settings_menu_state
            menu_transition_progress = 0.0
            menu_transition_direction = "none"


def _pieces_style_source(theme_id):
    return "Chess.com" if str(theme_id).startswith("chesscom/") else "Built-in"


def _pieces_style_layout(themes, offset_x=0, offset_y=0):
    """Shared Pieces Styles geometry for drawing, clicking and scrolling.

    Keeping these calculations in one place prevents the visual cards and
    their hit-boxes drifting apart when the menu layout changes.
    """
    cols = 2
    rows = (len(themes) + cols - 1) // cols
    viewport_h = THEME_LIST_BOTTOM - THEME_LIST_TOP
    total_h = max(
        1,
        rows * THEME_ITEM_HEIGHT + max(0, rows - 1) * THEME_ITEM_GAP,
    )
    max_scroll = max(0, total_h - viewport_h)
    scroll = max(0.0, min(float(theme_scroll_offset), float(max_scroll)))
    top = THEME_LIST_TOP + offset_y
    viewport = pygame.Rect(WIDTH // 2 - 200 + offset_x, top, 400, viewport_h)
    rects = []
    for i, (theme_id, display_name) in enumerate(themes):
        row, col = divmod(i, cols)
        rect = pygame.Rect(
            viewport.x + col * (THEME_ITEM_WIDTH + THEME_COL_GAP),
            top + row * (THEME_ITEM_HEIGHT + THEME_ITEM_GAP) - int(scroll),
            THEME_ITEM_WIDTH,
            THEME_ITEM_HEIGHT,
        )
        rects.append((theme_id, display_name, rect))
    return viewport, rects, total_h, max_scroll, scroll


def _draw_style_mini_preview(screen, theme_id, x, y):
    """Draw a white queen / black knight sample on two board squares."""
    previews = get_preview_images(theme_id)
    samples = (("wq", LIGHT_SQ), ("bn", DARK_SQ))
    for index, (piece_key, square_colour) in enumerate(samples):
        square = pygame.Rect(x + index * 31, y, 30, 30)
        pygame.draw.rect(screen, square_colour, square, border_radius=5)
        pygame.draw.rect(screen, (28, 28, 32), square, 1, border_radius=5)
        image = previews.get(piece_key)
        if image is not None:
            screen.blit(image, (square.x + 1, square.y + 1))


def draw_pieces_style_content(screen, offset_x=0, offset_y=0):
    global current_theme, theme_scroll_offset

    mx, my = pygame.mouse.get_pos()
    themes = get_available_themes()

    # --- Preview panel (reserved section) ---
    preview_rect = pygame.Rect(
        WIDTH // 2 - 200 + offset_x,
        THEME_PREVIEW_Y + offset_y,
        400,
        THEME_PREVIEW_H,
    )
    pygame.draw.rect(screen, (34, 34, 40), preview_rect.move(0, 3), border_radius=12)
    pygame.draw.rect(screen, (48, 48, 58), preview_rect, border_radius=12)
    pygame.draw.rect(screen, (120, 150, 220), preview_rect, 2, border_radius=12)

    viewport, style_rects, total_h, max_scroll, scroll = _pieces_style_layout(
        themes, offset_x, offset_y
    )
    theme_scroll_offset = scroll
    top = viewport.y
    viewport_h = viewport.height

    hovered_theme = None
    hovered_display = None
    if viewport.collidepoint(mx, my) and menu_transition_direction == "none":
        for tid, dname, rect in style_rects:
            if rect.collidepoint(mx, my):
                hovered_theme = tid
                hovered_display = dname
                break

    preview_theme = hovered_theme if hovered_theme is not None else current_theme
    preview_display = hovered_display if hovered_display is not None else next((d for tid, d in themes if tid == current_theme), current_theme)
    # The preview follows the hovered card, falling back to the active
    # style. A status pill makes it clear whether clicking would change it.
    title_s = font_tiny.render("STYLE PREVIEW", True, TEXT_MUTED)
    screen.blit(title_s, (preview_rect.x + 16, preview_rect.y + 10))
    preview_is_current = preview_theme == current_theme
    status_text = "Current style" if preview_is_current else "Click card to apply"
    status_s = font_tiny.render(status_text, True, (20, 34, 24) if preview_is_current else TEXT_COLOR)
    status_rect = pygame.Rect(
        preview_rect.right - status_s.get_width() - 24,
        preview_rect.y + 8,
        status_s.get_width() + 16,
        20,
    )
    pygame.draw.rect(
        screen,
        (126, 214, 148) if preview_is_current else (82, 82, 108),
        status_rect,
        border_radius=10,
    )
    screen.blit(status_s, status_s.get_rect(center=status_rect.center))

    display_text = _elide_text(preview_display, font_small, 245)
    name_s = font_small.render(display_text, True, TEXT_ACCENT if hovered_theme else TEXT_COLOR)
    screen.blit(name_s, (preview_rect.x + 16, preview_rect.y + 28))
    source_s = font_tiny.render(
        f"{_pieces_style_source(preview_theme)} piece set",
        True,
        TEXT_MUTED,
    )
    screen.blit(source_s, (preview_rect.x + 16, preview_rect.y + 48))

    # Draw all 12 piece types on miniature board-coloured squares. Using
    # the active board palette shows how the set will actually look in play.
    prev_imgs = get_preview_images(preview_theme)
    order = ["wk", "wq", "wr", "wb", "wn", "wp", "bk", "bq", "br", "bb", "bn", "bp"]
    piece_sz = 28
    gap_px = 5
    per_row = 6
    row_w = per_row * piece_sz + (per_row - 1) * gap_px
    start_x = preview_rect.centerx - row_w // 2
    for idx, pkey in enumerate(order):
        row = idx // per_row
        col = idx % per_row
        px = start_x + col * (piece_sz + gap_px)
        py = preview_rect.y + 64 + row * (piece_sz + gap_px)
        bg = LIGHT_SQ if (row + col) % 2 == 0 else DARK_SQ
        pygame.draw.rect(screen, bg, (px, py, piece_sz, piece_sz), border_radius=4)
        pygame.draw.rect(screen, (25, 25, 30), (px, py, piece_sz, piece_sz), 1, border_radius=4)
        if pkey in prev_imgs:
            screen.blit(prev_imgs[pkey], (px, py))

    count_text = f"{len(themes)} style{'s' if len(themes) != 1 else ''} available"
    count_s = font_tiny.render(count_text, True, TEXT_MUTED)
    screen.blit(count_s, (preview_rect.x, preview_rect.bottom + 8))
    if max_scroll > 0:
        scroll_s = font_tiny.render("Scroll to browse", True, TEXT_MUTED)
        screen.blit(
            scroll_s,
            (
                preview_rect.right - scroll_s.get_width(),
                preview_rect.bottom + 8,
            ),
        )

    # --- Two-column visual style cards ---
    screen.set_clip(viewport)
    for theme_id, display_name, theme_rect in style_rects:
        if not theme_rect.colliderect(viewport):
            continue

        is_current = theme_id == current_theme
        is_hovered = (
            theme_id == hovered_theme
            and viewport.collidepoint(mx, my)
            and menu_transition_direction == "none"
        )

        if is_current:
            bg_color = (79, 94, 116) if is_hovered else (64, 78, 98)
            border_color = (126, 214, 148)
        elif is_hovered:
            bg_color = (76, 76, 102)
            border_color = (150, 170, 255)
        else:
            bg_color = (55, 55, 65)
            border_color = MENU_BORDER_COLOR

        # A subtle offset shadow plus a stronger selected border gives the
        # cards depth without changing their click geometry.
        pygame.draw.rect(screen, (28, 28, 33), theme_rect.move(0, 2), border_radius=10)
        pygame.draw.rect(screen, bg_color, theme_rect, border_radius=10)
        pygame.draw.rect(
            screen,
            border_color,
            theme_rect,
            2 if is_current or is_hovered else 1,
            border_radius=10,
        )
        if is_current:
            accent = pygame.Rect(theme_rect.x, theme_rect.y + 10, 4, theme_rect.height - 20)
            pygame.draw.rect(screen, (126, 214, 148), accent, border_radius=2)

        _draw_style_mini_preview(
            screen,
            theme_id,
            theme_rect.x + 10,
            theme_rect.centery - 15,
        )

        text_x = theme_rect.x + 82
        name_text = _elide_text(display_name, font_small, theme_rect.right - text_x - 8)
        name_s = font_small.render(name_text, True, TEXT_COLOR)
        screen.blit(name_s, (text_x, theme_rect.y + 10))

        detail = "Current style" if is_current else _pieces_style_source(theme_id)
        detail_colour = (
            (126, 214, 148)
            if is_current
            else TEXT_ACCENT if is_hovered else TEXT_MUTED
        )
        detail_s = font_tiny.render(detail, True, detail_colour)
        screen.blit(detail_s, (text_x, theme_rect.y + 35))
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
    """Scroll the Pieces Styles cards (positive delta moves towards the end)."""
    global theme_scroll_offset
    _viewport, _rects, _total_h, max_scroll, _scroll = _pieces_style_layout(
        get_available_themes()
    )
    theme_scroll_offset = max(0.0, min(theme_scroll_offset + delta, max_scroll))


# --- Board hub (Pieces Styles / Board Themes) ---
_board_button_piece_preview_cache = {}


def _board_button_current_names():
    """Return current ``(pieces_style_name, board_theme_name)`` read-only."""
    piece_name = None
    try:
        piece_name = next(
            (
                display
                for theme_id, display in available_themes
                if theme_id == current_theme
            ),
            None,
        )
    except Exception:
        piece_name = None
    if not piece_name:
        piece_name = format_theme_name(
            str(current_theme or "classic").split("/")[-1]
        )

    board_id = normalise_board_theme(current_board_theme)
    board_name = BOARD_THEMES.get(board_id, {}).get("display", board_id)
    return str(piece_name), str(board_name)


def _get_board_button_piece_preview(piece_key, size):
    """Return a cached small current-style piece preview, or ``None``."""
    size = max(1, int(size))
    cache_key = (str(current_theme), str(piece_key), size)
    if cache_key in _board_button_piece_preview_cache:
        return _board_button_piece_preview_cache[cache_key]

    preview = None
    try:
        source = get_preview_images(current_theme).get(piece_key)
        if source is not None:
            if source.get_size() == (size, size):
                preview = source
            else:
                preview = pygame.transform.smoothscale(
                    source, (size, size)
                )
    except Exception:
        preview = None
    _board_button_piece_preview_cache[cache_key] = preview
    return preview


def _draw_board_button_mini_board(surface, rect, hover_amount=0.0, phase=0.0):
    """Draw a framed 4x4 board in the active square colours.

    On hover a warm scan highlight walks the sixteen squares in a
    boustrophedon (snake) path. The helper is size-agnostic so tests can
    render the same miniature at a larger inspection size.
    """
    hover_amount = max(0.0, min(1.0, hover_amount))
    light_colour = tuple(int(v) for v in LIGHT_SQ[:3])
    dark_colour = tuple(int(v) for v in DARK_SQ[:3])
    frame_colour = _lerp_color((111, 82, 48), (176, 132, 78), hover_amount)
    frame_colour = _lerp_color(frame_colour, dark_colour, 0.22)

    pygame.draw.rect(
        surface, (24, 17, 10), rect.move(0, 1), border_radius=4
    )
    pygame.draw.rect(surface, frame_colour, rect, border_radius=4)
    pygame.draw.rect(
        surface,
        _lerp_color(frame_colour, (255, 255, 255), 0.28),
        rect,
        1,
        border_radius=4,
    )

    inner_size = min(rect.width, rect.height) - 4
    inner_size -= inner_size % 4
    inner_size = max(8, inner_size)
    inner = pygame.Rect(0, 0, inner_size, inner_size)
    inner.center = rect.center
    cell = inner_size // 4

    for row in range(4):
        for col in range(4):
            colour = light_colour if (row + col) % 2 == 0 else dark_colour
            pygame.draw.rect(
                surface,
                colour,
                (inner.x + col * cell, inner.y + row * cell, cell, cell),
            )

    # Translucent scan/sheen belongs on a separate overlay, then the grid
    # and frame are restored above it.
    overlay = pygame.Surface(inner.size, pygame.SRCALPHA)
    if hover_amount > 0.03:
        scan_index = int(
            ((phase / (2.0 * math.pi)) % 1.0) * 16
        ) % 16
        scan_row = scan_index // 4
        scan_offset = scan_index % 4
        scan_col = scan_offset if scan_row % 2 == 0 else 3 - scan_offset
        scan_alpha = int(35 + 65 * hover_amount)
    else:
        scan_row, scan_col = 1, 1
        scan_alpha = 24
    scan_rect = pygame.Rect(
        scan_col * cell, scan_row * cell, cell, cell
    )
    pygame.draw.rect(
        overlay, (255, 240, 165, scan_alpha), scan_rect
    )
    pygame.draw.rect(
        overlay,
        (255, 255, 255, int(55 + 70 * hover_amount)),
        scan_rect,
        1,
    )
    pygame.draw.polygon(
        overlay,
        (255, 255, 255, int(13 + 12 * hover_amount)),
        [
            (-4, inner_size),
            (inner_size * 0.38, 0),
            (inner_size * 0.62, 0),
            (inner_size * 0.18, inner_size),
        ],
    )
    surface.blit(overlay, inner.topleft)

    grid_colour = _lerp_color(dark_colour, (35, 25, 16), 0.55)
    for i in range(1, 4):
        pygame.draw.line(
            surface,
            grid_colour,
            (inner.x + i * cell, inner.y),
            (inner.x + i * cell, inner.bottom - 1),
            1,
        )
        pygame.draw.line(
            surface,
            grid_colour,
            (inner.x, inner.y + i * cell),
            (inner.right - 1, inner.y + i * cell),
            1,
        )
        # Fine coordinate ticks on the lower/left frame.
        pygame.draw.line(
            surface,
            _lerp_color(frame_colour, (255, 255, 255), 0.35),
            (inner.x + i * cell, rect.bottom - 3),
            (inner.x + i * cell, rect.bottom - 1),
            1,
        )
        pygame.draw.line(
            surface,
            _lerp_color(frame_colour, (255, 255, 255), 0.35),
            (rect.x + 1, inner.y + i * cell),
            (rect.x + 3, inner.y + i * cell),
            1,
        )
    pygame.draw.rect(surface, (35, 25, 16), inner, 1)


def _draw_board_button_chevron(surface, cx, cy, colour):
    """Draw the Board row's forward chevron with a soft shadow."""
    points = [(cx - 4, cy - 7), (cx + 3, cy), (cx - 4, cy + 7)]
    shadow = [(px, py + 1) for px, py in points]
    pygame.draw.lines(surface, (25, 19, 12), False, shadow, 3)
    pygame.draw.lines(surface, colour, False, points, 3)
    try:
        pygame.draw.aalines(surface, colour, False, points)
    except Exception:
        pass


def draw_polished_board_button(screen, rect, hovered):
    """Draw the Settings-main Board navigation row.

    The caller supplies the original 400x50 row, including entrance slide,
    and retains click/navigation behaviour. This renderer reads only the
    active board colours and current piece/style names for its preview.
    """
    global board_button_hover_amount, board_button_pulse_phase

    try:
        pressed = bool(hovered and pygame.mouse.get_pressed()[0])
    except Exception:
        pressed = False

    hover_target = 1.0 if hovered else 0.0
    board_button_hover_amount += (
        hover_target - board_button_hover_amount
    ) * 0.22
    if abs(hover_target - board_button_hover_amount) < 0.01:
        board_button_hover_amount = hover_target
    hover_amount = max(0.0, min(1.0, board_button_hover_amount))
    if hovered:
        board_button_pulse_phase = (
            board_button_pulse_phase + (0.04 if pressed else 0.09)
        ) % (2.0 * math.pi)

    visual_rect = rect.move(0, 1 if pressed else 0)
    light_colour = tuple(int(v) for v in LIGHT_SQ[:3])
    dark_colour = tuple(int(v) for v in DARK_SQ[:3])
    piece_name, board_name = _board_button_current_names()

    # The halo is mixed from the active board colours, so a Chess.com
    # green board gives the row a subtly different aura from Classic wood.
    if hover_amount > 0.01:
        glow_colour = _lerp_color(dark_colour, light_colour, 0.38)
        glow = pygame.Surface(
            (visual_rect.width + 18, visual_rect.height + 18),
            pygame.SRCALPHA,
        )
        for inset, alpha in ((1, 15), (4, 24), (7, 36)):
            pygame.draw.rect(
                glow,
                (*glow_colour, int(alpha * hover_amount)),
                (
                    inset,
                    inset,
                    glow.get_width() - inset * 2,
                    glow.get_height() - inset * 2,
                ),
                border_radius=16 - inset,
            )
        screen.blit(glow, (visual_rect.x - 9, visual_rect.y - 9))

    pygame.draw.rect(
        screen,
        (24, 18, 11),
        visual_rect.move(0, 1 if pressed else 2),
        border_radius=10,
    )

    if pressed:
        base_top, base_bottom = (70, 61, 48), (46, 40, 31)
    elif hovered:
        base_top, base_bottom = (104, 91, 70), (66, 57, 43)
    else:
        base_top, base_bottom = (84, 74, 58), (54, 48, 38)
    top_colour = _lerp_color(base_top, dark_colour, 0.16)
    bottom_colour = _lerp_color(base_bottom, dark_colour, 0.16)
    border_colour = _lerp_color((196, 164, 116), dark_colour, 0.22)
    border_colour = _lerp_color(
        border_colour, light_colour, 0.24 * hover_amount
    )

    # Warm bronze/charcoal body with glass and a faint checker watermark.
    body = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    denom = max(1, visual_rect.height - 1)
    for row in range(visual_rect.height):
        colour = _lerp_color(top_colour, bottom_colour, row / denom)
        pygame.draw.line(
            body,
            (*colour, 255),
            (0, row),
            (visual_rect.width - 1, row),
        )

    overlay = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    pygame.draw.polygon(
        overlay,
        (255, 255, 255, int(9 + 14 * hover_amount)),
        [
            (-20, visual_rect.height),
            (visual_rect.width * 0.36, 0),
            (visual_rect.width * 0.55, 0),
            (visual_rect.width * 0.13, visual_rect.height),
        ],
    )
    pygame.draw.line(
        overlay,
        (255, 255, 255, int(62 + 44 * hover_amount)),
        (9, 1),
        (visual_rect.width - 10, 1),
        1,
    )
    pygame.draw.line(
        overlay,
        (255, 255, 255, 21),
        (2, 7),
        (2, visual_rect.height - 8),
        1,
    )
    pygame.draw.rect(
        overlay,
        (0, 0, 0, 42 if pressed else 29),
        (0, visual_rect.height - 4, visual_rect.width, 4),
    )
    pygame.draw.rect(
        overlay,
        (0, 0, 0, 24),
        (visual_rect.width - 3, 6, 3, visual_rect.height - 12),
        border_radius=2,
    )

    # Faint checker fields in the blank spaces either side of the title.
    checker_alpha = int(10 + 13 * hover_amount)
    for x_start in (58, 252):
        for row in range(4):
            for col in range(5):
                if (row + col) % 2 == 0:
                    pygame.draw.rect(
                        overlay,
                        (255, 244, 214, checker_alpha),
                        (x_start + col * 10, 6 + row * 10, 10, 10),
                    )
    body.blit(overlay, (0, 0))

    mask = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    mask.fill((0, 0, 0, 0))
    pygame.draw.rect(
        mask,
        (255, 255, 255, 255),
        mask.get_rect(),
        border_radius=10,
    )
    body.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
    screen.blit(body, visual_rect.topleft)

    pygame.draw.rect(screen, border_colour, visual_rect, 2, border_radius=10)
    pygame.draw.rect(
        screen,
        _lerp_color(border_colour, (255, 255, 255), 0.30),
        visual_rect.inflate(-3, -3),
        1,
        border_radius=8,
    )

    # Short checker inlays along the bottom, split to avoid the subtitle.
    inlay_y = visual_rect.bottom - 5
    for segment_x in (visual_rect.x + 56, visual_rect.x + 304):
        for i in range(8):
            colour = light_colour if i % 2 == 0 else dark_colour
            colour = _lerp_color(bottom_colour, colour, 0.38)
            pygame.draw.rect(
                screen,
                colour,
                (segment_x + i * 5, inlay_y, 5, 3),
            )

    mini_board_rect = pygame.Rect(
        visual_rect.x + 13, visual_rect.y + 7, 36, 36
    )
    _draw_board_button_mini_board(
        screen,
        mini_board_rect,
        hover_amount,
        board_button_pulse_phase,
    )

    # Two-line centred label: destination above, current selections below.
    label = "Board"
    title_colour = _lerp_color(
        (244, 238, 226), (255, 255, 255), hover_amount
    )
    title = font_large.render(label, True, title_colour)
    title_rect = title.get_rect(
        center=(visual_rect.centerx, visual_rect.centery - 9)
    )
    title_shadow = font_large.render(label, True, (24, 18, 11))
    screen.blit(title_shadow, title_rect.move(0, 1))
    screen.blit(title, title_rect)

    subtitle_text = f"Pieces: {piece_name} · Board: {board_name}"
    subtitle_text = _elide_text(subtitle_text, font_tiny, 204)
    subtitle_colour = _lerp_color(
        (202, 190, 168), light_colour, 0.38 * hover_amount
    )
    subtitle = font_tiny.render(subtitle_text, True, subtitle_colour)
    subtitle_rect = subtitle.get_rect(
        center=(visual_rect.centerx, visual_rect.centery + 15)
    )
    subtitle_shadow = font_tiny.render(
        subtitle_text, True, (24, 18, 11)
    )
    screen.blit(subtitle_shadow, subtitle_rect.move(0, 1))
    screen.blit(subtitle, subtitle_rect)

    # Right-hand destination summary: current piece pair over a board-
    # theme strip. The pieces bob one pixel on hover; the strip colours
    # themselves remain the exact active LIGHT_SQ/DARK_SQ pair.
    detail_x = visual_rect.right - 96
    for index, (piece_key, square_colour) in enumerate(
        (("wq", light_colour), ("bn", dark_colour))
    ):
        square = pygame.Rect(
            detail_x + index * 22, visual_rect.centery - 19, 20, 18
        )
        pygame.draw.rect(
            screen, (24, 18, 11), square.move(0, 1), border_radius=3
        )
        pygame.draw.rect(screen, square_colour, square, border_radius=3)
        pygame.draw.rect(
            screen,
            _lerp_color(square_colour, (35, 25, 16), 0.45),
            square,
            1,
            border_radius=3,
        )
        preview = _get_board_button_piece_preview(piece_key, 17)
        if preview is not None:
            bob = int(
                round(
                    math.sin(
                        board_button_pulse_phase + index * math.pi / 2.0
                    )
                    * hover_amount
                )
            )
            screen.blit(
                preview,
                preview.get_rect(
                    center=(square.centerx, square.centery + bob)
                ),
            )

    theme_strip = pygame.Rect(
        detail_x, visual_rect.centery + 3, 42, 14
    )
    pygame.draw.rect(
        screen, (24, 18, 11), theme_strip.move(0, 1), border_radius=3
    )
    cell_widths = (11, 10, 11, 10)
    cell_x = theme_strip.x
    for index, cell_w in enumerate(cell_widths):
        pygame.draw.rect(
            screen,
            light_colour if index % 2 == 0 else dark_colour,
            (cell_x, theme_strip.y, cell_w, theme_strip.height),
        )
        cell_x += cell_w
    if hover_amount > 0.03:
        scan_x = int(
            theme_strip.x
            + (
                (board_button_pulse_phase / (2.0 * math.pi)) % 1.0
            )
            * theme_strip.width
        )
        pygame.draw.line(
            screen,
            _lerp_color(light_colour, (255, 255, 255), 0.45),
            (scan_x, theme_strip.y + 1),
            (scan_x, theme_strip.bottom - 2),
            1,
        )
    pygame.draw.rect(
        screen, (35, 25, 16), theme_strip, 1, border_radius=3
    )

    chevron_colour = _lerp_color(
        (218, 201, 171), (255, 255, 255), hover_amount
    )
    chevron_x = (
        visual_rect.right
        - 24
        + int(round(2 * hover_amount))
        - (1 if pressed else 0)
    )
    _draw_board_button_chevron(
        screen,
        chevron_x,
        visual_rect.centery,
        chevron_colour,
    )


def get_board_hub_rects(offset_x=0, offset_y=0):
    w, h = 420, 104
    x = WIDTH // 2 - w // 2 + offset_x
    return {
        "pieces_style": pygame.Rect(x, 190 + offset_y, w, h),
        "board_themes": pygame.Rect(x, 318 + offset_y, w, h),
    }


def _draw_board_hub_card(screen, rect, title, subtitle, hovered):
    bg = (82, 82, 125) if hovered else (58, 58, 68)
    pygame.draw.rect(screen, bg, rect, border_radius=12)
    pygame.draw.rect(screen, (150, 170, 255) if hovered else MENU_BORDER_COLOR, rect, 2, border_radius=12)
    title_s = font_large.render(title, True, TEXT_COLOR)
    screen.blit(title_s, (rect.x + 150, rect.y + 18))
    sub_s = font_tiny.render(subtitle, True, TEXT_ACCENT if hovered else TEXT_MUTED)
    screen.blit(sub_s, (rect.x + 150, rect.y + 54))
    hint_s = font_tiny.render("Open", True, TEXT_MUTED)
    screen.blit(hint_s, (rect.x + 150, rect.y + 76))
    chev = font_large.render("›", True, (200, 210, 255) if hovered else TEXT_MUTED)
    screen.blit(chev, chev.get_rect(center=(rect.right - 26, rect.centery)))


def draw_board_hub_content(screen, offset_x=0, offset_y=0):
    mx, my = pygame.mouse.get_pos()
    idle = menu_transition_direction == "none"
    intro = font_small.render("Choose what to customise", True, TEXT_MUTED)
    screen.blit(intro, (WIDTH // 2 - intro.get_width() // 2 + offset_x, 142 + offset_y))

    rects = get_board_hub_rects(offset_x, offset_y)
    piece_name = next((d for tid, d in get_available_themes() if tid == current_theme), current_theme)
    pieces_rect = rects["pieces_style"]
    _draw_board_hub_card(screen, pieces_rect, "Pieces Styles", f"Currently: {piece_name}", idle and pieces_rect.collidepoint(mx, my))
    prev = get_preview_images(current_theme)
    for i, pkey in enumerate(("wk", "wq", "bk", "bq")):
        if pkey in prev:
            screen.blit(prev[pkey], (pieces_rect.x + 14 + i * 32, pieces_rect.centery - 14))

    themes_rect = rects["board_themes"]
    board_name = BOARD_THEMES[current_board_theme]["display"]
    _draw_board_hub_card(screen, themes_rect, "Board Themes", f"Currently: {board_name}", idle and themes_rect.collidepoint(mx, my))
    sq = 27
    for r in range(2):
        for f in range(4):
            col = LIGHT_SQ if (r + f) % 2 == 0 else DARK_SQ
            pygame.draw.rect(screen, col, (themes_rect.x + 14 + f * sq, themes_rect.centery - sq + r * sq, sq, sq), border_radius=2)


# --- Board Themes page ---
_BOARD_THEME_BLURBS = {
    "classic": "warm wood tones",
    "chesscom": "Chess.com green",
}


def get_board_theme_card_rects(offset_x=0, offset_y=0):
    w, h = 420, 150
    x = WIDTH // 2 - w // 2 + offset_x
    return [
        (tid, pygame.Rect(x, 154 + i * (h + 28) + offset_y, w, h))
        for i, tid in enumerate(BOARD_THEME_ORDER)
    ]


def draw_board_themes_content(screen, offset_x=0, offset_y=0):
    mx, my = pygame.mouse.get_pos()
    idle = menu_transition_direction == "none"
    intro = font_small.render("Light and dark square colours", True, TEXT_MUTED)
    screen.blit(intro, (WIDTH // 2 - intro.get_width() // 2 + offset_x, 112 + offset_y))

    prev = get_preview_images(current_theme)
    for tid, rect in get_board_theme_card_rects(offset_x, offset_y):
        theme = BOARD_THEMES[tid]
        is_current = tid == current_board_theme
        hovered = idle and rect.collidepoint(mx, my)
        bg = (86, 86, 130) if is_current else (72, 72, 82) if hovered else (56, 56, 66)
        pygame.draw.rect(screen, bg, rect, border_radius=12)
        pygame.draw.rect(screen, (130, 220, 150) if is_current else (150, 170, 255) if hovered else MENU_BORDER_COLOR, rect, 2, border_radius=12)

        # 4x4 mini-board preview in this theme's colours
        sq = 26
        bx, by = rect.x + 18, rect.y + 23
        for r in range(4):
            for f in range(4):
                col = theme["light"] if (r + f) % 2 == 0 else theme["dark"]
                pygame.draw.rect(screen, col, (bx + f * sq, by + r * sq, sq, sq))
        pygame.draw.rect(screen, BOARD_BORDER_COLOR, (bx - 1, by - 1, sq * 4 + 2, sq * 4 + 2), 1)
        if "wk" in prev:
            screen.blit(prev["wk"], (bx, by + sq * 3))
        if "bq" in prev:
            screen.blit(prev["bq"], (bx + sq * 3, by))

        name_s = font_large.render(theme["display"], True, TEXT_COLOR)
        screen.blit(name_s, (rect.x + 142, rect.y + 20))
        blurb_s = font_tiny.render(_BOARD_THEME_BLURBS.get(tid, ""), True, TEXT_MUTED)
        screen.blit(blurb_s, (rect.x + 142, rect.y + 54))
        light_hex = "#%02x%02x%02x" % theme["light"]
        dark_hex = "#%02x%02x%02x" % theme["dark"]
        hex_s = font_tiny.render(f"Light {light_hex}   Dark {dark_hex}", True, TEXT_MUTED)
        screen.blit(hex_s, (rect.x + 142, rect.y + 78))
        # colour chips
        pygame.draw.rect(screen, theme["light"], (rect.x + 142, rect.y + 104, 18, 18), border_radius=3)
        pygame.draw.rect(screen, theme["dark"], (rect.x + 166, rect.y + 104, 18, 18), border_radius=3)

        if is_current:
            sel = font_small.render("● Selected", True, (140, 230, 160))
            screen.blit(sel, (rect.right - sel.get_width() - 16, rect.y + 20))


# --- Board Detection ---
# The Analysis page can capture another Windows application window, find a
# Chess.com-coloured chessboard in that screenshot, and display the result
# with a blue 8x8 overlay. Detection is deliberately scoped to finding the
# board itself (White orientation: a8 top-left, h1 bottom-right); it does
# not sync or play moves from the external board yet.
BOARD_DETECTION_WINDOWS_PER_PAGE = 6
BOARD_DETECTION_MIN_SIDE = 120
BOARD_DETECTION_BLUE_BGR = (255, 0, 0)       # blue in OpenCV's BGR order
BOARD_DETECTION_LINE_BGR = (255, 140, 0)     # lighter blue/cyan in BGR

# Piece/empty-square recognition is handled by the trained PyTorch CNN in
# chessbotv_cnn.pth. The model is loaded lazily the first time Board
# Detection or Auto-play needs it; no sprite/template dataset is loaded into
# memory. The checkpoint's class metadata is used when present, with the
# known ChessBotV class order as the fallback.
CNN_MODEL_FILENAME = "chessbotv_cnn.pth"
CNN_MODEL_ENV_VAR = "CHESSBOTV_CNN_MODEL"
CNN_DEFAULT_IMAGE_SIZE = 64
CNN_CLASS_NAMES = (
    "black_bishop",
    "black_king",
    "black_knight",
    "black_pawn",
    "black_queen",
    "black_rook",
    "empty",
    "white_bishop",
    "white_king",
    "white_knight",
    "white_pawn",
    "white_queen",
    "white_rook",
)
CNN_CLASS_TO_SYMBOL = {
    "black_bishop": "b",
    "black_king": "k",
    "black_knight": "n",
    "black_pawn": "p",
    "black_queen": "q",
    "black_rook": "r",
    "empty": ".",
    "blank": ".",
    "white_bishop": "B",
    "white_king": "K",
    "white_knight": "N",
    "white_pawn": "P",
    "white_queen": "Q",
    "white_rook": "R",
}
PIECE_NAMES = {
    ".": "empty square",
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
PIECE_CODES = {
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
    ".": "blank",
}
SQUARE_BG_BGR = ((208, 236, 235), (82, 149, 115))  # #ebecd0 / #739552
SQUARE_BG_TOLERANCE = 24
_cnn_classifier_cache = None
_cnn_classifier_lock = threading.Lock()
_cnn_inference_lock = threading.Lock()


def _piece_name(symbol):
    """Human-readable name for a FEN-style piece symbol."""
    return PIECE_NAMES.get(symbol, str(symbol))


def _cnn_symbol_for_class_name(class_name):
    """Map one CNN class label to ChessBotV's FEN-style symbol."""
    if class_name in PIECE_NAMES and len(str(class_name)) == 1:
        return str(class_name)
    normalized = (
        str(class_name)
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    if normalized in CNN_CLASS_TO_SYMBOL:
        return CNN_CLASS_TO_SYMBOL[normalized]
    for symbol, code in PIECE_CODES.items():
        if normalized == code:
            return symbol
    return "?"


def _find_cnn_model_path():
    """Find chessbotv_cnn.pth beside new.py, in the cwd, or from the env."""
    candidates = []
    env_path = os.environ.get(CNN_MODEL_ENV_VAR)
    if env_path:
        candidates.append(env_path)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for base in (script_dir, os.getcwd()):
        path = os.path.join(base, CNN_MODEL_FILENAME)
        if path not in candidates:
            candidates.append(path)
    for path in candidates:
        if os.path.isfile(path):
            return path
    searched = ", ".join(candidates)
    raise FileNotFoundError(
        f"Could not find {CNN_MODEL_FILENAME}. Searched: {searched}"
    )


def _cnn_checkpoint_value(checkpoint, names, default=None):
    if not isinstance(checkpoint, dict):
        return default
    for name in names:
        if name in checkpoint:
            return checkpoint[name]
    return default


def _cnn_state_dict_from_checkpoint(checkpoint):
    """Extract a model state dict from the common checkpoint layouts."""
    if not isinstance(checkpoint, dict):
        return None
    for name in ("model_state_dict", "state_dict", "model"):
        value = checkpoint.get(name)
        if isinstance(value, dict):
            return value
    if any(str(key).startswith(("features.", "classifier.", "module.")) for key in checkpoint):
        return checkpoint
    return None


def _cnn_symbols_from_checkpoint(checkpoint, num_classes):
    """Class-index -> FEN-symbol mapping stored in (or implied by) the model."""
    class_names = _cnn_checkpoint_value(
        checkpoint, ("class_names", "classes", "labels")
    )
    if not class_names:
        class_to_idx = _cnn_checkpoint_value(
            checkpoint, ("class_to_idx", "label_to_idx")
        )
        if isinstance(class_to_idx, dict) and class_to_idx:
            class_names = [
                name
                for name, _index in sorted(
                    class_to_idx.items(), key=lambda item: int(item[1])
                )
            ]
    if not class_names:
        class_names = list(CNN_CLASS_NAMES)
    class_names = [str(name) for name in class_names]
    if len(class_names) != int(num_classes):
        raise ValueError(
            f"CNN has {num_classes} outputs but {len(class_names)} class labels"
        )
    symbols = [_cnn_symbol_for_class_name(name) for name in class_names]
    if "?" in symbols:
        unknown = class_names[symbols.index("?")]
        raise ValueError(f"Unsupported CNN class label: {unknown!r}")
    return class_names, symbols


def _cnn_image_size_from_checkpoint(checkpoint):
    value = _cnn_checkpoint_value(
        checkpoint,
        ("image_size", "input_size", "img_size", "image_shape", "input_shape"),
        CNN_DEFAULT_IMAGE_SIZE,
    )
    try:
        if isinstance(value, (list, tuple)):
            numbers = [int(item) for item in value if int(item) > 0]
            if len(numbers) >= 2:
                return numbers[-2], numbers[-1]
            if numbers:
                return numbers[0], numbers[0]
        size = int(value)
        if size > 0:
            return size, size
    except Exception:
        pass
    return CNN_DEFAULT_IMAGE_SIZE, CNN_DEFAULT_IMAGE_SIZE


def _cnn_normalization_from_checkpoint(checkpoint):
    """Optional per-channel mean/std saved with a future checkpoint."""
    mean = _cnn_checkpoint_value(
        checkpoint, ("mean", "input_mean", "normalize_mean", "image_mean")
    )
    std = _cnn_checkpoint_value(
        checkpoint, ("std", "input_std", "normalize_std", "image_std")
    )

    def as_rgb_tuple(value):
        if value is None:
            return None
        try:
            if isinstance(value, (list, tuple)):
                numbers = [float(item) for item in value]
            else:
                numbers = [float(value)]
        except Exception:
            return None
        if len(numbers) == 1:
            return tuple(numbers * 3)
        if len(numbers) >= 3:
            return tuple(numbers[:3])
        return None

    mean_tuple = as_rgb_tuple(mean)
    std_tuple = as_rgb_tuple(std)
    if std_tuple is not None and any(abs(value) < 1e-12 for value in std_tuple):
        std_tuple = None
    return mean_tuple, std_tuple


def _build_chess_cnn_model(torch, state_dict):
    """Build the ChessCNN architecture described by chessbotv_cnn.pth."""
    nn = torch.nn

    def shape(name):
        tensor = state_dict.get(name)
        if tensor is None or not hasattr(tensor, "shape"):
            raise ValueError(f"CNN checkpoint is missing tensor {name!r}")
        return tuple(int(value) for value in tensor.shape)

    conv1 = shape("features.0.weight")
    conv2 = shape("features.4.weight")
    conv3 = shape("features.8.weight")
    conv4 = shape("features.12.weight")
    hidden = shape("classifier.2.weight")
    output = shape("classifier.5.weight")
    if len(conv1) != 4 or len(conv2) != 4 or len(conv3) != 4 or len(conv4) != 4:
        raise ValueError("CNN checkpoint has unexpected convolution shapes")
    if len(hidden) != 2 or len(output) != 2:
        raise ValueError("CNN checkpoint has unexpected classifier shapes")

    def conv_block(in_channels, out_channels, kernel_size, pool=True):
        layers = [
            nn.Conv2d(
                in_channels,
                out_channels,
                kernel_size=kernel_size,
                padding=kernel_size // 2,
            ),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
        ]
        if pool:
            layers.append(nn.MaxPool2d(kernel_size=2, stride=2))
        return layers

    class ChessCNN(nn.Module):
        def __init__(self):
            super().__init__()
            self.features = nn.Sequential(
                *conv_block(conv1[1], conv1[0], conv1[-1]),
                *conv_block(conv2[1], conv2[0], conv2[-1]),
                *conv_block(conv3[1], conv3[0], conv3[-1]),
                *conv_block(conv4[1], conv4[0], conv4[-1], pool=False),
                nn.AdaptiveAvgPool2d((1, 1)),
            )
            self.classifier = nn.Sequential(
                nn.Flatten(),
                nn.Dropout(p=0.3),
                nn.Linear(hidden[1], hidden[0]),
                nn.ReLU(inplace=True),
                nn.Dropout(p=0.3),
                nn.Linear(output[1], output[0]),
            )

        def forward(self, x):
            return self.classifier(self.features(x))

    return ChessCNN(), int(output[0])


def _torch_load_checkpoint(torch, path, device):
    """Load the repository's trusted CNN checkpoint across torch versions."""
    try:
        return torch.load(path, map_location=device, weights_only=True)
    except TypeError:
        return torch.load(path, map_location=device)
    except Exception:
        # Older/custom checkpoint metadata can be rejected by the restricted
        # loader. This file is the model shipped with ChessBotV itself.
        return torch.load(path, map_location=device, weights_only=False)


def _load_cnn_classifier():
    """Load chessbotv_cnn.pth once and return its cached runtime details."""
    global _cnn_classifier_cache
    if _cnn_classifier_cache is not None:
        return _cnn_classifier_cache
    with _cnn_classifier_lock:
        if _cnn_classifier_cache is not None:
            return _cnn_classifier_cache

        import torch

        path = _find_cnn_model_path()
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        checkpoint = _torch_load_checkpoint(torch, path, device)

        if hasattr(checkpoint, "state_dict") and callable(checkpoint):
            model = checkpoint
            output_shape = None
            for name, tensor in model.state_dict().items():
                if name.endswith("classifier.5.weight"):
                    output_shape = tuple(tensor.shape)
                    break
            num_classes = int(output_shape[0]) if output_shape else len(CNN_CLASS_NAMES)
            class_names, symbols = _cnn_symbols_from_checkpoint({}, num_classes)
            image_size = (CNN_DEFAULT_IMAGE_SIZE, CNN_DEFAULT_IMAGE_SIZE)
            mean, std = None, None
        else:
            state_dict = _cnn_state_dict_from_checkpoint(checkpoint)
            if state_dict is None:
                raise ValueError(f"Unsupported CNN checkpoint format in {path}")
            if state_dict and all(
                str(key).startswith("module.") for key in state_dict
            ):
                state_dict = {
                    str(key)[len("module."):]: value
                    for key, value in state_dict.items()
                }
            model, num_classes = _build_chess_cnn_model(torch, state_dict)
            model.load_state_dict(state_dict, strict=True)
            class_names, symbols = _cnn_symbols_from_checkpoint(
                checkpoint, num_classes
            )
            image_size = _cnn_image_size_from_checkpoint(checkpoint)
            mean, std = _cnn_normalization_from_checkpoint(checkpoint)

        model.to(device)
        model.eval()
        for parameter in model.parameters():
            parameter.requires_grad_(False)

        _cnn_classifier_cache = {
            "torch": torch,
            "model": model,
            "device": device,
            "path": path,
            "class_names": class_names,
            "symbols": symbols,
            "image_size": image_size,
            "mean": mean,
            "std": std,
        }
        print(
            f"CNN vision model loaded: {path} "
            f"({len(class_names)} classes, input {image_size[1]}x{image_size[0]}, {device})."
        )
        return _cnn_classifier_cache


def _get_cnn_classifier():
    return _load_cnn_classifier()


def _prepare_cnn_square(square, classifier):
    """Convert one BGR/BGRA/grey square crop to a CNN input tensor."""
    import numpy as np

    torch = classifier["torch"]
    functional = torch.nn.functional
    array = np.asarray(square)
    if array.ndim == 2:
        array = np.stack((array, array, array), axis=2)
    elif array.ndim == 3:
        if array.shape[2] == 1:
            array = np.repeat(array, 3, axis=2)
        elif array.shape[2] >= 3:
            # Window captures and board crops are BGR; the CNN dataset/PIL
            # convention is RGB.
            array = array[:, :, :3][:, :, ::-1]
        else:
            raise ValueError("CNN square needs at least one colour channel")
    else:
        raise ValueError("CNN square must be a 2D or 3D image")

    if array.dtype != np.uint8:
        if np.issubdtype(array.dtype, np.floating) and array.size:
            if float(np.nanmax(array)) <= 1.0:
                array = array * 255.0
        array = np.clip(array, 0, 255).astype(np.uint8)

    tensor = (
        torch.from_numpy(np.ascontiguousarray(array))
        .permute(2, 0, 1)
        .unsqueeze(0)
        .float()
        .div(255.0)
    )
    target_h, target_w = classifier["image_size"]
    if tensor.shape[-2:] != (target_h, target_w):
        try:
            tensor = functional.interpolate(
                tensor,
                size=(target_h, target_w),
                mode="bilinear",
                align_corners=False,
                antialias=True,
            )
        except TypeError:
            tensor = functional.interpolate(
                tensor,
                size=(target_h, target_w),
                mode="bilinear",
                align_corners=False,
            )

    mean = classifier.get("mean")
    std = classifier.get("std")
    if mean is not None:
        mean_tensor = torch.tensor(mean, dtype=tensor.dtype).view(1, 3, 1, 1)
        tensor = tensor - mean_tensor
    if std is not None:
        std_tensor = torch.tensor(std, dtype=tensor.dtype).view(1, 3, 1, 1)
        tensor = tensor / std_tensor
    return tensor[0]


def _classify_squares_with_cnn(squares, classifier=None):
    """Classify square crops in one CNN batch.

    Returns the same ``(symbol, confidence, ranked)`` shape the board code
    already uses. ``ranked`` is sorted by descending probability, stored as
    ``(symbol, 1 - probability)`` so existing ranking consumers continue to
    see their preferred value first.
    """
    squares = list(squares)
    results = [("?", 0.0, []) for _ in squares]
    if not squares:
        return results

    classifier = classifier or _get_cnn_classifier()
    torch = classifier["torch"]
    valid_indexes = []
    tensors = []
    for index, square in enumerate(squares):
        try:
            if square is None or getattr(square, "size", 0) == 0:
                continue
            if getattr(square, "ndim", 0) >= 2 and min(square.shape[:2]) < 2:
                continue
            tensors.append(_prepare_cnn_square(square, classifier))
            valid_indexes.append(index)
        except Exception:
            continue
    if not tensors:
        return results

    batch = torch.stack(tensors).to(classifier["device"])
    with _cnn_inference_lock:
        inference_mode = getattr(torch, "inference_mode", torch.no_grad)
        with inference_mode():
            logits = classifier["model"](batch)
            probabilities = torch.softmax(logits, dim=1).detach().cpu()

    symbols = classifier["symbols"]
    for result_index, probability_row in zip(valid_indexes, probabilities):
        values = [float(value) for value in probability_row.tolist()]
        order = sorted(range(len(values)), key=lambda item: values[item], reverse=True)
        ranked = [
            (symbols[item], max(0.0, min(1.0, 1.0 - values[item])))
            for item in order
        ]
        best = order[0]
        results[result_index] = (symbols[best], float(values[best]), ranked)
    return results


def _classify_square_with_cnn(square, classifier=None):
    return _classify_squares_with_cnn([square], classifier=classifier)[0]


def _load_autoplay_dictionary():
    """Load autoplaydictionary.json from the cwd or beside new.py."""
    candidates = []
    for base in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        path = os.path.join(base, "autoplaydictionary.json")
        if path not in candidates:
            candidates.append(path)
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            continue
        except Exception as exc:
            print(f"Board Detection: could not read {path}: {exc}")
            return {}
    return {}


def _dictionary_symbol(data, image_path, default_symbol):
    """Resolve one autoplaydictionary entry to a FEN-style piece symbol."""
    value = data.get(image_path)
    if isinstance(value, dict):
        symbol = value.get("symbol")
        if symbol in PIECE_NAMES:
            return symbol
        value = value.get("piece") or value.get("label") or value.get("name")
    if isinstance(value, str):
        clean = value.strip()
        if clean in PIECE_NAMES:
            return clean
        cnn_symbol = _cnn_symbol_for_class_name(clean)
        if cnn_symbol != "?":
            return cnn_symbol
        labels = {
            str(name).strip().lower(): symbol
            for symbol, name in PIECE_NAMES.items()
        }
        return labels.get(clean.lower(), default_symbol)
    return default_symbol


def _expected_anchor_symbols():
    """Expected top-left/bottom-right pieces from autoplaydictionary.json.

    The dictionary entry for r0_c0 describes a8 and r7_c7 describes h1.
    For the normal White orientation/starting layout these default to the
    black rook and white rook requested as the two detection anchors.
    """
    data = _load_autoplay_dictionary()
    return (
        _dictionary_symbol(data, "board_debug/r0_c0.png", "r"),
        _dictionary_symbol(data, "board_debug/r7_c7.png", "R"),
    )


def _board_colour_masks(image):
    """Return (light, dark, combined) Chess.com square-colour masks."""
    import numpy as np

    if image is None or getattr(image, "ndim", 0) != 3:
        raise ValueError("Board detection needs a colour screenshot")
    colours = SQUARE_BG_BGR
    tolerance = SQUARE_BG_TOLERANCE
    bgr = image[:, :, :3].astype(np.int16)
    light_colour = np.asarray(colours[0], dtype=np.int16)
    dark_colour = np.asarray(colours[1], dtype=np.int16)
    light = np.max(np.abs(bgr - light_colour), axis=2) <= tolerance
    dark = np.max(np.abs(bgr - dark_colour), axis=2) <= tolerance
    return light, dark, light | dark


def _mask_integral(mask):
    import cv2

    return cv2.integral(mask.astype("uint8"))


def _integral_count(integral, x, y, width, height):
    """Count set pixels in a rectangle using an OpenCV integral image."""
    max_x = integral.shape[1] - 1
    max_y = integral.shape[0] - 1
    x0 = max(0, min(int(x), max_x))
    y0 = max(0, min(int(y), max_y))
    x1 = max(x0, min(int(x + width), max_x))
    y1 = max(y0, min(int(y + height), max_y))
    return int(
        integral[y1, x1] - integral[y0, x1] - integral[y1, x0] + integral[y0, x0]
    )


def _board_cell_bounds(board_rect, row, col):
    """Exclusive pixel bounds (left, top, right, bottom) for one board cell."""
    x, y, width, height = board_rect
    side = width
    left = x + round(col * side / 8)
    right = x + round((col + 1) * side / 8)
    top = y + round(row * side / 8)
    bottom = y + round((row + 1) * side / 8)
    return left, top, right, bottom


def _board_geometry_score(board_rect, integrals):
    """Score how strongly a square looks like an alternating 8x8 board."""
    x, y, width, height = board_rect
    side = width
    if side < 8 or width != height:
        return {"score": -1.0, "coverage": 0.0, "parity": 0.0}
    coverage = _integral_count(integrals["bg"], x, y, side, side) / float(side * side)

    parity_total = 0.0
    parity_cells = 0
    for row in range(8):
        for col in range(8):
            left, top, right, bottom = _board_cell_bounds(board_rect, row, col)
            cell_w = right - left
            cell_h = bottom - top
            light = _integral_count(integrals["light"], left, top, cell_w, cell_h)
            dark = _integral_count(integrals["dark"], left, top, cell_w, cell_h)
            total = light + dark
            if total < max(8, int(cell_w * cell_h * 0.05)):
                continue
            correct = light if (row + col) % 2 == 0 else dark
            parity_total += correct / float(total)
            parity_cells += 1
    parity = parity_total / parity_cells if parity_cells else 0.0
    return {
        "score": 2.2 * parity + 0.8 * coverage,
        "coverage": float(coverage),
        "parity": float(parity),
    }




def _add_board_candidate(candidates, x, y, side, image_w, image_h):
    x = int(round(x))
    y = int(round(y))
    side = int(round(side))
    if side < BOARD_DETECTION_MIN_SIDE:
        return
    if x < 0 or y < 0 or x + side > image_w or y + side > image_h:
        return
    candidates.add((x, y, side, side))


def _board_candidate_rects(bg_mask):
    """Generate plausible square board rectangles from board-colour blobs."""
    import cv2
    import numpy as np

    image_h, image_w = bg_mask.shape[:2]
    candidates = set()
    mask_u8 = bg_mask.astype("uint8") * 255
    kernel_size = max(5, min(21, ((min(image_w, image_h) // 80) | 1)))
    kernel = np.ones((kernel_size, kernel_size), np.uint8)
    closed = cv2.morphologyEx(mask_u8, cv2.MORPH_CLOSE, kernel)
    closed = cv2.morphologyEx(closed, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)[:10]

    for contour in contours:
        area = cv2.contourArea(contour)
        if area < image_w * image_h * 0.003:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if min(w, h) < BOARD_DETECTION_MIN_SIDE:
            continue
        aspect = w / float(h)
        if aspect < 0.5 or aspect > 2.0:
            continue
        for side in {min(w, h), max(w, h), round((w + h) / 2)}:
            positions = {
                (x, y),
                (x + w - side, y + h - side),
                (x + (w - side) // 2, y + (h - side) // 2),
            }
            for px, py in positions:
                _add_board_candidate(candidates, px, py, side, image_w, image_h)

    # Fallback: the bounding box of every board-coloured pixel. This helps
    # when a board is fragmented by pieces/highlights into several contours.
    ys, xs = np.where(bg_mask)
    if len(xs) and len(ys):
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        w, h = x1 - x0, y1 - y0
        for side in {min(w, h), max(w, h), round((w + h) / 2)}:
            _add_board_candidate(
                candidates,
                x0 + (w - side) // 2,
                y0 + (h - side) // 2,
                side,
                image_w,
                image_h,
            )
    return list(candidates)


def _refine_board_rect(board_rect, integrals, image_w, image_h):
    """Local-search x/y/side to maximise the checkerboard geometry score."""
    best_rect = board_rect
    best_metrics = _board_geometry_score(best_rect, integrals)
    passes = [
        (range(-24, 25, 4), range(-16, 17, 4)),
        (range(-3, 4, 1), range(-3, 4, 1)),
    ]
    for side_deltas, offsets in passes:
        base_x, base_y, base_side, _ = best_rect
        for side_delta in side_deltas:
            side = base_side + side_delta
            if side < BOARD_DETECTION_MIN_SIDE:
                continue
            for dx in offsets:
                for dy in offsets:
                    x = base_x + dx
                    y = base_y + dy
                    if x < 0 or y < 0 or x + side > image_w or y + side > image_h:
                        continue
                    candidate = (x, y, side, side)
                    metrics = _board_geometry_score(candidate, integrals)
                    if metrics["score"] > best_metrics["score"]:
                        best_rect = candidate
                        best_metrics = metrics
    return best_rect


def _anchor_match_score(ranked, expected_symbol):
    """Score a CNN ranking against the expected anchor piece."""
    if not ranked:
        return 0.0
    probabilities = {
        symbol: max(0.0, min(1.0, 1.0 - float(error)))
        for symbol, error in ranked
    }
    expected_probability = probabilities.get(expected_symbol, 0.0)
    score = 0.65 * expected_probability
    if ranked[0][0] == expected_symbol:
        score += 0.35
    return max(0.0, min(1.0, score))


def _piece_display_code(symbol):
    """Short overlay code for an occupied square; blank squares get ''."""
    if symbol in (None, "."):
        return ""
    if symbol == "?":
        return "?"
    return PIECE_CODES.get(symbol, str(symbol))


def _classify_board_anchor(image, board_rect, row, col, expected_symbol):
    left, top, right, bottom = _board_cell_bounds(board_rect, row, col)
    cell = image[top:bottom, left:right]
    centre = ((left + right) // 2, (top + bottom) // 2)
    info = {
        "row": row,
        "col": col,
        "coordinate": f"{'abcdefgh'[col]}{8 - row}",
        "centre": centre,
        "expected_symbol": expected_symbol,
        "expected_name": _piece_name(expected_symbol),
        "detected_symbol": None,
        "classified_symbol": None,
        "detected_name": None,
        "confidence": None,
        "score": 0.0,
        "match": None,
    }
    if cell.shape[0] < 8 or cell.shape[1] < 8:
        return info
    try:
        symbol, confidence, ranked = _classify_square_with_cnn(cell)
    except Exception:
        return info
    detected = ranked[0][0] if ranked else symbol
    info.update({
        "detected_symbol": detected,
        "classified_symbol": symbol,
        "detected_name": _piece_name(detected),
        "confidence": float(confidence),
        "score": _anchor_match_score(ranked, expected_symbol),
        "match": detected == expected_symbol,
    })
    return info


def _score_board_candidate(image, board_rect, integrals, expected_anchors):
    metrics = _board_geometry_score(board_rect, integrals)
    total = metrics["score"]
    anchors = {}
    expected_tl, expected_br = expected_anchors
    anchor_specs = (
        ("top_left", 0, 0, expected_tl),
        ("bottom_right", 7, 7, expected_br),
    )
    for name, row, col, expected in anchor_specs:
        anchors[name] = _classify_board_anchor(
            image,
            board_rect,
            row,
            col,
            expected,
        )
    total += 1.5 * (
        anchors["top_left"]["score"] + anchors["bottom_right"]["score"]
    ) / 2.0
    return {
        "board_rect": board_rect,
        "score": float(total),
        "coverage": metrics["coverage"],
        "parity": metrics["parity"],
        "anchors": anchors,
    }


def _classify_board_pieces(image, board_rect):
    """Classify every square of a located board with one CNN batch."""
    tasks = []
    positions = []
    for row in range(8):
        for col in range(8):
            left, top, right, bottom = _board_cell_bounds(board_rect, row, col)
            cell = image[top:bottom, left:right]
            if cell.shape[0] >= 8 and cell.shape[1] >= 8:
                positions.append((row, col))
                tasks.append(cell.copy())
    classified = _classify_squares_with_cnn(tasks)
    precomputed = {
        position: result for position, result in zip(positions, classified)
    }

    grid = []
    sources = []
    confidences = []
    ranked_grid = []
    piece_count = 0
    blank_count = 0
    unknown_squares = []

    for row in range(8):
        grid_row = []
        source_row = []
        confidence_row = []
        ranked_row = []
        for col in range(8):
            symbol, confidence, ranked = precomputed.get(
                (row, col), ("?", 0.0, [])
            )
            confidence = float(confidence)
            source = "unknown" if symbol == "?" else "cnn"

            grid_row.append(symbol)
            source_row.append(source)
            confidence_row.append(confidence)
            ranked_row.append(ranked)
            if symbol == ".":
                blank_count += 1
            elif symbol == "?":
                unknown_squares.append((row, col))
            else:
                piece_count += 1

        grid.append(grid_row)
        sources.append(source_row)
        confidences.append(confidence_row)
        ranked_grid.append(ranked_row)

    return {
        "piece_grid": grid,
        "piece_sources": sources,
        "piece_confidences": confidences,
        "piece_ranked": ranked_grid,
        "piece_count": piece_count,
        "blank_count": blank_count,
        "unknown_squares": unknown_squares,
    }


def _board_square_crop(image, board_rect, row, col):
    left, top, right, bottom = _board_cell_bounds(board_rect, row, col)
    return image[top:bottom, left:right].copy()


def _sync_board_anchor_labels(result):
    """Keep corner-anchor text in step with the final all-square labels."""
    grid = result.get("piece_grid") or []
    confidences = result.get("piece_confidences") or []
    anchors = result.get("anchors", {})
    for key, row, col in (("top_left", 0, 0), ("bottom_right", 7, 7)):
        anchor = anchors.get(key)
        if not anchor:
            continue
        try:
            symbol = grid[row][col]
        except Exception:
            continue
        if symbol in (None, "?"):
            continue
        anchor["detected_symbol"] = symbol
        anchor["classified_symbol"] = symbol
        anchor["detected_name"] = _piece_name(symbol)
        anchor["match"] = symbol == anchor.get("expected_symbol")
        try:
            anchor["confidence"] = float(confidences[row][col])
        except Exception:
            pass


def detect_chessboard_in_image(image, expected_anchors=None):
    """Find a White-oriented Chess.com board square in a BGR screenshot.

    The grid-colour geometry does the locating; the CNN classifies the two
    corner anchors (normally black rook on a8 and white rook on h1) as a
    verification signal and then classifies all 64 squares. Returns a
    detection dict or None.
    """
    if image is None or getattr(image, "ndim", 0) != 3:
        return None
    classifier = _get_cnn_classifier()
    image_h, image_w = image.shape[:2]
    light_mask, dark_mask, bg_mask = _board_colour_masks(image)
    integrals = {
        "light": _mask_integral(light_mask),
        "dark": _mask_integral(dark_mask),
        "bg": _mask_integral(bg_mask),
    }
    candidates = _board_candidate_rects(bg_mask)
    if not candidates:
        return None

    geometry_scored = []
    for rect in candidates:
        metrics = _board_geometry_score(rect, integrals)
        if metrics["parity"] >= 0.35 and metrics["coverage"] >= 0.15:
            geometry_scored.append((metrics["score"], rect))
    if not geometry_scored:
        return None
    geometry_scored.sort(key=lambda item: item[0], reverse=True)

    refined_rects = []
    for _score, rect in geometry_scored[:3]:
        refined = _refine_board_rect(rect, integrals, image_w, image_h)
        if refined not in refined_rects:
            refined_rects.append(refined)

    expected = expected_anchors or _expected_anchor_symbols()

    scored = [
        _score_board_candidate(
            image,
            rect,
            integrals,
            expected,
        )
        for rect in refined_rects
    ]
    best = max(scored, key=lambda item: item["score"])
    if best["parity"] < 0.55 or best["coverage"] < 0.30:
        return None
    best["expected_anchors"] = expected
    best["image_size"] = (image_w, image_h)
    best["cnn_model_path"] = classifier["path"]
    best["cnn_class_names"] = list(classifier["class_names"])
    best.update(_classify_board_pieces(image, best["board_rect"]))
    _sync_board_anchor_labels(best)
    return best


# --- Auto-play board synchronisation helpers ---
def _autoplay_side_name(side):
    return "black" if str(side).strip().lower() == "black" else "white"


def _autoplay_visual_square(row, col, side):
    """Chess square shown at a screenshot row/col for the player's side."""
    if _autoplay_side_name(side) == "black":
        return chess.square(7 - col, row)
    return chess.square(col, 7 - row)


def _autoplay_square_visual(square, side):
    """Screenshot (row, col) containing a chess square for the side."""
    file_idx = chess.square_file(square)
    rank_idx = chess.square_rank(square)
    if _autoplay_side_name(side) == "black":
        return rank_idx, 7 - file_idx
    return 7 - rank_idx, file_idx


def _autoplay_board_grid_from_board(board):
    """8x8 FEN-symbol grid in rank-8-to-rank-1 / a-to-h order."""
    grid = []
    for rank_idx in range(7, -1, -1):
        row = []
        for file_idx in range(8):
            piece = board.piece_at(chess.square(file_idx, rank_idx))
            row.append(piece.symbol() if piece else ".")
        grid.append(row)
    return grid


def _autoplay_visual_to_board_grid(visual_grid, side):
    """Convert screenshot/visual rows into FEN-oriented board rows."""
    board_grid = [["."] * 8 for _ in range(8)]
    for row in range(8):
        for col in range(8):
            square = _autoplay_visual_square(row, col, side)
            board_row = 7 - chess.square_rank(square)
            board_col = chess.square_file(square)
            board_grid[board_row][board_col] = visual_grid[row][col]
    return board_grid


def _autoplay_placement_from_grid(board_grid):
    ranks = []
    for row in board_grid:
        text = ""
        empty = 0
        for symbol in row:
            if symbol == ".":
                empty += 1
            else:
                if empty:
                    text += str(empty)
                    empty = 0
                text += str(symbol)
        if empty:
            text += str(empty)
        ranks.append(text)
    return "/".join(ranks)


def _autoplay_castling_rights(board_grid):
    """Best-effort castling rights from unmoved king/rook squares."""
    rights = ""
    if board_grid[7][4] == "K" and board_grid[7][7] == "R":
        rights += "K"
    if board_grid[7][4] == "K" and board_grid[7][0] == "R":
        rights += "Q"
    if board_grid[0][4] == "k" and board_grid[0][7] == "r":
        rights += "k"
    if board_grid[0][4] == "k" and board_grid[0][0] == "r":
        rights += "q"
    return rights or "-"


def _autoplay_fen_from_visual_grid(visual_grid, side):
    """Build a playable FEN from a first full-board scan.

    The external board is normally oriented toward the side the user is
    playing. A completely fresh starting position is always White to
    move; for a mid-game first scan the requested workflow starts on the
    player's turn. Castling is inferred from home king/rook squares and
    en passant cannot be recovered from one still image.
    """
    side = _autoplay_side_name(side)
    board_grid = _autoplay_visual_to_board_grid(visual_grid, side)
    if board_grid == _autoplay_board_grid_from_board(chess.Board()):
        turn = "w"
    else:
        turn = "w" if side == "white" else "b"
    return (
        f"{_autoplay_placement_from_grid(board_grid)} {turn} "
        f"{_autoplay_castling_rights(board_grid)} - 0 1"
    )


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


def _autoplay_changed_visual_squares(previous, current,
                                     mean_threshold=6.0,
                                     changed_fraction=0.035):
    """Screenshot cells whose pixels changed materially between polls."""
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


def _autoplay_classify_visual_squares(board_crop, visual_squares):
    """Classify only the requested visual cells of a board-only crop."""
    visual_squares = list(visual_squares)
    if board_crop is None or getattr(board_crop, "ndim", 0) != 3:
        return {(row, col): "?" for row, col in visual_squares}
    board_rect = (0, 0, board_crop.shape[1], board_crop.shape[0])
    cells = [
        _board_square_crop(board_crop, board_rect, row, col)
        for row, col in visual_squares
    ]
    try:
        classified = _classify_squares_with_cnn(cells)
    except Exception as exc:
        print(f"Auto-play: CNN changed-square classification failed: {exc}")
        return {(row, col): "?" for row, col in visual_squares}
    return {
        position: result[0]
        for position, result in zip(visual_squares, classified)
    }


def _autoplay_candidate_grid(base_board_grid, classified_visual, side):
    """Copy a board grid and replace only reclassified changed squares."""
    candidate = [list(row) for row in base_board_grid]
    for (row, col), symbol in classified_visual.items():
        square = _autoplay_visual_square(row, col, side)
        board_row = 7 - chess.square_rank(square)
        board_col = chess.square_file(square)
        candidate[board_row][board_col] = symbol
    return candidate


def _autoplay_square_screen_point(screen_board_rect, square, side):
    """Desktop centre point for one chess square in the target window."""
    row, col = _autoplay_square_visual(square, side)
    left, top, right, bottom = _board_cell_bounds(
        screen_board_rect, row, col
    )
    return (left + right) // 2, (top + bottom) // 2


def _autoplay_move_screen_points(screen_board_rect, move, side):
    return (
        _autoplay_square_screen_point(screen_board_rect, move.from_square, side),
        _autoplay_square_screen_point(screen_board_rect, move.to_square, side),
    )


def _cv2_label(image, text, origin, scale=0.55, thickness=1):
    """Draw white text on a fitted black label, clamped inside the image."""
    import cv2

    font = cv2.FONT_HERSHEY_SIMPLEX
    (text_w, text_h), baseline = cv2.getTextSize(text, font, scale, thickness)
    x, y = int(origin[0]), int(origin[1])
    x = max(0, min(x, max(0, image.shape[1] - text_w - 12)))
    y = max(text_h + 6, min(y, image.shape[0] - baseline - 4))
    cv2.rectangle(
        image,
        (x, y - text_h - 6),
        (min(image.shape[1] - 1, x + text_w + 12), y + baseline + 4),
        (0, 0, 0),
        -1,
    )
    cv2.putText(image, text, (x + 6, y), font, scale, (255, 255, 255), thickness, cv2.LINE_AA)
    return x, y


def _anchor_overlay_label(anchor):
    detected = anchor.get("detected_name")
    expected = anchor.get("expected_name", "piece")
    coord = anchor.get("coordinate", "")
    if detected and anchor.get("detected_symbol") != anchor.get("expected_symbol"):
        return f"{coord}: expected {expected}, found {detected}"
    return f"{coord}: {expected}"


def annotate_board_detection(image, detection):
    """Draw the blue board square, 8x8 grid, coordinates and rook anchors."""
    import cv2

    annotated = image.copy()
    x, y, width, height = detection["board_rect"]
    side = width
    line_thickness = max(1, side // 280)
    outer_thickness = max(3, side // 140)

    for index in range(1, 8):
        px = x + round(index * side / 8)
        py = y + round(index * side / 8)
        cv2.line(annotated, (px, y), (px, y + side - 1), BOARD_DETECTION_BLUE_BGR, line_thickness)
        cv2.line(annotated, (x, py), (x + side - 1, py), BOARD_DETECTION_BLUE_BGR, line_thickness)
    cv2.rectangle(
        annotated,
        (x, y),
        (x + side - 1, y + side - 1),
        BOARD_DETECTION_BLUE_BGR,
        outer_thickness,
    )

    cell = side / 8.0
    piece_grid = detection.get("piece_grid")
    coord_scale = max(0.32, min(0.62, cell / 105.0))
    coord_thickness = 2 if cell >= 52 else 1
    for row in range(8):
        for col in range(8):
            left, top, _right, _bottom = _board_cell_bounds(detection["board_rect"], row, col)
            coord = f"{'abcdefgh'[col]}{8 - row}"
            text_org = (left + max(3, int(cell * 0.07)), top + max(13, int(cell * 0.30)))
            if (row + col) % 2 == 0:  # light square: dark text reads best
                outline_colour, text_colour = (255, 255, 255), (0, 0, 0)
            else:                      # dark square: light text reads best
                outline_colour, text_colour = (0, 0, 0), (255, 255, 255)
            cv2.putText(
                annotated, coord, text_org, cv2.FONT_HERSHEY_SIMPLEX,
                coord_scale, outline_colour, coord_thickness + 2, cv2.LINE_AA,
            )
            cv2.putText(
                annotated, coord, text_org, cv2.FONT_HERSHEY_SIMPLEX,
                coord_scale, text_colour, coord_thickness, cv2.LINE_AA,
            )

    anchors = detection.get("anchors", {})
    for key in ("top_left", "bottom_right"):
        anchor = anchors.get(key)
        if not anchor:
            continue
        centre = anchor["centre"]
        radius = max(7, int(cell * 0.18))
        cv2.circle(annotated, centre, radius, (0, 255, 255), max(2, line_thickness + 1))
        cv2.circle(annotated, centre, 2, (0, 255, 255), -1)

    # Label occupied squares last so their short piece codes stay readable
    # over the grid/pieces/anchor markers. Blank squares intentionally get
    # no piece label; a square the CNN could not process shows '?'.
    if piece_grid:
        for row in range(8):
            for col in range(8):
                try:
                    symbol = piece_grid[row][col]
                except Exception:
                    continue
                piece_code = _piece_display_code(symbol)
                if not piece_code:
                    continue
                _left, _top, right, bottom = _board_cell_bounds(
                    detection["board_rect"], row, col
                )
                if (row + col) % 2 == 0:
                    piece_outline, piece_colour = (255, 255, 255), (0, 0, 0)
                else:
                    piece_outline, piece_colour = (0, 0, 0), (255, 255, 255)
                if piece_code == "?":
                    piece_outline, piece_colour = (0, 0, 0), (0, 255, 255)
                piece_scale = max(0.32, min(0.72, cell / 92.0))
                piece_thickness = 2 if cell >= 46 else 1
                (piece_w, _piece_h), _baseline = cv2.getTextSize(
                    piece_code,
                    cv2.FONT_HERSHEY_SIMPLEX,
                    piece_scale,
                    piece_thickness,
                )
                piece_org = (
                    right - piece_w - max(3, int(cell * 0.07)),
                    bottom - max(5, int(cell * 0.10)),
                )
                cv2.putText(
                    annotated, piece_code, piece_org, cv2.FONT_HERSHEY_SIMPLEX,
                    piece_scale, piece_outline, piece_thickness + 2, cv2.LINE_AA,
                )
                cv2.putText(
                    annotated, piece_code, piece_org, cv2.FONT_HERSHEY_SIMPLEX,
                    piece_scale, piece_colour, piece_thickness, cv2.LINE_AA,
                )

    top_left = anchors.get("top_left")
    if top_left:
        _cv2_label(
            annotated,
            _anchor_overlay_label(top_left),
            (x + 6, y - 10 if y >= 34 else y + 24),
            scale=max(0.45, min(0.65, cell / 110.0)),
        )
    bottom_right = anchors.get("bottom_right")
    if bottom_right:
        label = _anchor_overlay_label(bottom_right)
        scale = max(0.45, min(0.65, cell / 110.0))
        (text_w, _text_h), _baseline = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, scale, 1
        )
        label_x = x + side - text_w - 18
        label_y = y + side + 28 if y + side + 34 < annotated.shape[0] else y + side - 10
        _cv2_label(annotated, label, (label_x, label_y), scale=scale)
    return annotated




class _WinRect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BitmapInfoHeader),
        ("bmiColors", ctypes.c_uint32 * 1),
    ]


def _get_win32_capture_libs():
    """Cached, correctly prototyped user32/gdi32 handles for window capture."""
    global _win32_capture_libs_cache
    if _win32_capture_libs_cache is not None:
        return _win32_capture_libs_cache
    if os.name != "nt":
        return None
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_WinRect)]
    user32.GetWindowRect.restype = wintypes.BOOL
    get_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
    get_long.argtypes = [wintypes.HWND, ctypes.c_int]
    get_long.restype = ctypes.c_ssize_t
    user32._get_window_long_for_capture = get_long
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.GetWindowDC.restype = ctypes.c_void_p
    user32.ReleaseDC.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.PrintWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.mouse_event.argtypes = [
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.c_size_t,
    ]
    user32.mouse_event.restype = None
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short

    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.BitBlt.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
    ]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
        ctypes.c_void_p, ctypes.POINTER(_BitmapInfo), wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.restype = wintypes.BOOL

    _win32_capture_libs_cache = (user32, gdi32, enum_proc)
    return _win32_capture_libs_cache


def _enumerate_capture_windows():
    """Visible top-level windows suitable for the Board Detection picker."""
    libs = _get_win32_capture_libs()
    if libs is None:
        return []
    user32, _gdi32, enum_proc = libs
    own_hwnd = _get_window_hwnd(_user32())
    own_value = own_hwnd.value if isinstance(own_hwnd, ctypes.c_void_p) else own_hwnd
    windows = []

    def callback(hwnd, _lparam):
        try:
            hwnd_value = hwnd.value if isinstance(hwnd, ctypes.c_void_p) else int(hwnd)
            if own_value and hwnd_value == int(own_value):
                return True
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            # Skip tool windows (floating palettes, tooltips and the like).
            try:
                ex_style = user32._get_window_long_for_capture(hwnd, _GWL_EXSTYLE)
                if ex_style & 0x00000080:  # WS_EX_TOOLWINDOW
                    return True
            except Exception:
                pass
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if not title or title == "Program Manager":
                return True
            rect = _WinRect()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width < 240 or height < 180:
                return True
            windows.append({
                "hwnd": hwnd_value,
                "title": title,
                "rect": (rect.left, rect.top, rect.right, rect.bottom),
                "thumbnail": None,
            })
        except Exception:
            pass
        return True

    user32.EnumWindows(enum_proc(callback), 0)
    return windows[:12]


def _capture_window_bgr(hwnd):
    """Capture one window as (BGR numpy image, screen rect), or (None, None)."""
    libs = _get_win32_capture_libs()
    if libs is None or not hwnd:
        return None, None
    try:
        import numpy as np
    except Exception:
        return None, None
    user32, gdi32, _enum_proc = libs
    hwnd = ctypes.c_void_p(int(hwnd)) if not isinstance(hwnd, ctypes.c_void_p) else hwnd
    if not user32.IsWindow(hwnd):
        return None, None
    rect = _WinRect()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None, None
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        return None, None

    window_dc = user32.GetWindowDC(hwnd)
    if not window_dc:
        return None, None
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height) if memory_dc else None
    old_object = gdi32.SelectObject(memory_dc, bitmap) if bitmap else None

    def read_bits():
        bmi = _BitmapInfo()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height  # top-down rows
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        bmi.bmiHeader.biSizeImage = width * height * 4
        buffer = ctypes.create_string_buffer(width * height * 4)
        rows = gdi32.GetDIBits(
            memory_dc, bitmap, 0, height, buffer, ctypes.byref(bmi), 0
        )
        if rows == 0:
            return None
        bgra = np.frombuffer(buffer, dtype=np.uint8).reshape((height, width, 4)).copy()
        return bgra[:, :, :3].copy()

    try:
        if not memory_dc or not bitmap:
            return None, None
        # PW_RENDERFULLCONTENT helps modern/DirectX windows render off-screen.
        printed = bool(user32.PrintWindow(hwnd, memory_dc, 2))
        image = read_bits() if printed else None
        needs_fallback = image is None
        if image is not None and float(np.mean(image)) < 2.0 and float(np.std(image)) < 1.0:
            needs_fallback = True
        if needs_fallback:
            SRCCOPY_CAPTUREBLT = 0x00CC0020 | 0x40000000
            if gdi32.BitBlt(memory_dc, 0, 0, width, height, window_dc, 0, 0, SRCCOPY_CAPTUREBLT):
                fallback = read_bits()
                if fallback is not None:
                    image = fallback
        if image is None:
            return None, None
        return image, (rect.left, rect.top, rect.right, rect.bottom)
    finally:
        if old_object and memory_dc:
            gdi32.SelectObject(memory_dc, old_object)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        if window_dc:
            user32.ReleaseDC(hwnd, window_dc)


def _autoplay_user32():
    libs = _get_win32_capture_libs()
    return libs[0] if libs else None


def _autoplay_focus_window(hwnd):
    """Bring the target chess window forward before a real mouse drag."""
    user32 = _autoplay_user32()
    if user32 is None or not hwnd:
        return False
    try:
        window = ctypes.c_void_p(int(hwnd)) if not isinstance(hwnd, ctypes.c_void_p) else hwnd
        if not user32.IsWindow(window):
            return False
        if user32.IsIconic(window):
            user32.ShowWindow(window, 9)  # SW_RESTORE
        user32.BringWindowToTop(window)
        focused = bool(user32.SetForegroundWindow(window))
        time.sleep(0.05)
        return focused
    except Exception:
        return False


def _autoplay_click_screen_point(point):
    user32 = _autoplay_user32()
    if user32 is None:
        return False
    try:
        if not user32.SetCursorPos(int(point[0]), int(point[1])):
            return False
        time.sleep(0.03)
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
        time.sleep(0.03)
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
        return True
    except Exception:
        return False


def _autoplay_drag_screen_points(start, end, duration=AUTOPLAY_DRAG_SECONDS):
    """Drag the real cursor from one desktop point to another in <=0.4s."""
    user32 = _autoplay_user32()
    if user32 is None:
        return False
    duration = max(0.05, min(0.4, float(duration)))
    mouse_down = False
    try:
        if not user32.SetCursorPos(int(start[0]), int(start[1])):
            return False
        time.sleep(0.03)
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
        mouse_down = True
        started = time.monotonic()
        steps = 24
        for step in range(1, steps + 1):
            fraction = step / steps
            x = int(round(start[0] + (end[0] - start[0]) * fraction))
            y = int(round(start[1] + (end[1] - start[1]) * fraction))
            if not user32.SetCursorPos(x, y):
                return False
            target_elapsed = duration * fraction
            remaining = target_elapsed - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
        time.sleep(0.02)
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
        mouse_down = False
        return True
    except Exception:
        return False
    finally:
        if mouse_down:
            try:
                user32.mouse_event(0x0004, 0, 0, 0, 0)
            except Exception:
                pass


def _autoplay_drag_move(move, screen_board_rect, side, hwnd=None):
    """Focus the target and drag one chess move on its real board."""
    if screen_board_rect is None or move is None:
        return False
    _autoplay_focus_window(hwnd)
    start, end = _autoplay_move_screen_points(screen_board_rect, move, side)
    if not _autoplay_drag_screen_points(start, end):
        return False

    if move.promotion:
        # Chess.com presents promotion choices down the destination file
        # from the promotion square. Queen is on the destination itself;
        # this also harmlessly confirms auto-queen boards in most themes.
        # For an underpromotion, classify those choice cells and click
        # the one that actually shows the desired piece when possible.
        time.sleep(0.12)
        order = (chess.QUEEN, chess.KNIGHT, chess.ROOK, chess.BISHOP)
        try:
            choice_index = order.index(move.promotion)
        except ValueError:
            choice_index = 0
        dest_row, dest_col = _autoplay_square_visual(move.to_square, side)
        direction = 1 if dest_row == 0 else -1
        choice_row = max(0, min(7, dest_row + direction * choice_index))

        if move.promotion != chess.QUEEN:
            try:
                crop = _autoplay_capture_board_crop()
            except Exception:
                crop = None
            if crop is not None and _autoplay.get("enabled", True):
                choice_cells = [
                    (max(0, min(7, dest_row + direction * index)), dest_col)
                    for index in range(4)
                ]
                classified = _autoplay_classify_visual_squares(
                    crop,
                    choice_cells,
                )
                desired = chess.Piece(
                    move.promotion,
                    chess.WHITE if _autoplay_side_name(side) == "white" else chess.BLACK,
                ).symbol()
                for cell in choice_cells:
                    if classified.get(cell) == desired:
                        choice_row = cell[0]
                        break

        left, top, right, bottom = _board_cell_bounds(
            screen_board_rect, choice_row, dest_col
        )
        _autoplay_click_screen_point(((left + right) // 2, (top + bottom) // 2))
    return True


def _autoplay_stop_hotkey_down():
    """True while F8 is held anywhere on Windows (even target focused)."""
    user32 = _autoplay_user32()
    if user32 is None:
        return False
    try:
        return bool(user32.GetAsyncKeyState(AUTOPLAY_STOP_VK) & 0x8000)
    except Exception:
        return False


def _make_thumbnail_surface(image, max_w=276, max_h=92):
    """Convert a BGR screenshot to a small pygame preview surface."""
    try:
        import numpy as np

        if image is None:
            raise ValueError("no screenshot")
        rgb = np.ascontiguousarray(image[:, :, :3][:, :, ::-1].transpose((1, 0, 2)))
        surface = pygame.surfarray.make_surface(rgb)
        scale = min(max_w / surface.get_width(), max_h / surface.get_height())
        new_size = (
            max(1, int(surface.get_width() * scale)),
            max(1, int(surface.get_height() * scale)),
        )
        return pygame.transform.smoothscale(surface, new_size)
    except Exception:
        surface = pygame.Surface((max_w, max_h))
        surface.fill((45, 45, 52))
        return surface


def _refresh_board_detection_windows():
    global board_detection_windows, board_detection_selected
    global board_detection_page, board_detection_error
    windows = _enumerate_capture_windows()
    for window in windows:
        try:
            image, _rect = _capture_window_bgr(window["hwnd"])
        except Exception:
            image = None
        window["thumbnail"] = _make_thumbnail_surface(image)
    board_detection_windows = windows
    board_detection_selected = max(
        0, min(board_detection_selected, len(windows) - 1)
    ) if windows else 0
    board_detection_page = board_detection_selected // BOARD_DETECTION_WINDOWS_PER_PAGE
    if windows:
        board_detection_error = ""
    elif os.name != "nt":
        board_detection_error = "Window capture is available on Windows only."
    else:
        board_detection_error = "No suitable visible windows found. Open the chess window and Refresh."
    return windows


def _anchor_summary(anchor):
    if not anchor:
        return "not checked"
    expected = anchor.get("expected_name", "piece")
    if anchor.get("match") is True:
        return f"{anchor.get('coordinate', '')} {expected} OK"
    if anchor.get("detected_name"):
        return (
            f"{anchor.get('coordinate', '')} expected {expected}, "
            f"found {anchor['detected_name']}"
        )
    return f"{anchor.get('coordinate', '')} {expected} (grid only)"


def _board_detection_summary(result):
    rect = result.get("board_rect", (0, 0, 0, 0))
    anchors = result.get("anchors", {})
    summary = (
        f"Board found at x={rect[0]}, y={rect[1]}, size={rect[2]}px - "
        f"{_anchor_summary(anchors.get('top_left'))}; "
        f"{_anchor_summary(anchors.get('bottom_right'))}."
    )

    piece_grid = result.get("piece_grid")
    if piece_grid:
        flat = [symbol for row in piece_grid for symbol in row]
        piece_count = result.get(
            "piece_count",
            sum(1 for symbol in flat if symbol not in (".", "?", None)),
        )
        blank_count = result.get(
            "blank_count",
            sum(1 for symbol in flat if symbol == "."),
        )
        unknown_count = len(result.get("unknown_squares", []))
        if "unknown_squares" not in result:
            unknown_count = sum(1 for symbol in flat if symbol == "?")
        summary += (
            f" Pieces: {piece_count}, blanks: {blank_count}, "
            f"unknown: {unknown_count}."
        )
        if result.get("cnn_model_path"):
            summary += f" CNN: {os.path.basename(result['cnn_model_path'])}."
    return summary


def _board_detection_worker(window_info):
    global board_detection_running, board_detection_result
    global board_detection_result_ready, board_detection_error
    global board_detection_status, board_detection_last_summary
    try:
        import cv2

        _get_cnn_classifier()
        image, window_rect = _capture_window_bgr(window_info.get("hwnd"))
        if image is None:
            raise RuntimeError("Could not screenshot the selected window")
        detection = detect_chessboard_in_image(image)
        if detection is None:
            board_detection_error = (
                "No Chess.com-coloured 8x8 board found in that window. "
                "Make sure the full board is visible and try again."
            )
            board_detection_last_summary = board_detection_error
            return

        board_rect = detection["board_rect"]
        if window_rect:
            detection["window_rect"] = window_rect
            detection["screen_board_rect"] = (
                window_rect[0] + board_rect[0],
                window_rect[1] + board_rect[1],
                board_rect[2],
                board_rect[3],
            )
        detection["window_title"] = window_info.get("title", "Selected window")
        detection["screenshot"] = image
        detection["annotated"] = annotate_board_detection(image, detection)

        # Keep debug copies next to the existing board-vision outputs.
        try:
            os.makedirs("board_debug", exist_ok=True)
            detection["screenshot_path"] = os.path.join(
                "board_debug", "board_detection_screenshot.png"
            )
            detection["annotated_path"] = os.path.join(
                "board_debug", "board_detection_annotated.png"
            )
            cv2.imwrite(detection["screenshot_path"], image)
            cv2.imwrite(detection["annotated_path"], detection["annotated"])
        except Exception as exc:
            print(f"Board Detection: could not save debug screenshots: {exc}")

        board_detection_result = detection
        board_detection_last_summary = _board_detection_summary(detection)
        board_detection_status = board_detection_last_summary
        board_detection_error = ""
        board_detection_result_ready = True
    except Exception as exc:
        board_detection_error = f"Board detection failed: {exc}"
        board_detection_last_summary = board_detection_error
        print(board_detection_error)
    finally:
        board_detection_running = False


def start_board_detection(window_info):
    global show_board_detection_dialog, board_detection_running
    global board_detection_result, board_detection_result_ready
    global board_detection_error, board_detection_status, _board_detection_thread
    global board_detection_picker_purpose
    if board_detection_running or not window_info:
        return False
    board_detection_picker_purpose = "detect"
    show_board_detection_dialog = False
    board_detection_result = None
    board_detection_result_ready = False
    board_detection_error = ""
    board_detection_status = f"Capturing '{window_info.get('title', 'window')}'..."
    board_detection_running = True
    _board_detection_thread = threading.Thread(
        target=_board_detection_worker,
        args=(dict(window_info),),
        daemon=True,
    )
    _board_detection_thread.start()
    return True


def show_board_detection_result(result):
    """Open the CNN-annotated Board Detection screenshot."""
    global board_detection_error, board_detection_status
    global board_detection_last_summary
    try:
        import cv2

        annotated = result.get("annotated")
        if annotated is None:
            raise RuntimeError("No annotated screenshot available")

        title = "Board Detection Result - press Q/Esc to return"
        cv2.namedWindow(title, cv2.WINDOW_NORMAL)
        try:
            info = pygame.display.Info()
            max_w = max(480, info.current_w - 80)
            max_h = max(360, info.current_h - 80)
            scale = min(1.0, max_w / annotated.shape[1], max_h / annotated.shape[0])
            if scale < 1.0:
                cv2.resizeWindow(
                    title,
                    max(320, int(annotated.shape[1] * scale)),
                    max(240, int(annotated.shape[0] * scale)),
                )
            while True:
                cv2.imshow(title, annotated)
                key = cv2.waitKey(50) & 0xFF
                if key in (27, ord("q"), ord("Q"), 13, 10, 32):
                    break
                try:
                    if cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                        break
                except Exception:
                    pass
        finally:
            try:
                cv2.destroyWindow(title)
            except Exception:
                pass
        board_detection_last_summary = _board_detection_summary(result)
        board_detection_status = board_detection_last_summary
        return True
    except Exception as exc:
        board_detection_error = f"Board found, but the preview window could not open: {exc}"
        board_detection_last_summary = _board_detection_summary(result)
        board_detection_status = board_detection_last_summary
        print(board_detection_error)
        return False


def update_board_detection():
    """Open a finished worker's result on the main thread."""
    global board_detection_result_ready
    if board_detection_result_ready and board_detection_result is not None:
        board_detection_result_ready = False
        show_board_detection_result(board_detection_result)




# --- Auto-play session control ---
def _autoplay_set_status(status, error=""):
    _autoplay["status"] = str(status or "")
    _autoplay["error"] = str(error or "")


def stop_autoplay(reason="Auto-play stopped", error=False):
    """Stop any scan/game loop and close Auto-play's modal dialogs."""
    global show_autoplay_side_dialog, show_autoplay_gameover_dialog
    global show_board_detection_dialog, board_detection_picker_purpose
    _autoplay["scan_id"] = int(_autoplay.get("scan_id", 0)) + 1
    _autoplay.update({
        "enabled": False,
        "running": False,
        "state": "idle",
        "scan_running": False,
        "scan_ready": False,
        "scan_result": None,
        "pending_move": None,
        "pending_before_image": None,
        "pending_before_grid": None,
        "game_result": None,
    })
    _autoplay_set_status(reason, error=reason if error else "")
    show_autoplay_side_dialog = False
    show_autoplay_gameover_dialog = False
    if board_detection_picker_purpose == "autoplay":
        show_board_detection_dialog = False
        board_detection_picker_purpose = "detect"
    if reason:
        print(f"Auto-play: {reason}")
    return False


def request_autoplay_start():
    """Begin the window -> side -> first-scan Auto-play setup flow."""
    if _autoplay.get("enabled") or _autoplay.get("running") or _autoplay.get("scan_running"):
        return stop_autoplay()
    if os.name != "nt":
        _autoplay_set_status(
            "Auto-play needs Windows window capture and mouse control.",
            error="Auto-play needs Windows window capture and mouse control.",
        )
        return False
    if int(ANALYSIS_DEPTH or 0) < AUTOPLAY_DEPTH:
        message = f"Set Depth Limit to at least {AUTOPLAY_DEPTH} before Auto-play."
        _autoplay_set_status(message, error=message)
        return False
    if board_detection_running:
        message = "Wait for Board Detection to finish before starting Auto-play."
        _autoplay_set_status(message, error=message)
        return False

    _autoplay.update({
        "enabled": True,
        "running": False,
        "state": "choosing_window",
        "pending_window": None,
        "window": None,
        "game_result": None,
    })
    _autoplay_set_status("Choose the window you are playing on.")
    if not open_board_detection_dialog("autoplay"):
        return stop_autoplay("Could not open the Auto-play window picker.", error=True)
    return True


def _autoplay_window_selected(window_info):
    """Store the picker choice and ask which side the user is playing."""
    global show_autoplay_side_dialog, autoplay_side_dialog_anim
    global show_board_detection_dialog, board_detection_picker_purpose
    if not window_info:
        return stop_autoplay("No Auto-play window was selected.", error=True)
    _autoplay["pending_window"] = dict(window_info)
    _autoplay["window"] = dict(window_info)
    _autoplay["state"] = "choosing_side"
    _autoplay_set_status("Choose the side you are playing.")
    show_board_detection_dialog = False
    board_detection_picker_purpose = "detect"
    show_autoplay_side_dialog = True
    autoplay_side_dialog_anim = 0.0
    return True


def _autoplay_scan_worker(window_info, side, scan_id):
    """Capture the target and run the one full-board Auto-play scan."""
    try:
        _get_cnn_classifier()
        image, window_rect = _capture_window_bgr(window_info.get("hwnd"))
        if image is None:
            raise RuntimeError("Could not screenshot the selected window")
        expected_anchors = ("R", "r") if _autoplay_side_name(side) == "black" else None
        detection = detect_chessboard_in_image(
            image,
            expected_anchors=expected_anchors,
        )
        if detection is None:
            raise RuntimeError(
                "No Chess.com-coloured 8x8 board found in that window. "
                "Make sure the full board is visible and try again."
            )
        board_rect = detection["board_rect"]
        detection["window"] = dict(window_info)
        detection["window_title"] = window_info.get("title", "Selected window")
        detection["side"] = _autoplay_side_name(side)
        detection["screenshot"] = image
        if window_rect:
            detection["window_rect"] = window_rect
            detection["window_size"] = (
                int(window_rect[2] - window_rect[0]),
                int(window_rect[3] - window_rect[1]),
            )
            detection["screen_board_rect"] = (
                window_rect[0] + board_rect[0],
                window_rect[1] + board_rect[1],
                board_rect[2],
                board_rect[3],
            )
        if int(_autoplay.get("scan_id", 0)) != int(scan_id) or not _autoplay.get("enabled"):
            return
        _autoplay["scan_result"] = detection
        _autoplay["scan_ready"] = True
        _autoplay["state"] = "importing"
        _autoplay_set_status("Board scanned. Importing the position as FEN...")
    except Exception as exc:
        if int(_autoplay.get("scan_id", 0)) == int(scan_id) and _autoplay.get("enabled"):
            stop_autoplay(f"Auto-play scan failed: {exc}", error=True)
    finally:
        if int(_autoplay.get("scan_id", 0)) == int(scan_id):
            _autoplay["scan_running"] = False


def start_autoplay_scan(side=None):
    """Start (or restart for a new game) the initial full-board scan."""
    global menu_anim_closing
    window_info = _autoplay.get("pending_window") or _autoplay.get("window")
    if not window_info:
        return stop_autoplay("Choose a window before scanning.", error=True)
    side = _autoplay_side_name(side or _autoplay.get("side", "white"))
    scan_id = int(_autoplay.get("scan_id", 0)) + 1
    _autoplay.update({
        "enabled": True,
        "running": False,
        "state": "scanning",
        "window": dict(window_info),
        "pending_window": dict(window_info),
        "side": side,
        "scan_id": scan_id,
        "scan_running": True,
        "scan_ready": False,
        "scan_result": None,
        "last_image": None,
        "last_grid": None,
        "expected_fen": None,
        "pending_move": None,
        "pending_before_image": None,
        "pending_before_grid": None,
        "drag_attempts": 0,
        "game_result": None,
    })
    _autoplay_set_status(
        f"Scanning the full board as {side.capitalize()}..."
    )
    try:
        begin_settings_menu_close()
    except Exception:
        pass
    thread = threading.Thread(
        target=_autoplay_scan_worker,
        args=(dict(window_info), side, scan_id),
        name="autoplay-scan",
        daemon=True,
    )
    thread.start()
    return True


def restart_autoplay_scan():
    """Scan a newly started game in the same window/as the same side."""
    global show_autoplay_gameover_dialog
    show_autoplay_gameover_dialog = False
    if not _autoplay.get("window"):
        return request_autoplay_start()
    _autoplay["enabled"] = True
    return start_autoplay_scan(_autoplay.get("side", "white"))


def _autoplay_board_crop_from_screenshot(image, board_rect):
    if image is None or board_rect is None:
        return None
    x, y, width, height = [int(value) for value in board_rect]
    side = width
    return image[y:y + side, x:x + side].copy()


def _autoplay_capture_board_crop():
    """Capture the target and keep only its already-located board crop."""
    window_info = _autoplay.get("window") or {}
    board_rect = _autoplay.get("board_rect")
    if not window_info or board_rect is None:
        return None
    image, window_rect = _capture_window_bgr(window_info.get("hwnd"))
    if image is None or window_rect is None:
        return None
    window_size = (
        int(window_rect[2] - window_rect[0]),
        int(window_rect[3] - window_rect[1]),
    )
    expected_size = _autoplay.get("window_size")
    if expected_size is not None and tuple(expected_size) != window_size:
        stop_autoplay(
            "The target window was resized; restart Auto-play to scan its board again.",
            error=True,
        )
        return None
    _autoplay["window_rect"] = window_rect
    _autoplay["window_size"] = window_size
    _autoplay["screen_board_rect"] = (
        window_rect[0] + board_rect[0],
        window_rect[1] + board_rect[1],
        board_rect[2],
        board_rect[3],
    )
    return _autoplay_board_crop_from_screenshot(image, board_rect)


def get_autoplay_side_dialog_rects():
    e = cubic_ease_in_out(max(0.0, min(1.0, autoplay_side_dialog_anim)))
    w, h = 440, 224
    x = WIDTH // 2 - w // 2
    y = HEIGHT // 2 - h // 2 - int((1 - e) * 18)
    panel = pygame.Rect(x, y, w, h)
    bw, bh, gap = 168, 48, 16
    total = 2 * bw + gap
    x0 = panel.centerx - total // 2
    choices = {
        side: pygame.Rect(x0 + index * (bw + gap), y + 104, bw, bh)
        for index, side in enumerate(("white", "black"))
    }
    cancel = pygame.Rect(panel.centerx - 70, y + h - 52, 140, 36)
    return panel, choices, cancel


def draw_autoplay_side_dialog(screen):
    e = cubic_ease_in_out(max(0.0, min(1.0, autoplay_side_dialog_anim)))
    panel, choices, cancel = get_autoplay_side_dialog_rects()
    overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    overlay.fill((0, 0, 0, int(170 * e)))
    screen.blit(overlay, (0, 0))
    pygame.draw.rect(screen, (18, 18, 22), panel.move(0, 5), border_radius=14)
    pygame.draw.rect(screen, (52, 52, 64), panel, border_radius=14)
    pygame.draw.rect(screen, (130, 220, 150), panel, 2, border_radius=14)

    title = font_large.render("Auto-play", True, TEXT_COLOR)
    screen.blit(title, title.get_rect(center=(panel.centerx, panel.y + 30)))
    question = font_small.render("Which side are you playing?", True, TEXT_MUTED)
    screen.blit(question, question.get_rect(center=(panel.centerx, panel.y + 60)))
    window_info = _autoplay.get("pending_window") or _autoplay.get("window") or {}
    window_title = window_info.get("title", "Selected window")
    window_s = font_tiny.render(
        _elide_text(window_title, font_tiny, panel.width - 36),
        True, TEXT_ACCENT,
    )
    screen.blit(window_s, window_s.get_rect(center=(panel.centerx, panel.y + 82)))

    mx, my = pygame.mouse.get_pos()
    for side, rect in choices.items():
        hovered = rect.collidepoint(mx, my)
        bg = (74, 158, 96) if hovered else (66, 66, 80)
        pygame.draw.rect(screen, bg, rect, border_radius=10)
        pygame.draw.rect(screen, MENU_BORDER_COLOR, rect, 2, border_radius=10)
        pygame.draw.circle(
            screen,
            (240, 240, 240) if side == "white" else (25, 25, 25),
            (rect.x + 24, rect.centery),
            9,
        )
        text = font_small.render(side.capitalize(), True, TEXT_COLOR)
        screen.blit(text, text.get_rect(center=(rect.centerx + 10, rect.centery)))

    _draw_board_detection_button(screen, cancel, "Cancel")
    hint = font_tiny.render("The board is scanned once; later polls check changed squares only.", True, TEXT_MUTED)
    if hint.get_width() <= panel.width + 80:
        screen.blit(hint, hint.get_rect(center=(panel.centerx, panel.bottom + 16)))
    return choices, cancel


def handle_autoplay_side_click(mx, my):
    global show_autoplay_side_dialog
    panel, choices, cancel = get_autoplay_side_dialog_rects()
    for side, rect in choices.items():
        if rect.collidepoint(mx, my):
            show_autoplay_side_dialog = False
            start_autoplay_scan(side)
            return True
    if cancel.collidepoint(mx, my) or not panel.collidepoint(mx, my):
        return stop_autoplay("Auto-play cancelled before scanning.")
    return True


def get_autoplay_gameover_dialog_rects():
    e = cubic_ease_in_out(max(0.0, min(1.0, autoplay_gameover_dialog_anim)))
    w, h = 480, 228
    x = WIDTH // 2 - w // 2
    y = HEIGHT // 2 - h // 2 - int((1 - e) * 18)
    panel = pygame.Rect(x, y, w, h)
    scan_rect = pygame.Rect(panel.centerx - 176, panel.bottom - 62, 176, 42)
    stop_rect = pygame.Rect(panel.centerx + 16, panel.bottom - 62, 140, 42)
    return panel, scan_rect, stop_rect


def draw_autoplay_gameover_dialog(screen):
    e = cubic_ease_in_out(max(0.0, min(1.0, autoplay_gameover_dialog_anim)))
    panel, scan_rect, stop_rect = get_autoplay_gameover_dialog_rects()
    overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    overlay.fill((0, 0, 0, int(175 * e)))
    screen.blit(overlay, (0, 0))
    pygame.draw.rect(screen, (18, 18, 22), panel.move(0, 5), border_radius=14)
    pygame.draw.rect(screen, (52, 52, 64), panel, border_radius=14)
    pygame.draw.rect(screen, (130, 220, 150), panel, 2, border_radius=14)

    result = _autoplay.get("game_result") or ("Game over", "", "", TEXT_COLOR)
    title_text = str(result[0]) if len(result) > 0 else "Game over"
    subtitle_text = str(result[1]) if len(result) > 1 else ""
    score_text = str(result[2]) if len(result) > 2 else ""
    colour = result[3] if len(result) > 3 else TEXT_COLOR
    title = font_large.render(title_text, True, colour)
    screen.blit(title, title.get_rect(center=(panel.centerx, panel.y + 36)))
    detail = " ".join(part for part in (subtitle_text, score_text) if part)
    if detail:
        detail_s = font_small.render(detail, True, TEXT_MUTED)
        screen.blit(detail_s, detail_s.get_rect(center=(panel.centerx, panel.y + 68)))
    question = font_small.render("Are you starting again?", True, TEXT_COLOR)
    screen.blit(question, question.get_rect(center=(panel.centerx, panel.y + 104)))
    hint = font_tiny.render("Start the new game in the target window, then scan it.", True, TEXT_MUTED)
    screen.blit(hint, hint.get_rect(center=(panel.centerx, panel.y + 128)))

    _draw_board_detection_button(screen, scan_rect, "Scan New Game", primary=True)
    _draw_board_detection_button(screen, stop_rect, "Stop")
    return scan_rect, stop_rect


def handle_autoplay_gameover_click(mx, my):
    global show_autoplay_gameover_dialog
    _panel, scan_rect, stop_rect = get_autoplay_gameover_dialog_rects()
    if scan_rect.collidepoint(mx, my):
        show_autoplay_gameover_dialog = False
        return restart_autoplay_scan()
    if stop_rect.collidepoint(mx, my):
        return stop_autoplay()
    return True


def get_board_detection_button_rect(offset_x=0, offset_y=0):
    # Sit below the always-visible Auto-Move depth row, and below the
    # colour/status controls too when Auto-Move is enabled.
    y = 494 if auto_move_enabled else 386
    return pygame.Rect(WIDTH // 2 - 220 + offset_x, y + offset_y, 440, 88)


def _elide_text(text, font, max_width):
    text = str(text)
    if font.size(text)[0] <= max_width:
        return text
    suffix = "..."
    while text and font.size(text + suffix)[0] > max_width:
        text = text[:-1]
    return text + suffix if text else suffix


def open_board_detection_dialog(purpose="detect"):
    global show_board_detection_dialog, board_detection_dialog_anim
    global board_detection_status, board_detection_picker_purpose
    global board_detection_error
    if board_detection_running:
        return False
    purpose = "autoplay" if purpose == "autoplay" else "detect"
    if purpose == "detect" and _autoplay.get("enabled"):
        board_detection_error = "Stop Auto-play before running Board Detection."
        board_detection_status = board_detection_error
        return False
    board_detection_picker_purpose = purpose
    board_detection_status = "Refreshing window previews..."
    _refresh_board_detection_windows()
    board_detection_status = ""
    show_board_detection_dialog = True
    board_detection_dialog_anim = 0.0
    return True


def get_board_detection_dialog_rects():
    e = cubic_ease_in_out(max(0.0, min(1.0, board_detection_dialog_anim)))
    w, h = 680, 720
    x = WIDTH // 2 - w // 2
    y = HEIGHT // 2 - h // 2 - int((1 - e) * 18)
    panel = pygame.Rect(x, y, w, h)

    cards = []
    start = board_detection_page * BOARD_DETECTION_WINDOWS_PER_PAGE
    visible = board_detection_windows[start:start + BOARD_DETECTION_WINDOWS_PER_PAGE]
    for index, _window in enumerate(visible):
        card = pygame.Rect(
            panel.x + 32 + (index % 2) * (300 + 16),
            panel.y + 108 + (index // 2) * (150 + 14),
            300,
            150,
        )
        cards.append((start + index, card))

    prev_rect = pygame.Rect(panel.x + 32, panel.y + 66, 74, 30)
    next_rect = pygame.Rect(panel.x + 114, panel.y + 66, 74, 30)
    refresh_rect = pygame.Rect(panel.x + 32, panel.bottom - 62, 130, 40)
    cancel_rect = pygame.Rect(panel.centerx - 65, panel.bottom - 62, 130, 40)
    detect_rect = pygame.Rect(panel.right - 32 - 170, panel.bottom - 62, 170, 40)
    return panel, cards, refresh_rect, cancel_rect, detect_rect, prev_rect, next_rect


def _draw_board_detection_button(screen, rect, label, enabled=True, primary=False):
    mx, my = pygame.mouse.get_pos()
    hovered = enabled and rect.collidepoint(mx, my)
    if not enabled:
        bg = (55, 55, 62)
    elif primary:
        bg = (74, 158, 96) if hovered else (56, 130, 78)
    else:
        bg = (92, 92, 110) if hovered else (70, 70, 82)
    pygame.draw.rect(screen, bg, rect, border_radius=9)
    pygame.draw.rect(
        screen,
        (140, 230, 160) if hovered and primary else MENU_BORDER_COLOR,
        rect, 2, border_radius=9,
    )
    text = font_small.render(label, True, TEXT_COLOR if enabled else TEXT_MUTED)
    screen.blit(text, text.get_rect(center=rect.center))


def draw_board_detection_dialog(screen):
    e = cubic_ease_in_out(max(0.0, min(1.0, board_detection_dialog_anim)))
    panel, cards, refresh_rect, cancel_rect, detect_rect, prev_rect, next_rect = (
        get_board_detection_dialog_rects()
    )
    overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    overlay.fill((0, 0, 0, int(175 * e)))
    screen.blit(overlay, (0, 0))

    pygame.draw.rect(screen, (18, 18, 22), panel.move(0, 5), border_radius=14)
    pygame.draw.rect(screen, (52, 52, 64), panel, border_radius=14)
    pygame.draw.rect(screen, (80, 160, 255), panel, 2, border_radius=14)

    autoplay_picker = board_detection_picker_purpose == "autoplay"
    title = font_large.render("Auto-play" if autoplay_picker else "Board Detection", True, TEXT_COLOR)
    screen.blit(title, (panel.x + 32, panel.y + 20))
    subtitle_text = (
        "Choose the window you are playing on"
        if autoplay_picker else "Choose the window containing the chessboard"
    )
    subtitle = font_small.render(subtitle_text, True, TEXT_MUTED)
    screen.blit(subtitle, (panel.x + 32, panel.y + 50))

    page_count = max(
        1,
        (len(board_detection_windows) + BOARD_DETECTION_WINDOWS_PER_PAGE - 1)
        // BOARD_DETECTION_WINDOWS_PER_PAGE,
    )
    _draw_board_detection_button(screen, prev_rect, "Prev", enabled=board_detection_page > 0)
    _draw_board_detection_button(
        screen, next_rect, "Next", enabled=board_detection_page < page_count - 1
    )
    page_text = font_tiny.render(
        f"Page {board_detection_page + 1} of {page_count}", True, TEXT_MUTED
    )
    screen.blit(page_text, (next_rect.right + 14, prev_rect.y + 7))

    mx, my = pygame.mouse.get_pos()
    if not board_detection_windows:
        message = board_detection_error or "No windows available."
        message_s = font_small.render(_elide_text(message, font_small, panel.width - 80), True, TEXT_MUTED)
        screen.blit(message_s, message_s.get_rect(center=(panel.centerx, panel.centery - 20)))
        hint = font_tiny.render("Open the chess window, then press Refresh.", True, TEXT_MUTED)
        screen.blit(hint, hint.get_rect(center=(panel.centerx, panel.centery + 12)))
    else:
        for window_index, card in cards:
            window = board_detection_windows[window_index]
            selected = window_index == board_detection_selected
            hovered = card.collidepoint(mx, my)
            bg = (72, 82, 112) if selected else (64, 64, 76) if hovered else (56, 56, 66)
            pygame.draw.rect(screen, bg, card, border_radius=10)
            pygame.draw.rect(
                screen,
                (80, 220, 140) if selected else (130, 170, 255) if hovered else MENU_BORDER_COLOR,
                card, 3 if selected else 2, border_radius=10,
            )
            preview_rect = pygame.Rect(card.x + 10, card.y + 8, card.width - 20, 92)
            pygame.draw.rect(screen, (12, 12, 14), preview_rect, border_radius=6)
            thumb = window.get("thumbnail")
            if thumb is not None:
                screen.blit(thumb, thumb.get_rect(center=preview_rect.center))
            title_text = _elide_text(window.get("title", "Untitled window"), font_tiny, card.width - 20)
            title_s = font_tiny.render(title_text, True, TEXT_COLOR)
            screen.blit(title_s, (card.x + 10, card.y + 108))
            rect = window.get("rect", (0, 0, 0, 0))
            dims = f"{rect[2] - rect[0]} x {rect[3] - rect[1]}"
            dims_s = font_tiny.render(dims, True, TEXT_MUTED)
            screen.blit(dims_s, (card.x + 10, card.y + 128))
            if selected:
                sel_s = font_tiny.render("Selected", True, (140, 230, 160))
                screen.blit(sel_s, (card.right - sel_s.get_width() - 10, card.y + 128))

    if board_detection_windows:
        selected_title = board_detection_windows[board_detection_selected].get("title", "")
        detail = f"Selected: {_elide_text(selected_title, font_tiny, panel.width - 140)}"
    elif board_detection_error:
        detail = board_detection_error
    else:
        detail = (
            "Select a preview, then Use Window."
            if autoplay_picker else "Select a preview, then Capture & Detect."
        )
    detail_s = font_tiny.render(_elide_text(detail, font_tiny, panel.width - 64), True, TEXT_MUTED)
    screen.blit(detail_s, (panel.x + 32, panel.bottom - 92))

    _draw_board_detection_button(screen, refresh_rect, "Refresh")
    _draw_board_detection_button(screen, cancel_rect, "Cancel")
    _draw_board_detection_button(
        screen,
        detect_rect,
        "Working..." if board_detection_running else "Use Window" if autoplay_picker else "Capture & Detect",
        enabled=bool(board_detection_windows) and not board_detection_running,
        primary=True,
    )


def _board_detection_page_count():
    return max(
        1,
        (len(board_detection_windows) + BOARD_DETECTION_WINDOWS_PER_PAGE - 1)
        // BOARD_DETECTION_WINDOWS_PER_PAGE,
    )


def move_board_detection_page(delta):
    global board_detection_page
    board_detection_page = max(
        0, min(_board_detection_page_count() - 1, board_detection_page + int(delta))
    )
    return board_detection_page


def handle_board_detection_click(mx, my):
    global show_board_detection_dialog, board_detection_selected
    global board_detection_picker_purpose
    panel, cards, refresh_rect, cancel_rect, detect_rect, prev_rect, next_rect = (
        get_board_detection_dialog_rects()
    )
    if cancel_rect.collidepoint(mx, my) or not panel.collidepoint(mx, my):
        if board_detection_picker_purpose == "autoplay":
            stop_autoplay("Auto-play cancelled before choosing a window.")
        else:
            show_board_detection_dialog = False
            board_detection_picker_purpose = "detect"
        return True
    if refresh_rect.collidepoint(mx, my):
        _refresh_board_detection_windows()
        return True
    if prev_rect.collidepoint(mx, my):
        move_board_detection_page(-1)
        return True
    if next_rect.collidepoint(mx, my):
        move_board_detection_page(1)
        return True
    for window_index, card in cards:
        if card.collidepoint(mx, my):
            board_detection_selected = window_index
            return True
    if detect_rect.collidepoint(mx, my) and board_detection_windows:
        selected_window = board_detection_windows[board_detection_selected]
        if board_detection_picker_purpose == "autoplay":
            _autoplay_window_selected(selected_window)
        else:
            start_board_detection(selected_window)
        return True
    return True


def handle_board_detection_key(key):
    global show_board_detection_dialog, board_detection_selected, board_detection_page
    global board_detection_picker_purpose
    if key == pygame.K_ESCAPE:
        if board_detection_picker_purpose == "autoplay":
            stop_autoplay("Auto-play cancelled before choosing a window.")
        else:
            show_board_detection_dialog = False
            board_detection_picker_purpose = "detect"
        return True
    if key in (pygame.K_RETURN, pygame.K_KP_ENTER):
        if board_detection_windows:
            selected_window = board_detection_windows[board_detection_selected]
            if board_detection_picker_purpose == "autoplay":
                _autoplay_window_selected(selected_window)
            else:
                start_board_detection(selected_window)
        return True
    if key == pygame.K_r:
        _refresh_board_detection_windows()
        return True
    if not board_detection_windows:
        return True
    delta = 0
    if key == pygame.K_RIGHT:
        delta = 1
    elif key == pygame.K_LEFT:
        delta = -1
    elif key == pygame.K_DOWN:
        delta = 2
    elif key == pygame.K_UP:
        delta = -2
    elif key == pygame.K_PAGEDOWN:
        move_board_detection_page(1)
        return True
    elif key == pygame.K_PAGEUP:
        move_board_detection_page(-1)
        return True
    if delta:
        board_detection_selected = max(
            0,
            min(len(board_detection_windows) - 1, board_detection_selected + delta),
        )
        board_detection_page = (
            board_detection_selected // BOARD_DETECTION_WINDOWS_PER_PAGE
        )
    return True


# --- Toggle rows / Analysis page ---
def draw_toggle_row(screen, rect, label, subtitle, on, anim, hovered):
    """Card row with a sliding on/off pill on the right."""
    bg = (78, 78, 118) if hovered else (58, 58, 68)
    pygame.draw.rect(screen, bg, rect, border_radius=12)
    pygame.draw.rect(screen, (150, 170, 255) if hovered else MENU_BORDER_COLOR, rect, 2, border_radius=12)
    big = rect.height >= 70
    title_font = font_large if big else font_small
    title_s = title_font.render(label, True, TEXT_COLOR)
    ty = rect.y + 16 if big else rect.centery - title_s.get_height() // 2
    screen.blit(title_s, (rect.x + 18, ty))
    if subtitle and big:
        sub_s = font_tiny.render(subtitle, True, TEXT_MUTED)
        screen.blit(sub_s, (rect.x + 18, rect.y + 52))
    # pill + knob
    pill_w, pill_h = 54, 28
    pill = pygame.Rect(rect.right - 18 - pill_w, rect.centery - pill_h // 2, pill_w, pill_h)
    e = cubic_ease_in_out(max(0.0, min(1.0, anim)))
    on_col, off_col = (72, 175, 105), (95, 95, 105)
    pill_col = tuple(int(off_col[i] + (on_col[i] - off_col[i]) * e) for i in range(3))
    pygame.draw.rect(screen, pill_col, pill, border_radius=pill_h // 2)
    pygame.draw.rect(screen, (35, 35, 40), pill, 1, border_radius=pill_h // 2)
    knob_r = 11
    knob_x = int(pill.x + 4 + knob_r + e * (pill_w - 8 - knob_r * 2))
    pygame.draw.circle(screen, (235, 235, 240), (knob_x, pill.centery), knob_r)
    try:
        pygame.gfxdraw.aacircle(screen, knob_x, pill.centery, knob_r, (235, 235, 240))
    except Exception:
        pass
    state_s = font_tiny.render("On" if on else "Off", True, (140, 230, 160) if on else TEXT_MUTED)
    screen.blit(state_s, (pill.x - state_s.get_width() - 10, rect.centery - state_s.get_height() // 2))
    return pill


def get_always_on_top_rect(offset_x=0, offset_y=0):
    return pygame.Rect(WIDTH // 2 - 200 + offset_x, HEIGHT - 56 + offset_y, 400, 40)


def _draw_always_on_top_icon(
    surface, cx, cy, radius, anim=1.0, hover_amount=0.0, phase=0.0
):
    """Draw stacked windows with the front window rising when pinned.

    The front window literally lifts above the rear window as the existing
    0..1 Always on Top animation approaches the on position. A tiny upward
    arrow reinforces the meaning without relying on a text glyph.
    """
    cx, cy, radius = int(cx), int(cy), max(8, int(radius))
    hover_amount = max(0.0, min(1.0, hover_amount))
    e = cubic_ease_in_out(max(0.0, min(1.0, anim)))
    unit = radius / 12.0

    def scaled(value):
        return max(1, int(round(value * unit)))

    face_colour = _lerp_color((218, 224, 235), (225, 248, 235), e)
    inner_colour = _lerp_color((182, 191, 208), (184, 230, 200), e)
    rim_colour = _lerp_color((76, 84, 102), (43, 126, 78), e)
    rim_colour = _lerp_color(rim_colour, (255, 255, 255), 0.18 * hover_amount)
    accent_colour = _lerp_color((126, 142, 168), (57, 181, 105), e)

    # Layered enamel disc, matching the neighbouring About medallions.
    pygame.draw.circle(surface, (16, 20, 28), (cx, cy + 1), radius)
    pygame.draw.circle(surface, face_colour, (cx, cy), radius)
    pygame.draw.circle(
        surface, inner_colour, (cx, cy), max(5, radius - scaled(2))
    )
    pygame.draw.circle(surface, rim_colour, (cx, cy), radius, 1)
    try:
        pygame.gfxdraw.aacircle(surface, cx, cy, radius, (*rim_colour, 255))
        pygame.gfxdraw.aacircle(
            surface,
            cx,
            cy,
            max(5, radius - scaled(2)),
            (*_lerp_color(inner_colour, rim_colour, 0.45), 255),
        )
    except Exception:
        pass
    pygame.draw.circle(
        surface,
        (255, 255, 255),
        (int(cx - 7.0 * unit), int(cy - 7.0 * unit)),
        scaled(0.85),
    )

    bob = 0.35 * hover_amount * math.sin(phase)
    lift = 1.7 * e + bob
    back_rect = pygame.Rect(
        int(round(cx - 8.0 * unit)),
        int(round(cy - 3.5 * unit)),
        max(3, int(round(13.0 * unit))),
        max(3, int(round(10.0 * unit))),
    )
    front_rect = pygame.Rect(
        int(round(cx - 4.5 * unit)),
        int(round(cy + (0.5 - lift) * unit)),
        max(3, int(round(13.0 * unit))),
        max(3, int(round(10.0 * unit))),
    )
    corner = scaled(1.8)

    # Rear window: only its top and left edges peek out from the stack.
    back_fill = _lerp_color((111, 124, 148), (83, 130, 110), e)
    pygame.draw.rect(
        surface, (35, 42, 56), back_rect.move(0, scaled(0.8)), border_radius=corner
    )
    pygame.draw.rect(surface, back_fill, back_rect, border_radius=corner)
    pygame.draw.rect(surface, rim_colour, back_rect, 1, border_radius=corner)
    pygame.draw.line(
        surface,
        _lerp_color(back_fill, (255, 255, 255), 0.35),
        (back_rect.x + scaled(1.5), back_rect.y + scaled(3.0)),
        (back_rect.right - scaled(1.5), back_rect.y + scaled(3.0)),
        1,
    )

    # The upward arrow is drawn before the front window so its base tucks
    # behind the raised window at the end of the slide.
    arrow_cx = cx + 2.0 * unit
    arrow_tip_y = cy + (-8.8 - 0.45 * lift) * unit
    arrow_base_y = cy + (-4.9 - 0.45 * lift) * unit
    pygame.draw.line(
        surface,
        accent_colour,
        (arrow_cx, arrow_base_y),
        (arrow_cx, arrow_tip_y + scaled(1.4)),
        scaled(1.7),
    )
    arrowhead = [
        (arrow_cx, arrow_tip_y),
        (arrow_cx - 2.4 * unit, arrow_tip_y + 3.0 * unit),
        (arrow_cx + 2.4 * unit, arrow_tip_y + 3.0 * unit),
    ]
    pygame.draw.polygon(surface, accent_colour, arrowhead)
    try:
        pygame.gfxdraw.aapolygon(surface, arrowhead, (*accent_colour, 255))
    except Exception:
        pass

    # Front window with title bar, two window-control dots and content.
    front_fill = _lerp_color((220, 228, 240), (242, 255, 248), e)
    pygame.draw.rect(
        surface, (35, 42, 56), front_rect.move(0, scaled(0.9)), border_radius=corner
    )
    pygame.draw.rect(surface, front_fill, front_rect, border_radius=corner)
    pygame.draw.rect(surface, rim_colour, front_rect, 1, border_radius=corner)
    title_h = max(2, scaled(3.0))
    title_rect = pygame.Rect(
        front_rect.x + 1, front_rect.y + 1, front_rect.width - 2, title_h
    )
    pygame.draw.rect(surface, accent_colour, title_rect, border_radius=corner)
    for dot_dx in (2.0, 4.2):
        pygame.draw.circle(
            surface,
            (245, 250, 255),
            (
                int(front_rect.x + dot_dx * unit),
                int(front_rect.y + 1.55 * unit),
            ),
            scaled(0.55),
        )
    content_colour = _lerp_color(front_fill, rim_colour, 0.48)
    for line_dy, line_w in ((5.4, 7.2), (7.6, 5.2)):
        y = int(front_rect.y + line_dy * unit)
        pygame.draw.line(
            surface,
            content_colour,
            (int(front_rect.x + 2.0 * unit), y),
            (int(front_rect.x + (2.0 + line_w) * unit), y),
            1,
        )

    # A fine baseline anchors the stack inside the medallion.
    base_y = int(cy + 9.3 * unit)
    pygame.draw.line(
        surface,
        rim_colour,
        (int(cx - 7.8 * unit), base_y),
        (int(cx + 7.8 * unit), base_y),
        1,
    )


def draw_polished_always_on_top_toggle(screen, rect, on, anim, hovered):
    """Draw the Settings-main Always on Top row and return its pill rect.

    ``rect``, the 54x28 pill geometry and the caller's on/off animation
    semantics are unchanged; this renderer adds only visual polish and its
    own hover easing. Other toggle rows continue to use ``draw_toggle_row``.
    """
    global always_on_top_hover_amount, always_on_top_icon_phase

    try:
        pressed = bool(hovered and pygame.mouse.get_pressed()[0])
    except Exception:
        pressed = False

    hover_target = 1.0 if hovered else 0.0
    always_on_top_hover_amount += (
        hover_target - always_on_top_hover_amount
    ) * 0.22
    if abs(hover_target - always_on_top_hover_amount) < 0.01:
        always_on_top_hover_amount = hover_target
    hover_amount = max(0.0, min(1.0, always_on_top_hover_amount))
    if hovered:
        always_on_top_icon_phase = (
            always_on_top_icon_phase + (0.025 if pressed else 0.07)
        ) % (2.0 * math.pi)

    e = cubic_ease_in_out(max(0.0, min(1.0, anim)))
    visual_rect = rect.move(0, 1 if pressed else 0)
    state_accent = _lerp_color((116, 132, 160), (92, 220, 145), e)

    # State-tinted halo behind the whole row.
    if hover_amount > 0.01:
        glow = pygame.Surface(
            (visual_rect.width + 18, visual_rect.height + 18),
            pygame.SRCALPHA,
        )
        for inset, alpha in ((1, 15), (4, 24), (7, 36)):
            pygame.draw.rect(
                glow,
                (*state_accent, int(alpha * hover_amount)),
                (
                    inset,
                    inset,
                    glow.get_width() - inset * 2,
                    glow.get_height() - inset * 2,
                ),
                border_radius=16 - inset,
            )
        screen.blit(glow, (visual_rect.x - 9, visual_rect.y - 9))

    pygame.draw.rect(
        screen,
        (16, 18, 25),
        visual_rect.move(0, 1 if pressed else 2),
        border_radius=12,
    )

    if pressed:
        base_top, base_bottom = (58, 61, 76), (39, 41, 52)
        on_top, on_bottom = (54, 72, 65), (35, 52, 46)
    elif hovered:
        base_top, base_bottom = (83, 88, 112), (55, 59, 78)
        on_top, on_bottom = (73, 104, 91), (46, 74, 62)
    else:
        base_top, base_bottom = (66, 69, 84), (45, 47, 59)
        on_top, on_bottom = (62, 82, 76), (40, 61, 53)
    top_colour = _lerp_color(base_top, on_top, 0.45 * e)
    bottom_colour = _lerp_color(base_bottom, on_bottom, 0.45 * e)
    border_colour = _lerp_color((128, 139, 163), (102, 205, 140), e)
    border_colour = _lerp_color(
        border_colour, (255, 255, 255), 0.22 * hover_amount
    )

    # Slate-metal card body. Translucent detail is composited through an
    # overlay, then a rounded mask, as with the other polished controls.
    body = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    denom = max(1, visual_rect.height - 1)
    for row in range(visual_rect.height):
        colour = _lerp_color(top_colour, bottom_colour, row / denom)
        pygame.draw.line(
            body,
            (*colour, 255),
            (0, row),
            (visual_rect.width - 1, row),
        )

    overlay = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    pygame.draw.polygon(
        overlay,
        (255, 255, 255, int(8 + 13 * hover_amount)),
        [
            (-18, visual_rect.height),
            (visual_rect.width * 0.34, 0),
            (visual_rect.width * 0.52, 0),
            (visual_rect.width * 0.12, visual_rect.height),
        ],
    )
    pygame.draw.line(
        overlay,
        (255, 255, 255, int(58 + 42 * hover_amount)),
        (9, 1),
        (visual_rect.width - 10, 1),
        1,
    )
    pygame.draw.line(
        overlay,
        (255, 255, 255, 20),
        (2, 7),
        (2, visual_rect.height - 8),
        1,
    )
    pygame.draw.rect(
        overlay,
        (0, 0, 0, 42 if pressed else 29),
        (0, visual_rect.height - 4, visual_rect.width, 4),
    )
    pygame.draw.rect(
        overlay,
        (0, 0, 0, 24),
        (visual_rect.width - 3, 6, 3, visual_rect.height - 12),
        border_radius=2,
    )

    # A very faint stacked-window watermark occupies the otherwise empty
    # middle of the 400 px row; its front sheet rises with the toggle.
    watermark_alpha = int(12 + 16 * hover_amount + 8 * e)
    watermark_colour = (205, 232, 255, watermark_alpha)
    back_mark = pygame.Rect(232, 8, 34, 21)
    front_mark = pygame.Rect(240, 12 - int(round(2 * e)), 34, 21)
    pygame.draw.rect(overlay, watermark_colour, back_mark, 1, border_radius=4)
    pygame.draw.rect(overlay, watermark_colour, front_mark, 1, border_radius=4)
    pygame.draw.line(
        overlay,
        watermark_colour,
        (front_mark.x + 3, front_mark.y + 6),
        (front_mark.right - 3, front_mark.y + 6),
        1,
    )
    body.blit(overlay, (0, 0))

    mask = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    mask.fill((0, 0, 0, 0))
    pygame.draw.rect(
        mask,
        (255, 255, 255, 255),
        mask.get_rect(),
        border_radius=12,
    )
    body.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
    screen.blit(body, visual_rect.topleft)

    pygame.draw.rect(screen, border_colour, visual_rect, 2, border_radius=12)
    pygame.draw.rect(
        screen,
        _lerp_color(border_colour, (255, 255, 255), 0.30),
        visual_rect.inflate(-3, -3),
        1,
        border_radius=10,
    )

    # State rail at the left edge and a bottom accent under the label.
    pygame.draw.line(
        screen,
        state_accent,
        (visual_rect.x + 3, visual_rect.y + 9),
        (visual_rect.x + 3, visual_rect.bottom - 10),
        3,
    )
    accent_w = int(112 * max(e, 0.8 * hover_amount))
    if accent_w >= 2:
        pygame.draw.line(
            screen,
            state_accent,
            (visual_rect.x + 16, visual_rect.bottom - 3),
            (visual_rect.x + 16 + accent_w, visual_rect.bottom - 3),
            2,
        )

    _draw_always_on_top_icon(
        screen,
        visual_rect.x + 27,
        visual_rect.centery,
        12,
        e,
        hover_amount,
        always_on_top_icon_phase,
    )

    label = "Always on Top"
    title_colour = _lerp_color(
        (231, 236, 246), (255, 255, 255), hover_amount
    )
    title = font_small.render(label, True, title_colour)
    title_rect = title.get_rect(
        midleft=(visual_rect.x + 50, visual_rect.centery)
    )
    title_shadow = font_small.render(label, True, (18, 20, 28))
    screen.blit(title_shadow, title_rect.move(0, 1))
    screen.blit(title, title_rect)

    # Preserve the original pill geometry and knob travel exactly.
    pill_w, pill_h = 54, 28
    pill = pygame.Rect(
        rect.right - 18 - pill_w,
        rect.centery - pill_h // 2,
        pill_w,
        pill_h,
    )
    visual_pill = pill.move(0, 1 if pressed else 0)
    knob_r = 11
    knob_x = int(
        visual_pill.x + 4 + knob_r + e * (pill_w - 8 - knob_r * 2)
    )

    # Status LED + immediate On/Off label, right-aligned before the pill.
    state_text = "On" if on else "Off"
    state_colour = _lerp_color((166, 174, 190), (145, 238, 175), e)
    state_colour = _lerp_color(
        state_colour, (255, 255, 255), 0.18 * hover_amount
    )
    state_surface = font_tiny.render(state_text, True, state_colour)
    state_rect = state_surface.get_rect(
        midright=(visual_pill.x - 10, visual_rect.centery)
    )
    state_shadow = font_tiny.render(state_text, True, (18, 20, 28))
    screen.blit(state_shadow, state_rect.move(0, 1))
    screen.blit(state_surface, state_rect)
    led_centre = (state_rect.x - 9, visual_rect.centery)
    pygame.draw.circle(
        screen, (18, 20, 28), (led_centre[0], led_centre[1] + 1), 4
    )
    pygame.draw.circle(
        screen,
        _lerp_color((105, 112, 128), (104, 226, 151), e),
        led_centre,
        4,
    )
    pygame.draw.circle(
        screen,
        _lerp_color((148, 156, 174), (190, 255, 214), e),
        led_centre,
        4,
        1,
    )
    pygame.draw.circle(
        screen,
        (255, 255, 255),
        (led_centre[0] - 1, led_centre[1] - 1),
        1,
    )

    # Gradient switch track with a lighter travelled section behind the
    # knob, end glyphs, inset shading and a detailed tactile knob.
    pygame.draw.rect(
        screen,
        (20, 23, 30),
        visual_pill.move(0, 1),
        border_radius=pill_h // 2,
    )
    off_top, off_bottom = (103, 109, 124), (65, 69, 82)
    on_top_track, on_bottom_track = (82, 198, 124), (39, 132, 78)
    track_top = _lerp_color(off_top, on_top_track, e)
    track_bottom = _lerp_color(off_bottom, on_bottom_track, e)
    if pressed:
        track_top = _lerp_color(track_top, (0, 0, 0), 0.16)
        track_bottom = _lerp_color(track_bottom, (0, 0, 0), 0.16)
    else:
        track_top = _lerp_color(
            track_top, (255, 255, 255), 0.08 * hover_amount
        )
        track_bottom = _lerp_color(
            track_bottom, (255, 255, 255), 0.05 * hover_amount
        )

    track = pygame.Surface(visual_pill.size, pygame.SRCALPHA)
    track_denom = max(1, pill_h - 1)
    for row in range(pill_h):
        colour = _lerp_color(track_top, track_bottom, row / track_denom)
        pygame.draw.line(track, (*colour, 255), (0, row), (pill_w - 1, row))
    track_overlay = pygame.Surface(visual_pill.size, pygame.SRCALPHA)
    travelled_w = max(0, knob_x - visual_pill.x - 2)
    if travelled_w > 2:
        pygame.draw.rect(
            track_overlay,
            (255, 255, 255, int(16 + 22 * e)),
            (2, 2, travelled_w, pill_h - 4),
            border_radius=pill_h // 2,
        )
    pygame.draw.line(
        track_overlay,
        (255, 255, 255, int(62 + 36 * hover_amount)),
        (7, 1),
        (pill_w - 8, 1),
        1,
    )
    pygame.draw.rect(
        track_overlay,
        (0, 0, 0, 38 if pressed else 28),
        (0, pill_h - 4, pill_w, 4),
    )
    track.blit(track_overlay, (0, 0))
    track_mask = pygame.Surface(visual_pill.size, pygame.SRCALPHA)
    track_mask.fill((0, 0, 0, 0))
    pygame.draw.rect(
        track_mask,
        (255, 255, 255, 255),
        track_mask.get_rect(),
        border_radius=pill_h // 2,
    )
    track.blit(
        track_mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT
    )
    screen.blit(track, visual_pill.topleft)
    pygame.draw.rect(
        screen, (24, 27, 34), visual_pill, 1, border_radius=pill_h // 2
    )
    pygame.draw.rect(
        screen,
        _lerp_color(track_top, (255, 255, 255), 0.35),
        visual_pill.inflate(-3, -3),
        1,
        border_radius=pill_h // 2,
    )

    # I/O-style end glyphs. Whichever end the knob has vacated remains
    # faintly visible, reinforcing the two resting positions.
    glyph_colour = _lerp_color(track_bottom, (242, 247, 255), 0.62)
    pygame.draw.circle(
        screen,
        glyph_colour,
        (visual_pill.x + 14, visual_pill.centery),
        4,
        2,
    )
    pygame.draw.line(
        screen,
        glyph_colour,
        (visual_pill.right - 14, visual_pill.centery - 4),
        (visual_pill.right - 14, visual_pill.centery + 4),
        2,
    )

    stretch = math.sin(math.pi * e)
    knob_rx = knob_r + 2.1 * stretch + 0.45 * hover_amount
    knob_ry = knob_r - 0.65 * stretch + 0.45 * hover_amount
    knob_rect = pygame.Rect(
        int(round(knob_x - knob_rx)),
        int(round(visual_pill.centery - knob_ry)),
        int(round(knob_rx * 2)),
        int(round(knob_ry * 2)),
    )
    pygame.draw.ellipse(screen, (22, 25, 32), knob_rect.move(1, 2))
    pygame.draw.ellipse(screen, (238, 241, 246), knob_rect)
    pygame.draw.ellipse(screen, (214, 220, 230), knob_rect.inflate(-4, -4))
    highlight_rect = pygame.Rect(
        knob_rect.x + 4,
        knob_rect.y + 3,
        max(3, knob_rect.width // 3),
        max(3, knob_rect.height // 4),
    )
    pygame.draw.ellipse(screen, (255, 255, 255), highlight_rect)
    pygame.draw.ellipse(screen, (107, 116, 132), knob_rect, 1)
    try:
        pygame.gfxdraw.aaellipse(
            screen,
            knob_rect.centerx,
            knob_rect.centery,
            max(1, knob_rect.width // 2),
            max(1, knob_rect.height // 2),
            (107, 116, 132, 255),
        )
    except Exception:
        pass

    groove_colour = _lerp_color((150, 158, 172), (79, 160, 105), e)
    for groove_dx in (-3, 0, 3):
        pygame.draw.line(
            screen,
            groove_colour,
            (knob_x + groove_dx, visual_pill.centery - 4),
            (knob_x + groove_dx, visual_pill.centery + 4),
            1,
        )
    pygame.draw.circle(
        screen,
        state_accent,
        (knob_x, visual_pill.centery),
        2,
    )
    pygame.draw.circle(
        screen,
        (255, 255, 255),
        (knob_x - 1, visual_pill.centery - 1),
        1,
    )
    return pill


def get_auto_move_toggle_rect(offset_x=0, offset_y=0):
    return pygame.Rect(WIDTH // 2 - 220 + offset_x, 150 + offset_y, 440, 94)


def get_auto_move_depth_rects(offset_x=0, offset_y=0):
    """Row and − / value / + controls for Auto-Move's target depth."""
    row = pygame.Rect(WIDTH // 2 - 220 + offset_x, 304 + offset_y, 440, 58)
    minus = pygame.Rect(row.right - 178, row.centery - 18, 42, 36)
    value = pygame.Rect(minus.right + 8, row.centery - 18, 72, 36)
    plus = pygame.Rect(value.right + 8, row.centery - 18, 42, 36)
    return row, minus, value, plus


def _draw_auto_move_depth_row(screen, offset_x=0, offset_y=0, idle=True):
    row, minus, value_rect, plus = get_auto_move_depth_rects(offset_x, offset_y)
    mx, my = pygame.mouse.get_pos()
    row_hovered = idle and row.collidepoint(mx, my)
    pygame.draw.rect(
        screen,
        (68, 68, 88) if row_hovered else (54, 54, 64),
        row,
        border_radius=10,
    )
    pygame.draw.rect(
        screen,
        (150, 170, 255) if row_hovered else MENU_BORDER_COLOR,
        row,
        2,
        border_radius=10,
    )

    label = font_small.render("Auto-Move Depth", True, TEXT_COLOR)
    screen.blit(label, (row.x + 18, row.y + 8))
    effective = get_auto_move_depth()
    if auto_move_depth == 0:
        detail = f"0 = Depth Limit ({effective})"
    elif auto_move_depth > effective:
        detail = f"Plays at Depth Limit ({effective})"
    else:
        detail = f"Plays at depth {effective}"
    detail_s = font_tiny.render(detail, True, TEXT_MUTED)
    screen.blit(detail_s, (row.x + 18, row.y + 34))

    for rect, symbol, enabled in (
        (minus, "−", auto_move_depth > 0),
        (plus, "+", auto_move_depth < 40),
    ):
        hovered = idle and enabled and rect.collidepoint(mx, my)
        bg = (92, 92, 120) if hovered else (70, 70, 82) if enabled else (48, 48, 56)
        pygame.draw.rect(screen, bg, rect, border_radius=8)
        pygame.draw.rect(
            screen,
            (150, 170, 255) if hovered else MENU_BORDER_COLOR,
            rect,
            1,
            border_radius=8,
        )
        symbol_s = font_small.render(
            symbol, True, TEXT_COLOR if enabled else TEXT_MUTED
        )
        screen.blit(symbol_s, symbol_s.get_rect(center=rect.center))

    pygame.draw.rect(screen, (38, 38, 46), value_rect, border_radius=8)
    pygame.draw.rect(screen, MENU_BORDER_COLOR, value_rect, 1, border_radius=8)
    value_s = font_small.render(str(auto_move_depth), True, TEXT_ACCENT)
    screen.blit(value_s, value_s.get_rect(center=value_rect.center))
    return row, minus, value_rect, plus


def get_auto_move_color_rects(offset_x=0, offset_y=0):
    colors = ("white", "black", "both")
    w, h, gap = 140, 46, 10
    total = 3 * w + 2 * gap
    x0 = WIDTH // 2 - total // 2 + offset_x
    return {c: pygame.Rect(x0 + i * (w + gap), 402 + offset_y, w, h) for i, c in enumerate(colors)}


def get_autoplay_toggle_rect(offset_x=0, offset_y=0):
    """Auto-play sits below Board Detection (whose own Y is dynamic)."""
    bd_rect = get_board_detection_button_rect(offset_x, offset_y)
    return pygame.Rect(bd_rect.x, bd_rect.bottom + 42, bd_rect.width, 94)


def _analysis_settings_button_summary():
    """Read-only Analysis status summary for the Settings main menu.

    This mirrors the Analysis page's *shown* Auto-Move/Auto-play states,
    including their setup dialogs, but never starts, stops or persists any
    of those features. The returned short labels are shared by the row's
    subtitle, its three status rails and its hover tooltip.
    """
    try:
        auto_depth = int(get_auto_move_depth())
    except Exception:
        try:
            auto_depth = int(ANALYSIS_DEPTH)
        except Exception:
            auto_depth = 0
    auto_colour = {
        "white": "White",
        "black": "Black",
        "both": "Both",
    }.get(str(auto_move_color or "both").strip().lower(), "Both")
    if show_auto_color_dialog:
        auto_state = "setup"
        auto_label = "Auto-Move choosing colour"
        auto_subtitle = "Auto-Move · choose a colour"
    elif auto_move_enabled:
        auto_state = "on"
        auto_label = f"Auto-Move on ({auto_colour}, depth {auto_depth})"
        auto_subtitle = f"Auto-Move · {auto_colour} · depth {auto_depth}"
    else:
        auto_state = "off"
        auto_label = "Auto-Move off"
        auto_subtitle = ""

    picker_purpose = str(board_detection_picker_purpose or "detect").lower()
    detection_picker_open = bool(
        show_board_detection_dialog and picker_purpose != "autoplay"
    )
    if board_detection_running:
        detection_state = "working"
        detection_label = "Detection working"
        detection_subtitle = "Detecting board…"
    elif detection_picker_open:
        detection_state = "setup"
        detection_label = "Detection choosing window"
        detection_subtitle = "Detection · choose a window"
    elif str(board_detection_error or "").strip():
        detection_state = "error"
        detection_label = "Detection needs attention"
        detection_subtitle = ""
    elif str(board_detection_last_summary or "").strip() or board_detection_result_ready:
        detection_state = "ready"
        detection_label = "Detection complete"
        detection_subtitle = "Board detection complete"
    else:
        detection_state = "idle"
        detection_label = "Detection idle"
        detection_subtitle = ""

    autoplay = _autoplay if isinstance(_autoplay, dict) else {}
    autoplay_picker_open = bool(
        show_board_detection_dialog and picker_purpose == "autoplay"
    )
    autoplay_setup = bool(show_autoplay_side_dialog or autoplay_picker_open)
    autoplay_scan = bool(autoplay.get("scan_running"))
    autoplay_running = bool(autoplay.get("running"))
    autoplay_enabled = bool(autoplay.get("enabled"))
    autoplay_phase_text = str(autoplay.get("state") or "").strip().lower()
    autoplay_error_text = str(autoplay.get("error") or "").strip()
    try:
        autoplay_side = _autoplay_side_name(
            autoplay.get("side", "white")
        ).capitalize()
    except Exception:
        autoplay_side = "White"
    autoplay_detail = f"({autoplay_side}, depth {int(AUTOPLAY_DEPTH)})"
    if autoplay_error_text and not (autoplay_scan or autoplay_running):
        autoplay_state = "error"
        autoplay_label = "Auto-play needs attention"
        autoplay_subtitle = ""
    elif autoplay_scan or autoplay_phase_text in ("scanning", "importing"):
        autoplay_state = "working"
        autoplay_label = f"Auto-play working {autoplay_detail}"
        autoplay_subtitle = "Auto-play scanning…"
    elif autoplay_setup or autoplay_phase_text in (
        "choosing_side",
        "choosing_window",
    ):
        autoplay_state = "setup"
        autoplay_label = "Auto-play setup"
        autoplay_subtitle = "Auto-play setup in progress"
    elif autoplay_phase_text == "game_over" and autoplay_enabled:
        autoplay_state = "ready"
        autoplay_label = f"Auto-play finished {autoplay_detail}"
        autoplay_subtitle = "Auto-play finished"
    elif autoplay_enabled or autoplay_running:
        autoplay_state = "on"
        autoplay_label = f"Auto-play on {autoplay_detail}"
        autoplay_subtitle = (
            f"Auto-play · {autoplay_side} · depth {int(AUTOPLAY_DEPTH)}"
        )
    else:
        autoplay_state = "off"
        autoplay_label = "Auto-play off"
        autoplay_subtitle = ""

    statuses = (
        {"key": "auto_move", "state": auto_state, "label": auto_label},
        {
            "key": "board_detection",
            "state": detection_state,
            "label": detection_label,
        },
        {"key": "autoplay", "state": autoplay_state, "label": autoplay_label},
    )
    alert = any(status["state"] == "error" for status in statuses)
    live = any(status["state"] == "working" for status in statuses)
    active = any(
        status["state"] in ("on", "setup", "working") for status in statuses
    )

    if alert:
        subtitle = "Needs attention"
    elif detection_state == "working":
        subtitle = detection_subtitle
    elif autoplay_state == "working":
        subtitle = autoplay_subtitle
    elif autoplay_state == "setup":
        subtitle = autoplay_subtitle
    elif detection_state == "setup":
        subtitle = detection_subtitle
    elif autoplay_state == "on":
        subtitle = autoplay_subtitle
    elif autoplay_state == "ready":
        subtitle = autoplay_subtitle
    elif auto_state == "setup":
        subtitle = auto_subtitle
    elif auto_state == "on":
        subtitle = auto_subtitle
    elif detection_state == "ready":
        subtitle = detection_subtitle
    else:
        subtitle = "Auto-Move · Detection · Auto-play"

    return {
        "statuses": statuses,
        "subtitle": subtitle,
        "tooltip": "; ".join(status["label"] for status in statuses),
        "alert": alert,
        "live": live,
        "active": active,
        "animated": bool(active or live),
    }


def _analysis_settings_status_fraction(state, phase, index=0):
    """Small status-rail fill for one Analysis feature state."""
    if state in ("off", "idle"):
        return 0.14
    if state == "ready":
        return 0.72
    if state == "setup":
        return 0.52 + 0.07 * math.sin(phase + index * 0.9)
    if state == "working":
        return 0.34 + 0.32 * (
            0.5 + 0.5 * math.sin(phase + index * 0.9)
        )
    if state == "error":
        return 0.82
    if state == "on":
        return 0.92
    return 0.14


def _analysis_settings_status_colour(key, state, hover_amount=0.0):
    """Stable per-feature colour, with setup/error kept unmistakable."""
    colours = {
        "auto_move": (100, 220, 145),
        "board_detection": (86, 196, 255),
        "autoplay": (174, 140, 255),
    }
    colour = colours.get(key, (120, 200, 220))
    if state == "error":
        return TEXT_STATUS
    if state == "setup":
        return (255, 196, 102)
    if state in ("off", "idle"):
        return _lerp_color(colour, (91, 103, 116), 0.62)
    return _lerp_color(
        colour, (255, 255, 255), 0.08 + 0.18 * hover_amount
    )


def _draw_analysis_settings_status_marker(surface, centre, key, colour):
    """Tiny feature glyph at the start of one Analysis status rail."""
    cx, cy = int(centre[0]), int(centre[1])
    pygame.draw.circle(surface, (17, 25, 34), (cx, cy + 1), 4)
    if key == "auto_move":
        pygame.draw.polygon(
            surface,
            colour,
            [(cx - 2, cy - 3), (cx - 2, cy + 3), (cx + 3, cy)],
        )
    elif key == "board_detection":
        marker = pygame.Rect(cx - 3, cy - 3, 7, 7)
        pygame.draw.rect(surface, colour, marker, 1, border_radius=1)
        pygame.draw.circle(surface, colour, (cx, cy), 1)
    elif key == "autoplay":
        pygame.draw.polygon(
            surface,
            colour,
            [(cx, cy - 4), (cx + 3, cy), (cx, cy + 4), (cx - 3, cy)],
            1,
        )
        pygame.draw.circle(surface, colour, (cx, cy), 1)
    else:
        pygame.draw.circle(surface, colour, (cx, cy), 2)


def _draw_analysis_settings_icon(
    surface,
    cx,
    cy,
    radius,
    hover_amount=0.0,
    phase=0.0,
    alert=False,
    live=False,
):
    """Draw the Analysis scope: eval waveform inside a scanning reticle."""
    cx, cy = int(cx), int(cy)
    radius = max(9, int(radius))
    hover_amount = max(0.0, min(1.0, float(hover_amount)))
    activity = max(hover_amount, 0.46 if live else 0.0)
    rim_colour = (
        (255, 132, 116)
        if alert
        else _lerp_color((92, 166, 178), (150, 226, 232), hover_amount)
    )
    disc_colour = _lerp_color(
        (27, 45, 58), (39, 68, 78), hover_amount
    )

    pygame.draw.circle(surface, (13, 20, 29), (cx, cy + 2), radius)
    pygame.draw.circle(surface, rim_colour, (cx, cy), radius)
    pygame.draw.circle(surface, disc_colour, (cx, cy), max(1, radius - 2))
    pygame.draw.circle(
        surface, (14, 26, 36), (cx, cy), max(1, radius - 4)
    )

    # Radar-style sweep. Keep all alpha work on a disposable overlay so the
    # opaque scope face underneath cannot lose its alpha on SRCALPHA targets.
    if activity > 0.02:
        overlay_size = radius * 2 + 3
        overlay = pygame.Surface(
            (overlay_size, overlay_size), pygame.SRCALPHA
        )
        local_cx = local_cy = radius + 1
        sweep_radius = max(2, radius - 4)
        sweep_points = [(local_cx, local_cy)]
        for step in range(9):
            angle = phase - 0.68 + step * (0.68 / 8.0)
            sweep_points.append(
                (
                    local_cx + int(round(math.cos(angle) * sweep_radius)),
                    local_cy + int(round(math.sin(angle) * sweep_radius)),
                )
            )
        pygame.draw.polygon(
            overlay,
            (86, 210, 225, int(58 * activity)),
            sweep_points,
        )
        sweep_end = (
            local_cx + int(round(math.cos(phase) * sweep_radius)),
            local_cy + int(round(math.sin(phase) * sweep_radius)),
        )
        pygame.draw.line(
            overlay,
            (170, 240, 248, int(150 * activity)),
            (local_cx, local_cy),
            sweep_end,
            1,
        )
        surface.blit(overlay, (cx - radius - 1, cy - radius - 1))

    grid_colour = _lerp_color(
        (52, 91, 103), (78, 132, 143), hover_amount
    )
    for fraction in (0.44, 0.72):
        grid_radius = max(2, int(round((radius - 4) * fraction)))
        pygame.draw.circle(surface, grid_colour, (cx, cy), grid_radius, 1)
    inner_extent = max(2, radius - 5)
    pygame.draw.line(
        surface, grid_colour, (cx - inner_extent, cy), (cx + inner_extent, cy), 1
    )
    pygame.draw.line(
        surface, grid_colour, (cx, cy - inner_extent), (cx, cy + inner_extent), 1
    )

    # Twelve rim pips and four detection-style corner brackets.
    for index in range(12):
        angle = index * (math.tau / 12.0)
        pip = (
            cx + int(round(math.cos(angle) * (radius - 3))),
            cy + int(round(math.sin(angle) * (radius - 3))),
        )
        pygame.draw.circle(surface, (88, 139, 151), pip, 1)
    bracket_extent = max(4, int(round(radius * 0.62)))
    bracket_arm = max(3, int(round(radius * 0.24)))
    bracket_colour = _lerp_color(
        (86, 196, 220), (170, 240, 248), hover_amount
    )
    for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        corner_x = cx + sx * bracket_extent
        corner_y = cy + sy * bracket_extent
        pygame.draw.lines(
            surface,
            bracket_colour,
            False,
            [
                (corner_x - sx * bracket_arm, corner_y),
                (corner_x, corner_y),
                (corner_x, corner_y - sy * bracket_arm),
            ],
            1,
        )

    wave_offsets = (-0.68, -0.45, -0.22, 0.0, 0.22, 0.45, 0.68)
    wave_shape = (0.18, -0.12, 0.05, -0.27, 0.02, 0.22, -0.04)
    wave_points = []
    for x_fraction, y_fraction in zip(wave_offsets, wave_shape):
        animated_y = y_fraction + 0.05 * activity * math.sin(
            phase + x_fraction * 5.0
        )
        wave_points.append(
            (
                cx + int(round(x_fraction * radius)),
                cy + int(round(animated_y * radius)),
            )
        )
    pygame.draw.aalines(
        surface,
        (10, 18, 27),
        False,
        [(x, y + 1) for x, y in wave_points],
    )
    pygame.draw.lines(
        surface,
        (103, 222, 242),
        False,
        wave_points,
        max(2, radius // 10),
    )
    pygame.draw.aalines(surface, (205, 250, 255), False, wave_points)
    for point in wave_points[1:-1]:
        pygame.draw.circle(surface, (190, 244, 252), point, 1)

    # Amber eval marker on the trace and a bright scope hub.
    marker_x, marker_y = wave_points[4]
    marker_radius = max(2, radius // 7)
    pygame.draw.polygon(
        surface,
        EVAL_MARKER_COLOR,
        [
            (marker_x, marker_y - marker_radius),
            (marker_x + marker_radius, marker_y),
            (marker_x, marker_y + marker_radius),
            (marker_x - marker_radius, marker_y),
        ],
    )
    pygame.draw.circle(surface, (16, 29, 39), (cx, cy), 2)
    pygame.draw.circle(surface, (215, 250, 255), (cx, cy), 1)

    glint_centre = (
        cx - int(round(radius * 0.38)),
        cy - int(round(radius * 0.42)),
    )
    pygame.draw.circle(
        surface,
        (143, 191, 201),
        glint_centre,
        max(1, int(round(radius * 0.07))),
    )
    pygame.draw.circle(surface, (225, 250, 255), glint_centre, 1)


def _draw_analysis_settings_chevron(surface, centre, colour, scale=1.0):
    """Small two-stroke navigation chevron for the Analysis menu row."""
    cx, cy = centre
    arm = max(4.0, 7.0 * scale)
    points = [(cx - arm * 0.55, cy - arm), (cx + arm, cy), (cx - arm * 0.55, cy + arm)]
    pygame.draw.aalines(surface, (17, 29, 36), False, [(x, y + 1) for x, y in points])
    pygame.draw.aalines(surface, colour, False, points)


def draw_polished_analysis_settings_button(screen, rect, hovered):
    """Draw the Settings-main Analysis Settings navigation row.

    The caller supplies the original 400x50 row, including entrance and
    page-transition offsets, and retains click/navigation behaviour. This
    renderer only reads Auto-Move, Board Detection and Auto-play state so
    its scope, subtitle and three status rails can summarise that page.
    """
    global analysis_settings_button_hover_amount
    global analysis_settings_button_pulse_phase

    try:
        pressed = bool(hovered and pygame.mouse.get_pressed()[0])
    except Exception:
        pressed = False

    summary = _analysis_settings_button_summary()
    status_by_key = {
        status["key"]: status for status in summary["statuses"]
    }

    hover_target = 1.0 if hovered else 0.0
    analysis_settings_button_hover_amount += (
        hover_target - analysis_settings_button_hover_amount
    ) * 0.22
    if abs(hover_target - analysis_settings_button_hover_amount) < 0.01:
        analysis_settings_button_hover_amount = hover_target
    hover_amount = max(
        0.0, min(1.0, analysis_settings_button_hover_amount)
    )
    if hovered or summary["animated"]:
        if pressed:
            phase_step = 0.038
        elif hovered:
            phase_step = 0.082
        else:
            phase_step = 0.055
        analysis_settings_button_pulse_phase = (
            analysis_settings_button_pulse_phase + phase_step
        ) % (2.0 * math.pi)
    phase = analysis_settings_button_pulse_phase

    visual_rect = rect.move(0, 1 if pressed else 0)

    if summary["alert"]:
        glow_colour = TEXT_STATUS
    elif status_by_key["autoplay"]["state"] in ("on", "working", "setup"):
        glow_colour = (174, 140, 255)
    elif status_by_key["board_detection"]["state"] == "working":
        glow_colour = (86, 196, 255)
    elif status_by_key["auto_move"]["state"] in ("on", "setup"):
        glow_colour = (100, 220, 145)
    else:
        glow_colour = (94, 190, 200)
    glow_strength = max(
        hover_amount,
        0.30 if summary["live"] else 0.15 if summary["active"] else 0.0,
    )
    if glow_strength > 0.01:
        glow = pygame.Surface(
            (visual_rect.width + 18, visual_rect.height + 18),
            pygame.SRCALPHA,
        )
        for inset, alpha in ((1, 15), (4, 24), (7, 36)):
            pygame.draw.rect(
                glow,
                (*glow_colour, int(alpha * glow_strength)),
                (
                    inset,
                    inset,
                    glow.get_width() - inset * 2,
                    glow.get_height() - inset * 2,
                ),
                border_radius=16 - inset,
            )
        screen.blit(glow, (visual_rect.x - 9, visual_rect.y - 9))

    pygame.draw.rect(
        screen,
        (15, 23, 31),
        visual_rect.move(0, 1 if pressed else 2),
        border_radius=10,
    )

    if pressed:
        top_colour = (55, 79, 88)
        bottom_colour = (40, 43, 70)
        border_colour = (104, 151, 160)
    elif hovered:
        top_colour = (82, 124, 130)
        bottom_colour = (57, 64, 104)
        border_colour = (158, 220, 224)
    else:
        top_colour = (64, 98, 104)
        bottom_colour = (46, 51, 82)
        border_colour = (116, 172, 180)
    if summary["alert"]:
        border_colour = _lerp_color(border_colour, TEXT_STATUS, 0.38)
    elif summary["active"]:
        border_colour = _lerp_color(border_colour, glow_colour, 0.22)

    # Teal-to-violet glass body. The graph-paper/waveform traces sit on an
    # overlay and are clipped by the same rounded mask as earlier rows.
    body = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    denom = max(1, visual_rect.height - 1)
    for row in range(visual_rect.height):
        colour = _lerp_color(top_colour, bottom_colour, row / denom)
        pygame.draw.line(
            body,
            (*colour, 255),
            (0, row),
            (visual_rect.width - 1, row),
        )

    overlay = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    pygame.draw.polygon(
        overlay,
        (255, 255, 255, int(9 + 14 * hover_amount)),
        [
            (-20, visual_rect.height),
            (visual_rect.width * 0.36, 0),
            (visual_rect.width * 0.55, 0),
            (visual_rect.width * 0.13, visual_rect.height),
        ],
    )
    pygame.draw.line(
        overlay,
        (255, 255, 255, int(62 + 44 * hover_amount)),
        (9, 1),
        (visual_rect.width - 10, 1),
        1,
    )
    pygame.draw.line(
        overlay,
        (255, 255, 255, 21),
        (2, 7),
        (2, visual_rect.height - 8),
        1,
    )
    pygame.draw.rect(
        overlay,
        (0, 0, 0, 42 if pressed else 29),
        (0, visual_rect.height - 4, visual_rect.width, 4),
    )
    pygame.draw.rect(
        overlay,
        (0, 0, 0, 24),
        (visual_rect.width - 3, 6, 3, visual_rect.height - 12),
        border_radius=2,
    )

    grid_alpha = int(8 + 10 * hover_amount)
    for x_start, x_end in ((58, 118), (282, 342)):
        for grid_x in range(x_start, x_end + 1, 10):
            pygame.draw.line(
                overlay,
                (190, 240, 248, grid_alpha),
                (grid_x, 6),
                (grid_x, visual_rect.height - 7),
                1,
            )
        for grid_y in (10, 20, 30, 40):
            pygame.draw.line(
                overlay,
                (190, 240, 248, grid_alpha),
                (x_start, grid_y),
                (x_end, grid_y),
                1,
            )
    trace_alpha = int(20 + 30 * hover_amount)
    waveform_traces = (
        (
            (103, 222, 242, trace_alpha),
            [(58, 31), (70, 24), (80, 35), (92, 18), (104, 28), (118, 22)],
        ),
        (
            (174, 140, 255, trace_alpha),
            [(282, 20), (294, 29), (304, 15), (316, 32), (328, 22), (342, 27)],
        ),
    )
    for trace_colour, trace_points in waveform_traces:
        pygame.draw.lines(overlay, trace_colour, False, trace_points, 1)
        for point in trace_points[1:-1]:
            pygame.draw.circle(overlay, trace_colour, point, 1)
    body.blit(overlay, (0, 0))

    mask = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    mask.fill((0, 0, 0, 0))
    pygame.draw.rect(
        mask,
        (255, 255, 255, 255),
        mask.get_rect(),
        border_radius=10,
    )
    body.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
    screen.blit(body, visual_rect.topleft)

    pygame.draw.rect(screen, border_colour, visual_rect, 2, border_radius=10)
    pygame.draw.rect(
        screen,
        _lerp_color(border_colour, (255, 255, 255), 0.30),
        visual_rect.inflate(-3, -3),
        1,
        border_radius=8,
    )

    # Short tri-colour telemetry inlays along the bottom, split around the
    # centred subtitle and coloured after the row's three feature rails.
    inlay_y = visual_rect.bottom - 5
    inlay_colours = ((100, 220, 145), (86, 196, 255), (174, 140, 255))
    for segment_x in (visual_rect.x + 56, visual_rect.x + 304):
        for index in range(8):
            colour = _lerp_color(
                bottom_colour, inlay_colours[index % 3], 0.38
            )
            pygame.draw.rect(
                screen,
                colour,
                (segment_x + index * 5, inlay_y, 5, 3),
            )

    icon_centre = (visual_rect.x + 31, visual_rect.centery)
    icon_hover_amount = max(
        hover_amount, 0.28 if summary["active"] else 0.0
    )
    _draw_analysis_settings_icon(
        screen,
        icon_centre[0],
        icon_centre[1],
        17,
        hover_amount=icon_hover_amount,
        phase=phase,
        alert=summary["alert"],
        live=summary["live"],
    )
    if summary["alert"]:
        badge = (icon_centre[0] + 14, icon_centre[1] - 14)
        pygame.draw.circle(screen, (43, 20, 18), (badge[0], badge[1] + 1), 5)
        pygame.draw.circle(screen, TEXT_STATUS, badge, 5)
        pygame.draw.circle(screen, (255, 205, 195), badge, 5, 1)
        pygame.draw.line(
            screen,
            (255, 255, 255),
            (badge[0], badge[1] - 2),
            (badge[0], badge[1] + 1),
            1,
        )
        pygame.draw.circle(
            screen, (255, 255, 255), (badge[0], badge[1] + 3), 1
        )

    # Two-line centred label: destination above, highest-priority live
    # Analysis state below (or the page's three features when all idle).
    label = "Analysis Settings"
    title_colour = _lerp_color(
        (237, 247, 248), (255, 255, 255), hover_amount
    )
    title = font_large.render(label, True, title_colour)
    title_rect = title.get_rect(
        center=(visual_rect.centerx, visual_rect.centery - 9)
    )
    title_shadow = font_large.render(label, True, (16, 26, 35))
    screen.blit(title_shadow, title_rect.move(0, 1))
    screen.blit(title, title_rect)

    subtitle_text = _elide_text(summary["subtitle"], font_tiny, 204)
    if summary["alert"]:
        subtitle_colour = (255, 166, 156)
    elif summary["live"]:
        subtitle_colour = (153, 224, 255)
    else:
        subtitle_colour = _lerp_color(
            (183, 211, 215), (225, 250, 252), hover_amount
        )
    subtitle = font_tiny.render(subtitle_text, True, subtitle_colour)
    subtitle_rect = subtitle.get_rect(
        center=(visual_rect.centerx, visual_rect.centery + 15)
    )
    subtitle_shadow = font_tiny.render(subtitle_text, True, (16, 26, 35))
    screen.blit(subtitle_shadow, subtitle_rect.move(0, 1))
    screen.blit(subtitle, subtitle_rect)

    # Three read-only status rails, top-to-bottom in Analysis-page order:
    # Auto-Move, Board Detection, then external Auto-play.
    detail_x = visual_rect.right - 96
    rail_x = detail_x + 11
    rail_width = 31
    for index, status in enumerate(summary["statuses"]):
        rail_y = visual_rect.centery + (index - 1) * 14
        colour = _analysis_settings_status_colour(
            status["key"], status["state"], hover_amount
        )
        _draw_analysis_settings_status_marker(
            screen, (detail_x + 3, rail_y), status["key"], colour
        )
        pygame.draw.line(
            screen,
            (17, 25, 34),
            (rail_x, rail_y + 1),
            (rail_x + rail_width, rail_y + 1),
            3,
        )
        pygame.draw.line(
            screen,
            (43, 59, 70),
            (rail_x, rail_y),
            (rail_x + rail_width, rail_y),
            3,
        )
        fraction = _analysis_settings_status_fraction(
            status["state"], phase, index
        )
        endpoint_x = int(round(rail_x + fraction * rail_width))
        if endpoint_x > rail_x:
            pygame.draw.line(
                screen, colour, (rail_x, rail_y), (endpoint_x, rail_y), 3
            )
        if hover_amount > 0.03 or status["state"] == "working":
            scan_t = (phase / (2.0 * math.pi) + index * 0.19) % 1.0
            scan_x = int(round(rail_x + scan_t * rail_width))
            pygame.draw.circle(
                screen,
                _lerp_color(colour, (255, 255, 255), 0.55),
                (scan_x, rail_y),
                1,
            )
        pygame.draw.circle(screen, (17, 25, 34), (endpoint_x, rail_y + 1), 3)
        pygame.draw.circle(
            screen,
            _lerp_color(colour, (255, 255, 255), 0.42),
            (endpoint_x, rail_y),
            3,
        )
        pygame.draw.circle(screen, colour, (endpoint_x, rail_y), 2)
        pygame.draw.circle(
            screen, (255, 255, 255), (endpoint_x - 1, rail_y - 1), 1
        )

    chevron_colour = _lerp_color(
        (190, 220, 224), (255, 255, 255), hover_amount
    )
    chevron_x = (
        visual_rect.right
        - 24
        + int(round(2 * hover_amount))
        - (1 if pressed else 0)
    )
    _draw_analysis_settings_chevron(
        screen,
        (chevron_x, visual_rect.centery),
        chevron_colour,
    )

    # Detailed feature states below the row. The fourth main-menu row is
    # followed by clear space before "About", so this stays unoccluded.
    if hovered and not pressed:
        tooltip_text = _elide_text(summary["tooltip"], font_tiny, 560)
        tip = font_tiny.render(tooltip_text, True, TEXT_COLOR)
        tip_w = tip.get_width() + 16
        tip_h = 24
        tip_x = max(
            4,
            min(visual_rect.centerx - tip_w // 2, WIDTH - tip_w - 4),
        )
        tip_y = min(visual_rect.bottom + 7, HEIGHT - tip_h - 4)
        tip_rect = pygame.Rect(tip_x, tip_y, tip_w, tip_h)
        pygame.draw.rect(screen, (17, 28, 34), tip_rect, border_radius=7)
        pygame.draw.rect(
            screen,
            TEXT_STATUS if summary["alert"] else (126, 204, 214),
            tip_rect,
            1,
            border_radius=7,
        )
        pointer_x = max(
            tip_rect.x + 8,
            min(visual_rect.centerx, tip_rect.right - 8),
        )
        pygame.draw.polygon(
            screen,
            (17, 28, 34),
            [
                (pointer_x - 5, tip_rect.y + 1),
                (pointer_x + 5, tip_rect.y + 1),
                (pointer_x, max(visual_rect.bottom + 1, tip_rect.y - 5)),
            ],
        )
        screen.blit(tip, tip.get_rect(center=tip_rect.center))


def draw_analysis_content(screen, offset_x=0, offset_y=0):
    mx, my = pygame.mouse.get_pos()
    idle = menu_transition_direction == "none"
    intro = font_small.render("Engine assistance while you play", True, TEXT_MUTED)
    screen.blit(intro, (WIDTH // 2 - intro.get_width() // 2 + offset_x, 112 + offset_y))

    threshold = get_auto_move_depth()
    rect = get_auto_move_toggle_rect(offset_x, offset_y)
    shown_on = auto_move_enabled or show_auto_color_dialog
    draw_toggle_row(
        screen, rect, "Auto-Move",
        f"Play the best move automatically at depth {threshold}",
        shown_on, auto_move_anim, idle and rect.collidepoint(mx, my),
    )

    line1 = font_tiny.render("When on, the engine's best move is played for you", True, TEXT_MUTED)
    line2 = font_tiny.render(f"as soon as the search reaches depth {threshold}.", True, TEXT_MUTED)
    screen.blit(line1, (WIDTH // 2 - line1.get_width() // 2 + offset_x, 254 + offset_y))
    screen.blit(line2, (WIDTH // 2 - line2.get_width() // 2 + offset_x, 272 + offset_y))

    _draw_auto_move_depth_row(screen, offset_x, offset_y, idle=idle)

    if auto_move_enabled:
        label = font_small.render("Auto-move for", True, TEXT_COLOR)
        screen.blit(label, (WIDTH // 2 - label.get_width() // 2 + offset_x, 376 + offset_y))
        for color, crect in get_auto_move_color_rects(offset_x, offset_y).items():
            selected = color == auto_move_color
            hovered = idle and crect.collidepoint(mx, my)
            bg = (86, 86, 130) if selected else (72, 72, 82) if hovered else (56, 56, 66)
            pygame.draw.rect(screen, bg, crect, border_radius=10)
            pygame.draw.rect(screen, (130, 220, 150) if selected else (150, 170, 255) if hovered else MENU_BORDER_COLOR, crect, 2, border_radius=10)
            txt = font_small.render(color.capitalize(), True, TEXT_COLOR)
            screen.blit(txt, txt.get_rect(center=crect.center))
        status = auto_move_status_text or "Active"
        status_s = font_tiny.render(status, True, TEXT_ACCENT)
        screen.blit(status_s, (WIDTH // 2 - status_s.get_width() // 2 + offset_x, 464 + offset_y))

    # Board Detection action card (not a toggle): opens the window picker.
    bd_rect = get_board_detection_button_rect(offset_x, offset_y)
    bd_hovered = idle and not board_detection_running and bd_rect.collidepoint(mx, my)
    bd_bg = (62, 72, 104) if bd_hovered else (58, 58, 68)
    pygame.draw.rect(screen, bd_bg, bd_rect, border_radius=12)
    pygame.draw.rect(
        screen,
        (80, 160, 255) if bd_hovered else MENU_BORDER_COLOR,
        bd_rect, 2, border_radius=12,
    )
    bd_title = font_large.render("Board Detection", True, TEXT_COLOR)
    screen.blit(bd_title, (bd_rect.x + 18, bd_rect.y + 14))
    bd_sub = font_tiny.render("Find a chessboard in another window", True, TEXT_MUTED)
    screen.blit(bd_sub, (bd_rect.x + 18, bd_rect.y + 50))
    chip_rect = pygame.Rect(bd_rect.right - 136, bd_rect.centery - 18, 118, 36)
    _draw_board_detection_button(
        screen,
        chip_rect,
        "Working..." if board_detection_running else "Detect",
        enabled=not board_detection_running,
        primary=True,
    )

    if board_detection_running:
        bd_status = board_detection_status or "Capturing and detecting board..."
        bd_colour = TEXT_MUTED
    elif board_detection_error:
        bd_status = board_detection_error
        bd_colour = TEXT_STATUS
    elif board_detection_last_summary:
        bd_status = board_detection_last_summary
        bd_colour = TEXT_ACCENT
    else:
        bd_status = "Anchors: a8 black rook (top-left) and h1 white rook (bottom-right)."
        bd_colour = TEXT_MUTED
    bd_status_s = font_tiny.render(
        _elide_text(bd_status, font_tiny, 620), True, bd_colour
    )
    screen.blit(
        bd_status_s,
        (WIDTH // 2 - bd_status_s.get_width() // 2 + offset_x, bd_rect.bottom + 16 + offset_y),
    )

    # Auto-play is a separate live-window mode from internal Auto-Move.
    # Its toggle starts the window/side setup; clicking it again (or F8
    # anywhere on Windows) stops the session immediately.
    autoplay_rect = get_autoplay_toggle_rect(offset_x, offset_y)
    autoplay_shown_on = bool(
        _autoplay.get("enabled")
        or show_autoplay_side_dialog
        or (
            show_board_detection_dialog
            and board_detection_picker_purpose == "autoplay"
        )
    )
    draw_toggle_row(
        screen,
        autoplay_rect,
        "Auto-play",
        f"Scan a live window, sync FEN, drag the best move at depth {AUTOPLAY_DEPTH}",
        autoplay_shown_on,
        autoplay_anim,
        idle and autoplay_rect.collidepoint(mx, my),
    )
    if _autoplay.get("error"):
        autoplay_status = _autoplay["error"]
        autoplay_colour = TEXT_STATUS
    elif _autoplay.get("status"):
        autoplay_status = _autoplay["status"]
        autoplay_colour = TEXT_ACCENT if autoplay_shown_on else TEXT_MUTED
    else:
        autoplay_status = "Choose a window and side. F8 stops Auto-play at any time."
        autoplay_colour = TEXT_MUTED
    if autoplay_shown_on and "F8" not in autoplay_status:
        autoplay_status = f"{autoplay_status}  ·  F8 stops"
    autoplay_status_s = font_tiny.render(
        _elide_text(autoplay_status, font_tiny, 620), True, autoplay_colour
    )
    screen.blit(
        autoplay_status_s,
        (
            WIDTH // 2 - autoplay_status_s.get_width() // 2 + offset_x,
            autoplay_rect.bottom + 16 + offset_y,
        ),
    )


def get_auto_color_dialog_rects():
    """Panel + White/Black/Both/Cancel rects for the colour question."""
    e = cubic_ease_in_out(max(0.0, min(1.0, auto_color_dialog_anim)))
    w, h = 420, 210
    x = WIDTH // 2 - w // 2
    y = HEIGHT // 2 - h // 2 - int((1 - e) * 18)
    panel = pygame.Rect(x, y, w, h)
    bw, bh, gap = 118, 44, 12
    total = 3 * bw + 2 * gap
    x0 = panel.centerx - total // 2
    choices = {c: pygame.Rect(x0 + i * (bw + gap), y + 96, bw, bh) for i, c in enumerate(("white", "black", "both"))}
    cancel = pygame.Rect(panel.centerx - 70, y + h - 52, 140, 36)
    return panel, choices, cancel


def draw_auto_color_dialog(screen):
    e = cubic_ease_in_out(max(0.0, min(1.0, auto_color_dialog_anim)))
    panel, choices, cancel = get_auto_color_dialog_rects()
    overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    overlay.fill((0, 0, 0, int(165 * e)))
    screen.blit(overlay, (0, 0))
    pygame.draw.rect(screen, (18, 18, 22), panel.move(0, 5), border_radius=14)
    pygame.draw.rect(screen, (52, 52, 64), panel, border_radius=14)
    pygame.draw.rect(screen, (130, 220, 150), panel, 2, border_radius=14)

    title = font_large.render("Auto-Move", True, TEXT_COLOR)
    screen.blit(title, title.get_rect(center=(panel.centerx, panel.y + 30)))
    q = font_small.render("Which colour should auto-move?", True, TEXT_MUTED)
    screen.blit(q, q.get_rect(center=(panel.centerx, panel.y + 62)))

    mx, my = pygame.mouse.get_pos()
    for color, rect in choices.items():
        hovered = rect.collidepoint(mx, my)
        bg = (74, 158, 96) if hovered else (60, 100, 130) if color == "both" else (66, 66, 80)
        pygame.draw.rect(screen, bg, rect, border_radius=10)
        pygame.draw.rect(screen, (140, 230, 160) if hovered else MENU_BORDER_COLOR, rect, 2, border_radius=10)
        # small king disc hinting the side
        if color in ("white", "both"):
            pygame.draw.circle(screen, (240, 240, 240), (rect.x + 20, rect.centery), 8)
        if color in ("black", "both"):
            pygame.draw.circle(screen, (25, 25, 25), (rect.x + (34 if color == "both" else 20), rect.centery), 8)
        txt = font_small.render(color.capitalize(), True, TEXT_COLOR)
        screen.blit(txt, txt.get_rect(center=(rect.centerx + 10, rect.centery)))

    chov = cancel.collidepoint(mx, my)
    pygame.draw.rect(screen, (98, 98, 110) if chov else (72, 72, 84), cancel, border_radius=9)
    pygame.draw.rect(screen, MENU_BORDER_COLOR, cancel, 2, border_radius=9)
    ctxt = font_small.render("Cancel", True, TEXT_COLOR)
    screen.blit(ctxt, ctxt.get_rect(center=cancel.center))
    hint = font_tiny.render("White/Black start after you move once, then the opponent moves.", True, TEXT_MUTED)
    if hint.get_width() <= panel.width - 16:
        screen.blit(hint, hint.get_rect(center=(panel.centerx, panel.bottom + 16)))
    return choices, cancel


# --- Engine Settings Page (Sliders) ---
ENGINE_ROW_KEYS = ["threads", "skill", "hash", "depth", "multipv"]
ENGINE_ROW_LABELS = {"threads": "Threads", "skill": "Skill Level", "hash": "Hash", "depth": "Depth Limit", "multipv": "MultiPV"}
# Threads are capped at the processor's logical thread count and Hash at a
# safe share of physical RAM (see _safe_hash_mb), so neither slider can be
# pushed into slowdown/crash territory on this machine.
ENGINE_ROW_LIMITS = {"threads": (1, MAX_THREADS), "skill": (0, 20), "hash": (16, SAFE_HASH_MB), "depth": (5, 40), "multipv": (1, 5)}
ENGINE_PARAM_NAMES = {"threads": "Threads", "skill": "Skill Level", "hash": "Hash", "multipv": "MultiPV"}
HASH_STEPS = []
_step = 16
while _step <= SAFE_HASH_MB:
    HASH_STEPS.append(_step)
    _step *= 2
if not HASH_STEPS:
    HASH_STEPS = [16]

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
    # Reset draft to defaults (clamped to this machine's safe limits),
    # not yet saved
    for k in ENGINE_ROW_KEYS:
        if k in ENGINE_DEFAULTS:
            low, high = ENGINE_ROW_LIMITS[k]
            value = max(low, min(ENGINE_DEFAULTS[k], high))
            if k == "hash":
                value = _snap_hash_down(value)
            engine_draft[k]=value

def apply_engine_settings():
    global ANALYSIS_DEPTH, engine_has_changes
    changed=False
    multipv_changed=False
    for k in ENGINE_ROW_KEYS:
        if engine_draft.get(k) != config.get(k):
            config[k]=engine_draft[k]
            changed=True
            if k == "multipv":
                multipv_changed=True
            if k == "depth":
                ANALYSIS_DEPTH = engine_draft[k]
            if engine_thread is not None and k in ENGINE_PARAM_NAMES:
                engine_thread.set_pending_parameters({ENGINE_PARAM_NAMES[k]: engine_draft[k]})
    if changed:
        save_config()
    # MultiPV changes the number/shape of lines for the position already
    # on screen, so applying it must immediately recalculate that board
    # rather than waiting for the next move.
    if multipv_changed and engine_thread is not None:
        try:
            engine_thread.recalculate_current()
        except Exception as exc:
            print(f"Failed to recalculate after MultiPV change: {exc}")
    engine_has_changes=False


def draw_engine_settings_content(screen, offset_x=0, offset_y=0):
    mx, my = pygame.mouse.get_pos()

    ram_txt = f"{TOTAL_RAM_MB} MB RAM" if TOTAL_RAM_MB else "RAM unknown"
    limits_s = font_tiny.render(f"Safe limits for this PC: {MAX_THREADS} threads · {SAFE_HASH_MB} MB hash ({ram_txt})", True, TEXT_MUTED)
    screen.blit(limits_s, (WIDTH // 2 - limits_s.get_width() // 2 + offset_x, ENGINE_ROW_TOP - 26 + offset_y))

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


def _engine_settings_button_values():
    """Return ``(draft_values, has_unsaved_changes)`` for the menu button.

    The main-menu button is only a visual summary. It reads the same staged
    ``engine_draft`` shown by the Engine Settings sliders and compares it
    with ``config`` in the same way ``update_slider_display`` does; it never
    writes either mapping.
    """
    values = {}
    for key in ENGINE_ROW_KEYS:
        raw = engine_draft.get(
            key, config.get(key, ENGINE_DEFAULTS.get(key, 0))
        )
        try:
            values[key] = int(round(float(raw)))
        except (TypeError, ValueError):
            values[key] = int(ENGINE_DEFAULTS.get(key, 0))

    dirty = False
    for key in ENGINE_ROW_KEYS:
        saved_raw = config.get(key, ENGINE_DEFAULTS.get(key, 0))
        try:
            saved = int(round(float(saved_raw)))
        except (TypeError, ValueError):
            saved = int(ENGINE_DEFAULTS.get(key, 0))
        if values[key] != saved:
            dirty = True
            break
    return values, dirty


def _engine_settings_fraction(key, value):
    """Normalise one staged engine value for a miniature slider track."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = float(ENGINE_DEFAULTS.get(key, 0))

    if key == "hash" and HASH_STEPS:
        if len(HASH_STEPS) == 1 or numeric <= HASH_STEPS[0]:
            return 0.0
        if numeric >= HASH_STEPS[-1]:
            return 1.0
        for index in range(len(HASH_STEPS) - 1):
            low = HASH_STEPS[index]
            high = HASH_STEPS[index + 1]
            if low <= numeric <= high:
                inner = (numeric - low) / max(1.0, high - low)
                return (index + inner) / (len(HASH_STEPS) - 1)
        return 0.0

    low, high = ENGINE_ROW_LIMITS.get(key, (0, 1))
    if high <= low:
        return 0.0
    return max(0.0, min(1.0, (numeric - low) / (high - low)))


def _draw_engine_settings_chevron(surface, cx, cy, colour):
    """Draw the Engine Settings row's forward chevron with a soft shadow."""
    points = [(cx - 4, cy - 7), (cx + 3, cy), (cx - 4, cy + 7)]
    shadow = [(px, py + 1) for px, py in points]
    pygame.draw.lines(surface, (20, 24, 38), False, shadow, 3)
    pygame.draw.lines(surface, colour, False, points, 3)
    try:
        pygame.draw.aalines(surface, colour, False, points)
    except Exception:
        pass


def draw_polished_engine_settings_button(screen, rect, hovered):
    """Draw the Settings-main Engine Settings navigation row.

    The caller supplies the original 400x50 row (including menu entrance
    offsets) and retains all click/navigation behaviour. This renderer adds
    an indigo-steel body, detailed gear medallion, five miniature staged
    slider tracks, unsaved-change indication and a values tooltip.
    """
    global engine_settings_button_hover_amount
    global engine_settings_button_gear_rotation
    global engine_settings_button_pulse_phase

    try:
        pressed = bool(hovered and pygame.mouse.get_pressed()[0])
    except Exception:
        pressed = False

    hover_target = 1.0 if hovered else 0.0
    engine_settings_button_hover_amount += (
        hover_target - engine_settings_button_hover_amount
    ) * 0.22
    if abs(hover_target - engine_settings_button_hover_amount) < 0.01:
        engine_settings_button_hover_amount = hover_target
    hover_amount = max(
        0.0, min(1.0, engine_settings_button_hover_amount)
    )
    if hovered:
        engine_settings_button_gear_rotation = (
            engine_settings_button_gear_rotation
            + (0.006 if pressed else 0.018)
        ) % (2.0 * math.pi)
        engine_settings_button_pulse_phase = (
            engine_settings_button_pulse_phase
            + (0.035 if pressed else 0.075)
        ) % (2.0 * math.pi)

    values, has_unsaved = _engine_settings_button_values()
    visual_rect = rect.move(0, 1 if pressed else 0)

    # Indigo/cyan halo; an unsaved draft warms a small part of it amber.
    if hover_amount > 0.01:
        glow_colour = (
            (255, 190, 92) if has_unsaved else (92, 156, 255)
        )
        glow = pygame.Surface(
            (visual_rect.width + 18, visual_rect.height + 18),
            pygame.SRCALPHA,
        )
        for inset, alpha in ((1, 15), (4, 24), (7, 36)):
            pygame.draw.rect(
                glow,
                (*glow_colour, int(alpha * hover_amount)),
                (
                    inset,
                    inset,
                    glow.get_width() - inset * 2,
                    glow.get_height() - inset * 2,
                ),
                border_radius=16 - inset,
            )
        screen.blit(glow, (visual_rect.x - 9, visual_rect.y - 9))

    pygame.draw.rect(
        screen,
        (17, 20, 32),
        visual_rect.move(0, 1 if pressed else 2),
        border_radius=10,
    )

    if pressed:
        top_colour = (59, 65, 91)
        bottom_colour = (39, 43, 64)
        border_colour = (126, 140, 180)
    elif hovered:
        top_colour = (96, 108, 148)
        bottom_colour = (61, 70, 106)
        border_colour = (174, 203, 255)
    else:
        top_colour = (76, 82, 112)
        bottom_colour = (48, 53, 78)
        border_colour = (126, 140, 180)
    if has_unsaved:
        border_colour = _lerp_color(
            border_colour, (255, 200, 104), 0.38
        )

    # Indigo-steel body. Translucent glass/circuit detail is built on an
    # overlay and clipped by a rounded mask, matching recent controls.
    body = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    denom = max(1, visual_rect.height - 1)
    for row in range(visual_rect.height):
        colour = _lerp_color(top_colour, bottom_colour, row / denom)
        pygame.draw.line(
            body,
            (*colour, 255),
            (0, row),
            (visual_rect.width - 1, row),
        )

    overlay = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    pygame.draw.polygon(
        overlay,
        (255, 255, 255, int(9 + 14 * hover_amount)),
        [
            (-20, visual_rect.height),
            (visual_rect.width * 0.36, 0),
            (visual_rect.width * 0.55, 0),
            (visual_rect.width * 0.13, visual_rect.height),
        ],
    )
    pygame.draw.line(
        overlay,
        (255, 255, 255, int(62 + 44 * hover_amount)),
        (9, 1),
        (visual_rect.width - 10, 1),
        1,
    )
    pygame.draw.line(
        overlay,
        (255, 255, 255, 21),
        (2, 7),
        (2, visual_rect.height - 8),
        1,
    )
    pygame.draw.rect(
        overlay,
        (0, 0, 0, 42 if pressed else 29),
        (0, visual_rect.height - 4, visual_rect.width, 4),
    )
    pygame.draw.rect(
        overlay,
        (0, 0, 0, 24),
        (visual_rect.width - 3, 6, 3, visual_rect.height - 12),
        border_radius=2,
    )

    # Fine circuit traces at the top/bottom edges, mostly visible in the
    # spaces between medallion, label and miniature slider bank.
    trace_alpha = int(18 + 24 * hover_amount)
    trace_colour = (170, 214, 255, trace_alpha)
    for trace in (
        [(54, 8), (92, 8), (104, 15), (142, 15)],
        [(258, 35), (292, 35), (303, 42), (338, 42)],
    ):
        pygame.draw.lines(overlay, trace_colour, False, trace, 1)
        for node in (trace[0], trace[-1]):
            pygame.draw.circle(overlay, trace_colour, node, 2)
    body.blit(overlay, (0, 0))

    mask = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    mask.fill((0, 0, 0, 0))
    pygame.draw.rect(
        mask,
        (255, 255, 255, 255),
        mask.get_rect(),
        border_radius=10,
    )
    body.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
    screen.blit(body, visual_rect.topleft)

    pygame.draw.rect(screen, border_colour, visual_rect, 2, border_radius=10)
    pygame.draw.rect(
        screen,
        _lerp_color(border_colour, (255, 255, 255), 0.30),
        visual_rect.inflate(-3, -3),
        1,
        border_radius=8,
    )

    if hover_amount > 0.04:
        accent_colour = _lerp_color(
            bottom_colour,
            (255, 200, 104) if has_unsaved else (142, 205, 255),
            hover_amount,
        )
        accent_w = int(132 * hover_amount)
        if accent_w >= 2:
            pygame.draw.line(
                screen,
                accent_colour,
                (visual_rect.centerx - accent_w // 2, visual_rect.bottom - 3),
                (visual_rect.centerx + accent_w // 2, visual_rect.bottom - 3),
                2,
            )

    # Layered medallion carrying the same supersampled detailed cog as the
    # top-bar Settings button, but at main-menu scale and on a dark socket.
    medallion_centre = (visual_rect.x + 31, visual_rect.centery)
    medallion_radius = 17
    pygame.draw.circle(
        screen,
        (14, 17, 28),
        (medallion_centre[0], medallion_centre[1] + 1),
        medallion_radius,
    )
    pygame.draw.circle(
        screen,
        _lerp_color((151, 170, 210), (210, 229, 255), hover_amount),
        medallion_centre,
        medallion_radius,
    )
    pygame.draw.circle(
        screen,
        _lerp_color((38, 46, 70), (48, 59, 88), hover_amount),
        medallion_centre,
        medallion_radius - 1,
    )
    pygame.draw.circle(
        screen,
        _lerp_color((55, 66, 94), (72, 85, 116), hover_amount),
        medallion_centre,
        medallion_radius - 4,
    )
    try:
        pygame.gfxdraw.aacircle(
            screen,
            medallion_centre[0],
            medallion_centre[1],
            medallion_radius,
            (*border_colour, 255),
        )
        pygame.gfxdraw.aacircle(
            screen,
            medallion_centre[0],
            medallion_centre[1],
            medallion_radius - 4,
            (126, 145, 184, 255),
        )
    except Exception:
        pass

    # Quantise the cog colour a little so its rotated-icon cache stays
    # compact while hover is easing; size/rotation are already quantised
    # inside draw_gear_icon's cache key.
    gear_hover = round(hover_amount * 4.0) / 4.0
    gear_colour = _lerp_color(
        (192, 207, 235), GEAR_COLOR_HOVER, gear_hover
    )
    draw_gear_icon(
        screen,
        medallion_centre[0],
        medallion_centre[1],
        13.5 + 0.5 * hover_amount - (0.25 if pressed else 0.0),
        gear_colour,
        thickness=2,
        rotation=engine_settings_button_gear_rotation,
    )

    # Orbiting telemetry dot on the medallion rim while hovered.
    if hover_amount > 0.03:
        orbit_angle = -0.9 + engine_settings_button_pulse_phase
        orbit = (
            int(
                round(
                    medallion_centre[0]
                    + (medallion_radius - 1) * math.cos(orbit_angle)
                )
            ),
            int(
                round(
                    medallion_centre[1]
                    + (medallion_radius - 1) * math.sin(orbit_angle)
                )
            ),
        )
        pygame.draw.circle(screen, (24, 31, 49), orbit, 3)
        pygame.draw.circle(screen, (126, 211, 255), orbit, 2)
        pygame.draw.circle(
            screen, (255, 255, 255), (orbit[0] - 1, orbit[1] - 1), 1
        )

    # Amber notification badge for staged-but-unsaved engine changes.
    if has_unsaved:
        badge = (
            medallion_centre[0] + medallion_radius - 3,
            medallion_centre[1] - medallion_radius + 3,
        )
        pygame.draw.circle(screen, (43, 29, 9), (badge[0], badge[1] + 1), 5)
        pygame.draw.circle(screen, (255, 196, 92), badge, 5)
        pygame.draw.circle(screen, (255, 236, 178), badge, 5, 1)
        pygame.draw.circle(
            screen, (255, 255, 255), (badge[0] - 1, badge[1] - 1), 1
        )

    # Keep the original centred large label so the main menu's rhythm is
    # retained; the medallion and slider bank occupy its side spaces.
    label = "Engine Settings"
    title_colour = _lerp_color(
        (232, 238, 250), (255, 255, 255), hover_amount
    )
    title = font_large.render(label, True, title_colour)
    title_rect = title.get_rect(center=visual_rect.center)
    title_shadow = font_large.render(label, True, (18, 21, 34))
    screen.blit(title_shadow, title_rect.move(0, 1))
    screen.blit(title, title_rect)

    # Five miniature staged-slider tracks, in Engine Settings page order.
    # Their thumbs stay at the true normalised draft values; only a tiny
    # scan glint travels on hover, so the visual summary remains honest.
    slider_x = visual_rect.right - 96
    slider_w = 40
    slider_colours = {
        "threads": (96, 174, 255),
        "skill": (150, 140, 255),
        "hash": (82, 211, 190),
        "depth": (100, 220, 145),
        "multipv": (255, 196, 102),
    }
    for index, key in enumerate(ENGINE_ROW_KEYS):
        track_y = visual_rect.centery + (index - 2) * 7
        fraction = _engine_settings_fraction(key, values[key])
        thumb_x = int(round(slider_x + fraction * slider_w))
        colour = slider_colours.get(key, (150, 180, 255))
        pygame.draw.line(
            screen,
            (19, 23, 36),
            (slider_x, track_y + 1),
            (slider_x + slider_w, track_y + 1),
            3,
        )
        pygame.draw.line(
            screen,
            (43, 50, 72),
            (slider_x, track_y),
            (slider_x + slider_w, track_y),
            3,
        )
        if thumb_x > slider_x:
            pygame.draw.line(
                screen,
                colour,
                (slider_x, track_y),
                (thumb_x, track_y),
                3,
            )
        if hover_amount > 0.03:
            scan_t = (
                engine_settings_button_pulse_phase / (2.0 * math.pi)
                + index * 0.17
            ) % 1.0
            scan_x = int(slider_x + scan_t * slider_w)
            pygame.draw.circle(
                screen,
                _lerp_color(colour, (255, 255, 255), 0.55),
                (scan_x, track_y),
                1,
            )
        pygame.draw.circle(screen, (20, 24, 38), (thumb_x, track_y + 1), 3)
        pygame.draw.circle(screen, (238, 244, 255), (thumb_x, track_y), 3)
        pygame.draw.circle(screen, colour, (thumb_x, track_y), 2)
        pygame.draw.circle(
            screen,
            (255, 255, 255),
            (thumb_x - 1, track_y - 1),
            1,
        )

    chevron_colour = _lerp_color(
        (188, 201, 228), (255, 255, 255), hover_amount
    )
    chevron_x = (
        visual_rect.right
        - 24
        + int(round(2 * hover_amount))
        - (1 if pressed else 0)
    )
    _draw_engine_settings_chevron(
        screen,
        chevron_x,
        visual_rect.centery,
        chevron_colour,
    )

    # Current staged values in a tooltip above the row, where the main
    # menu has clear space. It is suppressed while pressed so the click
    # feedback remains clean during the page transition.
    if hovered and not pressed:
        summary = (
            f"Threads {values['threads']} · Skill {values['skill']} · "
            f"Hash {values['hash']} MB · Depth {values['depth']} · "
            f"MultiPV {values['multipv']}"
        )
        if has_unsaved:
            summary = "Unsaved · " + summary
        summary = _elide_text(summary, font_tiny, 560)
        tip = font_tiny.render(summary, True, TEXT_COLOR)
        tip_w = tip.get_width() + 16
        tip_h = 24
        tip_x = max(
            4,
            min(visual_rect.centerx - tip_w // 2, WIDTH - tip_w - 4),
        )
        tip_y = max(4, visual_rect.y - tip_h - 7)
        tip_rect = pygame.Rect(tip_x, tip_y, tip_w, tip_h)
        pygame.draw.rect(screen, (20, 24, 38), tip_rect, border_radius=7)
        pygame.draw.rect(
            screen,
            (255, 200, 104) if has_unsaved else (157, 183, 255),
            tip_rect,
            1,
            border_radius=7,
        )
        pointer_x = max(
            tip_rect.x + 8,
            min(visual_rect.centerx, tip_rect.right - 8),
        )
        pygame.draw.polygon(
            screen,
            (20, 24, 38),
            [
                (pointer_x - 5, tip_rect.bottom - 1),
                (pointer_x + 5, tip_rect.bottom - 1),
                (
                    pointer_x,
                    min(visual_rect.y - 1, tip_rect.bottom + 5),
                ),
            ],
        )
        screen.blit(tip, tip.get_rect(center=tip_rect.center))


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


def _draw_github_icon(
    surface, cx, cy, radius, hover_amount=0.0, pulse_phase=0.0
):
    """Draw a compact cat medallion for the Github repository button.

    The mark is built from scalable primitives so the same helper can be
    rendered at the button's 24 px size or enlarged in tests. On hover the
    pupils glance toward the external-link arrow and a small orbit dot
    travels around the medallion rim.
    """
    cx, cy, radius = int(cx), int(cy), max(8, int(radius))
    hover_amount = max(0.0, min(1.0, hover_amount))
    unit = radius / 12.0

    def point(dx, dy):
        return (cx + dx * unit, cy + dy * unit)

    def scaled(value):
        return max(1, int(round(value * unit)))

    # Layered pale-blue enamel disc, matching Restart's medallion depth.
    pygame.draw.circle(surface, (13, 21, 39), (cx, cy + 1), radius)
    pygame.draw.circle(surface, (239, 245, 255), (cx, cy), radius)
    pygame.draw.circle(
        surface, (211, 225, 250), (cx, cy), max(5, radius - scaled(2))
    )
    pygame.draw.circle(surface, (37, 55, 94), (cx, cy), radius, 1)
    try:
        pygame.gfxdraw.aacircle(surface, cx, cy, radius, (37, 55, 94, 255))
        pygame.gfxdraw.aacircle(
            surface,
            cx,
            cy,
            max(5, radius - scaled(2)),
            (116, 145, 190, 255),
        )
    except Exception:
        pass

    glint = point(-4.0, -4.2)
    pygame.draw.circle(
        surface,
        (255, 255, 255),
        (int(glint[0]), int(glint[1])),
        scaled(0.9),
    )

    ink = (31, 45, 76)
    ink_light = (73, 98, 143)
    inner_ear = (126, 158, 205)

    # Ears and a curled tail sit behind the head silhouette.
    for sign in (-1, 1):
        ear = [
            point(sign * 6.0, -3.0),
            point(sign * 7.7, -9.0),
            point(sign * 2.0, -6.0),
        ]
        pygame.draw.polygon(surface, ink, ear)
        try:
            pygame.gfxdraw.aapolygon(surface, ear, (*ink, 255))
        except Exception:
            pass
        inner = [
            point(sign * 5.7, -4.3),
            point(sign * 6.4, -7.3),
            point(sign * 3.7, -5.8),
        ]
        pygame.draw.polygon(surface, inner_ear, inner)

    tail_points = []
    tail_steps = 10
    for step in range(tail_steps + 1):
        t = step / tail_steps
        x = (
            (1.0 - t) ** 2 * 3.0
            + 2.0 * (1.0 - t) * t * 10.0
            + t**2 * 8.0
        )
        y = (
            (1.0 - t) ** 2 * 5.0
            + 2.0 * (1.0 - t) * t * 6.0
            + t**2 * -1.0
        )
        tail_points.append(point(x, y))
    if len(tail_points) >= 2:
        pygame.draw.lines(
            surface, ink, False, tail_points, scaled(2.0)
        )
        try:
            pygame.draw.aalines(surface, ink, False, tail_points)
        except Exception:
            pass

    # Three tiny foot/tentacle nubs peek out below the head.
    for dx, dy in ((-4.0, 7.0), (0.0, 7.8), (4.0, 7.0)):
        nub = point(dx, dy)
        pygame.draw.circle(
            surface,
            ink,
            (int(nub[0]), int(nub[1])),
            scaled(1.25),
        )

    head_rect = pygame.Rect(
        int(round(cx - 7.0 * unit)),
        int(round(cy - 6.0 * unit)),
        max(2, int(round(14.0 * unit))),
        max(2, int(round(13.0 * unit))),
    )
    pygame.draw.ellipse(surface, ink, head_rect)
    try:
        pygame.gfxdraw.aaellipse(
            surface,
            cx,
            int(round(cy + 0.5 * unit)),
            scaled(7.0),
            scaled(6.5),
            (*ink, 255),
        )
    except Exception:
        pass

    # Soft brow and muzzle shading give the tiny silhouette some modelling.
    brow_rect = pygame.Rect(
        int(round(cx - 5.0 * unit)),
        int(round(cy - 5.2 * unit)),
        max(2, int(round(6.0 * unit))),
        max(2, int(round(3.0 * unit))),
    )
    pygame.draw.ellipse(surface, ink_light, brow_rect)
    muzzle_rect = pygame.Rect(
        int(round(cx - 3.0 * unit)),
        int(round(cy + 1.8 * unit)),
        max(2, int(round(6.0 * unit))),
        max(2, int(round(3.8 * unit))),
    )
    pygame.draw.ellipse(surface, (86, 111, 153), muzzle_rect)

    # The pupils ease up/right with hover, toward the external-link glyph.
    look_x = hover_amount * 0.9
    look_y = -hover_amount * 0.8
    for eye_dx in (-3.0, 3.0):
        eye_rect = pygame.Rect(
            int(round(cx + (eye_dx - 1.65) * unit)),
            int(round(cy - 2.5 * unit)),
            max(2, int(round(3.3 * unit))),
            max(2, int(round(3.9 * unit))),
        )
        pygame.draw.ellipse(surface, (240, 247, 255), eye_rect)
        pupil = point(eye_dx + look_x, -0.55 + look_y)
        pygame.draw.circle(
            surface,
            ink,
            (int(round(pupil[0])), int(round(pupil[1]))),
            scaled(0.85),
        )

    nose_y = cy + 2.45 * unit
    pygame.draw.line(
        surface,
        (226, 238, 255),
        (cx - 0.75 * unit, nose_y),
        (cx + 0.75 * unit, nose_y),
        1,
    )
    for whisker_dx in (-5.1, 5.1):
        whisker = point(whisker_dx, 3.0)
        pygame.draw.circle(
            surface,
            (173, 199, 235),
            (int(round(whisker[0])), int(round(whisker[1]))),
            scaled(0.65),
        )

    # A cyan orbit dot rides the rim only while the button is hovered.
    if hover_amount > 0.03:
        orbit_angle = -0.85 + pulse_phase
        orbit_radius = max(4, radius - scaled(1))
        orbit = (
            int(round(cx + orbit_radius * math.cos(orbit_angle))),
            int(round(cy + orbit_radius * math.sin(orbit_angle))),
        )
        pygame.draw.circle(surface, (37, 55, 94), orbit, scaled(1.7))
        pygame.draw.circle(surface, (126, 211, 255), orbit, scaled(1.2))
        pygame.draw.circle(
            surface,
            (255, 255, 255),
            (orbit[0] - scaled(0.4), orbit[1] - scaled(0.4)),
            scaled(0.45),
        )


def _draw_external_link_icon(surface, cx, cy, colour, shift=0):
    """Draw the small box-and-arrow 'opens externally' glyph."""
    cx, cy, shift = int(cx), int(cy), int(shift)
    box_path = [
        (cx - 1, cy - 5),
        (cx - 6, cy - 5),
        (cx - 6, cy + 6),
        (cx + 6, cy + 6),
        (cx + 6, cy + 1),
    ]
    pygame.draw.lines(surface, colour, False, box_path, 2)
    try:
        pygame.draw.aalines(surface, colour, False, box_path)
    except Exception:
        pass

    tip = (cx + 6 + shift, cy - 6 - shift)
    shaft = [(cx - 1, cy + 1), tip]
    pygame.draw.lines(surface, colour, False, shaft, 2)
    try:
        pygame.draw.aalines(surface, colour, False, shaft)
    except Exception:
        pass
    for head_end in (
        (tip[0] - 4, tip[1]),
        (tip[0], tip[1] + 4),
    ):
        pygame.draw.line(surface, colour, tip, head_end, 2)
        try:
            pygame.draw.aaline(surface, colour, tip, head_end)
        except Exception:
            pass


def draw_polished_github_button(screen, rect, hovered):
    """Draw Settings > About > Github as a polished sapphire link button.

    The click rectangle and web-browser behaviour are unchanged. This
    helper owns only visual hover/orbit animation state.
    """
    global github_button_hover_amount, github_button_pulse_phase

    try:
        pressed = bool(hovered and pygame.mouse.get_pressed()[0])
    except Exception:
        pressed = False

    hover_target = 1.0 if hovered else 0.0
    github_button_hover_amount += (
        hover_target - github_button_hover_amount
    ) * 0.22
    if abs(hover_target - github_button_hover_amount) < 0.01:
        github_button_hover_amount = hover_target
    hover_amount = max(
        0.0, min(1.0, github_button_hover_amount)
    )
    if hovered:
        github_button_pulse_phase = (
            github_button_pulse_phase
            + (0.018 if pressed else 0.055)
        ) % (2.0 * math.pi)

    visual_rect = rect.move(0, 1 if pressed else 0)

    # Sapphire halo easing in behind the button.
    if hover_amount > 0.01:
        glow = pygame.Surface(
            (visual_rect.width + 18, visual_rect.height + 18),
            pygame.SRCALPHA,
        )
        for inset, alpha in ((1, 18), (4, 28), (7, 42)):
            pygame.draw.rect(
                glow,
                (82, 144, 255, int(alpha * hover_amount)),
                (
                    inset,
                    inset,
                    glow.get_width() - inset * 2,
                    glow.get_height() - inset * 2,
                ),
                border_radius=15 - inset,
            )
        screen.blit(glow, (visual_rect.x - 9, visual_rect.y - 9))

    pygame.draw.rect(
        screen,
        (13, 20, 38),
        visual_rect.move(0, 1 if pressed else 2),
        border_radius=10,
    )

    if pressed:
        top_colour = (57, 73, 112)
        bottom_colour = (38, 50, 84)
        border_colour = (124, 151, 196)
    elif hovered:
        top_colour = (91, 118, 174)
        bottom_colour = (56, 76, 125)
        border_colour = (168, 198, 255)
    else:
        top_colour = (72, 91, 139)
        bottom_colour = (45, 59, 98)
        border_colour = (124, 145, 190)

    # Rounded sapphire-metal body, using the same overlay-and-mask
    # construction as the neighbouring polished Restart button.
    body = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    denom = max(1, visual_rect.height - 1)
    for row in range(visual_rect.height):
        colour = _lerp_color(top_colour, bottom_colour, row / denom)
        pygame.draw.line(
            body,
            (*colour, 255),
            (0, row),
            (visual_rect.width - 1, row),
        )

    overlay = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    pygame.draw.polygon(
        overlay,
        (255, 255, 255, int(10 + 17 * hover_amount)),
        [
            (-12, visual_rect.height),
            (visual_rect.width * 0.42, 0),
            (visual_rect.width * 0.62, 0),
            (visual_rect.width * 0.14, visual_rect.height),
        ],
    )
    pygame.draw.line(
        overlay,
        (255, 255, 255, int(70 + 48 * hover_amount)),
        (8, 1),
        (visual_rect.width - 9, 1),
        1,
    )
    pygame.draw.line(
        overlay,
        (255, 255, 255, 23),
        (2, 6),
        (2, visual_rect.height - 7),
        1,
    )
    pygame.draw.rect(
        overlay,
        (0, 0, 0, 46 if pressed else 31),
        (0, visual_rect.height - 4, visual_rect.width, 4),
    )
    pygame.draw.rect(
        overlay,
        (0, 0, 0, 25),
        (visual_rect.width - 3, 5, 3, visual_rect.height - 10),
        border_radius=2,
    )
    for stripe_x in (10, 18):
        pygame.draw.polygon(
            overlay,
            (190, 225, 255, 12),
            [
                (stripe_x, visual_rect.height),
                (stripe_x + 12, 0),
                (stripe_x + 17, 0),
                (stripe_x + 5, visual_rect.height),
            ],
        )
        pygame.draw.polygon(
            overlay,
            (190, 225, 255, 12),
            [
                (visual_rect.width - stripe_x - 17, visual_rect.height),
                (visual_rect.width - stripe_x - 5, 0),
                (visual_rect.width - stripe_x, 0),
                (visual_rect.width - stripe_x - 12, visual_rect.height),
            ],
        )

    # Faint source-branch traces in the open space at either edge. They
    # brighten with hover and sit beneath the medallion/external glyph.
    branch_alpha = int(22 + 30 * hover_amount)
    branch_colour = (180, 220, 255, branch_alpha)
    left_branch = [(12, 29), (27, 29), (36, 21), (51, 21)]
    right_branch = [(169, 15), (184, 15), (193, 7), (208, 7)]
    for branch in (left_branch, right_branch):
        pygame.draw.lines(overlay, branch_colour, False, branch, 1)
        for node in (branch[0], branch[-1]):
            pygame.draw.circle(overlay, branch_colour, node, 2)
    body.blit(overlay, (0, 0))

    mask = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    mask.fill((0, 0, 0, 0))
    pygame.draw.rect(
        mask,
        (255, 255, 255, 255),
        mask.get_rect(),
        border_radius=10,
    )
    body.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
    screen.blit(body, visual_rect.topleft)

    pygame.draw.rect(screen, border_colour, visual_rect, 2, border_radius=10)
    pygame.draw.rect(
        screen,
        _lerp_color(border_colour, (255, 255, 255), 0.30),
        visual_rect.inflate(-3, -3),
        1,
        border_radius=8,
    )

    if hover_amount > 0.04:
        accent_colour = _lerp_color(
            bottom_colour, (150, 215, 255), hover_amount
        )
        accent_w = int(92 * hover_amount)
        if accent_w >= 2:
            pygame.draw.line(
                screen,
                accent_colour,
                (visual_rect.centerx - accent_w // 2, visual_rect.bottom - 3),
                (visual_rect.centerx + accent_w // 2, visual_rect.bottom - 3),
                2,
            )

    # External-link indicator: the arrow eases one pixel up/right on hover
    # while its box stays anchored, making the browser action explicit.
    external_cx = visual_rect.right - 24
    external_shift = int(round(hover_amount))
    _draw_external_link_icon(
        screen,
        external_cx,
        visual_rect.centery + 1,
        (17, 27, 50),
        external_shift,
    )
    _draw_external_link_icon(
        screen,
        external_cx,
        visual_rect.centery,
        _lerp_color(
            (165, 190, 226), (235, 247, 255), hover_amount
        ),
        external_shift,
    )

    # Medallion + label as one centred group, shifted to leave the
    # external-link glyph clear—matching Restart's status-dot layout.
    label = "GitHub"
    text_colour = _lerp_color(
        (226, 236, 255), (255, 255, 255), hover_amount
    )
    text = font_small.render(label, True, text_colour)
    icon_radius = 12
    gap = 8
    group_w = icon_radius * 2 + gap + text.get_width()
    group_x = visual_rect.centerx - group_w // 2 - 8
    icon_cx = group_x + icon_radius
    _draw_github_icon(
        screen,
        icon_cx,
        visual_rect.centery,
        icon_radius,
        hover_amount,
        github_button_pulse_phase,
    )
    text_rect = text.get_rect(
        midleft=(group_x + icon_radius * 2 + gap, visual_rect.centery)
    )
    shadow_text = font_small.render(label, True, (17, 27, 50))
    screen.blit(shadow_text, text_rect.move(0, 1))
    screen.blit(text, text_rect)


def _draw_restart_icon(surface, cx, cy, radius, rotation):
    """Draw twin circular arrows on a small pale Restart medallion."""
    cx, cy, radius = int(cx), int(cy), max(8, int(radius))

    # Layered disc: offset shadow, pale enamel face, inner ring and rim.
    pygame.draw.circle(surface, (43, 13, 17), (cx, cy + 1), radius)
    pygame.draw.circle(surface, (255, 238, 238), (cx, cy), radius)
    pygame.draw.circle(
        surface, (249, 211, 211), (cx, cy), max(5, radius - 2)
    )
    pygame.draw.circle(surface, (104, 31, 37), (cx, cy), radius, 1)
    try:
        pygame.gfxdraw.aacircle(
            surface, cx, cy, radius, (104, 31, 37, 255)
        )
        pygame.gfxdraw.aacircle(
            surface,
            cx,
            cy,
            max(5, radius - 2),
            (190, 91, 95, 255),
        )
    except Exception:
        pass

    glint_x = cx - max(2, radius // 3)
    glint_y = cy - max(2, radius // 3)
    pygame.draw.circle(surface, (255, 255, 255), (glint_x, glint_y), 1)

    # Two opposing arcs, drawn as short polylines so their arrowheads can
    # be aligned exactly with each arc's tangent at any rotation.
    arc_radius = max(5, radius - 4)
    arc_colours = ((126, 31, 38), (181, 65, 68))
    for arc_index in range(2):
        start = rotation + arc_index * math.pi - 0.35
        end = start + 2.25
        points = []
        steps = 14
        for step in range(steps + 1):
            angle = start + (end - start) * step / steps
            points.append(
                (
                    cx + arc_radius * math.cos(angle),
                    cy + arc_radius * math.sin(angle),
                )
            )
        colour = arc_colours[arc_index]
        if len(points) >= 2:
            pygame.draw.lines(surface, colour, False, points, 2)
            try:
                pygame.draw.aalines(surface, colour, False, points)
            except Exception:
                pass

        # Screen-space polar derivative: this is the clockwise tangent.
        end_x, end_y = points[-1]
        tangent = (-math.sin(end), math.cos(end))
        normal = (math.cos(end), math.sin(end))
        tip = (
            end_x + tangent[0] * 3.2,
            end_y + tangent[1] * 3.2,
        )
        base = (
            end_x - tangent[0] * 1.4,
            end_y - tangent[1] * 1.4,
        )
        arrowhead = [
            tip,
            (base[0] + normal[0] * 2.6, base[1] + normal[1] * 2.6),
            (base[0] - normal[0] * 2.6, base[1] - normal[1] * 2.6),
        ]
        pygame.draw.polygon(surface, colour, arrowhead)
        try:
            pygame.gfxdraw.aapolygon(surface, arrowhead, (*colour, 255))
        except Exception:
            pass

    pygame.draw.circle(surface, (154, 45, 49), (cx, cy), 2)
    pygame.draw.circle(surface, (255, 226, 220), (cx - 1, cy - 1), 1)


def draw_polished_restart_button(screen, rect, hovered):
    """Draw Settings > About > Restart as a polished ruby action button.

    The click rectangle and restart/exit-code behaviour are unchanged.
    This helper owns only visual hover/pending animation state.
    """
    global restart_button_hover_amount, restart_icon_rotation

    pending = bool(restart_requested)
    try:
        pressed = bool(hovered and pygame.mouse.get_pressed()[0])
    except Exception:
        pressed = False

    hover_target = 1.0 if hovered or pending else 0.0
    restart_button_hover_amount += (
        hover_target - restart_button_hover_amount
    ) * 0.22
    if abs(hover_target - restart_button_hover_amount) < 0.01:
        restart_button_hover_amount = hover_target
    hover_amount = max(
        0.0, min(1.0, restart_button_hover_amount)
    )
    if pending:
        restart_icon_rotation = (
            restart_icon_rotation + 0.30
        ) % (2.0 * math.pi)
    elif hovered:
        restart_icon_rotation = (
            restart_icon_rotation + (0.012 if pressed else 0.035)
        ) % (2.0 * math.pi)

    visual_rect = rect.move(0, 1 if pressed or pending else 0)

    # Ruby halo that eases in behind the button.
    if hover_amount > 0.01:
        glow = pygame.Surface(
            (visual_rect.width + 18, visual_rect.height + 18),
            pygame.SRCALPHA,
        )
        for inset, alpha in ((1, 18), (4, 28), (7, 42)):
            pygame.draw.rect(
                glow,
                (255, 86, 92, int(alpha * hover_amount)),
                (
                    inset,
                    inset,
                    glow.get_width() - inset * 2,
                    glow.get_height() - inset * 2,
                ),
                border_radius=15 - inset,
            )
        screen.blit(glow, (visual_rect.x - 9, visual_rect.y - 9))

    pygame.draw.rect(
        screen,
        (30, 10, 13),
        visual_rect.move(0, 1 if pressed or pending else 2),
        border_radius=10,
    )

    if pressed or pending:
        top_colour = (97, 46, 50)
        bottom_colour = (65, 31, 36)
        border_colour = (190, 105, 108)
    elif hovered:
        top_colour = (165, 76, 79)
        bottom_colour = (106, 46, 51)
        border_colour = (255, 151, 151)
    else:
        top_colour = (129, 64, 67)
        bottom_colour = (80, 39, 44)
        border_colour = (184, 105, 108)

    # Rounded ruby-metal body. Sheen/shading goes through an overlay and
    # rounded mask, matching the polished top-bar buttons' construction.
    body = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    denom = max(1, visual_rect.height - 1)
    for row in range(visual_rect.height):
        colour = _lerp_color(top_colour, bottom_colour, row / denom)
        pygame.draw.line(
            body,
            (*colour, 255),
            (0, row),
            (visual_rect.width - 1, row),
        )

    overlay = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    pygame.draw.polygon(
        overlay,
        (255, 255, 255, int(10 + 17 * hover_amount)),
        [
            (-12, visual_rect.height),
            (visual_rect.width * 0.42, 0),
            (visual_rect.width * 0.62, 0),
            (visual_rect.width * 0.14, visual_rect.height),
        ],
    )
    pygame.draw.line(
        overlay,
        (255, 255, 255, int(70 + 48 * hover_amount)),
        (8, 1),
        (visual_rect.width - 9, 1),
        1,
    )
    pygame.draw.line(
        overlay,
        (255, 255, 255, 23),
        (2, 6),
        (2, visual_rect.height - 7),
        1,
    )
    pygame.draw.rect(
        overlay,
        (0, 0, 0, 46 if pressed or pending else 31),
        (0, visual_rect.height - 4, visual_rect.width, 4),
    )
    pygame.draw.rect(
        overlay,
        (0, 0, 0, 25),
        (visual_rect.width - 3, 5, 3, visual_rect.height - 10),
        border_radius=2,
    )
    # Two very faint diagonal caution facets at the far edges. They add
    # detail without turning the whole button into a hazard sign.
    for stripe_x in (10, 18):
        pygame.draw.polygon(
            overlay,
            (255, 205, 170, 13),
            [
                (stripe_x, visual_rect.height),
                (stripe_x + 12, 0),
                (stripe_x + 17, 0),
                (stripe_x + 5, visual_rect.height),
            ],
        )
        pygame.draw.polygon(
            overlay,
            (255, 205, 170, 13),
            [
                (visual_rect.width - stripe_x - 17, visual_rect.height),
                (visual_rect.width - stripe_x - 5, 0),
                (visual_rect.width - stripe_x, 0),
                (visual_rect.width - stripe_x - 12, visual_rect.height),
            ],
        )
    body.blit(overlay, (0, 0))

    mask = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    mask.fill((0, 0, 0, 0))
    pygame.draw.rect(
        mask,
        (255, 255, 255, 255),
        mask.get_rect(),
        border_radius=10,
    )
    body.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
    screen.blit(body, visual_rect.topleft)

    pygame.draw.rect(screen, border_colour, visual_rect, 2, border_radius=10)
    pygame.draw.rect(
        screen,
        _lerp_color(border_colour, (255, 255, 255), 0.30),
        visual_rect.inflate(-3, -3),
        1,
        border_radius=8,
    )

    if hover_amount > 0.04:
        accent_colour = _lerp_color(
            bottom_colour, (255, 178, 158), hover_amount
        )
        accent_w = int(92 * hover_amount)
        if accent_w >= 2:
            pygame.draw.line(
                screen,
                accent_colour,
                (visual_rect.centerx - accent_w // 2, visual_rect.bottom - 3),
                (visual_rect.centerx + accent_w // 2, visual_rect.bottom - 3),
                2,
            )

    # Three little reboot-sequence dots on the right. They fill as hover
    # eases in, then chase one another while a restart is pending.
    active_dot = int(restart_icon_rotation / (math.pi / 3.0)) % 3
    for dot_index in range(3):
        dot_centre = (
            visual_rect.right - 34 + dot_index * 9,
            visual_rect.centery,
        )
        pygame.draw.circle(
            screen, (43, 14, 18), (dot_centre[0], dot_centre[1] + 1), 3
        )
        if pending:
            lit = dot_index <= active_dot
        else:
            lit = hover_amount >= (dot_index + 1) / 3.0
        pygame.draw.circle(
            screen,
            (255, 190, 164) if lit else (105, 54, 59),
            dot_centre,
            3,
        )
        pygame.draw.circle(
            screen,
            (255, 151, 151) if lit else (150, 82, 86),
            dot_centre,
            3,
            1,
        )
        if lit:
            pygame.draw.circle(
                screen,
                (255, 246, 238),
                (dot_centre[0] - 1, dot_centre[1] - 1),
                1,
            )

    # Medallion + label as one centred group, leaving the dots clear.
    label = "Restarting…" if pending else "Restart"
    text_colour = _lerp_color(
        (246, 226, 226), (255, 255, 255), hover_amount
    )
    text = font_small.render(label, True, text_colour)
    icon_radius = 12
    gap = 8
    group_w = icon_radius * 2 + gap + text.get_width()
    group_x = visual_rect.centerx - group_w // 2 - 8
    icon_cx = group_x + icon_radius
    _draw_restart_icon(
        screen,
        icon_cx,
        visual_rect.centery,
        icon_radius,
        restart_icon_rotation,
    )
    text_rect = text.get_rect(
        midleft=(group_x + icon_radius * 2 + gap, visual_rect.centery)
    )
    shadow_text = font_small.render(label, True, (43, 13, 17))
    screen.blit(shadow_text, text_rect.move(0, 1))
    screen.blit(text, text_rect)

    # Explanatory hover tooltip in the 40 px gap before Always on Top.
    # It uses the same below-button, clamped-pointer language as the
    # top-bar tooltips, in the Restart button's ruby palette.
    if hovered and not pressed and not pending:
        tip = font_tiny.render(
            "Restart ChessBotV via start.bat", True, TEXT_COLOR
        )
        tip_w = tip.get_width() + 16
        tip_h = 24
        tip_x = max(
            4,
            min(visual_rect.centerx - tip_w // 2, WIDTH - tip_w - 4),
        )
        tip_y = min(visual_rect.bottom + 7, HEIGHT - tip_h - 4)
        tip_rect = pygame.Rect(tip_x, tip_y, tip_w, tip_h)
        pygame.draw.rect(screen, (34, 18, 21), tip_rect, border_radius=7)
        pygame.draw.rect(
            screen, (255, 151, 151), tip_rect, 1, border_radius=7
        )
        pointer_x = max(
            tip_rect.x + 8,
            min(visual_rect.centerx, tip_rect.right - 8),
        )
        pygame.draw.polygon(
            screen,
            (34, 18, 21),
            [
                (pointer_x - 5, tip_rect.y + 1),
                (pointer_x + 5, tip_rect.y + 1),
                (pointer_x, max(visual_rect.bottom + 1, tip_rect.y - 5)),
            ],
        )
        screen.blit(tip, tip.get_rect(center=tip_rect.center))


def _draw_close_x_icon(
    surface,
    cx,
    cy,
    radius,
    hover_amount=0.0,
    fold_amount=0.0,
    phase=0.0,
    pressed=False,
):
    """Draw the Settings Close seal: a ringed socket with tapered X arms.

    Hover lengthens and very slightly turns the arms. While Settings is
    closing, ``fold_amount`` retracts and turns them toward the hub, so the
    X appears to fold away with the menu instead of simply vanishing.
    """
    cx, cy, radius = int(cx), int(cy), max(8, int(radius))
    hover_amount = max(0.0, min(1.0, hover_amount))
    fold_amount = max(0.0, min(1.0, fold_amount))

    face_colour = _lerp_color((112, 44, 50), (143, 54, 59), hover_amount)
    face_colour = _lerp_color(face_colour, (82, 34, 39), 0.45 * fold_amount)
    inner_colour = _lerp_color((139, 59, 64), (177, 72, 76), hover_amount)
    inner_colour = _lerp_color(
        inner_colour, (100, 42, 48), 0.42 * fold_amount
    )
    rim_colour = _lerp_color((204, 116, 119), (255, 169, 169), hover_amount)
    rim_colour = _lerp_color(rim_colour, (176, 91, 97), 0.35 * fold_amount)

    # Layered circular socket/seal inside the pill.
    pygame.draw.circle(surface, (45, 14, 18), (cx, cy + 1), radius)
    pygame.draw.circle(surface, rim_colour, (cx, cy), radius)
    pygame.draw.circle(surface, face_colour, (cx, cy), max(5, radius - 1))
    pygame.draw.circle(surface, inner_colour, (cx, cy), max(4, radius - 3))
    try:
        pygame.gfxdraw.aacircle(surface, cx, cy, radius, (*rim_colour, 255))
        pygame.gfxdraw.aacircle(
            surface,
            cx,
            cy,
            max(4, radius - 3),
            (*_lerp_color(inner_colour, rim_colour, 0.45), 255),
        )
    except Exception:
        pass

    # A broken inner ring slowly turns on hover and faster while folding.
    arc_radius = max(4, radius - 2)
    arc_points = []
    arc_steps = 22
    arc_start = phase - 2.25
    arc_end = phase + 2.25
    for step in range(arc_steps + 1):
        angle = arc_start + (arc_end - arc_start) * step / arc_steps
        arc_points.append(
            (
                cx + arc_radius * math.cos(angle),
                cy + arc_radius * math.sin(angle),
            )
        )
    arc_colour = _lerp_color(rim_colour, (255, 255, 255), 0.28)
    if len(arc_points) >= 2:
        pygame.draw.lines(surface, arc_colour, False, arc_points, 2)
        try:
            pygame.draw.aalines(surface, arc_colour, False, arc_points)
        except Exception:
            pass

    glint_x = int(cx - radius * 0.35)
    glint_y = int(cy - radius * 0.36)
    pygame.draw.circle(surface, (255, 255, 255), (glint_x, glint_y), 1)

    # Four independently tapered rays form the X. Drawing rays rather than
    # two crossed rectangles gives the fold a mechanical, petal-like feel
    # and lets opposite rays carry slightly different shading.
    rotation = 0.10 * hover_amount + 0.62 * fold_amount
    arm_length = radius * (
        0.62
        + 0.08 * hover_amount
        - 0.34 * fold_amount
        - (0.03 if pressed else 0.0)
    )
    inner_gap = radius * (0.15 + 0.10 * fold_amount)
    base_half = max(1.0, radius * 0.12 * (1.0 - 0.25 * fold_amount))
    tip_half = max(0.55, radius * 0.075)
    bright_arm = _lerp_color(
        (242, 231, 231), (255, 255, 255), hover_amount
    )
    shaded_arm = _lerp_color((218, 200, 202), (246, 231, 231), hover_amount)
    outline_colour = (107, 38, 44)

    for arm_index in range(4):
        angle = math.pi / 4.0 + arm_index * math.pi / 2.0 + rotation
        direction = (math.cos(angle), math.sin(angle))
        normal = (-direction[1], direction[0])
        base_centre = (
            cx + direction[0] * inner_gap,
            cy + direction[1] * inner_gap,
        )
        tip_centre = (
            cx + direction[0] * arm_length,
            cy + direction[1] * arm_length,
        )
        points = [
            (
                base_centre[0] + normal[0] * base_half,
                base_centre[1] + normal[1] * base_half,
            ),
            (
                tip_centre[0] + normal[0] * tip_half,
                tip_centre[1] + normal[1] * tip_half,
            ),
            (
                tip_centre[0] - normal[0] * tip_half,
                tip_centre[1] - normal[1] * tip_half,
            ),
            (
                base_centre[0] - normal[0] * base_half,
                base_centre[1] - normal[1] * base_half,
            ),
        ]
        shadow_points = [(px, py + 1) for px, py in points]
        arm_colour = bright_arm if arm_index % 2 == 0 else shaded_arm
        pygame.draw.polygon(surface, (62, 20, 25), shadow_points)
        pygame.draw.polygon(surface, arm_colour, points)
        pygame.draw.polygon(surface, outline_colour, points, 1)
        try:
            pygame.gfxdraw.aapolygon(
                surface, points, (*outline_colour, 255)
            )
        except Exception:
            pass

        # Fine centre highlight on each ray.
        highlight_start = (
            base_centre[0] - normal[0] * base_half * 0.35,
            base_centre[1] - normal[1] * base_half * 0.35,
        )
        highlight_end = (
            tip_centre[0] - normal[0] * tip_half * 0.35,
            tip_centre[1] - normal[1] * tip_half * 0.35,
        )
        try:
            pygame.draw.aaline(
                surface, (255, 255, 255), highlight_start, highlight_end
            )
        except Exception:
            pygame.draw.line(
                surface, (255, 255, 255), highlight_start, highlight_end, 1
            )

    # Central hub covering the ray roots.
    hub_radius = max(2, int(round(radius * 0.19)))
    pygame.draw.circle(surface, (62, 20, 25), (cx, cy + 1), hub_radius)
    pygame.draw.circle(surface, bright_arm, (cx, cy), hub_radius)
    pygame.draw.circle(
        surface, (154, 55, 62), (cx, cy), max(1, hub_radius - 1)
    )
    pygame.draw.circle(surface, (255, 226, 226), (cx - 1, cy - 1), 1)


def draw_polished_close_button(screen, rect, hovered):
    """Draw the Settings header Close/X button.

    The caller retains the original 44x32 rect and click handling. This
    renderer only adds eased hover, pressed and menu-closing visuals.
    """
    global close_button_hover_amount
    global close_button_fold_amount, close_button_icon_phase

    closing = bool(menu_anim_closing)
    try:
        pressed = bool(hovered and pygame.mouse.get_pressed()[0])
    except Exception:
        pressed = False

    hover_target = 1.0 if hovered or closing else 0.0
    close_button_hover_amount += (
        hover_target - close_button_hover_amount
    ) * 0.24
    if abs(hover_target - close_button_hover_amount) < 0.01:
        close_button_hover_amount = hover_target
    hover_amount = max(0.0, min(1.0, close_button_hover_amount))

    fold_target = 1.0 if closing else 0.0
    close_button_fold_amount += (
        fold_target - close_button_fold_amount
    ) * 0.34
    if abs(fold_target - close_button_fold_amount) < 0.02:
        close_button_fold_amount = fold_target
    fold_amount = max(0.0, min(1.0, close_button_fold_amount))

    if closing:
        close_button_icon_phase = (
            close_button_icon_phase + 0.28
        ) % (2.0 * math.pi)
    elif hovered:
        close_button_icon_phase = (
            close_button_icon_phase + (0.012 if pressed else 0.035)
        ) % (2.0 * math.pi)

    visual_rect = rect.move(0, 1 if pressed or closing else 0)
    active_amount = max(hover_amount, 0.72 * fold_amount)

    # Layered ruby halo.
    if active_amount > 0.01:
        glow = pygame.Surface(
            (visual_rect.width + 18, visual_rect.height + 18),
            pygame.SRCALPHA,
        )
        for inset, alpha in ((1, 18), (4, 28), (7, 42)):
            pygame.draw.rect(
                glow,
                (255, 86, 92, int(alpha * active_amount)),
                (
                    inset,
                    inset,
                    glow.get_width() - inset * 2,
                    glow.get_height() - inset * 2,
                ),
                border_radius=20 - inset,
            )
        screen.blit(glow, (visual_rect.x - 9, visual_rect.y - 9))

    pygame.draw.rect(
        screen,
        (35, 12, 16),
        visual_rect.move(0, 1 if pressed or closing else 2),
        border_radius=14,
    )

    if pressed or closing:
        top_colour = (112, 48, 52)
        bottom_colour = (76, 33, 38)
        border_colour = (190, 105, 108)
    elif hovered:
        top_colour = (205, 78, 80)
        bottom_colour = (138, 48, 54)
        border_colour = (255, 169, 169)
    else:
        top_colour = (157, 67, 69)
        bottom_colour = (105, 42, 47)
        border_colour = (184, 105, 108)

    # Ruby-metal pill, built through separate overlay/mask surfaces like
    # the other polished controls so translucent glass blends correctly.
    body = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    denom = max(1, visual_rect.height - 1)
    for row in range(visual_rect.height):
        colour = _lerp_color(top_colour, bottom_colour, row / denom)
        pygame.draw.line(
            body,
            (*colour, 255),
            (0, row),
            (visual_rect.width - 1, row),
        )

    overlay = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    pygame.draw.polygon(
        overlay,
        (255, 255, 255, int(11 + 17 * hover_amount)),
        [
            (-8, visual_rect.height),
            (18, 0),
            (29, 0),
            (7, visual_rect.height),
        ],
    )
    pygame.draw.line(
        overlay,
        (255, 255, 255, int(70 + 46 * hover_amount)),
        (8, 1),
        (visual_rect.width - 9, 1),
        1,
    )
    pygame.draw.line(
        overlay,
        (255, 255, 255, 23),
        (2, 6),
        (2, visual_rect.height - 7),
        1,
    )
    pygame.draw.rect(
        overlay,
        (0, 0, 0, 46 if pressed or closing else 31),
        (0, visual_rect.height - 4, visual_rect.width, 4),
    )
    pygame.draw.rect(
        overlay,
        (0, 0, 0, 25),
        (visual_rect.width - 3, 5, 3, visual_rect.height - 10),
        border_radius=2,
    )
    # Two hairline diagonal facets in the pill's end caps.
    for facet_x in (4, visual_rect.width - 11):
        pygame.draw.polygon(
            overlay,
            (255, 205, 190, 13),
            [
                (facet_x, visual_rect.height),
                (facet_x + 8, 0),
                (facet_x + 11, 0),
                (facet_x + 3, visual_rect.height),
            ],
        )
    body.blit(overlay, (0, 0))

    mask = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    mask.fill((0, 0, 0, 0))
    pygame.draw.rect(
        mask,
        (255, 255, 255, 255),
        mask.get_rect(),
        border_radius=14,
    )
    body.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
    screen.blit(body, visual_rect.topleft)

    pygame.draw.rect(screen, border_colour, visual_rect, 2, border_radius=14)
    pygame.draw.rect(
        screen,
        _lerp_color(border_colour, (255, 255, 255), 0.30),
        visual_rect.inflate(-3, -3),
        1,
        border_radius=12,
    )

    if hover_amount > 0.04:
        accent_colour = _lerp_color(
            bottom_colour, (255, 178, 166), hover_amount
        )
        accent_w = int(22 * hover_amount)
        if accent_w >= 2:
            pygame.draw.line(
                screen,
                accent_colour,
                (visual_rect.centerx - accent_w // 2, visual_rect.bottom - 3),
                (visual_rect.centerx + accent_w // 2, visual_rect.bottom - 3),
                2,
            )

    # Tiny end-cap rivets, clear of the central seal.
    for rivet_x in (visual_rect.x + 6, visual_rect.right - 7):
        rivet_centre = (rivet_x, visual_rect.centery)
        pygame.draw.circle(
            screen,
            (43, 15, 19),
            (rivet_centre[0], rivet_centre[1] + 1),
            2,
        )
        pygame.draw.circle(
            screen,
            _lerp_color(border_colour, (255, 255, 255), 0.18),
            rivet_centre,
            2,
        )
        pygame.draw.circle(
            screen,
            _lerp_color(bottom_colour, (255, 255, 255), 0.25),
            rivet_centre,
            2,
            1,
        )
        pygame.draw.line(
            screen,
            (74, 29, 34),
            (rivet_centre[0] - 1, rivet_centre[1] + 1),
            (rivet_centre[0] + 1, rivet_centre[1] - 1),
            1,
        )

    _draw_close_x_icon(
        screen,
        visual_rect.centerx,
        visual_rect.centery,
        12,
        hover_amount,
        fold_amount,
        close_button_icon_phase,
        pressed,
    )

    # Below-button tooltip, clamped inside the right edge of the window.
    if hovered and not pressed and not closing:
        tip = font_tiny.render("Close Settings", True, TEXT_COLOR)
        tip_w = tip.get_width() + 16
        tip_h = 24
        tip_x = max(
            4,
            min(visual_rect.centerx - tip_w // 2, WIDTH - tip_w - 4),
        )
        tip_y = min(visual_rect.bottom + 7, HEIGHT - tip_h - 4)
        tip_rect = pygame.Rect(tip_x, tip_y, tip_w, tip_h)
        pygame.draw.rect(screen, (34, 18, 21), tip_rect, border_radius=7)
        pygame.draw.rect(
            screen, (255, 169, 169), tip_rect, 1, border_radius=7
        )
        pointer_x = max(
            tip_rect.x + 8,
            min(visual_rect.centerx, tip_rect.right - 8),
        )
        pygame.draw.polygon(
            screen,
            (34, 18, 21),
            [
                (pointer_x - 5, tip_rect.y + 1),
                (pointer_x + 5, tip_rect.y + 1),
                (pointer_x, max(visual_rect.bottom + 1, tip_rect.y - 5)),
            ],
        )
        screen.blit(tip, tip.get_rect(center=tip_rect.center))

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

def _draw_flip_board_icon(surface, cx, cy, hover_amount, pressed, turn):
    """Draw opposing ivory/charcoal arrows around a miniature board.

    ``turn`` is the eased 0..1 board-orientation amount. The arrows keep
    their up/down directions (the action is always “exchange ends”), while
    their ivory/charcoal fills cross-fade to the opposite arrow as the
    board flips. On hover they also travel one pixel apart.
    """
    cx, cy = int(cx), int(cy)
    turn = max(0.0, min(1.0, float(turn)))
    travel = hover_amount * (0.4 if pressed else 1.0)

    # Miniature 2x2 board in the active board-theme colours.
    board_size = 16
    half = board_size // 2
    board_rect = pygame.Rect(cx - half, cy - half, board_size, board_size)
    pygame.draw.rect(
        surface,
        (28, 21, 12),
        board_rect.move(0, 1),
        border_radius=3,
    )
    for row in range(2):
        for col in range(2):
            colour = LIGHT_SQ if (row + col) % 2 == 0 else DARK_SQ
            pygame.draw.rect(
                surface,
                colour,
                (
                    board_rect.x + col * half,
                    board_rect.y + row * half,
                    half,
                    half,
                ),
            )
    pygame.draw.line(
        surface,
        _lerp_color(LIGHT_SQ, (255, 255, 255), 0.45),
        (board_rect.x + 2, board_rect.y + 1),
        (board_rect.right - 3, board_rect.y + 1),
        1,
    )
    pygame.draw.rect(
        surface,
        (24, 20, 16),
        board_rect,
        1,
        border_radius=3,
    )

    ivory = (243, 234, 214)
    charcoal = (35, 38, 46)
    up_fill = _lerp_color(ivory, charcoal, turn)
    down_fill = _lerp_color(charcoal, ivory, turn)
    up_outline = _lerp_color((74, 58, 35), (218, 224, 235), turn)
    down_outline = _lerp_color((218, 224, 235), (74, 58, 35), turn)
    up_highlight = _lerp_color((255, 255, 255), (116, 124, 140), turn)
    down_highlight = _lerp_color((116, 124, 140), (255, 255, 255), turn)

    # Canonical chunky up-arrow. The down arrow is the same polygon
    # reflected through the centre, giving the pair a balanced exchange
    # silhouette instead of the old single line with mismatched heads.
    canonical = [
        (0, -10),
        (5, -4),
        (2, -4),
        (2, 8),
        (-2, 8),
        (-2, -4),
        (-5, -4),
    ]

    def draw_arrow(centre_x, pointing_down, fill, outline, highlight):
        def transform(dx, dy):
            if pointing_down:
                return (centre_x + dx, cy - dy + travel)
            return (centre_x + dx, cy + dy - travel)

        points = [transform(dx, dy) for dx, dy in canonical]
        shadow_points = [(px, py + 1) for px, py in points]
        pygame.draw.polygon(surface, (28, 21, 12), shadow_points)
        pygame.draw.polygon(surface, fill, points)
        pygame.draw.polygon(surface, outline, points, 1)
        try:
            pygame.gfxdraw.aapolygon(surface, points, (*outline, 255))
        except Exception:
            pass
        pygame.draw.line(
            surface,
            highlight,
            transform(-1, 6),
            transform(-1, -3),
            1,
        )

    draw_arrow(cx - 14, False, up_fill, up_outline, up_highlight)
    draw_arrow(cx + 14, True, down_fill, down_outline, down_highlight)

    # Amber centre pivot ties the exchange arrows to the mini board and
    # gives the eye a still point while their colours cross-fade.
    pygame.draw.circle(surface, (55, 38, 8), (cx, cy + 1), 3)
    pygame.draw.circle(surface, EVAL_MARKER_COLOR, (cx, cy), 3)
    pygame.draw.circle(surface, (255, 246, 190), (cx - 1, cy - 1), 1)


def draw_flip_button(screen, mouse_pos):
    """Draw the polished brass Flip Board top-bar button."""
    global flip_button_hover_amount, flip_icon_turn
    if menu_anim_progress != 0:
        return

    rect = flip_button_rect
    hovered = rect.collidepoint(mouse_pos)
    try:
        pressed = bool(hovered and pygame.mouse.get_pressed()[0])
    except Exception:
        pressed = False

    hover_target = 1.0 if hovered else 0.0
    flip_button_hover_amount += (
        hover_target - flip_button_hover_amount
    ) * 0.22
    if abs(hover_target - flip_button_hover_amount) < 0.01:
        flip_button_hover_amount = hover_target
    hover_amount = max(0.0, min(1.0, flip_button_hover_amount))

    turn_target = 1.0 if board_flipped else 0.0
    flip_icon_turn += (turn_target - flip_icon_turn) * 0.20
    if abs(turn_target - flip_icon_turn) < 0.01:
        flip_icon_turn = turn_target
    turn = cubic_ease_in_out(
        max(0.0, min(1.0, flip_icon_turn))
    )

    visual_rect = rect.move(0, 1 if pressed else 0)

    # Warm amber halo on hover, matching the eval marker/board palette.
    if hover_amount > 0.01:
        glow = pygame.Surface(
            (visual_rect.width + 16, visual_rect.height + 16),
            pygame.SRCALPHA,
        )
        for inset, alpha in ((1, 17), (4, 27), (7, 40)):
            pygame.draw.rect(
                glow,
                (255, 190, 80, int(alpha * hover_amount)),
                (
                    inset,
                    inset,
                    glow.get_width() - inset * 2,
                    glow.get_height() - inset * 2,
                ),
                border_radius=13 - inset,
            )
        screen.blit(glow, (visual_rect.x - 8, visual_rect.y - 8))

    pygame.draw.rect(
        screen,
        (24, 18, 10),
        visual_rect.move(0, 1 if pressed else 2),
        border_radius=9,
    )

    if pressed:
        top_colour = (83, 67, 43)
        bottom_colour = (56, 44, 29)
        border_colour = (179, 144, 88)
    elif hovered:
        top_colour = (145, 118, 74)
        bottom_colour = (91, 71, 44)
        border_colour = (235, 192, 117)
    else:
        top_colour = (120, 97, 63)
        bottom_colour = (73, 58, 38)
        border_colour = (191, 155, 96)

    # Rounded brass-metal body; translucent sheen/shading is composited
    # through an overlay and clipped by the same rounded mask technique as
    # the New/Settings buttons.
    body = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    denom = max(1, visual_rect.height - 1)
    for row in range(visual_rect.height):
        colour = _lerp_color(top_colour, bottom_colour, row / denom)
        pygame.draw.line(
            body,
            (*colour, 255),
            (0, row),
            (visual_rect.width - 1, row),
        )

    overlay = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    pygame.draw.polygon(
        overlay,
        (255, 255, 255, int(11 + 18 * hover_amount)),
        [
            (-8, visual_rect.height),
            (visual_rect.width * 0.44, 0),
            (visual_rect.width * 0.67, 0),
            (visual_rect.width * 0.13, visual_rect.height),
        ],
    )
    pygame.draw.line(
        overlay,
        (255, 255, 255, int(74 + 48 * hover_amount)),
        (6, 1),
        (visual_rect.width - 7, 1),
        1,
    )
    pygame.draw.line(
        overlay,
        (255, 255, 255, 23),
        (2, 5),
        (2, visual_rect.height - 6),
        1,
    )
    pygame.draw.rect(
        overlay,
        (0, 0, 0, 46 if pressed else 31),
        (0, visual_rect.height - 4, visual_rect.width, 4),
    )
    pygame.draw.rect(
        overlay,
        (0, 0, 0, 25),
        (visual_rect.width - 3, 4, 3, visual_rect.height - 8),
        border_radius=2,
    )
    body.blit(overlay, (0, 0))

    mask = pygame.Surface(visual_rect.size, pygame.SRCALPHA)
    mask.fill((0, 0, 0, 0))
    pygame.draw.rect(
        mask,
        (255, 255, 255, 255),
        mask.get_rect(),
        border_radius=9,
    )
    body.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
    screen.blit(body, visual_rect.topleft)

    pygame.draw.rect(screen, border_colour, visual_rect, 2, border_radius=9)
    pygame.draw.rect(
        screen,
        _lerp_color(border_colour, (255, 255, 255), 0.30),
        visual_rect.inflate(-3, -3),
        1,
        border_radius=7,
    )

    if hover_amount > 0.04:
        accent_colour = _lerp_color(
            bottom_colour, (255, 207, 104), hover_amount
        )
        accent_w = int(26 * hover_amount)
        if accent_w >= 2:
            pygame.draw.line(
                screen,
                accent_colour,
                (visual_rect.centerx - accent_w // 2, visual_rect.bottom - 3),
                (visual_rect.centerx + accent_w // 2, visual_rect.bottom - 3),
                2,
            )

    _draw_flip_board_icon(
        screen,
        visual_rect.centerx,
        visual_rect.centery,
        hover_amount,
        pressed,
        turn,
    )

    # The button sits in the top bar, so the tooltip goes below it and is
    # clamped inside the window. Its wording names the perspective the
    # click will switch to. Suppress it under modal dialogs.
    if (
        hovered
        and not pressed
        and not show_newgame_confirm
        and not show_game_over
    ):
        perspective = "White" if board_flipped else "Black"
        tip = font_tiny.render(
            f"Flip board — view from {perspective}", True, TEXT_COLOR
        )
        tip_w = tip.get_width() + 16
        tip_h = 24
        tip_x = max(
            4,
            min(visual_rect.centerx - tip_w // 2, WIDTH - tip_w - 4),
        )
        tip_y = min(visual_rect.bottom + 7, HEIGHT - tip_h - 4)
        tip_rect = pygame.Rect(tip_x, tip_y, tip_w, tip_h)
        pygame.draw.rect(screen, (31, 26, 18), tip_rect, border_radius=7)
        pygame.draw.rect(
            screen, (235, 192, 117), tip_rect, 1, border_radius=7
        )
        pointer_x = max(
            tip_rect.x + 8,
            min(visual_rect.centerx, tip_rect.right - 8),
        )
        pygame.draw.polygon(
            screen,
            (31, 26, 18),
            [
                (pointer_x - 5, tip_rect.y + 1),
                (pointer_x + 5, tip_rect.y + 1),
                (pointer_x, max(visual_rect.bottom + 1, tip_rect.y - 5)),
            ],
        )
        screen.blit(tip, tip.get_rect(center=tip_rect.center))

def get_flip_button_rect():
    return flip_button_rect


def draw_settings_menu(screen):
    global menu_anim_progress, settings_menu_state, menu_transition_progress
    global menu_transition_direction

    if menu_anim_progress <= 0.01:
        return

    # During the shell's open/close choreography, render the existing
    # pages once to an offscreen surface at their settled coordinates and
    # composite that surface with the presentation scale/fade. At rest
    # ``screen`` remains the real destination, so final pixels, hover
    # positions and click rects are exactly the established ones.
    destination = screen
    presentation = _settings_menu_presentation()
    shell_animating = bool(presentation["animating"])
    content_surface = None
    if shell_animating:
        content_surface = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        screen = content_surface

    update_menu_transition()

    mx, my = pygame.mouse.get_pos()
    transition_idle = (
        menu_transition_direction == "none"
        and menu_anim_progress >= 0.99
        and not shell_animating
    )

    # --- Choreographed shell open/close: backdrop, reveal, then pages ---
    eased = presentation["presence"]
    backdrop_alpha = presentation["backdrop_alpha"]
    overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    overlay.fill((0, 0, 0, backdrop_alpha))
    destination.blit(overlay, (0, 0))
    _draw_settings_menu_reveal(destination, presentation)
    # The composite transform supplies the shared rise/sink. Individual
    # main-menu rows add only their staggered cascade below, avoiding the
    # old double-slide where every element shared one 22 px offset.
    open_slide = 0

    # --- Page switching ---
    # The outgoing and incoming pages are drawn at the same time and slide
    # together: going forward the new page pushes in from the right while
    # the old one recedes slightly to the left (parallax) and dims; going
    # back the current page slides away to reveal the previous one
    # underneath. (The old version slid one page completely out, left the
    # screen empty, then slid the next one in.)
    out_off = in_off = 0
    trans_e = 0.0
    if menu_transition_direction == "none":
        pages_to_draw = [(settings_menu_state, 0)]
        effective_page = settings_menu_state
    else:
        trans_e = cubic_ease_in_out(max(0.0, min(1.0, menu_transition_progress)))
        if menu_transition_direction == "forward":
            out_off = -int(115 * trans_e)
            in_off = int(390 * (1 - trans_e))
            pages_to_draw = [(settings_menu_from, out_off), (settings_menu_target, in_off)]
        else:  # back
            out_off = int(390 * trans_e)
            in_off = -int(115 * (1 - trans_e))
            pages_to_draw = [(settings_menu_target, in_off), (settings_menu_from, out_off)]
        effective_page = settings_menu_target if trans_e >= 0.5 else settings_menu_from

    def draw_page(page_state, offset_x):
        if page_state == "main":
            menu_items = SETTINGS_MAIN_ITEMS
            item_height = 50
            total_menu_height = len(menu_items) * item_height + (len(menu_items) - 1) * 20
            start_y = (HEIGHT - total_menu_height) // 2

            for i, item in enumerate(menu_items):
                # Rows cascade in on open and collect back out in reverse
                # arrival order on close; the shell composite handles the
                # shared scale/fade/rise around these local offsets.
                item_slide = _settings_main_item_slide(
                    i, len(menu_items), presentation
                )
                y_slide = open_slide + item_slide
                item_rect = pygame.Rect(
                    WIDTH // 2 - 200 + offset_x,
                    start_y + i * (item_height + 20) + y_slide,
                    400,
                    item_height,
                )

                is_interactive = item in ("Board", "Engine Settings", "Analysis Settings")
                is_hovered = (
                    is_interactive
                    and transition_idle
                    and item_rect.collidepoint(mx, my)
                )
                if item == "Engine Settings":
                    draw_polished_engine_settings_button(
                        screen, item_rect, is_hovered
                    )
                elif item == "Board":
                    draw_polished_board_button(
                        screen, item_rect, is_hovered
                    )
                elif item == "Analysis Settings":
                    draw_polished_analysis_settings_button(
                        screen, item_rect, is_hovered
                    )
                else:
                    bg = (80, 80, 120) if is_hovered else (60, 60, 60)
                    pygame.draw.rect(screen, bg, item_rect, border_radius=10)
                    pygame.draw.rect(
                        screen,
                        (150, 170, 255) if is_hovered else MENU_BORDER_COLOR,
                        item_rect, 1, border_radius=10,
                    )
                    text_color = TEXT_COLOR if is_interactive else (150, 150, 150)
                    item_text = font_large.render(item, True, text_color)
                    screen.blit(item_text, item_text.get_rect(center=item_rect.center))
                    if is_interactive:
                        chev = font_large.render("›", True, (200, 210, 255) if is_hovered else TEXT_MUTED)
                        screen.blit(chev, chev.get_rect(center=(item_rect.right - 24, item_rect.centery)))

            about_y = start_y + len(menu_items) * (item_height + 20) + 40 + open_slide

            about_title = font_large.render("About", True, TEXT_COLOR)
            screen.blit(about_title, about_title.get_rect(center=(WIDTH // 2 + offset_x, about_y)))

            version_text = font_small.render(
                "ChessBotV Rewrite Version 1.5-beta2", True, TEXT_COLOR
            )
            screen.blit(version_text, version_text.get_rect(center=(WIDTH // 2 + offset_x, about_y + 40)))

            github_rect = get_github_button_rect(offset_x, about_y)
            github_hovered = transition_idle and github_rect.collidepoint(mx, my)
            draw_polished_github_button(screen, github_rect, github_hovered)

            restart_rect = get_restart_button_rect(offset_x, about_y)
            restart_hovered = transition_idle and restart_rect.collidepoint(mx, my)
            draw_polished_restart_button(screen, restart_rect, restart_hovered)

            # Always on Top toggle, pinned to the bottom of the main menu
            aot_rect = get_always_on_top_rect(offset_x, open_slide)
            draw_polished_always_on_top_toggle(
                screen, aot_rect,
                always_on_top_enabled, always_on_top_anim,
                transition_idle and aot_rect.collidepoint(mx, my),
            )

        elif page_state == "board":
            draw_board_hub_content(screen, offset_x, open_slide)
        elif page_state == "pieces_style":
            draw_pieces_style_content(screen, offset_x, open_slide)
        elif page_state == "board_themes":
            draw_board_themes_content(screen, offset_x, open_slide)
        elif page_state == "engine_settings":
            draw_engine_settings_content(screen, offset_x, open_slide)
        elif page_state == "analysis":
            draw_analysis_content(screen, offset_x, open_slide)

    for idx, (page_state, page_off) in enumerate(pages_to_draw):
        # The page on top rides in on a "sheet" that occludes the page
        # underneath (pages have different heights, so without it the old
        # page would show through below the new one). The sheet fades in
        # and back out over the transition, so nothing changes at rest.
        if len(pages_to_draw) > 1 and idx == 1:
            sheet_alpha = int(225 * math.sin(math.pi * max(0.0, min(1.0, trans_e))))
            if sheet_alpha > 0:
                sheet = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
                sheet.fill((34, 34, 41, sheet_alpha))
                screen.blit(sheet, (page_off, 0))
                # soft shadow on the sheet's leading edge
                shadow = pygame.Surface((22, HEIGHT), pygame.SRCALPHA)
                for i in range(22):
                    edge_a = int(sheet_alpha * 0.45 * (i / 22))
                    if edge_a > 0:
                        pygame.draw.line(shadow, (0, 0, 0, edge_a), (i, 0), (i, HEIGHT), 1)
                screen.blit(shadow, (page_off - 22, 0))
        draw_page(page_state, page_off)
        # Dim the page underneath while the other one is on top of it.
        if len(pages_to_draw) > 1 and idx == 0:
            dim_alpha = int(100 * trans_e) if menu_transition_direction == "forward" else int(100 * (1 - trans_e))
            if dim_alpha > 0:
                dim = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
                dim.fill((0, 0, 0, dim_alpha))
                screen.blit(dim, (0, 0))

    # --- Header: cross-fading title, breadcrumb, back/close ---
    if menu_transition_direction == "none":
        title_text = font_large.render(SETTINGS_PAGE_TITLES.get(settings_menu_state, "Settings"), True, TEXT_COLOR)
        screen.blit(title_text, title_text.get_rect(center=(WIDTH // 2, 38 + open_slide)))
    else:
        out_title = font_large.render(SETTINGS_PAGE_TITLES.get(settings_menu_from, "Settings"), True, TEXT_COLOR)
        out_title.set_alpha(max(0, int(255 * (1 - trans_e))))
        screen.blit(out_title, out_title.get_rect(center=(WIDTH // 2 + out_off, 38 + open_slide)))
        in_title = font_large.render(SETTINGS_PAGE_TITLES.get(settings_menu_target, "Settings"), True, TEXT_COLOR)
        in_title.set_alpha(max(0, int(255 * trans_e)))
        screen.blit(in_title, in_title.get_rect(center=(WIDTH // 2 + in_off, 38 + open_slide)))

    crumbs = settings_breadcrumb(effective_page)
    if len(crumbs) > 1:
        crumb_text = font_tiny.render("  ›  ".join(crumbs), True, TEXT_MUTED)
        screen.blit(crumb_text, crumb_text.get_rect(center=(WIDTH // 2, 62 + open_slide)))

    # Thin accent line that fills as you go deeper into the menu.
    line_w = 46 + 46 * (len(crumbs) - 1)
    pygame.draw.line(screen, (110, 150, 255), (WIDTH // 2 - line_w // 2, 74 + open_slide), (WIDTH // 2 + line_w // 2, 74 + open_slide), 2)

    close_button_rect = pygame.Rect(WIDTH - 60, 14 + open_slide, 44, 32)
    close_hovered = transition_idle and close_button_rect.collidepoint(mx, my)
    draw_polished_close_button(screen, close_button_rect, close_hovered)

    show_back = effective_page != "main" or settings_menu_from != "main"
    if show_back:
        back_button_rect = pygame.Rect(20, 14 + open_slide, 44, 32)
        back_hovered = transition_idle and back_button_rect.collidepoint(mx, my)
        draw_polished_back_button(screen, back_button_rect, back_hovered)

    if content_surface is not None:
        _composite_settings_menu_content(
            destination, content_surface, presentation
        )

def handle_menu_click(mx, my):
    global menu_anim_closing, settings_menu_state, menu_transition_direction
    global current_theme, images, theme_scroll_offset, settings_menu_target
    global engine_slider_dragging, restart_requested
    global show_auto_color_dialog, auto_color_dialog_anim

    if menu_anim_progress < 1.0 or menu_transition_direction != "none":
        return True

    close_button_rect = pygame.Rect(WIDTH - 60, 14, 44, 32)
    if close_button_rect.collidepoint(mx, my):
        begin_settings_menu_close(close_button_rect.center)
        return True

    if settings_menu_state != "main":
        back_button_rect = pygame.Rect(20, 14, 44, 32)
        if back_button_rect.collidepoint(mx, my):
            navigate_back()
            return True

    if settings_menu_state == "analysis":
        if get_auto_move_toggle_rect().collidepoint(mx, my):
            if auto_move_enabled:
                set_auto_move(False)
            elif not show_auto_color_dialog:
                # Ask which colour first; Auto-Move only turns on once
                # a colour is chosen in the dialog.
                show_auto_color_dialog = True
                auto_color_dialog_anim = 0.0
            return True
        _depth_row, depth_minus, _depth_value, depth_plus = (
            get_auto_move_depth_rects()
        )
        if depth_minus.collidepoint(mx, my):
            set_auto_move_depth(auto_move_depth - 1)
            return True
        if depth_plus.collidepoint(mx, my):
            set_auto_move_depth(auto_move_depth + 1)
            return True
        if auto_move_enabled:
            for color, rect in get_auto_move_color_rects().items():
                if rect.collidepoint(mx, my):
                    if color != auto_move_color:
                        set_auto_move(True, color=color)
                    return True
        if get_board_detection_button_rect().collidepoint(mx, my):
            if not board_detection_running:
                open_board_detection_dialog()
            return True
        if get_autoplay_toggle_rect().collidepoint(mx, my):
            request_autoplay_start()
            return True

    elif settings_menu_state == "board":
        for page, rect in get_board_hub_rects().items():
            if rect.collidepoint(mx, my):
                if page == "pieces_style":
                    theme_scroll_offset = 0.0
                navigate_to(page, forward=True)
                return True

    elif settings_menu_state == "board_themes":
        for theme_id, rect in get_board_theme_card_rects():
            if rect.collidepoint(mx, my):
                if theme_id != current_board_theme:
                    apply_board_theme(theme_id, persist=True)
                return True

    elif settings_menu_state == "pieces_style":
        viewport, style_rects, _total_h, _max_scroll, _scroll = (
            _pieces_style_layout(get_available_themes())
        )
        if viewport.collidepoint(mx, my):
            for theme_id, _display, theme_rect in style_rects:
                if theme_rect.collidepoint(mx, my):
                    if theme_id != current_theme:
                        current_theme = theme_id
                        images = get_piece_images(current_theme)
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
        menu_items = SETTINGS_MAIN_ITEMS
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
                if item == "Board":
                    navigate_to("board", forward=True)
                elif item == "Engine Settings":
                    navigate_to("engine_settings", forward=True)
                elif item == "Analysis Settings":
                    navigate_to("analysis", forward=True)
                return True

        if get_always_on_top_rect().collidepoint(mx, my):
            apply_always_on_top(not always_on_top_enabled, persist=True)
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
            begin_settings_menu_close()
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


def get_game_over_rects():
    """Panel + button rects for the game-over dialog (drawn & clicked)."""
    e = cubic_ease_in_out(max(0.0, min(1.0, game_over_anim)))
    w, h = 384, 204
    x = WIDTH // 2 - w // 2
    y = HEIGHT // 2 - h // 2 - int((1 - e) * 18)
    panel = pygame.Rect(x, y, w, h)
    new_rect = pygame.Rect(x + 28, y + h - 62, 152, 42)
    keep_rect = pygame.Rect(x + w - 180, y + h - 62, 152, 42)
    return panel, new_rect, keep_rect


def draw_game_over_dialog(screen, board):
    """Win/draw dialog offering a fresh game (or keep viewing the board)."""
    result = get_game_result(board)
    if result is None:
        return None, None
    title, subtitle, score, colour = result
    e = cubic_ease_in_out(max(0.0, min(1.0, game_over_anim)))
    panel, new_rect, keep_rect = get_game_over_rects()

    overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
    overlay.fill((0, 0, 0, int(165 * e)))
    screen.blit(overlay, (0, 0))

    # soft shadow + panel
    pygame.draw.rect(screen, (18, 18, 22), panel.move(0, 5), border_radius=14)
    pygame.draw.rect(screen, (52, 52, 64), panel, border_radius=14)
    pygame.draw.rect(screen, colour, panel, 2, border_radius=14)
    # accent strip across the top
    pygame.draw.line(screen, colour, (panel.x + 18, panel.y + 3), (panel.right - 18, panel.y + 3), 3)

    score_s = font_large.render(score, True, colour)
    screen.blit(score_s, score_s.get_rect(center=(panel.centerx, panel.y + 32)))
    title_s = font_large.render(title, True, TEXT_COLOR)
    screen.blit(title_s, title_s.get_rect(center=(panel.centerx, panel.y + 66)))
    ply = len(board.move_stack)
    moves = (ply + 1) // 2
    sub = f"{subtitle} · {moves} move{'s' if moves != 1 else ''}"
    opening = update_opening_name(board)
    if opening and opening != "Unknown Opening":
        sub += f" · {opening}"
    sub_s = font_small.render(sub, True, TEXT_MUTED)
    # keep the line inside the panel even for long opening names
    if sub_s.get_width() > panel.width - 24:
        sub_s = font_tiny.render(sub, True, TEXT_MUTED)
    screen.blit(sub_s, sub_s.get_rect(center=(panel.centerx, panel.y + 96)))
    ask_s = font_tiny.render("Start a new game?", True, TEXT_MUTED)
    screen.blit(ask_s, ask_s.get_rect(center=(panel.centerx, panel.y + 118)))

    mx, my = pygame.mouse.get_pos()
    new_hover = new_rect.collidepoint(mx, my)
    keep_hover = keep_rect.collidepoint(mx, my)
    pygame.draw.rect(screen, (74, 158, 96) if new_hover else (56, 130, 78), new_rect, border_radius=9)
    pygame.draw.rect(screen, (120, 220, 150) if new_hover else MENU_BORDER_COLOR, new_rect, 2, border_radius=9)
    pygame.draw.rect(screen, (98, 98, 110) if keep_hover else (72, 72, 84), keep_rect, border_radius=9)
    pygame.draw.rect(screen, MENU_BORDER_COLOR, keep_rect, 2, border_radius=9)
    new_t = font_small.render("New Game", True, TEXT_COLOR)
    keep_t = font_small.render("Keep viewing", True, TEXT_COLOR)
    screen.blit(new_t, new_t.get_rect(center=new_rect.center))
    screen.blit(keep_t, keep_t.get_rect(center=keep_rect.center))
    return new_rect, keep_rect

def draw_gui(
    screen, board, engine_thread, anim_mgr, check_effect, selected_sq, mouse_pos,
    drag_sq=None, drag_offset=None, dragging=False, view_ply=None, total_ply=None
):
    global depth_bar_fraction
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

    # App title (top-left, left of the top-bar buttons incl. Flip) and the
    # move counter / history position beside it (← / → step through it).
    hist_x = PADDING
    if menu_anim_progress == 0:
        title_s = font_large.render("ChessBotV Rewrite", True, TEXT_COLOR)
        # One-shot opening slide: start fully off the left edge and move
        # into the normal title position with cubic-in easing (t³). The
        # history label keeps using the final title width, so it does not
        # jump around while the title is arriving.
        title_ease = cubic_ease_in(title_slide_progress)
        title_start_x = -title_s.get_width() - 16
        title_x = int(
            title_start_x + (PADDING - title_start_x) * title_ease
        )
        if title_slide_progress < 1.0:
            title_s.set_alpha(
                int(70 + 185 * max(0.0, min(1.0, title_slide_progress)))
            )
        screen.blit(
            title_s,
            (title_x, (TOP_BAR_HEIGHT - title_s.get_height()) // 2),
        )
        hist_x = PADDING + title_s.get_width() + 16
    if total_ply:
        _view = len(board.move_stack) if view_ply is None else view_ply
        if _view < total_ply:
            hist_s = font_tiny.render(f"◀  Move {_view}/{total_ply} — viewing history  ▶", True, (255, 205, 110))
        else:
            hist_s = font_tiny.render(f"Move {_view}", True, TEXT_MUTED)
        if hist_x + hist_s.get_width() < flip_button_rect.x - 8:
            screen.blit(hist_s, (hist_x, (TOP_BAR_HEIGHT - hist_s.get_height()) // 2))

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
    # Click hint when hovered. Centre it on the narrow eval bar, but clamp
    # the whole tooltip inside the window: previously the text was centred
    # on a 32px-wide bar at either edge, so roughly half of it was clipped
    # off-screen. A small pointer still indicates which bar it belongs to.
    if get_eval_bar_rect().collidepoint(mouse_pos):
        hint = font_tiny.render("Click to swap side", True, TEXT_COLOR)
        tip_w = hint.get_width() + 18
        tip_h = 24
        tip_x = eval_bar_x + EVAL_BAR_WIDTH // 2 - tip_w // 2
        tip_x = max(4, min(tip_x, WIDTH - tip_w - 4))
        tip_y = max(4, eval_bar_y - tip_h - 4)
        tip_rect = pygame.Rect(tip_x, tip_y, tip_w, tip_h)
        pygame.draw.rect(screen, (22, 22, 28), tip_rect, border_radius=7)
        pygame.draw.rect(screen, (120, 150, 220), tip_rect, 1, border_radius=7)
        pointer_x = max(
            tip_rect.x + 8,
            min(eval_bar_x + EVAL_BAR_WIDTH // 2, tip_rect.right - 8),
        )
        pygame.draw.polygon(
            screen,
            (22, 22, 28),
            [
                (pointer_x - 5, tip_rect.bottom - 1),
                (pointer_x + 5, tip_rect.bottom - 1),
                (pointer_x, min(eval_bar_y - 1, tip_rect.bottom + 5)),
            ],
        )
        screen.blit(hint, hint.get_rect(center=tip_rect.center))

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
            (is_castling or anim_mgr.rook_anim)
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
    target_depth_str = f"/{ANALYSIS_DEPTH}" if ANALYSIS_DEPTH else ""
    depth_value_full = font_small.render(f"{depth}{target_depth_str}", True, TEXT_COLOR)
    screen.blit(depth_value_full, (col2_x + depth_label.get_width() + 8, stats_y))
    screen.blit(nps_label, (col3_x, stats_y))
    screen.blit(nps_value, (col3_x + nps_label.get_width() + 8, stats_y))

    # Small progress bar under the Depth readout (depth / target depth)
    _target_frac = max(0.0, min(1.0, depth / max(1, ANALYSIS_DEPTH))) if ANALYSIS_DEPTH else 0.0
    if _target_frac < depth_bar_fraction - 0.02:
        depth_bar_fraction = _target_frac  # new search: snap back
    else:
        depth_bar_fraction += (_target_frac - depth_bar_fraction) * 0.20
        if abs(depth_bar_fraction - _target_frac) < 0.005:
            depth_bar_fraction = _target_frac
    bar_w = min(140, col3_x - col2_x - 16)
    bar_rect = pygame.Rect(col2_x, stats_y + 23, bar_w, 5)
    pygame.draw.rect(screen, (52, 52, 60), bar_rect, border_radius=3)
    fill_w = int(bar_rect.width * depth_bar_fraction)
    if fill_w > 0:
        fill_col = (110, 220, 140) if depth_bar_fraction >= 0.999 else (90, 150, 255)
        pygame.draw.rect(screen, fill_col, (bar_rect.x, bar_rect.y, fill_w, bar_rect.height), border_radius=3)
    pygame.draw.rect(screen, (90, 90, 100), bar_rect, 1, border_radius=3)

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

    # Win/draw dialog sits above everything else.
    if show_game_over:
        draw_game_over_dialog(screen, board)

    # Auto-Move colour question (opened from the Analysis page, so it
    # must render above the settings menu).
    if show_auto_color_dialog:
        draw_auto_color_dialog(screen)

    # Board Detection window picker (also from the Analysis page).
    if show_board_detection_dialog:
        draw_board_detection_dialog(screen)

    # Auto-play side question and finished-game restart question.
    if show_autoplay_side_dialog:
        draw_autoplay_side_dialog(screen)
    if show_autoplay_gameover_dialog:
        draw_autoplay_gameover_dialog(screen)

    pygame.display.flip()


def main():
    global menu_anim_progress, menu_anim_opening, menu_anim_closing
    global menu_transition_direction, engine_thread, promotion_pending, restart_requested, engine_slider_dragging
    global show_newgame_confirm, confirm_anim, board_flipped, board_ox_current, eval_bar_x_current, board_ox_target, eval_bar_x_target, board_anim_t, eval_bar_side
    global engine_draft, engine_slider_display, engine_has_changes
    global show_game_over, game_over_anim, game_over_seen_key
    global show_auto_color_dialog, auto_color_dialog_anim, auto_move_status_text, auto_move_arm_reset
    global show_autoplay_side_dialog, show_autoplay_gameover_dialog

    clock = pygame.time.Clock()
    board = chess.Board()
    anim_mgr = AnimationManager()
    check_effect = CheckEffect()
    engine_thread = EngineThread(ENGINE_PATH, config)

    # Decode the GUI piece sets once at startup. The CNN vision model is
    # loaded lazily when Board Detection or Auto-play first needs it, so no
    # recognition dataset is held in memory.
    preload_piece_images()

    selected_sq = None
    dragging = False
    drag_sq = None
    drag_offset = None
    running = True
    # Moves stepped back over with ←; → replays them (and only them).
    history_redo = []

    def reset_game():
        """Start a fresh game (used by New, the confirm dialog, and the
        game-over dialog)."""
        global promotion_pending, show_game_over, game_over_seen_key
        nonlocal selected_sq, dragging, drag_sq, drag_offset, auto_seen_ply, auto_fired_key
        old_board = board.copy()
        anim_mgr.add_board_reset(old_board, chess.Board())
        board.reset()
        reset_opening()
        history_redo.clear()
        selected_sq = None
        dragging = False
        drag_sq = None
        drag_offset = None
        anim_mgr.capture_anims = []
        anim_mgr.current_eval = 0.0
        anim_mgr.target_eval = 0.0
        anim_mgr.arrow_target_alpha = 0.0
        anim_mgr.set_multipv_arrows({})
        check_effect.reset()
        engine_thread.update_position(board)
        promotion_pending = None
        show_game_over = False
        game_over_seen_key = None
        auto_seen_ply = 0
        auto_fired_key = None

    def note_new_move():
        """A move was just played from the displayed position. If it is
        exactly the next move in the recorded line, keep the rest of that
        line for →; otherwise the game has branched, so drop it."""
        if history_redo and board.move_stack and history_redo[-1] == board.move_stack[-1]:
            history_redo.pop()
        else:
            history_redo.clear()

    def sync_after_history_step():
        """Common cleanup after ← / → changes the displayed position.
        The move animation queued by add_move_animation_for_move is
        deliberately kept so the piece still slides to its square."""
        global promotion_pending
        nonlocal selected_sq, dragging, drag_sq, drag_offset
        selected_sq = None
        dragging = False
        drag_sq = None
        drag_offset = None
        promotion_pending = None
        anim_mgr.arrow_target_alpha = 0.0
        anim_mgr.set_multipv_arrows({})
        check_effect.reset()
        engine_thread.update_position(board)

    def execute_engine_best_move():
        """Play the engine's current best move (Space / Auto-Move)."""
        if board.is_game_over(claim_draw=True):
            return False
        with engine_thread.lock:
            pv = list(engine_thread.info.get("pv", []))
        if not pv or pv[0] not in board.legal_moves:
            return False
        best_move = pv[0]
        add_move_animation_for_move(anim_mgr, board, best_move, reverse=False)
        board.push(best_move)
        note_new_move()
        engine_thread.update_position(board)
        return True

    # Auto-Move arming: for a single colour it only starts after that
    # colour has moved once since it was turned on AND the opponent has
    # replied ("both" skips this waiting entirely).
    auto_stage = 2 if auto_move_color == "both" else 0  # 0 own, 1 opp, 2 armed
    auto_seen_ply = len(board.move_stack)
    auto_fired_key = None
    auto_pos_key = None
    auto_pos_frames = 0
    aot_first_frame_reapplied = False

    # --- Auto-play live-window state machine ---
    def autoplay_player_color():
        return chess.WHITE if _autoplay_side_name(_autoplay.get("side")) == "white" else chess.BLACK

    def autoplay_push_move(move):
        nonlocal auto_seen_ply, auto_fired_key
        if move not in board.legal_moves:
            return False
        add_move_animation_for_move(anim_mgr, board, move, reverse=False)
        board.push(move)
        note_new_move()
        engine_thread.update_position(board)
        _autoplay["expected_fen"] = board.fen()
        auto_seen_ply = len(board.move_stack)
        auto_fired_key = None
        return True

    def autoplay_finish_if_game_over():
        global show_game_over, game_over_seen_key
        global show_autoplay_gameover_dialog, autoplay_gameover_dialog_anim
        result = get_game_result(board)
        if result is None:
            return False
        _autoplay.update({
            "running": False,
            "state": "game_over",
            "pending_move": None,
            "pending_before_image": None,
            "pending_before_grid": None,
            "game_result": result,
        })
        _autoplay_set_status(f"{result[0]} — Auto-play finished.")
        show_autoplay_gameover_dialog = True
        autoplay_gameover_dialog_anim = 0.0
        # Suppress ChessBot's built-in game-over dialog for this same
        # terminal position; Auto-play asks about starting again instead.
        show_game_over = False
        game_over_seen_key = (board.fen(), len(board.move_stack))
        return True

    def autoplay_import_scan_result(result):
        nonlocal selected_sq, dragging, drag_sq, drag_offset
        nonlocal auto_seen_ply, auto_fired_key, auto_pos_key, auto_pos_frames
        global board_flipped, promotion_pending, show_game_over, game_over_seen_key
        global show_autoplay_gameover_dialog
        side = _autoplay_side_name(result.get("side", _autoplay.get("side")))

        if not _autoplay.get("enabled"):
            return False
        unknown = [
            (row, col)
            for row in range(8)
            for col in range(8)
            if result.get("piece_grid", [[None] * 8] * 8)[row][col] == "?"
        ]
        if unknown:
            return stop_autoplay(
                f"Auto-play scan stopped: {len(unknown)} square(s) are still unknown.",
                error=True,
            )

        try:
            fen = _autoplay_fen_from_visual_grid(result["piece_grid"], side)
            imported = chess.Board(fen)
            if imported.status():
                raise ValueError(f"scanned position is not a valid chess position ({fen})")
        except Exception as exc:
            return stop_autoplay(f"Auto-play could not import the scan as FEN: {exc}", error=True)

        old_board = board.copy()
        anim_mgr.add_board_reset(old_board, imported.copy())
        board.set_fen(fen)
        reset_opening()
        history_redo.clear()
        selected_sq = None
        dragging = False
        drag_sq = None
        drag_offset = None
        promotion_pending = None
        show_game_over = False
        game_over_seen_key = None
        show_autoplay_gameover_dialog = False
        board_flipped = side == "black"
        anim_mgr.capture_anims = []
        anim_mgr.current_eval = 0.0
        anim_mgr.target_eval = 0.0
        anim_mgr.arrow_target_alpha = 0.0
        anim_mgr.set_multipv_arrows({})
        check_effect.reset()
        engine_thread.update_position(board)

        board_crop = _autoplay_board_crop_from_screenshot(
            result.get("screenshot"), result.get("board_rect")
        )
        if board_crop is None:
            return stop_autoplay("Auto-play scan did not include a usable board image.", error=True)

        _autoplay.update({
            "running": True,
            "board_rect": result.get("board_rect"),
            "window_rect": result.get("window_rect"),
            "window_size": result.get("window_size"),
            "screen_board_rect": result.get("screen_board_rect"),
            "window": result.get("window") or _autoplay.get("window"),
            "side": side,
            "last_image": board_crop,
            "last_grid": _autoplay_board_grid_from_board(board),
            "expected_fen": board.fen(),
            "next_poll_at": time.monotonic() + AUTOPLAY_POLL_SECONDS,
            "pending_move": None,
            "pending_before_image": None,
            "pending_before_grid": None,
            "drag_attempts": 0,
        })
        auto_seen_ply = len(board.move_stack)
        auto_fired_key = None
        auto_pos_key = None
        auto_pos_frames = 0
        print(f"Auto-play imported FEN: {fen}")

        if autoplay_finish_if_game_over():
            return True
        if board.turn == autoplay_player_color():
            _autoplay["state"] = "our_turn"
            _autoplay_set_status(
                f"Auto-play synced as {side.capitalize()}. Waiting for depth {AUTOPLAY_DEPTH}..."
            )
        else:
            _autoplay["state"] = "wait_opponent"
            _autoplay_set_status(
                f"Auto-play synced as {side.capitalize()}. Waiting for the opponent..."
            )
        return True

    def autoplay_poll_candidate(base_grid, baseline_image):
        """Return (board crop, candidate grid or None, changed visual cells)."""
        crop = _autoplay_capture_board_crop()
        if not _autoplay.get("enabled"):
            return None, None, []
        if crop is None:
            return None, None, []
        changed = _autoplay_changed_visual_squares(baseline_image, crop)
        if not changed:
            return crop, None, []
        classified = _autoplay_classify_visual_squares(crop, changed)
        candidate = _autoplay_candidate_grid(
            base_grid, classified, _autoplay.get("side", "white")
        )
        return crop, candidate, changed

    def update_autoplay():
        # F8 is a global Windows stop key, useful while the chess window
        # (not ChessBotV) has keyboard focus during a live game.
        stop_down = _autoplay_stop_hotkey_down()
        was_down = bool(_autoplay.get("stop_key_down"))
        _autoplay["stop_key_down"] = stop_down
        if stop_down and not was_down and (
            _autoplay.get("enabled")
            or show_autoplay_side_dialog
            or show_autoplay_gameover_dialog
        ):
            stop_autoplay("Auto-play stopped with F8.")
            return

        if _autoplay.get("scan_ready"):
            result = _autoplay.get("scan_result")
            _autoplay["scan_ready"] = False
            _autoplay["scan_result"] = None
            if result is not None and _autoplay.get("enabled"):
                autoplay_import_scan_result(result)

        if not _autoplay.get("running"):
            return

        expected_fen = _autoplay.get("expected_fen")
        if expected_fen is not None and board.fen() != expected_fen:
            stop_autoplay(
                "Auto-play stopped: the ChessBot board was changed manually and is no longer synced.",
                error=True,
            )
            return
        if autoplay_finish_if_game_over():
            return

        now = time.monotonic()
        state = _autoplay.get("state")
        player = autoplay_player_color()
        side = _autoplay_side_name(_autoplay.get("side"))

        if state == "our_turn":
            if board.turn != player:
                _autoplay["state"] = "wait_opponent"
                _autoplay["next_poll_at"] = now + AUTOPLAY_POLL_SECONDS
                _autoplay_set_status("Auto-play waiting for the opponent...")
                return
            with engine_thread.lock:
                depth_now = int(engine_thread.info.get("depth", 0) or 0)
                pv_now = list(engine_thread.info.get("pv", []))
                info_fen = engine_thread.info.get("fen")
                engine_fen = (
                    engine_thread.board.fen()
                    if getattr(engine_thread, "board", None) is not None else None
                )
            if (
                depth_now < AUTOPLAY_DEPTH
                or not pv_now
                or pv_now[0] not in board.legal_moves
                or engine_fen != board.fen()
                or info_fen != board.fen()
            ):
                _autoplay_set_status(
                    f"Auto-play ({side}) waiting for depth {AUTOPLAY_DEPTH} (now {depth_now})..."
                )
                return

            move = pv_now[0]
            # Take a fresh board-only baseline immediately before touching
            # the external board. Known placement mismatches mean the live
            # window no longer matches ChessBot, so do not drag blindly.
            crop = _autoplay_capture_board_crop()
            if not _autoplay.get("enabled"):
                return
            if crop is not None and _autoplay.get("last_image") is not None:
                changed = _autoplay_changed_visual_squares(_autoplay["last_image"], crop)
                if changed:
                    classified = _autoplay_classify_visual_squares(crop, changed)
                    candidate = _autoplay_candidate_grid(
                        _autoplay_board_grid_from_board(board), classified, side
                    )
                    expected_grid = _autoplay_board_grid_from_board(board)
                    known_mismatch = any(
                        candidate[row][col] != "?"
                        and candidate[row][col] != expected_grid[row][col]
                        for row in range(8)
                        for col in range(8)
                    )
                    if known_mismatch:
                        stop_autoplay(
                            "Auto-play stopped before dragging: the live board no longer matches ChessBot.",
                            error=True,
                        )
                        return
                _autoplay["last_image"] = crop

            pending_before_image = _autoplay.get("last_image")
            pending_before_grid = _autoplay_board_grid_from_board(board)
            move_san = board.san(move)
            _autoplay_set_status(f"Auto-play dragging {move_san} in the target window...")
            window_info = _autoplay.get("window") or {}
            dragged = _autoplay_drag_move(
                move,
                _autoplay.get("screen_board_rect"),
                side,
                hwnd=window_info.get("hwnd"),
            )
            if not _autoplay.get("enabled"):
                return
            if not dragged:
                stop_autoplay("Auto-play could not drag the piece in the target window.", error=True)
                return
            _autoplay.update({
                "state": "verify_our_move",
                "pending_move": move,
                "pending_before_image": pending_before_image,
                "pending_before_grid": pending_before_grid,
                "pending_started_at": time.monotonic(),
                "drag_attempts": 1,
                "next_poll_at": time.monotonic() + AUTOPLAY_POLL_SECONDS,
            })
            _autoplay_set_status(
                f"Auto-play played {move_san}; checking the board in {AUTOPLAY_POLL_SECONDS:.1f}s..."
            )
            return

        if state == "wait_opponent":
            if board.turn == player:
                _autoplay["state"] = "our_turn"
                return
            if now < float(_autoplay.get("next_poll_at", 0.0)):
                return
            _autoplay["next_poll_at"] = now + AUTOPLAY_POLL_SECONDS
            base_grid = _autoplay_board_grid_from_board(board)
            crop, candidate, changed = autoplay_poll_candidate(
                base_grid, _autoplay.get("last_image")
            )
            if not _autoplay.get("running"):
                return
            if crop is None:
                _autoplay_set_status("Auto-play waiting: could not screenshot the target board; retrying...")
                return
            if not changed:
                _autoplay_set_status("Auto-play waiting for the opponent (board unchanged)...")
                return
            move = _autoplay_find_legal_move(board, candidate)
            if move is None:
                _autoplay_set_status(
                    f"Auto-play saw {len(changed)} changed square(s); waiting for the move to settle..."
                )
                return
            move_san = board.san(move)
            if not autoplay_push_move(move):
                stop_autoplay("Auto-play lost sync while applying the opponent move.", error=True)
                return
            _autoplay["last_image"] = crop
            _autoplay["last_grid"] = _autoplay_board_grid_from_board(board)
            if autoplay_finish_if_game_over():
                return
            _autoplay["state"] = "our_turn"
            _autoplay_set_status(
                f"Auto-play detected opponent move {move_san}. Waiting for depth {AUTOPLAY_DEPTH}..."
            )
            return

        if state == "verify_our_move":
            if now < float(_autoplay.get("next_poll_at", 0.0)):
                return
            _autoplay["next_poll_at"] = now + AUTOPLAY_POLL_SECONDS
            pending = _autoplay.get("pending_move")
            if pending is None:
                _autoplay["state"] = "wait_opponent"
                return
            before_grid = _autoplay.get("pending_before_grid") or _autoplay_board_grid_from_board(board)
            crop, candidate, changed = autoplay_poll_candidate(
                before_grid, _autoplay.get("pending_before_image")
            )
            if not _autoplay.get("running"):
                return
            if crop is None:
                _autoplay_set_status("Auto-play verifying move: could not screenshot the board; retrying...")
                return
            elapsed = now - float(_autoplay.get("pending_started_at", now))
            if not changed:
                if elapsed >= 1.2 and int(_autoplay.get("drag_attempts", 0)) < 2:
                    window_info = _autoplay.get("window") or {}
                    _autoplay_set_status("Auto-play move was not seen; dragging once more...")
                    if _autoplay_drag_move(
                        pending,
                        _autoplay.get("screen_board_rect"),
                        side,
                        hwnd=window_info.get("hwnd"),
                    ):
                        _autoplay["drag_attempts"] = int(_autoplay.get("drag_attempts", 0)) + 1
                        _autoplay["pending_started_at"] = time.monotonic()
                        _autoplay["next_poll_at"] = time.monotonic() + AUTOPLAY_POLL_SECONDS
                    else:
                        stop_autoplay("Auto-play could not retry the drag in the target window.", error=True)
                elif elapsed >= 2.8:
                    stop_autoplay(
                        "Auto-play stopped: the dragged move did not appear on the live board.",
                        error=True,
                    )
                else:
                    _autoplay_set_status("Auto-play verifying move (board unchanged yet)...")
                return

            clone = board.copy(stack=False)
            clone.push(pending)
            after_our_grid = _autoplay_board_grid_from_board(clone)
            if _autoplay_candidate_matches(candidate, after_our_grid, before_grid):
                move_san = board.san(pending)
                if not autoplay_push_move(pending):
                    stop_autoplay("Auto-play lost sync while applying its verified move.", error=True)
                    return
                _autoplay.update({
                    "last_image": crop,
                    "last_grid": _autoplay_board_grid_from_board(board),
                    "pending_move": None,
                    "pending_before_image": None,
                    "pending_before_grid": None,
                    "drag_attempts": 0,
                })
                if autoplay_finish_if_game_over():
                    return
                _autoplay["state"] = "wait_opponent"
                _autoplay["next_poll_at"] = time.monotonic() + AUTOPLAY_POLL_SECONDS
                _autoplay_set_status(
                    f"Auto-play verified {move_san}. Waiting for the opponent..."
                )
                return

            # A very fast opponent reply/premove can already be present in
            # the verification screenshot. Accept the two-move sequence if
            # it uniquely matches the changed squares.
            opponent_move = _autoplay_find_legal_move(clone, candidate)
            if opponent_move is not None:
                our_san = board.san(pending)
                if not autoplay_push_move(pending):
                    stop_autoplay("Auto-play lost sync while applying its verified move.", error=True)
                    return
                opponent_san = board.san(opponent_move)
                if not autoplay_push_move(opponent_move):
                    stop_autoplay("Auto-play lost sync while applying the fast opponent reply.", error=True)
                    return
                _autoplay.update({
                    "last_image": crop,
                    "last_grid": _autoplay_board_grid_from_board(board),
                    "pending_move": None,
                    "pending_before_image": None,
                    "pending_before_grid": None,
                    "drag_attempts": 0,
                })
                if autoplay_finish_if_game_over():
                    return
                _autoplay["state"] = "our_turn"
                _autoplay_set_status(
                    f"Auto-play verified {our_san} and detected {opponent_san}. Waiting for depth {AUTOPLAY_DEPTH}..."
                )
                return

            if elapsed >= 4.0:
                stop_autoplay(
                    "Auto-play stopped: the live board does not match the dragged move. Rescan to resync.",
                    error=True,
                )
            else:
                _autoplay_set_status("Auto-play verifying move; waiting for the board to settle...")
            return

    while running:
        anim_mgr.update()
        update_board_layout_anim()
        update_slider_display()
        update_global_anims()
        update_board_detection()
        update_autoplay()

        is_check = board.is_check()
        is_mate = board.is_checkmate()
        king_sq = (
            board.king(board.turn) if (is_check or is_mate) else None
        )
        check_effect.update(is_check, is_mate, king_sq)

        # Game-over dialog: show once per finished position (win or draw).
        # Stepping back with ← hides it; "Keep viewing" dismisses it for
        # this position only.
        _result = get_game_result(board)
        if _result is not None:
            _term_key = (board.fen(), len(board.move_stack))
            if not show_game_over and _term_key != game_over_seen_key:
                show_game_over = True
                game_over_anim = 0.0
                game_over_seen_key = _term_key
        else:
            show_game_over = False
            game_over_seen_key = None

        # --- Auto-Move ---
        if auto_move_arm_reset:
            auto_move_arm_reset = False
            auto_stage = 2 if auto_move_color == "both" else 0
            auto_seen_ply = len(board.move_stack)
            auto_fired_key = None
            auto_pos_key = None
            auto_pos_frames = 0
        _ply = len(board.move_stack)
        if auto_move_color != "both":
            _chosen = chess.WHITE if auto_move_color == "white" else chess.BLACK
            if _ply > auto_seen_ply:
                for _i in range(auto_seen_ply, _ply):
                    _mover = chess.WHITE if _i % 2 == 0 else chess.BLACK
                    if auto_stage == 0 and _mover == _chosen:
                        auto_stage = 1
                    elif auto_stage == 1 and _mover != _chosen:
                        auto_stage = 2
            auto_seen_ply = _ply
        else:
            auto_stage = 2
            auto_seen_ply = _ply
        if not auto_move_enabled:
            auto_move_status_text = ""
        elif auto_move_color == "both":
            auto_move_status_text = "Active — auto-moving both colours"
        elif auto_stage == 0:
            auto_move_status_text = f"Waiting — move once as {auto_move_color.capitalize()} first"
        elif auto_stage == 1:
            _opp = "Black" if auto_move_color == "white" else "White"
            auto_move_status_text = f"Waiting for {_opp} to move"
        else:
            auto_move_status_text = f"Active — auto-moving {auto_move_color.capitalize()}"

        _pos_key = (board.fen(), _ply, len(history_redo))
        if _pos_key != auto_pos_key:
            auto_pos_key = _pos_key
            auto_pos_frames = 0
        else:
            auto_pos_frames += 1
        if (
            auto_move_enabled
            and not _autoplay.get("enabled")
            and auto_stage == 2
            and not history_redo
            and menu_anim_progress == 0
            and not (menu_anim_opening or menu_anim_closing)
            and not show_newgame_confirm
            and not show_game_over
            and not show_auto_color_dialog
            and not show_board_detection_dialog
            and not board_detection_running
            and promotion_pending is None
            and not dragging
            and not (anim_mgr.move_anim or anim_mgr.rook_anim or anim_mgr.board_reset_anims)
            and _result is None
            and auto_pos_frames >= 2
            and _pos_key != auto_fired_key
            and (auto_move_color == "both" or board.turn == (chess.WHITE if auto_move_color == "white" else chess.BLACK))
        ):
            _threshold = get_auto_move_depth()
            with engine_thread.lock:
                _depth_now = engine_thread.info.get("depth", 0)
                _pv_now = list(engine_thread.info.get("pv", []))
                _engine_fen = engine_thread.board.fen() if getattr(engine_thread, "board", None) is not None else None
            if (
                _depth_now >= _threshold
                and _pv_now
                and _pv_now[0] in board.legal_moves
                and _engine_fen == board.fen()
            ):
                if execute_engine_best_move():
                    auto_fired_key = _pos_key
                    auto_seen_ply = len(board.move_stack)

        update_menu_animation()
        if restart_requested:
            if menu_anim_progress == 0 and not menu_anim_opening and not menu_anim_closing:
                running = False
            elif menu_anim_progress > 0 and not menu_anim_closing and not menu_anim_opening:
                begin_settings_menu_close()
        mouse_pos = pygame.mouse.get_pos()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                mx, my = event.pos

                # Board Detection window picker is modal over Settings.
                if show_board_detection_dialog:
                    handle_board_detection_click(mx, my)
                    continue

                # Auto-play setup/restart questions are modal too.
                if show_autoplay_side_dialog:
                    handle_autoplay_side_click(mx, my)
                    continue
                if show_autoplay_gameover_dialog:
                    handle_autoplay_gameover_click(mx, my)
                    continue

                # Confirmation dialog has priority over everything
                if show_newgame_confirm:
                    # compute dialog buttons positions (same as draw_confirm_dialog)
                    w, h = 360, 160
                    x = WIDTH//2 - w//2
                    y = HEIGHT//2 - h//2
                    yes_rect = pygame.Rect(x+30, y+95, 140, 40)
                    no_rect = pygame.Rect(x+w-170, y+95, 140, 40)
                    if yes_rect.collidepoint(mx, my):
                        reset_game()
                        show_newgame_confirm = False
                    elif no_rect.collidepoint(mx, my):
                        show_newgame_confirm = False
                    else:
                        # click outside — dismiss
                        # keep dialog open? we close on outside click for convenience
                        show_newgame_confirm = False
                    continue

                # Game-over dialog (win / draw) — modal, above the board
                if show_game_over:
                    _panel, _new_rect, _keep_rect = get_game_over_rects()
                    if _new_rect.collidepoint(mx, my):
                        reset_game()
                    elif _keep_rect.collidepoint(mx, my):
                        # game_over_seen_key already holds this position,
                        # so the dialog stays dismissed until the position
                        # stops being terminal (e.g. stepping back).
                        show_game_over = False
                    continue

                # Auto-Move colour question — modal, above the settings menu
                if show_auto_color_dialog:
                    _panel, _choices, _cancel = get_auto_color_dialog_rects()
                    _picked = None
                    for _color, _rect in _choices.items():
                        if _rect.collidepoint(mx, my):
                            _picked = _color
                            break
                    if _picked is not None:
                        set_auto_move(True, color=_picked)
                        show_auto_color_dialog = False
                    elif _cancel.collidepoint(mx, my) or not _panel.collidepoint(mx, my):
                        show_auto_color_dialog = False
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
                                if try_execute_promotion(board, anim_mgr, engine_thread, promotion_pending["from_sq"], promotion_pending["to_sq"], promo_piece, animate=True):
                                    note_new_move()
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
                        if len(board.move_stack) + len(history_redo) > 0:
                            show_newgame_confirm = True
                            confirm_anim = 0.0
                        else:
                            reset_game()
                        continue

                    elif settings_button_rect.collidepoint(mx, my):
                        begin_settings_menu_open()

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
                                            note_new_move()
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
                            note_new_move()
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
                if show_board_detection_dialog:
                    if event.y < 0:
                        move_board_detection_page(1)
                    elif event.y > 0:
                        move_board_detection_page(-1)
                elif (
                    settings_menu_open
                    and settings_menu_state == "pieces_style"
                    and menu_transition_direction == "none"
                ):
                    # pygame reports wheel-up as +y; negating makes scrolling
                    # down move the list down (and up move it up).
                    scroll_theme_list(-event.y * theme_scroll_speed)

            elif event.type == pygame.KEYDOWN:
                if show_board_detection_dialog:
                    handle_board_detection_key(event.key)
                    continue
                if event.key == pygame.K_F8 and (
                    _autoplay.get("enabled")
                    or show_autoplay_side_dialog
                    or show_autoplay_gameover_dialog
                ):
                    stop_autoplay("Auto-play stopped with F8.")
                    continue
                if show_autoplay_side_dialog:
                    if event.key == pygame.K_ESCAPE:
                        stop_autoplay("Auto-play cancelled before scanning.")
                    elif event.key == pygame.K_w:
                        show_autoplay_side_dialog = False
                        start_autoplay_scan("white")
                    elif event.key == pygame.K_b:
                        show_autoplay_side_dialog = False
                        start_autoplay_scan("black")
                    continue
                if show_autoplay_gameover_dialog:
                    if event.key == pygame.K_ESCAPE:
                        stop_autoplay()
                    elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        show_autoplay_gameover_dialog = False
                        restart_autoplay_scan()
                    continue
                if event.key == pygame.K_ESCAPE:
                    if show_newgame_confirm:
                        show_newgame_confirm=False
                        continue
                    if show_game_over:
                        show_game_over=False
                        continue
                    if show_auto_color_dialog:
                        show_auto_color_dialog=False
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
                            navigate_back()
                        else:
                            begin_settings_menu_close()
                    else:
                        selected_sq = None
                elif event.key in (pygame.K_LEFT, pygame.K_RIGHT) and menu_anim_progress == 0 and not show_newgame_confirm and not show_auto_color_dialog and promotion_pending is None and not dragging and not anim_mgr.board_reset_anims:
                    if event.key == pygame.K_LEFT:
                        # Previous move (kept in history_redo for →), played backwards
                        if board.move_stack:
                            add_move_animation_for_move(anim_mgr, board, board.move_stack[-1], reverse=True)
                            history_redo.append(board.pop())
                            sync_after_history_step()
                    else:
                        # Next move — only if it has actually been played
                        if history_redo:
                            add_move_animation_for_move(anim_mgr, board, history_redo[-1], reverse=False)
                            board.push(history_redo.pop())
                            sync_after_history_step()
                elif event.key == pygame.K_f and menu_anim_progress == 0 and not show_newgame_confirm:
                    toggle_flip_board()
                elif event.key == pygame.K_e and menu_anim_progress == 0 and not show_newgame_confirm:
                    toggle_eval_side()

                elif event.key == pygame.K_SPACE and menu_anim_progress == 0 and not show_auto_color_dialog:
                    if not (anim_mgr.move_anim or anim_mgr.rook_anim):
                        execute_engine_best_move()


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
            view_ply=len(board.move_stack),
            total_ply=len(board.move_stack) + len(history_redo),
        )
        # The import-time SetWindowPos can race SDL actually presenting
        # the native window; assert the saved topmost preference once
        # more after the first presented frame.
        if not aot_first_frame_reapplied:
            aot_first_frame_reapplied = True
            if config.get("always_on_top", 1):
                apply_always_on_top(True, persist=False)
        clock.tick(FPS)

    engine_thread.quit()
    pygame.quit()
    if restart_requested:
        sys.exit(RESTART_EXIT_CODE)
    sys.exit(0)


if __name__ == "__main__":
    main()