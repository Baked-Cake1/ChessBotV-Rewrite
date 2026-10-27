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
- **Opening title animation:** “ChessBotV Rewrite” slides in once at launch with cubic-in (`t³`) easing
- **Lichess-style eval bar** with gradient fill, tick marks (0.25/0.5/0.75), `W/B` labels, glowing marker, and a fully on-screen “Click to swap side” tooltip
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
  - **Pieces Styles** — visual, scrollable style cards with sample pieces on board-coloured squares, source/current-state labels, hover highlighting, and a full 12-piece preview using the active board colours
  - **Board Theme** — scrollable list with scrollbar + "Scroll for more"
  - **Engine Settings** — sliders for `Threads` (1-1024), `Skill Level` (0-20), `Hash` (16-32768 MB stepped), `Depth Limit` (5-40), and `MultiPV` (1-5); saving a changed MultiPV value immediately recalculates the current board
  - **Analysis** — internal **Auto-Move** always starts off and its on/off state is never saved. Its separate target-depth control accepts `0-40`; `0` means “use the current Depth Limit”
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
| **Mouse wheel** (Pieces Styles) | Scroll the visual style cards; hover a card to preview all 12 pieces, click to apply |
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
multipv=1
auto_move_depth=0
```
- Loaded at startup and saved by the settings UI
- `Hash` steps: 16,32,64,128,256,512,1024,2048,4096,8192,16384,32768
- `Depth` slider: 5-40, directly controls `ANALYSIS_DEPTH` for `go depth`
- `auto_move_depth=0` means Auto-Move plays when the search reaches the current Depth Limit; positive values use that depth (capped by the Depth Limit)
- Auto-Move's enabled state is deliberately not written to the file and is always off on a fresh launch, including when an older file contains `auto_move=1`
- Changes are queued as `setoption` + `isready`/`readyok` before the next search (never mid-search); a saved MultiPV change forces that next search to be a recalculation of the current position

---

## CNN Dataset Creation & Training

`board-vision.py` builds the square-recognition dataset used by `cnn_trainer.py`:

1. Run `python board-vision.py` (or drag a screenshot onto it). Each uploaded board is assigned a new `game_id` at upload time, and all 64 tiles from that upload share it.
2. Select the outer edge of the chessboard exactly as before; it is cropped and split into the 64 squares.
3. Review every square. Semi-auto OpenCV help is on by default: its suggested piece and then suggested **Light square**, **Dark square**, or **Highlighted square** are highlighted in cyan, but you make the final choice for every square (including blanks). Press **A** or **Enter** to use a suggestion, click another answer to correct it, or press **S** to toggle suggestions during a board. Use `python board-vision.py --manual-only` to disable semi-auto suggestions and learning completely. After recognition, the terminal asks **“Is this board flipped?”**; the classifications are rotated when needed and the generated FEN is printed and saved as `board_debug/fen_<game_id>.txt`.
4. The labelled tiles are saved with unique names such as `wp_light_000001.png`, `blank_dark_000002.png`, and `bq_highlighted_000003.png`, and their rows are appended to `board_debug/dataset.csv` (`image_path`, `label`, `label_code`, `square_type`, `split`, row/column, source-board details, and `game_id`).
5. At the end of the board, answer whether to create the CNN model now:
   - **Yes** runs `cnn_trainer.py` against `dataset.csv` and writes `chessbotv_cnn.pth`.
   - **No** returns to the terminal and waits for Enter before opening the next file picker, so you can prepare another screenshot first. Answering with another board appends to the CSV rather than overwriting earlier rows/images.
6. The first creation of `dataset.csv` leaves every row as `unsplit`. On every later CSV change, splits are recalculated by **whole game ID**, never by individual square image: every tile from one game stays in the same split. Game counts approximate **70% train / 15% validation / 15% test**—for example, 4 games → 2 train / 1 validation / 1 test, and 20 games → 14 / 3 / 3—and `train.csv`, `validation.csv`, and `test.csv` are refreshed beside the master CSV.
7. For every class, light-square and dark-square counts must differ by at most **2** before training. Highlighted-square images are excluded from that balance check; `cnn_trainer.py` trains them without colour jitter and also supports `--highlighted-mode exclude|only` for separate experiments.

The generated FEN's piece placement comes directly from the reviewed squares. Because one still image cannot prove every remaining FEN field, side-to-move follows the normal/flipped answer (with the exact starting position always White to move), castling is inferred from home king/rook squares, and en passant/clocks are `- 0 1`.

### Semi-auto assistant learning folder

The assistant does **not** learn inside `board_debug/` (that folder remains only for the CNN model dataset). After a board is completed, a bounded and visually diverse selection of your confirmed squares is saved separately to:

```
board_vision_assistant/
├── examples.csv
└── examples/
    ├── wp/light/assistant_wp_light_000001.png
    ├── blank/dark/assistant_blank_dark_000002.png
    └── bq/highlighted/assistant_bq_highlighted_000003.png
```

It learns each confirmed **piece + Light/Dark/Highlighted** combination. Squares confirmed earlier on the current board are used immediately for later suggestions on that same board; persistent examples are saved only after the board is completed, so cancelling a review does not teach partial answers. At most 16 saved references per piece/square-type combination are kept. These assistant examples are ignored by `cnn_trainer.py` and are not included in `board_debug/dataset.csv` or its train/validation/test splits.

You can also train directly:

```bash
python cnn_trainer.py --dataset-dir board_debug --csv board_debug/dataset.csv
```

The trainer still falls back to the older filename-prefix scan when no `dataset.csv` is present.

---

## Project Structure

```
ChessBotV-Rewrite/
├── new.py              # Main GUI + engine thread (60 FPS, 640x640 board)
├── board-vision.py      # Screenshot → 64 reviewed CNN dataset squares (+ semi-auto help)
├── cnn_trainer.py       # Trains chessbotv_cnn.pth from board_debug/dataset.csv
├── board_debug/         # Generated CNN dataset images and CSV splits (not assistant data)
├── board_vision_assistant/ # Generated OpenCV semi-auto learning examples, kept separate
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

