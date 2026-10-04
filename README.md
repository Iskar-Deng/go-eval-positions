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

Extract for 60 minutes, then balance for up to 30 minutes. Use a new output folder for each run.

```bash
.venv/bin/python scripts/generate.py --out outputs/run01 --extract-minutes 60 --balance-minutes 30
```

## Files

- `datasets/accepted/`: 20 sample pairs. Each contains `X.sgf`, `Y.sgf`, and `sample.json` with candidate moves, winrates, and score leads.
- `datasets/sources/` and `datasets/manifest.json`: original games and sample metadata.
- `datasets/games/`: 1,000 input games for extraction.
- `scripts/` and `configs/`: generation code and KataGo settings.
- `outputs/run01/datasets/accepted/`: generated samples; counts are in `outputs/run01/summary.json`.
