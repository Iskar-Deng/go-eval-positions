# Go evaluation positions

Extract and balance X/Y Go positions while preserving full move histories.

## Install

Requires Ubuntu/Debian Linux x86_64, Python 3.10+, and an NVIDIA GPU with a driver supporting CUDA 12.1.

```bash
git clone -b main --single-branch https://github.com/Iskar-Deng/go-eval-positions.git
cd go-eval-positions
bash setup.sh
```

## Run

Each extracted pair is immediately balanced if needed and saved if it passes. Runs until the input games are exhausted or you press Ctrl+C. Run the same command again to resume. Difficult pairs get about two minutes of balancing before the search moves on.

```bash
.venv/bin/python scripts/generate.py
```

Progress shows completed/remaining games, saved samples, elapsed time, and a rough ETA after warm-up.

All positions use Chinese rules, 7.5 komi, and a 19×19 board, regardless of the source SGF settings. These values are passed to KataGo by `query_position()` in `scripts/generate.py` and also written to exported SGFs. GPU, thread, and cache settings are in `configs/katago-analysis.cfg`.

## Files

- `datasets/sample/`: 20 example pairs. Each contains full-history `X.sgf` and `Y.sgf`, plus `sample.json` with candidate moves, winrates, and score leads. These examples do not affect generation.
- `datasets/games/`: 96,121 input SGF files for extraction; duplicate games are skipped.
- `scripts/generate.py`: the complete extraction and balancing script.
- `configs/katago-analysis.cfg`: KataGo settings.
- `outputs/<id>/`: generated samples (`X.sgf`, `Y.sgf`, and `sample.json`). `outputs/state.json` tracks progress for resuming.
