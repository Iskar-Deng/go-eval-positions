#!/usr/bin/env python3
"""Find atari opportunities that persist while actual games continue elsewhere.

This is a cheap source of hypotheses, not a test of life, sente, or move value.
No engine, winrate filter, candidate B, or board mutation is used.
"""
import argparse
from collections import Counter
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import time

from sgfmill import sgf, sgf_moves
from find_atari import PROJECT, ataris, gtp, point_of


@lru_cache(maxsize=16384)
def region_points(size, a, target, radius):
    anchors = [point_of(a)] + [point_of(p) for p in target]
    return tuple((r, c) for r in range(size) for c in range(size)
                 if min(abs(r-y) + abs(c-x) for y, x in anchors) <= radius)


def scan(path, radius=3, min_remote_moves=16, max_turn=None):
    raw = path.read_bytes()
    game = sgf.Sgf_game.from_bytes(raw)
    board, moves = sgf_moves.get_setup_and_moves(game)
    active, finished = {}, []

    def finish(record):
        if record['outside_nonpass_moves'] < min_remote_moves or not record['sample_turns']:
            return
        record['unchanged_plies'] = record['end_turn'] - record['start_turn']
        turns = record['sample_turns']
        record['sample_turns'] = sorted({turns[0], turns[len(turns)//2], turns[-1]})
        finished.append(record)

    for turn, (color, move) in enumerate(moves[:max_turn], 1):
        if move is not None:
            board.play(*move, color)
        if turn == len(moves) or (turn >= 2 and moves[turn-1][1] is None and moves[turn-2][1] is None):
            break
        next_color = moves[turn][0]
        current = {}
        for player in ['b', 'w']:
            for a, targets in ataris(board, player, 2).items():
                for target in targets:
                    points = region_points(board.side, a, tuple(target['stones']), radius)
                    footprint = tuple(board.get(*p) for p in points)
                    key = (player, a, tuple(target['stones']), points, footprint)
                    if key in active:
                        record = active[key]
                        record['end_turn'] = turn
                        if move is not None:
                            # A real move inside this region cannot leave its state unchanged.
                            assert move not in points
                            record['outside_nonpass_moves'] += 1
                    else:
                        record = {'source': str(path), 'source_sha256': hashlib.sha256(raw).hexdigest(),
                                  'player': player.upper(), 'A': a, 'target': target,
                                  'radius': radius, 'region': list(map(gtp, points)),
                                  'start_turn': turn, 'end_turn': turn,
                                  'outside_nonpass_moves': 0, 'sample_turns': []}
                    if next_color == player:
                        record['sample_turns'].append(turn)
                    current[key] = record
        for key in active.keys() - current.keys():
            finish(active[key])
        active = current
    for record in active.values():
        finish(record)
    return finished


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--radius', type=int, default=3)
    ap.add_argument('--min-remote-moves', type=int, default=16)
    ap.add_argument('--games-dir', type=Path, default=PROJECT/'datasets/games')
    ap.add_argument('--max-games', type=int, default=100)
    ap.add_argument('--start-game', type=int, default=0)
    ap.add_argument('--max-turn', type=int, default=None)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args()
    paths = sorted(args.games_dir.resolve().rglob('*.sgf'))[args.start_game:args.start_game + args.max_games]
    if not paths:
        raise SystemExit('No SGF games found in the selected range')
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / 'config.json').write_text(json.dumps(vars(args), default=str, indent=2))
    start, records, errors = time.monotonic(), [], []
    for path in paths:
        try:
            records.extend(scan(path, args.radius, args.min_remote_moves, args.max_turn))
        except (ValueError, AssertionError) as exc:
            errors.append({'source': str(path), 'error': repr(exc)})
    records.sort(key=lambda r: (-r['outside_nonpass_moves'], -r['target']['stone_count'], r['source']))
    (args.out / 'candidates.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in records))
    summary = {'games_scanned': len(paths), 'games_with_candidates': len({r['source'] for r in records}),
               'persistent_opportunities': len(records), 'radius': args.radius,
               'min_remote_moves': args.min_remote_moves,
               'target_sizes': dict(Counter(r['target']['stone_count'] for r in records)),
               'longest_remote_sequence': max((r['outside_nonpass_moves'] for r in records), default=0),
               'seconds': time.monotonic() - start, 'errors': errors,
               'interpretation': 'Unchanged local board state and persistent structural atari only. No life, sente, global value, or history-sensitive legality certification.'}
    (args.out / 'summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
