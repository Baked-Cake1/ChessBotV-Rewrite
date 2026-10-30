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
- **Lichess-style eval bar** with a rounded framed glass track, anchored slate/ivory gradients, subtle winning-side halo, quarter ticks and centre diamond, circular `W`/`B` chips (plus a central `M` for forced mate), a slim amber boundary pointer, and a fully on-screen “Click to swap side” tooltip
- **Engine suggestion arrows render over pieces:** blue, width 7, fades/morphs smoothly; **L-shaped for knight moves** (long-leg first, correct overlay layering)
- **Check / Checkmate FX:**
  - Under: soft radial red glow
  - Over: corner brackets, expanding ripple rings, pulsing red border on mate
- **Animations:** cubic ease move + rook slide for castling, capture pop-and-fade (scale 1.0→1.8), board-reset multi-piece slide
- **Always-on-top** window via `ctypes` (Windows)

### Board vision, Advisor & Auto-play
- **Advisor Mode:** the compact toggle at the top of **Analysis Settings** asks for the window to broadcast on, asks which side you are playing, then scans/imports that board without ever clicking or dragging in the target window. Its `cliv-gui` `ProcessOverlay` runs in an isolated child process whose main thread owns Tk, follows the exact selected window, outlines Stockfish's current best-move origin in **red** and destination in **blue**, and shows a live `Eval · Depth · Best` readout directly under the board. Advisor polls every 0.25s but suppresses screenshots while a mouse button that was pressed in the target remains held, waits briefly after release, and only then checks for a unique 0/1/2-ply board sync. Checkmate (and other terminal results) turns Advisor fully off and forgets the window, so turning it on again asks for a window again. Advisor is session-only and cannot run at the same time as Auto-play
- **Board Detection / Auto-play locator:** board location first uses OpenCV `cv2.findChessboardCornersSB` to find the 7×7 internal corners of an 8×8 board, walking plain, blurred, downscaled, thresholded, colour-channel, and dominant-colour-pair views of the screenshot so textured, rounded, low-contrast, and hue-only board styles get a chance to match; OpenCV's classic `findChessboardCorners` runs next. If neither corner detector finds a usable grid, ChessBotV falls back to the Chess.com (`#ebecd0` / `#739552`) and Basic/Lichess (`#f0d9b5` / `#b58863`) square-colour search. The detection summary names the locator (and palette when known), while the selected CNN/package recogniser still classifies the located board
- **Auto-play finish + F8:** when a verified Auto-play move ends the game (including checkmate), Auto-play turns itself fully off and waits. **F8** is the universal Auto-play toggle — it stops an active setup/scan/game, rescans the retained window/side after a finished game, or starts the normal setup flow when no reusable session exists
- **Non-blocking external drag:** focusing the target window and dragging/promotion-clicking runs on a cancellable background thread, so ChessBotV keeps rendering and responding (including to F8) while Always on Top is enabled; screenshot verification and the one-drag retry still happen on the GUI thread
- **Auto-play depth:** the stepper inside the Auto-play card saves `autoplay_depth` (`1-40`, default `20`). Auto-play drags when the shared search reaches that depth, capped by the current Depth Limit (shown as `chosen>effective` when capped)

