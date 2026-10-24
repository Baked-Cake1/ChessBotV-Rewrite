# ChessBotV — Rewrite (v1.5-beta2)

A polished **UCI chess GUI** built with `pygame` + `python-chess`. Play against **Stockfish** (or any UCI engine), get live evaluation, opening names, and engine suggestions — with smooth animations, board themes, and modern UI.

> Originally `ChessBotV`, completely rewritten for stability, performance, and UX. Work in progress — issues and visual bug reports welcome.

## Versions

- **Current stable version:** **v1.0** (Initial release)
- **Current beta / developer release:** **v1.5-beta2** (active development on this branch)

---

## Features

### Play & Input
- **Two input modes:** Click piece → green circles, *or* **drag & drop** to any legal square
- Handles **castling, en-passant, promotion (choice dialog Q/R/B/N, no pawn/king), captures** and all legal chess rules via `python-chess`
- **Space** to play the engine's top move instantly
- **New Game** smoothly animates pieces back to starting squares

### UCI Engine (Stockfish by default)
- Background-threaded UCI (`uci`, `isready`, `go depth`, `stop`) — UI stays 60 FPS
- **Live readout:** `Eval` (±0.00, `M#` for mate), `Depth` (slider 5-40, default 25), `NPS` (K/M/B/T formatted, resets to 0 at depth limit), `Rec. Move`
- Single search per position with idle after full depth — no sawtooth depth reset
- **Auto game-over** on checkmate, stalemate, insufficient material, 75-move, fivefold repetition, 50-move and threefold repetition

### Visuals
- **Lichess-style eval bar** with gradient fill, tick marks (0.25/0.5/0.75), `W/B` labels and glowing marker
- **Engine suggestion arrows render over pieces:** blue, width 7, fades/morphs smoothly; **L-shaped for knight moves** (long-leg first, correct overlay layering)
- **Check / Checkmate FX:**
  - Under: soft radial red glow
  - Over: corner brackets, expanding ripple rings, pulsing red border on mate
- **Animations:** cubic ease move + rook slide for castling, capture pop-and-fade (scale 1.0→1.8), board-reset multi-piece slide
- **Always-on-top** window via `ctypes` (Windows)

