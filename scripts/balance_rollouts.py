#!/usr/bin/env python3
"""Try long shared remote continuations; evaluate A/B only at checkpoints.

No monotonic-improvement requirement between checkpoints. Both players take
turns on the same coordinates in X and Y. Fixed rank variants let an estimated
leading side play slower remote moves; they are hypotheses, not value guarantees.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import time

from find_atari import PROJECT, gtp, point_of
from search_pairs import Evaluator, export_board, make_board, metric, position, stones
from search_endpoints import check_scores, check_structure, values
from balance_append import export_history, play_pair, quality


def pair_positions(record, original, extra):
    if 'history' in record:
        from full_history import query_position
        return [query_position(record['history'], side, extra) for side in ['X', 'Y']]
    return [position(board, record['player'].lower(), 'chinese', 7.5, extra) for board in original]


def remote_mask(r, radius=4, b_radius=3):
    anchors = [r['A'], r['edit']['from'], r['edit']['to']]
    anchors += r['edit']['target_X'] + r['edit']['target_Y']
    points = {point_of(p) for p in anchors}
    b = point_of(r['B'])
    return {gtp((row,col)) for row in range(19) for col in range(19)
            if any(abs(row-y)+abs(col-x) <= radius for y,x in points)
            or abs(row-b[0])+abs(col-b[1]) <= b_radius}


def shared_candidates(xr, yr, x, y, actor, r, blocked, limit=32):
    ranked = []
    for i in range(361):
        px, py = xr['policy'][i], yr['policy'][i]
        if px < 0 or py < 0:
            continue
        move = gtp((18-i//19, i%19))
        if move not in blocked:
            ranked.append(((px+py)/2, move))
    result = []
    for _, move in sorted(ranked, reverse=True):
        pair = play_pair(x,y,actor,move,r)
        if pair is not None:
            result.append((move,pair))
        if len(result) == limit:
            break
    return result


def save(out, source, r, orig, boards, moves, vx, vy, visits):
    _, checks = quality(vx,vy)
    structure = check_structure(*boards,r['player'],r['A'],r['B'])
    assert all(checks.values()) and structure['valid']
    assert set(structure['changed_points']) == {r['edit']['from'],r['edit']['to']}
    assert len(moves)%2 == 0 and r['turn']+len(moves)+1 <= 150
    dest=out/r['id'];dest.mkdir(exist_ok=True)
    record=dict(r,status='balanced_by_shared_continuation',original_turn=r['turn'],
        turn=r['turn']+len(moves),candidate_move_number=r['turn']+len(moves)+1,
        moves_remaining_after_candidate=149-r['turn']-len(moves),appended_moves=moves,
        board_X=stones(boards[0]),board_Y=stones(boards[1]),X=vx,Y=vy,
        original_sample=str(source.resolve()),balanced_checks=checks,
        balanced_criteria={'good_winrate_interval':[.30,.70],'Y_B_minus_A_min_winrate':.20,'B_max_winrate_drift':.30},
        checks=check_scores(vx,vy),B_winrate_drift=abs(vx['B']['winrate']-vy['B']['winrate']),
        structure=structure,visits=visits,protocol='original_setup_plus_shared_appended_history')
    if 'history' in r:
        history = dict(r['history'], X=r['history']['X']+list(moves), Y=r['history']['Y']+list(moves))
        record.update(history=history, protocol='full_history_single_historical_move_edit',
                      original_turn=history['source_turn'],
                      appended_moves=r.get('appended_moves',[])+list(moves))
    (dest/'sample.json').write_text(json.dumps(record,indent=2))
    for label,board,initial in zip(['X','Y'],boards,orig):
        export_history(dest/f'{label}.sgf',initial,r,moves,label)
        export_board(dest/f'{label}_final.sgf',board,r['player'].lower(),r['A'],r['B'],'chinese',7.5,
                     'Final snapshot; saved analysis uses full appended history.')
    return record
