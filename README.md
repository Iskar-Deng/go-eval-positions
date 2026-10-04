# Go evaluation positions

Automatically extract paired Go positions X/Y, then append common moves to balance them. Full move histories are retained. X and Y differ at one historical move and by relocating one stone on the final board.

## Install

Use Ubuntu 22.04 or later on Linux x86_64, Python 3.10+, and an NVIDIA GPU with a driver supporting CUDA 12.1. Run these commands from the repository root. The NVIDIA driver must already be installed.

```bash
git clone --branch codex/simple-server --single-branch https://github.com/Iskar-Deng/go-eval-positions.git
cd go-eval-positions
nvidia-smi
sudo apt-get update
sudo apt-get install -y python3-venv curl unzip libzip4
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

mkdir -p .local/katago/bin .local/katago/models
curl -fL --retry 3 https://github.com/lightvector/KataGo/releases/download/v1.18.2/katago-v1.18.2-cuda12.1-cudnn8.9.7-linux-x64.zip -o .local/katago/katago.zip
echo '16c69f42291fe8c6d196d722d92299a5b04de852611d6c022a0dd9e0e83b5688  .local/katago/katago.zip' | sha256sum -c -
unzip -jo .local/katago/katago.zip '*katago' -d .local/katago/bin
chmod +x .local/katago/bin/katago scripts/katago.sh
curl -fL --retry 3 https://media.katagotraining.org/uploaded/networks/models/kata1/kata1-b18c384nbt-s9996604416-d4316597426.bin.gz -o .local/katago/models/kata1-b18c384nbt-s9996604416-d4316597426.bin.gz
echo '9d7a6afed8ff5b74894727e156f04f0cd36060a24824892008fbb6e0cba51f1d  .local/katago/models/kata1-b18c384nbt-s9996604416-d4316597426.bin.gz' | sha256sum -c -
```

## Run

Extract for 60 minutes, then balance for up to 30 minutes. Preparation and final verification add some time. Use a new output directory for each run.

```bash
.venv/bin/python scripts/generate.py --out outputs/run01 --extract-minutes 60 --balance-minutes 30
```

To keep running after disconnecting from SSH:

```bash
nohup .venv/bin/python -u scripts/generate.py --out outputs/run02 --extract-minutes 60 --balance-minutes 30 > run02.log 2>&1 &
tail -f run02.log
```

For another collection of SGF games, add `--games-dir /path/to/sgfs`. `--start-game 1000 --games 1000` selects the next 1,000 files in sorted order. Games represented in the bundled samples or previous `outputs/*/datasets/manifest.json` files are skipped.

## Files

- `datasets/accepted/<id>/`: 20 example pairs. `X.sgf` and `Y.sgf` contain complete move histories up to the test position. `sample.json` contains candidate moves A/B, winrates, score leads, and the stones threatened by X/A.
- `datasets/sources/`: original SGF games referenced by the examples. Paths in `source_sgf` are relative to the directory containing `datasets/`.
- `datasets/manifest.json`: sample IDs, history edits, appended moves, and file checksums.
- `datasets/games/`: 1,000 input SGF games for further extraction.
- `scripts/generate.py`: runs extraction, balancing, and export. Other Python files provide the search, history replay, and engine helpers; `katago.sh` launches KataGo.
- `configs/katago-analysis.cfg`: GPU and search settings.
- `outputs/run01/datasets/accepted/`: newly accepted pairs in the same format as the examples. Intermediate records and logs remain under `outputs/run01/`; `summary.json` reports counts.

Winrates are fractions and score leads are in points, both from the original player-to-move perspective. Accepted pairs use 1,000-visit checks, have X/A, X/B, and Y/B winrates in 30–70%, and preserve the required score differences and atari pattern. The candidate move is at move 150 or earlier. The 20 examples passed full-history verification locally; Linux GPU execution still needs validation on the target server.
