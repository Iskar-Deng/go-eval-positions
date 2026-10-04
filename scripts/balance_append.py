#!/usr/bin/env python3
"""Balance paired snapshots by appending identical legal alternating moves.

Search in two-ply blocks to retain the original player to move; several blocks
may be appended. Protect local test material only as a generation heuristic.
All acceptance checks are repeated on the final position with full appended history.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import time

from sgfmill import sgf
from find_atari import PROJECT, gtp, point_of
from search_pairs import Evaluator, export_board, legal, make_board, metric, position, stones
from search_endpoints import check_scores, check_structure, values


def play_pair(x, y, player, move, record):
    xx, yy = x.copy(), y.copy()
    p = point_of(move)
    try:
        if p is not None:
            xx.play(*p, player.lower())
            yy.play(*p, player.lower())
    except ValueError:
        return None
    structure = check_structure(xx, yy, record['player'], record['A'], record['B'])
    if not structure['valid'] or set(structure['changed_points']) != {record['edit']['from'], record['edit']['to']}:
        return None
    return xx, yy


def protected_points(record, radius=2):
    anchors = [record['A'], record['edit']['from'], record['edit']['to']]
    anchors += record['edit']['target_X'] + record['edit']['target_Y']
    points = {point_of(p) for p in anchors}
    b = point_of(record['B'])
    return {gtp((r, c)) for r in range(19) for c in range(19)
            if min(abs(r-y) + abs(c-x) for y, x in points) <= radius
            or abs(r-b[0]) + abs(c-b[1]) <= 1}


def move_pool(xr, yr, blocked, limit=6):
    # Include strong moves and a few lower-prior choices so the leading side can
    # concede value. A policy rank is a sampling heuristic, never a quality claim.
    candidates = []
    for root in [xr, yr]:
        legal_points = sorted(((v, gtp((18-i//19, i % 19)))
            for i, v in enumerate(root['policy'][:361]) if v >= 0), reverse=True)
        ordered = [m['move'] for m in sorted(root['moveInfos'], key=lambda m: m['order'])]
        ordered += [p for _, p in legal_points]
        ordered = list(dict.fromkeys(p for p in ordered if p not in blocked and p != 'pass'
                    and legal(xr, p, 19) and legal(yr, p, 19)))
        candidates.extend(ordered[:3] + [ordered[i] for i in [5, 10, 18] if i < len(ordered)])
    return list(dict.fromkeys(candidates))[:limit]


def quality(x, y):
    good = [x['A']['winrate'], x['B']['winrate'], y['B']['winrate']]
    distance = sum(max(.30-v, 0, v-.70) for v in good)
    checks = check_scores(x, y)
    checks.update(good_winrates_in_range=distance <= 1e-12,
                  Y_winrate_gap=y['B']['winrate']-y['A']['winrate'] >= .20,
                  B_winrate_drift=abs(x['B']['winrate']-y['B']['winrate']) <= .30)
    xgap = abs(x['A']['scoreLead']-x['B']['scoreLead'])
    drift = abs(x['B']['scoreLead']-y['B']['scoreLead'])
    ygap = y['B']['scoreLead']-y['A']['scoreLead']
    loss = 12*distance + max(xgap-2, 0) + max(drift-3, 0) + max(4-ygap, 0)
    loss += .015 * sum(abs(v-.5) for v in good)
    return loss, checks


def export_history(path, original, record, moves, label):
    if 'history' in record:
        from full_history import write_sgf
        write_sgf(path, record['history'], label, moves, labels={'A': record['A'], 'B': record['B']})
        return
    game = sgf.Sgf_game(19)
    root = game.get_root()
    root.set_setup_stones([point_of(p) for c,p in stones(original) if c=='B'],
                          [point_of(p) for c,p in stones(original) if c=='W'])
    root.set('PL', record['player'].lower())
    root.set('RU', 'Chinese'); root.set('KM', 7.5)
    root.set('C', f'{label}: initial snapshot after source move {record["turn"]}; actual shared continuation follows.')
    node = root
    for color, move in moves:
        node = game.extend_main_sequence()
        node.set_move(color.lower(), point_of(move))
    node.set('LB', [(point_of(record[m]), m) for m in ['A','B']])
    path.write_bytes(game.serialise())
