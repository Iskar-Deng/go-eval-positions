#!/usr/bin/env python3
"""Extract full-history X/Y pairs, balance them, and export accepted samples."""
import argparse
import json
from pathlib import Path
import random
import subprocess
import sys

from find_atari import PROJECT
from balance_append import quality


def run(script, *args):
    command = [sys.executable, '-u', str(PROJECT/'scripts'/script), *map(str, args)]
    print('Running:', ' '.join(command), flush=True)
    subprocess.run(command, cwd=PROJECT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--games-dir', type=Path, default=PROJECT/'datasets/games')
    parser.add_argument('--start-game', type=int, default=0)
    parser.add_argument('--games', type=int, default=1000)
    parser.add_argument('--extract-minutes', type=float, default=60)
    parser.add_argument('--balance-minutes', type=float, default=30)
    parser.add_argument('--sample-minutes', type=float, default=2)
    parser.add_argument('--seed', type=int, default=104)
    args = parser.parse_args()
    if min(args.games, args.extract_minutes, args.balance_minutes, args.sample_minutes) <= 0 or args.start_game < 0:
        parser.error('Budgets and game count must be positive; start-game must be nonnegative')
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    run('scan_stable_atari.py', '--games-dir', args.games_dir.resolve(),
        '--start-game', args.start_game, '--max-games', args.games,
        '--max-turn', 149, '--out', out/'seeds')
    run('prepare_atari_seeds.py', out/'seeds/candidates.jsonl',
        '--out', out/'candidates.jsonl', '--max-move', 150)
    # Avoid source games already represented in bundled or previously exported data.
    excluded = set()
    manifests = [PROJECT/'datasets/manifest.json', *sorted((PROJECT/'outputs').glob('*/datasets/manifest.json'))]
    for path in manifests:
        if path.exists():
            excluded.update(s['source_sha256'] for s in json.loads(path.read_text())['samples'])
    pool = out/'candidates.jsonl'
    rows = [json.loads(line) for line in pool.read_text().splitlines() if line.strip()]
    source_paths = {}
    selected = []
    for row in rows:
        sha = row['source_sha256']
        source_paths.setdefault(sha, row['source'])
        if sha not in excluded and row['source'] == source_paths[sha]:
            selected.append(row)
    pool.write_text(''.join(json.dumps(row)+'\n' for row in selected))
    if not selected:
        print('No new candidate games found. Use another game range or SGF folder.')
        return
    run('search_endpoints.py', '--input', pool, '--out', out/'extract',
        '--history-mode', 'full', '--metric', 'scoreLead', '--max-move', 150,
        '--minutes', args.extract_minutes, '--max-seeds', 0,
        '--max-passed', 1000000, '--per-seed', 4, '--seed', args.seed)
    direct, pending = [], []
    extracted = sorted((out/'extract').glob('*/sample.json'))
    for path in extracted:
        record = json.loads(path.read_text())
        if all(quality(record['X'], record['Y'])[1].values()):
            direct.append(path)
        elif record['turn'] + 3 <= 150:
            pending.append(path)
    random.Random(args.seed).shuffle(pending)
    if pending:
        run('balance_passlike.py', *pending, '--out', out/'balance',
            '--minutes', args.sample_minutes, '--total-minutes', args.balance_minutes,
            '--max-added', 24, '--size-control')
    balanced = list((out/'balance').glob('*/sample.json'))
    run('export_verified.py', '--input', out/'extract', out/'balance',
        '--out', out/'datasets')
    summary = dict(extracted=len(extracted), original_direct=len(direct),
                   queued_for_balance=len(pending), balanced=len(balanced),
                   accepted=len(direct)+len(balanced))
    (out/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