### Themes & UI
- **Board themes:** any folder under `pieces/<theme>/` (e.g. `classic`). Auto-discovered, `classic` pinned first
- **Chess.com styles:** any subfolder in `pieces/chesscom/<style>/` (e.g. `8_bit`, `chesskid` → "8-Bit", "ChessKid")
- **Info panel:** opening name (from `openings.json`), game status (`Check!` / `Draw — …`), stats, opening/rec-move row
- **Top bar:** a polished brass **Flip** button — miniature board between chunky ivory/charcoal exchange arrows whose colours ease-swap with board orientation, plus hover/pressed states and a perspective tooltip — alongside a polished emerald `New` button with circled-plus badge and hover sparkle, and a polished metallic settings button with a supersampled cog featuring beveled teeth, rim, bolts and hub that slowly turns while hovered → choreographed **Settings** shell (opening rings out from the cog as the backdrop leads and the pages fade, scale and rise into place with a top-to-bottom row cascade; closing uses a dedicated accelerating sink/scale/fade exit, collects the lower rows first and rings back toward the X/gear)
- **Settings pages:**
  - **Header Close/X** — polished ruby pill with a ringed central seal and tapered X arms; hover eases in its halo and “Close Settings” tooltip, while the X folds inward as the overlay closes
  - **Board** — its polished bronze main-menu button shows a framed 4×4 board and right-hand current piece/theme previews in the active `LIGHT_SQ`/`DARK_SQ` colours, names the current Pieces and Board selections in its subtitle, and opens the Pieces Styles / Board Themes hub
  - **Pieces Styles** — visual, scrollable style cards with sample pieces on board-coloured squares, source/current-state labels, hover highlighting, and a full 12-piece preview using the active board colours
  - **Board Theme** — scrollable list with scrollbar + "Scroll for more"
  - **Engine Settings** — its polished indigo main-menu button carries a detailed gear medallion, five miniature tracks mirroring the staged slider values, an amber unsaved-changes badge and a hover values tooltip; its polished Threads slider adds a CPU chip/core-bank track with core pips and parallel lanes, saved/staged markers, a four-core thumb, an `x of y` value chip capped at this PC's logical-thread limit and an in-row hover/drag bubble; its polished Skill Level slider adds a crown medallion, Gentle/Club/Expert/Master/Full-strength tiers, a violet-to-gold rung ladder with level pips, saved/staged markers, a star thumb, an `x of 20` value chip, five ascending mastery bars and an in-row hover/drag bubble; the page itself has sliders for `Threads` (1-1024), `Skill Level` (0-20), `Hash` (16-32768 MB stepped), `Depth Limit` (5-40), and `MultiPV` (1-5), and saving a changed MultiPV value immediately recalculates the current board
  - **Analysis** — its polished teal-to-violet main-menu button carries a scanning eval-scope medallion, a live priority subtitle and three status rails for Auto-Move, Board Detection and external Auto-play, with a hover tooltip spelling out each state; on the page, internal **Auto-Move** always starts off and its on/off state is never saved. Its separate target-depth control accepts `0-40`; `0` means “use the current Depth Limit”. The same page also hosts a compact session-only **Advisor Mode** toggle, Board Detection, Auto-play and a saved **Use Package** toggle: off uses the local `chessbotv_cnn.pth`, while on sends ChessBotV's already-located full-board crop to the optional `chess-fen-detector` package for the next detection/Auto-play/Advisor scan and that session's changed-square checks
  - **About** — version `v1.5-beta2` + polished sapphire **GitHub** button with a cat medallion, source-branch traces and an external-link arrow (opens https://github.com/Baked-Cake1/ChessBotV-Rewrite) + polished ruby **Restart** button with a rotating circular-arrow medallion, reboot-status dots and a brief “Restarting…” state (fires `start.bat` via exit code 42 loop); pinned at the bottom is a polished **Always on Top** toggle whose stacked-window icon rises when on, with a status LED, gradient track and tactile sliding knob

---

## Requirements

- **Python 3.11+** (3.14 recommended, as noted in `start.bat`)
- **Packages:** `pygame`, `python-chess`

  ```bash
  pip install pygame python-chess
  ```

- **Board Detection / Auto-play / Advisor screenshots:** OpenCV and NumPy locate the external board (OpenCV corners first, then the colour fallback) before the selected recogniser classifies it:

  ```bash
  pip install opencv-python numpy
  ```

- **Optional package recogniser:** only needed when **Analysis → Use Package** is on. ChessBotV imports it lazily for a Board Detection/Auto-play vision operation, so its TensorFlow/scikit-image dependencies are not needed for normal play or for the built-in CNN path:

  ```bash
  pip install chess-fen-detector
  ```

  ChessBotV still finds and crops the board itself because `chess-fen-detector` recognises an already-cropped board and returns its piece placement. If the package or its dependencies cannot load, the vision operation reports the install problem instead of silently falling back to `chessbotv_cnn.pth`.

- **Optional Advisor overlay:** Advisor Mode needs `cliv-gui` on Windows. ChessBotV checks that it is discoverable without importing Tk into the pygame process, then imports and runs it only inside a short-lived overlay child process (the 1.0.4 distribution installs the `clivgui` import package). It reports this install command instead of opening the window picker if the package is missing:

  ```bash
  pip install cliv-gui
  ```

- **CNN dataset/training tools:** `board-vision.py` needs `numpy`, `Pillow`, and `pygame` (no OpenCV); `cnn_trainer.py` additionally needs `torch`, `torchvision`, `scikit-learn`, `tqdm`, and Tkinter for its live dashboard (Tkinter is included with standard Windows/macOS Python; some Linux installs package it separately as `python3-tk`). Use `cnn_trainer.py --no-ui` when Tk/a display is unavailable.

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
| **Flip** (top bar) or **F** | Flip the board perspective; the brass button's ivory/charcoal arrows ease-swap colours and its tooltip names the side you will view from |
| **New** (top bar) | Smooth animated reset to starting position and clear eval; hovering shows its polished emerald glow/sparkle state and “Start a new game” tooltip |
| **Gear** (top bar) | Open Settings overlay; hovering brightens the button, turns the detailed cog slowly, and shows a “Settings” tooltip; clicking rings out from the cog while the shell fades/scales/rises and its main rows cascade into place |
| **X** (Settings header) | Close Settings with its dedicated exit animation; hovering shows the polished ruby seal/button state and a right-edge-clamped “Close Settings” tooltip, then the X folds while the shell sinks/scales/fades and rings back toward the X/gear |
| **Engine Settings** (Settings main menu) | Open engine sliders; hovering turns the gear, animates the five staged-value tracks and shows the current Threads/Skill/Hash/Depth/MultiPV values (amber when unsaved) |
| **Board** (Settings main menu) | Open Pieces Styles / Board Themes; its miniature board, piece pair, theme strip, halo and accent follow the active board colours, with a scanning square highlight on hover |
| **Analysis Settings** (Settings main menu) | Open Advisor Mode, Auto-Move, Board Detection and Auto-play; its eval scope sweeps on hover or while a feature is live, while the subtitle, three colour-coded status rails and below-row tooltip summarise Auto-Move, Detection and Auto-play (amber for setup, red when attention is needed) |
| **Advisor Mode** (Analysis Settings) | Choose a window to broadcast on and your side, scan its board, then receive a read-only `cliv-gui` overlay: red origin square, blue destination square, and live eval/depth/best move under the board. Polling pauses while the mouse is held in the target; checkmate stops Advisor and the next activation asks for a window again |
| **Use Package** (Analysis Settings) | Toggle the board-vision recogniser for the next Board Detection/Auto-play/Advisor scan: off uses `chessbotv_cnn.pth`; on uses the installed `chess-fen-detector` package on ChessBotV's located board crop. A running Auto-play or Advisor session keeps the recogniser from its initial scan |
| **Auto-play Depth − / +** (Analysis Settings) | Set and save the engine depth Auto-play waits for before dragging (`1-40`, default `20`); the effective depth is capped by Depth Limit |
| **F8** | Universal Auto-play toggle: stop an active session, rescan after a finished game, or start Auto-play setup |
| **Mouse drag on slider** (Engine Settings) | Adjust the staged engine values; Threads lights its CPU core ticks/pips and lanes, while Skill Level lights its crown, 21-rung tier ladder and five mastery bars from violet toward gold; both distinguish the smoothed thumb from the amber staged target and ivory saved marker, and show an in-row value bubble while hovering/dragging |
| **Github / Restart** (About) | Open repo in browser / restart via start.bat; hovering GitHub animates its cat medallion and external-link arrow, while hovering Restart turns its circular-arrow medallion, lights the status dots, and clicking it briefly shows “Restarting…” |
| **Always on Top** (Settings main menu) | Toggle whether the ChessBotV window stays above other windows; the stacked-window icon, row and switch track cross-fade between slate/off and emerald/on |
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
autoplay_depth=20
use_package=0
```
- Loaded at startup and saved by the settings UI
- `Hash` steps: 16,32,64,128,256,512,1024,2048,4096,8192,16384,32768
- `Depth` slider: 5-40, directly controls `ANALYSIS_DEPTH` for `go depth`
- `auto_move_depth=0` means Auto-Move plays when the search reaches the current Depth Limit; positive values use that depth (capped by the Depth Limit)
- `autoplay_depth=20` is Auto-play's separate saved target depth (`1-40`), also capped by the current Depth Limit; Auto-play's on/off state itself remains session-only
- `use_package=0` selects the local CNN; `use_package=1` selects `chess-fen-detector` for the next Board Detection/Auto-play/Advisor scan. Existing scans/sessions keep the backend they started with; Advisor Mode's own on/off state is never saved
- Auto-Move's enabled state is deliberately not written to the file and is always off on a fresh launch, including when an older file contains `auto_move=1`
- Changes are queued as `setoption` + `isready`/`readyok` before the next search (never mid-search); a saved MultiPV change forces that next search to be a recalculation of the current position

---

## CNN Dataset Creation & Training

`board-vision.py` builds the square-recognition dataset used by `cnn_trainer.py`. The dataset creator is now **fully manual and does not use OpenCV**: it uses Pillow for image files/pixels and Pygame for its zoom, grid-check, and marking windows. For dataset creation install:

```bash
pip install numpy Pillow pygame
```

Training additionally uses the existing trainer dependencies (`torch`, `torchvision`, `scikit-learn`, and `tqdm`). The trainer opens a Tk dashboard by default, with editable dataset/output paths, epochs, batch size, learning rate, seed, and duplicate-removal mode; a live training/validation accuracy graph; current epoch/batch; training and validation loss/accuracy; best validation accuracy; overall and per-step progress bars; elapsed/ETA; run details; a scrollable log and final per-class report; and Start/Stop controls.

1. Run `python board-vision.py` (or drag a screenshot onto it). On startup it creates `datasets/training/` and `datasets/validation/`, each containing the same 13 CNN class folders: `white_king`, `white_queen`, `white_rook`, `white_bishop`, `white_knight`, `white_pawn`, `black_king`, `black_queen`, `black_rook`, `black_bishop`, `black_knight`, `black_pawn`, and `empty`. Each uploaded board receives a new `game_id` for provenance; tiles from one board may be exported to different splits when the weighted class split is applied.
2. Select the outer edge of the chessboard exactly as before; mouse-wheel zoom and right-drag pan still allow pixel-accurate selection. The board is cropped and split into the same 64 squares, followed by the grid/tile inspection view.
3. **Manually mark each unique square with one click:** choose only the piece/blank on the square. There is no Light/Dark/Highlighted square step, and there are no OpenCV or learned suggestions. Before a square is shown, board-vision compares its normalised 64×64 RGB pixels against every image already in both training and validation and every unique tile accepted earlier on the current board.
4. If the pixels are an **exact duplicate** or a **near duplicate**, the square is **automatically skipped**: the GUI and terminal say so and name the existing match, whose known label fills that square's answer for the FEN/metadata, and explain the problem—duplicate images make training slower and can make the model faulty/over-confident (and can leak between training and validation). The duplicate tile is **not saved**. The check searches across all classes and both splits, so identical pixels under a conflicting label—or a copy placed in the opposite split—are also caught. **Back** can revisit an auto-skipped square to confirm or correct its inferred label.
5. Unique tiles are saved only after the complete board is reviewed. At export, board-vision assigns them separately within each class using a deterministic cumulative **80% training / 20% validation** split, so training receives more images and single-image classes stay in training. Examples include `datasets/training/white_pawn/wp__game_...__r6c4__000001.png` and `datasets/validation/empty/blank__game_...__r4c4__000002.png`. `datasets/metadata.csv` keeps each image's assigned split, provenance, and skipped-duplicate details, while board crops, grid-debug images, and FEN files go under `datasets/_boards/<game_id>/`. After review, the terminal still asks **“Is this board flipped?”** before generating/saving the FEN.
6. At the end of a board, answer whether to create the CNN model now:
   - **Yes** runs `cnn_trainer.py --dataset-dir datasets --ui --start`, opening the live training dashboard already running and writing the best checkpoint to `chessbotv_cnn.pth`. Close the dashboard when finished to return to board-vision; if Tk/a display is unavailable, the trainer safely falls back to its terminal output.
   - **No** returns to the terminal and waits for Enter before opening the next file picker. Adding another board appends only its unique tiles to the physical training/validation folders.
7. **The trainer reads the physical `datasets/training/<class>/` and `datasets/validation/<class>/` folders.** It first removes any exact/near duplicates still present across both folders (including manually copied files), trains on the training records, and uses validation both to select the best checkpoint and for the final per-class evaluation report. There is **no separate test set**. Legacy direct class folders, flat prefix files, and unsplit CSV records remain readable through a temporary label-stratified 80/20 fallback; legacy `train/<class>` is treated as training and legacy `test/<class>` as validation.

The generated FEN's piece placement comes from the manually marked unique squares plus the known labels of any auto-skipped duplicates. Because one still image cannot prove every remaining FEN field, side-to-move follows the normal/flipped answer (with the exact starting position always White to move), castling is inferred from home king/rook squares, and en passant/clocks are `- 0 1`.

Train directly from the physical training/validation folders with:

```bash
python cnn_trainer.py --dataset-dir datasets
```

That opens the dashboard ready to start. Add `--start` to begin as soon as it opens, or use the original terminal/`tqdm` trainer with:

```bash
python cnn_trainer.py --dataset-dir datasets --no-ui
```

The dashboard and terminal modes share the same dataset discovery, duplicate removal, training, best-checkpoint, and final-validation code. `--epochs`, `--batch-size`, `--learning-rate`, and `--seed` set the same values shown in the dashboard. Duplicate removal defaults to `--dedupe exact-near`; `--dedupe exact` and `--dedupe off` are available for diagnostics. A legacy flat/CSV dataset can still be trained only when explicitly requested, for example:

```bash
python cnn_trainer.py --dataset-dir board_debug --csv board_debug/dataset.csv
```

To migrate an old local `board_debug` dataset once, run:

```bash
python board-vision.py --import-legacy
```

It copies only unique legacy tiles into the physical `datasets/training/<class>/` and `datasets/validation/<class>/` folders using the same deterministic per-class 80/20 export split, records skipped exact/near duplicates in `metadata.csv`, and leaves the old files untouched. If the physical folders are still empty but an old local `board_debug/dataset.csv` exists, a default trainer run also uses that legacy CSV as a temporary fallback and says so; as soon as folder images exist, they take precedence. Generated images and metadata under `datasets/` are ignored by Git—the repository tracks only the 26 empty training/validation class-folder placeholders.

---

## Project Structure

```
ChessBotV-Rewrite/
├── new.py              # Main GUI + engine thread (60 FPS, 640x640 board)
├── board-vision.py      # Screenshot → 64 reviewed squares (duplicates auto-skip)
├── cnn_trainer.py       # Live UI/CLI trainer for chessbotv_cnn.pth
├── datasets/            # Physical split folders; generated data is local & ignored
│   ├── training/        # white_king/ … black_pawn/ empty/
│   ├── validation/      # white_king/ … black_pawn/ empty/
│   ├── metadata.csv     # Generated split/provenance/duplicate log (ignored)
│   └── _boards/         # Generated board crops/grid/FEN per game (ignored)
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
- FEN/PGN export and takeback
- Per-theme board colors

---

## Contributing

Bug reports / visual issues / ideas → open an **Issue**. Pull requests welcome — keep style close to `new.py` (PEP 8-ish, CRLF ok, 80-col board rendering).

**Version:** `ChessBotV Rewrite Version 1.5-beta2` (about page)