### Themes & UI
- **Board themes:** any folder under `pieces/<theme>/` (e.g. `classic`). Auto-discovered, `classic` pinned first
- **Chess.com styles:** any subfolder in `pieces/chesscom/<style>/` (e.g. `8_bit`, `chesskid` → "8-Bit", "ChessKid")
- **Info panel:** opening name (from `openings.json`), game status (`Check!` / `Draw — …`), stats, opening/rec-move row
- **Top bar:** `New` + detailed gear icon → animated **Settings** overlay with eased open/close (backdrop fade, slide, spring)
- **Settings pages:**
  - **Board Theme** — scrollable list with scrollbar + "Scroll for more"
  - **Engine Settings** — sliders for `Threads` (1-1024), `Skill Level` (0-20), `Hash` (16-32768 MB stepped), `Depth Limit` (5-40), live `setoption` + `save_config()`, Reset to Defaults
  - **About** — version `v1.5-beta2` + **Github** button (opens https://github.com/Baked-Cake1/ChessBotV-Rewrite) + **Restart** button (fires `start.bat` via exit code 42 loop)

---

## Requirements

- **Python 3.11+** (3.14 recommended, as noted in `start.bat`)
- **Packages:** `pygame`, `python-chess`

  ```bash
  pip install pygame python-chess
  ```

- **Engine:** Stockfish (or any UCI engine) — place anywhere and set `ENGINE_PATH` in `new.py`

  Default:
  ```python
  ENGINE_PATH = r"C:\Users\Blake\Documents\_Engines\Chess\Stockfish\..."
  ```

  Point it to your `stockfish` binary, e.g. `stockfish-windows-x86-64-universal.exe` or `/usr/bin/stockfish`.

- **Assets:**
  - `pieces/<theme>/*.png` — `bb,bn,br,bk,bq,bp,wb,wn,wr,wk,wq,wp` (80x80, scaled to `SQUARE_SIZE`)
  - `openings.json` — `{ "fen_placement": "Opening Name" }`
  - `configuration.cfg` — optional, see below

---

## Quick Start

### Windows (recommended)
Double-click `start.bat`:
```bat
start.bat
```
- Checks `python --version`, launches `python new.py %*` in a loop — **restarts automatically on exit code 42** (Restart button), otherwise **auto-closes on success** (exit code 0). On crash (non-zero other) it stays open to show the error and waits for a key.

### Manual
```bash
python new.py
# pass through args if any:
python new.py --help
```

---

## Controls

| Input | Action |
|---|---|
| **Left-click** piece | Select, show legal moves (green = quiet, red+ring = capture) |
| **Left-click** green/red | Move selected piece |
| **Drag & drop** | Alternative: drag any of your pieces to a legal square |
| **Pawn to last rank** | Promotion dialog — click Q/R/B/N (no pawn/king) |
| **Space** (menu closed, not animating) | Play engine's `pv[0]` if legal |
| **New** (top bar) | Smooth animated reset to starting position, clear eval |
| **Gear** (top bar) | Open Settings overlay (detailed cog with spokes) |
| **Mouse drag on slider** (Engine Settings) | Adjust Threads/Skill/Hash/Depth live |
| **Github / Restart** (About) | Open repo in browser / restart via start.bat |
| **Mouse wheel** (Board Theme) | Scroll theme list |
| **Esc** | Cancel promotion → Back from sub-page → close Settings; also cancels selection |

- Board ignores clicks during move/rook/board-reset animations or when `is_game_over(claim_draw=True)`.
- Selected square highlighted `HIGHLIGHT_COLOR` (205,210,106,150).

---

## Configuration

`ENGINE_DEFAULTS = { hash:2048, threads:4, skill:20, depth:25 }` in `new.py`.

`configuration.cfg` (or `config.cfg`) overrides it:
```ini
hash=2048
threads=12
skill=20
depth=25
```
- Loaded at startup, saved via **Engine Settings** sliders → persists instantly
- `Hash` steps: 16,32,64,128,256,512,1024,2048,4096,8192,16384,32768
- `Depth` slider: 5-40, directly controls `ANALYSIS_DEPTH` for `go depth`
- Changes are queued as `setoption` + `isready`/`readyok` before next search (never mid-search); NPS resets to 0 at depth limit

---

## Project Structure

```
ChessBotV-Rewrite/
├── new.py              # Main GUI + engine thread (60 FPS, 640x640 board)
├── start.bat            # Windows launcher (auto-close + restart loop on exit 42)
├── configuration.cfg    # Engine settings (hash/threads/skill/depth)
├── openings.json        # FEN placement → opening name (~370k entries)
├── pieces/              # Board themes
│   ├── classic/         # bb.png … wp.png (required fallback)
│   └── chesscom/8_bit/  # Chess.com styles auto-discovered
└── audio/               # (reserved, currently empty)
```

- **Layout constants:** `SQUARE_SIZE=80`, `BOARD_WIDTH=640`, `EVAL_BAR_WIDTH=32`, `PADDING=24`, `TOP_BAR_HEIGHT=44`, `INFO_HEIGHT=152`
- **Colors:** `LIGHT_SQ` (240,217,181), `DARK_SQ` (181,136,99), `ENGINE_ARROW_COLOR` (0,100,255,180), `BG_COLOR` (40,40,40)

---

## Engine Details

- **Thread:** `subprocess.Popen([ENGINE_PATH], ...)` + `_handshake()` → `uciok` → `setoption Threads/Hash/Skill Level`
- **Loop:** `update_position(board)` → `stop` → bump `search_token` → `_analyze_loop` → `position fen …` + `go depth <limit>` → parse `info depth/nps/score/mate/pv`
- **Score:** UCI-relative → white-relative conversion for correct eval bar (`W/B`)
- **PV arrow:** only if `best_move in board.legal_moves`; hidden on `game_over`; rendered over pieces with correct knight-L layering

---

## Troubleshooting

- `Engine not found or error` → check `ENGINE_PATH` exists and is executable
- `engine closed its output stream` → engine crashed; try lower `Hash`/`Threads`
- No pieces → ensure `pieces/<theme>` has 12 PNGs or fallback colored circles will show
- No openings → `openings.json not found.` → place file in repo root
- Themes missing → falls back to `classic`; if even that missing, uses first found theme
- Config not saving → check write permission for `configuration.cfg`

---

## Roadmap

- Sound settings & audio playback (`audio/` placeholder)
- Game Controls / Analysis Settings pages (currently inactive in menu, now have sliders for engine)
- FEN/PGN export, takeback, flip board
- Per-theme board colors

---

## Contributing

Bug reports / visual issues / ideas → open an **Issue**. Pull requests welcome — keep style close to `new.py` (PEP 8-ish, CRLF ok, 80-col board rendering).

**Version:** `ChessBotV Rewrite Version 1.5-beta2` (about page)

