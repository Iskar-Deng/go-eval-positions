# Go evaluation positions

Extract and balance X/Y Go positions while preserving full move histories.

## Install

Requires Linux x86_64, Python 3.10+, and an NVIDIA GPU with a driver supporting CUDA 12.1.

```bash
git clone -b server --single-branch https://github.com/Iskar-Deng/go-eval-positions.git
cd go-eval-positions
sudo apt-get update
sudo apt-get install -y python3-venv curl unzip libzip4
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

mkdir -p .local/katago/bin .local/katago/models
curl -fL https://github.com/lightvector/KataGo/releases/download/v1.18.2/katago-v1.18.2-cuda12.1-cudnn8.9.7-linux-x64.zip -o .local/katago/katago.zip
unzip -jo .local/katago/katago.zip '*katago' -d .local/katago/bin
chmod +x .local/katago/bin/katago
curl -fL https://media.katagotraining.org/uploaded/networks/models/kata1/kata1-b18c384nbt-s9996604416-d4316597426.bin.gz -o .local/katago/models/kata1-b18c384nbt-s9996604416-d4316597426.bin.gz
```

## Run

Each extracted pair is immediately balanced if needed and saved if it passes. Runs until the input games are exhausted or you press Ctrl+C. Run the same command again to resume. Difficult pairs get about two minutes of balancing before the search moves on.

```bash
.venv/bin/python scripts/generate.py
```

## Files

- `datasets/sample/`: 20 example pairs. Each contains full-history `X.sgf` and `Y.sgf`, plus `sample.json` with candidate moves, winrates, and score leads. These examples do not affect generation.
- `datasets/games/`: 1,000 input games for extraction.
- `scripts/generate.py`: the complete extraction and balancing script.
- `configs/katago-analysis.cfg`: KataGo settings.
- `outputs/datasets/sample/`: generated samples. `outputs/state.json` tracks progress for resuming.
