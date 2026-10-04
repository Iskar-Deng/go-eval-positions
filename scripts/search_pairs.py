#!/usr/bin/env python3
"""Search single-stone counterfactuals. All engine queries use setup snapshots.

Exploratory results live under outputs/auto_pairs, never datasets/accepted.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import time

from sgfmill import boards, sgf
from find_atari import Engine, PROJECT, ataris, group, gtp, neighbors, point_of


def stones(board):
    return [[c.upper(), gtp(p)] for c, p in board.list_occupied_points()]


def make_board(items, size=19):
    board = boards.Board(size)
    black = [point_of(p) for c, p in items if c.lower() == 'b']
    white = [point_of(p) for c, p in items if c.lower() == 'w']
    if not board.apply_setup(black, white, []):
        raise ValueError('Setup includes a group with no liberties')
    return board


def relocate(board, p, q):
    color = board.get(*p)
    if color is None or board.get(*q) is not None or p == q:
        return None
    result = board.copy()
    if not result.apply_setup([q] if color == 'b' else [],
                              [q] if color == 'w' else [], [p]):
        return None
    return result


def md(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def has_two_vital_regions(board, chain):
    """Conservative life certificate: two disjoint empty regions, each bounded
    by this same chain, with every region point a liberty of the chain.
    This covers a subset of pass-alive shapes; False does not mean dead.
    """
    seen, count = set(), 0
    for p in sorted({q for s in chain for q in neighbors(board, s) if board.get(*q) is None}):
        if p in seen:
            continue
        pending, region, boundary = [p], set(), set()
        while pending:
            q = pending.pop()
            if q in region:
                continue
            region.add(q)
            for t in neighbors(board, q):
                if board.get(*t) is None:
                    if t not in region:
                        pending.append(t)
                else:
                    boundary.add(t)
        seen.update(region)
        if boundary and boundary <= chain and all(set(neighbors(board, t)) & chain for t in region):
            count += 1
    return count >= 2


def mutations(board, player, a, target, radius=3, motif='both'):
    """Exactly two changed intersections; exactly one additional target liberty.

    surround: retain the target chain and the moved stone's own chain membership.
    target: move one target stone, retaining target size and connectivity.
    These are structural filters, not proofs that the move is neutral or sente.
    """
    target = {point_of(p) for p in target}
    anchor = min(target)
    _, original_libs = group(board, anchor)
    a = point_of(a)
    source_points = set(target) if motif in ('both', 'target') else set()
    if motif in ('both', 'surround'):
        source_points.update(q for p in target for q in neighbors(board, p)
                             if board.get(*q) == player)
    results = []
    for p in sorted(source_points):
        color = board.get(*p)
        original_chain, own_libs = group(board, p)
        kind = 'surround' if color == player else 'target'
        safe_before = kind == 'surround' and has_two_vital_regions(board, original_chain)
        if kind == 'surround' and len(original_chain) < 2:
            continue
        for r in range(max(0, p[0] - radius), min(board.side, p[0] + radius + 1)):
            for c in range(max(0, p[1] - radius), min(board.side, p[1] + radius + 1)):
                q = (r, c)
                if md(p, q) > radius or q == a or board.get(*q) is not None:
                    continue
                y = relocate(board, p, q)
                if y is None:
                    continue
                new_chain, new_own_libs = group(y, q)
                if new_chain != (original_chain - {p}) | {q}:
                    continue  # no splits, merges, or isolated replacement stones
                new_target = target if kind == 'surround' else (target - {p}) | {q}
                new_anchor = min(new_target)
                actual_target, new_libs = group(y, new_anchor)
                if actual_target != new_target or len(new_libs) != 3 or a not in new_libs:
                    continue
                if kind == 'surround' and new_libs != original_libs | {p}:
                    continue
                if gtp(a) in ataris(y, player):
                    continue  # A must not atari some other group either
                after = y.copy()
                after.play(*a, player)
                if after.get(*a) != player or after.get(*new_anchor) != board.get(*anchor):
                    continue
                if len(group(after, new_anchor)[1]) != 2:
                    continue
                safe_after = safe_before and has_two_vital_regions(y, new_chain)
                if safe_before and not safe_after:
                    continue
                # Rank small displacements and small changes to own liberties first.
                rank = (not safe_after, kind != 'surround', abs(len(new_own_libs) - len(own_libs)), md(p, q))
                results.append((rank, y, {'motif': kind, 'color': color.upper(),
                    'from': gtp(p), 'to': gtp(q), 'target_X': sorted(map(gtp, target)),
                    'target_Y': sorted(map(gtp, new_target)),
                    'moved_chain_life_certified_X_and_Y': bool(safe_after),
                    'liberties_X': sorted(map(gtp, original_libs)),
                    'liberties_Y': sorted(map(gtp, new_libs))}))
    return sorted(results, key=lambda x: (x[0], x[2]['from'], x[2]['to']))


def position(board, player, rules, komi, moves=()):
    current = player if len(moves) % 2 == 0 else ('w' if player == 'b' else 'b')
    return {'initialStones': stones(board), 'initialPlayer': player.upper(),
            'moves': list(moves), '_player': current.upper(), 'rules': rules, 'komi': komi,
            'boardXSize': board.side, 'boardYSize': board.side, 'includePolicy': True}


def compact(result):
    keys = ('move', 'winrate', 'scoreLead', 'visits', 'pv', 'prior', 'order')
    value = {'rootInfo': result['rootInfo'],
            'moveInfos': [{k: m[k] for k in keys if k in m} for m in result['moveInfos']],
            'policy': result.get('policy')}
    if 'ownership' in result:
        value['ownership'] = result['ownership']
    return value


class Evaluator:
    def __init__(self, out):
        self.engine = Engine(PROJECT / 'scripts/katago.sh', out / 'engine.log', 300, 5)
        self.cache = {}
        self.queries = 0
        self.journal = (out / 'queries.jsonl').open('a')

    def query(self, base, visits, allowed=None):
        key = json.dumps([base, visits, allowed], sort_keys=True)
        if key not in self.cache:
            start = time.monotonic()
            value = compact(self.engine.query(base, visits, allowed))
            self.cache[key] = value
            self.queries += 1
            self.journal.write(json.dumps({'request': base, 'visits': visits, 'allowed': allowed,
                'elapsed': time.monotonic() - start, 'result': value}) + '\n')
            self.journal.flush()
        return self.cache[key]

    def close(self):
        self.engine.close()
        self.journal.close()


def legal(result, move, size):
    p = point_of(move)
    i = size * size if p is None else (size - 1 - p[0]) * size + p[1]
    return result['policy'][i] >= 0


def metric(result, move=None):
    if move is None:
        return result['rootInfo']
    return next(m for m in result['moveInfos'] if m['move'] == move)


def measure(e, base, a, b, visits):
    root = e.query(base, visits)
    if not all(legal(root, move, base['boardXSize']) for move in (a, b)):
        return None
    return {'root': metric(root), 'A': metric(e.query(base, visits, [a]), a),
            'B': metric(e.query(base, visits, [b]), b)}


CRITERIA = {
    'version': '2026-10-03-v4',
    'cross_position_winrate_preferred': .20,
    'cross_position_winrate_max': .30,
    'cross_position_score_drift_is_diagnostic_only': True,
    'within_position_score_tolerance': 1.5,
    'within_position_winrate_tolerance': .12,
    'Y_AB_min_score_gap': 3,
    'Y_AB_min_winrate_gap': .20,
    'X_outside_min_loss_points': 1,
    'X_outside_min_loss_winrate': .05,
}


def drift_profile(x, y):
    root = abs(x['root']['winrate'] - y['root']['winrate'])
    b = abs(x['B']['winrate'] - y['B']['winrate'])
    maximum = max(root, b)
    tier = ('preferred' if maximum <= .20 + 1e-12 else
            'extended' if maximum <= .30 + 1e-12 else 'outside')
    return {'tier': tier, 'root_winrate_drift': root, 'B_winrate_drift': b,
            'root_score_drift': abs(x['root']['scoreLead'] - y['root']['scoreLead']),
            'B_score_drift': abs(x['B']['scoreLead'] - y['B']['scoreLead'])}


def value_gates(x, y, loose=False):
    """Cross-position drift uses the user's 20pp preference / 30pp maximum.
    Keep within-position A/B quality and the Y/A penalty as separate conditions.
    """
    slack = 1.8 if loose else 1
    failures = []
    drift_limit = .35 if loose else CRITERIA['cross_position_winrate_max']
    for key in ('root', 'B'):
        if abs(x[key]['winrate'] - y[key]['winrate']) > drift_limit + 1e-12:
            failures.append(key + '_drift_winrate')
    for field, tolerance in [('scoreLead', 1.5), ('winrate', .12)]:
        for label, diff in [
                ('X_AB', abs(x['A'][field] - x['B'][field])),
                ('X_A_loss', x['root'][field] - x['A'][field]),
                ('X_B_loss', x['root'][field] - x['B'][field]),
                ('Y_B_loss', y['root'][field] - y['B'][field])]:
            if diff > tolerance * slack:
                failures.append(label + '_' + field)
    for field, minimum in [('scoreLead', 3), ('winrate', .20)]:
        if y['B'][field] - y['A'][field] < minimum / slack:
            failures.append('Y_AB_gap_' + field)
    return failures


def x_gates(x, loose=False):
    slack = 1.8 if loose else 1
    return [field for field, tol in [('scoreLead', 1.5), ('winrate', .12)]
            if max(abs(x['A'][field] - x['B'][field]),
                   x['root'][field] - min(x['A'][field], x['B'][field])) > tol * slack]


def behavior(e, board, player, a, b, target, rules, komi, visits, followup=True):
    """Compare best local and best nonlocal responses after A.

    Values in reply records are from the opponent's perspective, explicitly.
    Continue with full A/reply history, so simple ko is not reset at a child node.
    """
    opponent = 'w' if player == 'b' else 'b'
    moves = [[player.upper(), a]]
    base = position(board, player, rules, komi, moves)
    root = e.query(base, visits)
    area = {point_of(p) for p in target} | {point_of(a)}
    local, outside = [], []
    for r in range(board.side):
        for c in range(board.side):
            p = (r, c)
            m = gtp(p)
            if legal(root, m, board.side):
                (local if min(md(p, t) for t in area) <= 2 else outside).append(m)
    if legal(root, 'pass', board.side):
        outside.append('pass')
    if not local or not outside:
        return {'valid': False, 'reason': 'empty_reply_set'}
    lr, ore = e.query(base, visits, local), e.query(base, visits, outside)
    lm = min(lr['moveInfos'], key=lambda m: m['order'])
    om = min(ore['moveInfos'], key=lambda m: m['order'])
    out = {'valid': True, 'perspective': opponent.upper(), 'local_radius': 2,
           'local': lm, 'outside': om,
           'outside_loss_points': metric(lr)['scoreLead'] - metric(ore)['scoreLead'],
           'outside_loss_winrate': metric(lr)['winrate'] - metric(ore)['winrate']}
    if not followup:
        return out
    follow = position(board, player, rules, komi, moves + [[opponent.upper(), lm['move']]])
    fr = e.query(follow, visits)
    if legal(fr, b, board.side):
        fb = metric(e.query(follow, visits, [b]), b)
        out['B_after_local_reply'] = fb
        out['B_after_local_reply_loss_points'] = metric(fr)['scoreLead'] - fb['scoreLead']
    return out


def behavior_gates(x, y):
    failures = []
    if not x['valid'] or not y['valid']:
        return ['reply_sets']
    if x['outside_loss_points'] < CRITERIA['X_outside_min_loss_points'] or x['outside_loss_winrate'] < CRITERIA['X_outside_min_loss_winrate']:
        failures.append('X_not_forcing')
    if y['outside_loss_points'] > 1:
        failures.append('Y_still_forcing')
    if x.get('B_after_local_reply_loss_points', 1e6) > 1.5:
        failures.append('X_B_not_preserved_after_reply')
    return failures


def export_board(path, board, player, a, b, rules, komi, comment):
    game = sgf.Sgf_game(board.side)
    root = game.get_root()
    root.set_setup_stones({p for c, p in board.list_occupied_points() if c == 'b'},
                          {p for c, p in board.list_occupied_points() if c == 'w'})
    root.set('PL', player)
    root.set('RU', rules)
    root.set('KM', komi)
    root.set('LB', [(point_of(a), 'A'), (point_of(b), 'B')])
    root.set('C', comment)
    path.write_bytes(game.serialise())


def seeds(paths, source_filter=None):
    found = []
    seen = set()
    for path in paths:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if source_filter and source_filter not in str(path):
                continue
            for a in row['A']:
                for b in row['B']:
                    key = (row['source_sha256'], row['move_number'], a['move'], b['move'])
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append((abs(a['scoreLead'] - b['scoreLead']), str(path), row, a, b))
    return sorted(found, key=lambda v: (v[0], v[1], v[2]['move_number']))


def diverse_seeds(candidates):
    """One best B per (game, turn, A); visit every game before repeating one.
    Within a game, try larger targets first, then similar A/B values.
    """
    buckets, seen = {}, set()
    for item in candidates:
        _, _, row, a, _ = item
        key = (row['source_sha256'], row['move_number'], a['move'])
        if key in seen:
            continue
        seen.add(key)
        buckets.setdefault(key[0], []).append(item)
    for bucket in buckets.values():
        bucket.sort(key=lambda v: (-max(t['stone_count'] for t in v[3]['targets']), v[0]))
    output = []
    for i in range(max(map(len, buckets.values()), default=0)):
        output.extend(bucket[i] for bucket in buckets.values() if i < len(bucket))
    return output
