#!/usr/bin/env python3
"""Create single-stone candidate edits from persistent-atari scan records."""
import argparse
from collections import Counter
import json
from pathlib import Path

from search_endpoints import replay, within_move_budget
from search_pairs import mutations


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('input', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--max-move', type=int, default=150)
    ap.add_argument('--radius', type=int, default=3)
    args = ap.parse_args()
    records = [json.loads(line) for line in args.input.read_text().splitlines() if line.strip()]
    seen, rows, counts = set(), [], Counter()
    for record in records:
        for turn in record['sample_turns']:
            key = (record['source'], record['player'], record['A'], tuple(record['target']['stones']), turn)
            if key in seen:
                continue
            seen.add(key)
            row = {k: record[k] for k in ['source', 'source_sha256', 'player', 'A', 'target', 'outside_nonpass_moves']}
            row['turn'] = turn
            if not within_move_budget(row, args.max_move):
                continue
            counts['X_examined'] += 1
            board = replay(row)
            edits = [edit for _, _, edit in mutations(board, row['player'].lower(), row['A'],
                       row['target']['stones'], radius=args.radius)]
            if edits:
                row.update(edits=edits, status='structural_only_not_engine_verified')
                rows.append(row)
                counts['Y_variants'] += len(edits)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x') as stream:
        stream.write(''.join(json.dumps(row) + '\n' for row in rows))
    summary = dict(counts, X_with_single_stone_Y=len(rows), games=len({r['source'] for r in rows}),
                   max_turn=max((r['turn'] for r in rows), default=None), max_move=args.max_move)
    args.out.with_suffix('.summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
