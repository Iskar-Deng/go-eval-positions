# Go evaluation positions

Generate X/Y Go positions with A/B/C candidate moves and full move histories.

## Install

Requires Ubuntu/Debian Linux x86_64, Python 3.10+, and an NVIDIA GPU with a driver supporting CUDA 12.1.

```bash
git clone -b main --single-branch https://github.com/Iskar-Deng/go-eval-positions.git
cd go-eval-positions
bash setup.sh
```

## Run

```bash
.venv/bin/python scripts/generate.py
```

Automatically fills missing C in saved samples, then continues extraction and balancing. Press Ctrl+C to stop; run the same command to resume. Samples without a suitable C are kept for another attempt on the next run.

For the additional experiment (atari that is not sente):

```bash
.venv/bin/python scripts/generate_addition.py
```

Searches all games and supports the same stop/resume workflow. The initial position must be balanced (30–70% winrate). Both playing atari instead of the best move and saving the threatened stones instead of the best remote reply must lose at least 4 points. Evaluated moves stay within 150 plies.

## Files

- `datasets/games/`: input SGF games.
- `datasets/sample/`: example pairs.
- `scripts/generate.py`: generation script. Uses a 19×19 board, Chinese rules, and 7.5 komi.
- `configs/katago-analysis.cfg`: KataGo GPU and search settings.
- `outputs/<id>/`: `X.sgf`, `Y.sgf`, and `sample.json`. Progress is saved in `outputs/state.json`.
- `scripts/generate_addition.py`: additional experiment, without Y. Saves `X.sgf` and `sample.json` in `outputs-addition/<id>/`; progress in `outputs-addition/state.json`. JSON uses the same `moves`, `results`, and `X_A_targets` fields as the main experiment, with only X and A/B. Winrates and score leads use the player to move in X.
