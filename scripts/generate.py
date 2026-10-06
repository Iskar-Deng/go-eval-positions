#!/usr/bin/env python3
"""Continuously extract X/Y pairs, immediately balance each pair, and save results."""
import argparse
from collections import Counter, OrderedDict, deque
from functools import lru_cache
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import random
import shutil
import signal
import statistics
import subprocess
import sysconfig
import threading
import time

from sgfmill import boards, sgf, sgf_moves

PROJECT = Path(__file__).resolve().parents[1]
BALANCE_SECONDS = 120

def gtp(point):
    if point is None:
        return 'pass'
    row, col = point
    return 'ABCDEFGHJKLMNOPQRST'[col] + str(row + 1)


def point_of(move):
    if move.lower() == 'pass':
        return None
    return int(move[1:]) - 1, 'ABCDEFGHJKLMNOPQRST'.index(move[0].upper())


def neighbors(board, point):
    r, c = point
    return [(y, x) for y, x in ((r-1, c), (r+1, c), (r, c-1), (r, c+1))
            if 0 <= y < board.side and 0 <= x < board.side]


def group(board, start):
    color = board.get(*start)
    stones, liberties, pending = set(), set(), [start]
    while pending:
        p = pending.pop()
        if p in stones:
            continue
        stones.add(p)
        for q in neighbors(board, p):
            value = board.get(*q)
            if value is None:
                liberties.add(q)
            elif value == color and q not in stones:
                pending.append(q)
    return stones, liberties


def ataris(board, player, min_target_stones=1):
    """Structural candidates only; KataGo subsequently checks ko/superko legality."""
    seen, result = set(), {}
    for color, p in board.list_occupied_points():
        if color == player or p in seen:
            continue
        stones, libs = group(board, p)
        seen.update(stones)
        if len(libs) != 2 or len(stones) < min_target_stones:
            continue
        for move in sorted(libs):
            after = board.copy()
            after.play(*move, player)
            if after.get(*move) != player or after.get(*p) != color:
                continue
            _, remaining = group(after, p)
            if len(remaining) == 1:
                result.setdefault(gtp(move), []).append({
                    'color': color.upper(), 'stones': sorted(map(gtp, stones)),
                    'stone_count': len(stones),
                    'liberties_before': sorted(map(gtp, libs)),
                    'liberties_after': sorted(map(gtp, remaining)),
                })
    return result


class Engine:
    def __init__(self, log, timeout=300, shutdown_timeout=5):
        self.timeout = timeout
        self.shutdown_timeout = shutdown_timeout
        self.log = open(log, 'a')
        self.process = subprocess.Popen(engine_command(), env=engine_environment(), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=self.log, text=True)
        self.responses = queue.Queue()
        self.counter = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.process.stdout:
            self.responses.put(line)
        self.responses.put(None)

    def query(self, base, visits, allowed=None):
        self.counter += 1
        query_id = str(self.counter)
        query = dict(base, id=query_id, maxVisits=visits,
                     overrideSettings={'reportAnalysisWinratesAs': 'SIDETOMOVE'})
        if allowed is not None:
            query['allowMoves'] = [{'player': base['_player'], 'moves': allowed, 'untilDepth': 1}]
        query.pop('_player')
        self.process.stdin.write(json.dumps(query) + '\n')
        self.process.stdin.flush()
        while True:
            try:
                line = self.responses.get(timeout=self.timeout)
            except queue.Empty:
                raise RuntimeError('KataGo timed out; see engine.log')
            if line is None:
                raise RuntimeError('KataGo exited; see engine.log')
            data = json.loads(line)
            if 'error' in data:
                raise RuntimeError(data['error'])
            if 'warning' in data:
                raise RuntimeError('KataGo warning (refusing silent rule changes): ' + data['warning'])
            if data.get('id') == query_id and not data.get('isDuringSearch', False):
                return data

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=self.shutdown_timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.log.close()


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


def compact(result):
    keys = ('move', 'winrate', 'scoreLead', 'visits', 'pv', 'prior', 'order')
    value = {'rootInfo': result['rootInfo'],
            'moveInfos': [{k: m[k] for k in keys if k in m} for m in result['moveInfos']],
            'policy': result.get('policy')}
    if 'ownership' in result:
        value['ownership'] = result['ownership']
    return value


def legal(result, move, size):
    p = point_of(move)
    i = size * size if p is None else (size - 1 - p[0]) * size + p[1]
    return result['policy'][i] >= 0


class MissingMoveError(Exception):
    """KataGo did not return an evaluation for the requested move."""


def metric(result, move=None):
    if move is None:
        return result['rootInfo']
    for item in result['moveInfos']:
        if item['move'] == move:
            return item
    raise MissingMoveError(f'KataGo returned no evaluation for {move}')


def check_scores(x, y, loose=False):
    tol, gap, drift, increase = (3.5, 2., 5., 1.5) if loose else (2., 4., 3., 3.)
    x_loss = x['B']['scoreLead'] - x['A']['scoreLead']
    y_loss = y['B']['scoreLead'] - y['A']['scoreLead']
    return {'X_AB_close': abs(x_loss) <= tol + 1e-12,
            'Y_B_better': y_loss >= gap - 1e-12,
            'B_stable': abs(x['B']['scoreLead'] - y['B']['scoreLead']) <= drift + 1e-12,
            'A_loss_increases': y_loss - x_loss >= increase - 1e-12}


def check_structure(x, y, player, a, b):
    xs, ys = dict((p, c) for c, p in stones(x)), dict((p, c) for c, p in stones(y))
    diff = [p for p in xs.keys() | ys.keys() if xs.get(p) != ys.get(p)]
    same_counts = Counter(xs.values()) == Counter(ys.values())
    relocation = len(diff) == 2 and same_counts and sum(p in xs and p not in ys for p in diff) == 1
    xa, ya = ataris(x, player.lower()), ataris(y, player.lower())
    labels = {'X': {'A': a in xa, 'B': b in xa}, 'Y': {'A': a in ya, 'B': b in ya}}
    valid = x.side == y.side and a != b and relocation and labels == {'X': {'A': True, 'B': False}, 'Y': {'A': False, 'B': False}}
    return {'valid': valid, 'changed_points': sorted(diff), 'same_color_counts': same_counts,
            'atari': labels}


def replay(row):
    raw = Path(row['source']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != row['source_sha256']:
        raise ValueError('Source SGF hash changed')
    board, moves = sgf_moves.get_setup_and_moves(sgf.Sgf_game.from_bytes(raw))
    for color, p in moves[:row['turn']]:
        if p is not None:
            board.play(*p, color)
    if moves[row['turn']][0] != row['player'].lower():
        raise ValueError('Wrong side to move')
    return board


def candidate_bs(xroot, yroot, x, y, player, a, limit):
    forbidden = set(ataris(x, player)) | set(ataris(y, player)) | {a, 'pass'}
    ordered = []
    for result in [yroot, xroot]:
        ordered.extend(m['move'] for m in sorted(result['moveInfos'], key=lambda m: m['order']))
    return list(dict.fromkeys(m for m in ordered if m not in forbidden
                and legal(xroot, m, x.side) and legal(yroot, m, x.side)))[:limit]


def values(e, base, a, b, visits):
    root = e.query(base, visits)
    if not all(legal(root, m, base['boardXSize']) for m in [a, b]):
        return None
    return {'root': metric(root), 'A': metric(e.query(base, visits, [a]), a),
            'B': metric(e.query(base, visits, [b]), b)}


def board_key(board):
    return tuple(board.get(r, c) for r in range(board.side) for c in range(board.side))


def board_stones(board):
    return [[color.upper(), gtp(p)] for color, p in board.list_occupied_points()]


def replay_checked(initial, moves, first):
    """Chinese positional superko, no suicide, alternating turns; track surviving origins."""
    board = initial.copy()
    origins = {p: None for _, p in board.list_occupied_points()}
    seen = {board_key(board)}
    actor = first.upper()
    for number, (color, move) in enumerate(moves, 1):
        if color.upper() != actor:
            raise ValueError(f'nonalternating_history_at_{number}')
        p = point_of(move)
        if p is not None:
            try:
                board.play(*p, color.lower())
            except (ValueError, IndexError) as exc:
                raise ValueError(f'illegal_occupied_move_at_{number}') from exc
            if board.get(*p) != color.lower():
                raise ValueError(f'suicide_at_{number}')
            key = board_key(board)
            if key in seen:
                raise ValueError(f'positional_superko_at_{number}')
            seen.add(key)
            occupied = {q for _, q in board.list_occupied_points()}
            origins = {q: origin for q, origin in origins.items() if q in occupied}
            origins[p] = number
        actor = 'W' if actor == 'B' else 'B'
    return board, origins, actor


def source_history(row):
    raw = Path(row['source']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != row['source_sha256']:
        raise ValueError('Source SGF hash changed')
    game = sgf.Sgf_game.from_bytes(raw)
    initial, plays = sgf_moves.get_setup_and_moves(game)
    turn = row.get('original_turn', row['turn'])
    if not 0 <= turn <= len(plays):
        raise ValueError('Invalid source turn')
    root = game.get_root()
    first = root.get('PL') if root.has_property('PL') else (plays[0][0] if plays else 'b')
    moves = [[color.upper(), gtp(p)] for color, p in plays[:turn]]
    board, origins, player = replay_checked(initial, moves, first)
    if player != row['player'].upper():
        raise ValueError('Wrong player after source prefix')
    return initial, moves, first.upper(), board, origins


def construct(row, edit, xboard, yboard):
    initial, moves, first, _, origins = source_history(row)
    old, new = point_of(edit['from']), point_of(edit['to'])
    number = origins.get(old)
    if number is None:
        raise ValueError('Relocated stone is not a surviving historical move')
    y_moves = [list(m) for m in moves]
    if y_moves[number-1] != [edit['color'].upper(), edit['from']]:
        raise ValueError('Historical stone origin does not match edit')
    y_moves[number-1][1] = edit['to']
    tail = [list(m) for m in row.get('appended_moves', [])]
    history = dict(initial_stones=board_stones(initial), initial_player=first,
                   X=moves+tail, Y=y_moves+tail, changed_move_number=number,
                   source_turn=len(moves))
    for side, expected in [('X', xboard), ('Y', yboard)]:
        actual, _, player = replay_checked(initial, history[side], first)
        if board_key(actual) != board_key(expected):
            raise ValueError(f'{side}_final_board_mismatch')
        if player != row['player'].upper():
            raise ValueError('Wrong final player')
    if len(history['X']) != row['turn'] or len(history['X'])+1 > 150:
        raise ValueError('Historical move count mismatch or candidate beyond move 150')
    return history


def initial_board(history, size=19):
    from sgfmill.boards import Board
    board = Board(size)
    black = [point_of(p) for c, p in history['initial_stones'] if c == 'B']
    white = [point_of(p) for c, p in history['initial_stones'] if c == 'W']
    if not board.apply_setup(black, white, []):
        raise ValueError('Illegal initial setup')
    return board


def query_position(history, side, extra=(), rules='chinese', komi=7.5):
    moves = history[side] + list(extra)
    first = history['initial_player']
    player = first if len(moves)%2 == 0 else ('W' if first == 'B' else 'B')
    return dict(initialStones=history['initial_stones'], initialPlayer=first, moves=moves,
                _player=player, rules=rules, komi=komi, boardXSize=19, boardYSize=19,
                includePolicy=True)


def write_sgf(path, history, side, extra=(), labels=None):
    game = sgf.Sgf_game(19)
    root = game.get_root()
    root.set_setup_stones([point_of(p) for c,p in history['initial_stones'] if c=='B'],
                          [point_of(p) for c,p in history['initial_stones'] if c=='W'])
    root.set('PL', history['initial_player'].lower())
    root.set('RU', 'Chinese'); root.set('KM', 7.5)
    node = root
    for color, move in history[side] + list(extra):
        node = game.extend_main_sequence()
        node.set_move(color.lower(), point_of(move))
    if labels:
        node.set('LB', [(point_of(move), label) for label, move in labels.items()])
    path.write_bytes(game.serialise())


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


def empty_region(board,p):
    seen=set();pending=[p]
    while pending:
        q=pending.pop()
        if q in seen:continue
        seen.add(q)
        pending.extend(t for t in neighbors(board,q) if board.get(*t) is None and t not in seen)
    return seen


def filler_pool(xr,yr,x,y,actor,r,blocked):
    actor=actor.lower();choices=[]
    for i in range(361):
        p=(18-i//19,i%19);move=gtp(p)
        if move in blocked or xr['policy'][i]<0 or yr['policy'][i]<0:continue
        ownership=min(xr['ownership'][i],yr['ownership'][i])
        # The engine reports ownership from SIDETOMOVE, so own space is positive.
        if ownership < .50:continue
        pair=play_pair(x,y,actor,move,r)
        if pair is None:continue
        strength=[]
        for before,after in zip([x,y],pair):
            nearby=[(rr,cc) for rr in range(max(0,p[0]-3),min(19,p[0]+4))
                    for cc in range(max(0,p[1]-3),min(19,p[1]+4)) if abs(rr-p[0])+abs(cc-p[1])<=3]
            own=sum(before.get(*q)==actor for q in nearby)
            enemy_close=any(before.get(*q) not in (None,actor) and abs(q[0]-p[0])+abs(q[1]-p[1])<=1 for q in nearby)
            if own<3 or enemy_close or not any(before.get(*q)==actor for q in neighbors(before,p)):
                break
            if len(empty_region(before,p))<3 or len(group(after,p)[1])<3:
                break
            strength.append(own)
        if len(strength)==2:
            choices.append((ownership+.01*min(strength),move,pair))
    return sorted(choices,reverse=True,key=lambda v:(v[0],v[1]))


def export_one(r, destination):
    if 'history' not in r or not all(quality(r['X'],r['Y'])[1].values()):
        raise ValueError('Only fully verified full-history samples may be exported')
    history=r['history'];initial=initial_board(history)
    boards={}
    if len(history['X'])!=r['turn'] or len(history['Y'])!=r['turn'] or r['turn']+1>150:
        raise ValueError('Move count mismatch')
    changes=[i+1 for i,(a,b) in enumerate(zip(history['X'],history['Y'])) if a!=b]
    if changes!=[history['changed_move_number']]:raise ValueError('History must differ at exactly one move')
    for side in ['X','Y']:
        board,_,player=replay_checked(initial,history[side],history['initial_player'])
        if board_key(board)!=board_key(make_board(r['board_'+side])) or player!=r['player']:
            raise ValueError('History/board/player mismatch')
        boards[side]=board
    if not check_structure(boards['X'],boards['Y'],r['player'],r['A'],r['B'])['valid']:
        raise ValueError('Pair structure no longer valid')
    destination.mkdir(parents=True,exist_ok=False)
    for side in ['X','Y']:
        write_sgf(destination/f'{side}.sgf',history,side)
    sample=dict(source_sha256=r['source_sha256'],move_number=r['turn'],
        moves={m:r[m] for m in ['A','B']},
        results={s:dict(winrate={m:r[s][m]['winrate'] for m in ['A','B']},
                        score_lead={m:r[s][m]['scoreLead'] for m in ['A','B']}) for s in ['X','Y']},
        X_A_targets=[dict(size=t['stone_count'],stones=t['stones']) for t in ataris(boards['X'],r['player'].lower())[r['A']]])
    (destination/'sample.json').write_text(json.dumps(sample,indent=2)+'\n')


MODEL_NAME='kata1-b18c384nbt-s9996604416-d4316597426.bin.gz'


def model_path():
    return Path(os.environ.get('KATAGO_MODEL',PROJECT/'.local/katago/models'/MODEL_NAME)).resolve()


def config_path():
    default='katago-analysis.cfg'
    return Path(os.environ.get('KATAGO_ANALYSIS_CONFIG',PROJECT/'configs'/default)).resolve()


def provenance():
    model=model_path();config=config_path()
    binary=Path(os.environ.get('KATAGO_BIN',PROJECT/'.local/katago/bin/katago')).resolve()
    return dict(model=str(model),model_sha256=hashlib.sha256(model.read_bytes()).hexdigest(),
                engine_binary=str(binary),engine_binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                engine_config=str(config),engine_config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
                platform=platform.platform())


def engine_command():
    binary = os.environ.get('KATAGO_BIN', str(PROJECT/'.local/katago/bin/katago'))
    return [binary, 'analysis', '-model', str(model_path()), '-config', str(config_path())]


def engine_environment():
    env = os.environ.copy()
    libraries = Path(sysconfig.get_paths()['purelib'])/'nvidia'
    paths = [str(p) for p in sorted(libraries.glob('*/lib'))]
    if env.get('LD_LIBRARY_PATH'):
        paths.append(env['LD_LIBRARY_PATH'])
    env['LD_LIBRARY_PATH'] = ':'.join(paths)
    return env


class Evaluator:
    """Reuse one engine and bound the in-memory cache for long runs."""
    def __init__(self, out):
        self.engine = Engine(out/'engine.log')
        self.cache = OrderedDict()
        self.queries = 0

    def query(self, base, visits, allowed=None):
        key = json.dumps([base, visits, allowed], sort_keys=True)
        if key not in self.cache:
            for attempt in range(2):
                result = compact(self.engine.query(base, visits, allowed))
                self.queries += 1
                if allowed is not None and len(allowed) == 1:
                    try:
                        metric(result, allowed[0])
                    except MissingMoveError:
                        if attempt:
                            raise
                        print(f'KataGo omitted {allowed[0]}; retrying once', flush=True)
                        continue
                break
            self.cache[key] = result
            if len(self.cache) > 256:
                self.cache.popitem(last=False)
        self.cache.move_to_end(key)
        return self.cache[key]

    def close(self):
        self.engine.close()


def candidate_rows(path, rng):
    records = scan(path, max_turn=149)
    rows, seen = [], set()
    for record in records:
        for turn in record['sample_turns']:
            key = (record['player'], record['A'], tuple(record['target']['stones']), turn)
            if key in seen:
                continue
            seen.add(key)
            row = {k: record[k] for k in ['source', 'source_sha256', 'player', 'A', 'target']}
            row['turn'] = turn
            rows.append(row)
    rng.shuffle(rows)
    return rows


def extract_pair(e, row):
    """Return the first score-qualified pair for this source position."""
    x = replay(row)
    player, a = row['player'].lower(), row['A']
    try:
        initial, moves, first, _, _ = source_history(row)
    except ValueError:
        return None
    xp = query_position(dict(initial_stones=board_stones(initial), initial_player=first, X=moves), 'X')
    edits = mutations(x, player, a, row['target']['stones'], radius=3)[:4]
    if not edits:
        return None
    xr = e.query(xp, 96)
    if not legal(xr, a, 19):
        return None
    xa = metric(e.query(xp, 96, [a]), a)
    for _, y, edit in edits:
        try:
            history = construct(row, edit, x, y)
        except ValueError:
            continue
        yp = query_position(history, 'Y')
        yr = e.query(yp, 96)
        if not legal(yr, a, 19):
            continue
        ya = metric(e.query(yp, 96, [a]), a)
        if metric(yr)['scoreLead'] - ya['scoreLead'] < 1:
            continue
        for b in candidate_bs(xr, yr, x, y, player, a, 3):
            if not check_structure(x, y, player, a, b)['valid']:
                continue
            xm = {'A': xa, 'B': metric(e.query(xp, 96, [b]), b)}
            ym = {'A': ya, 'B': metric(e.query(yp, 96, [b]), b)}
            if not all(check_scores(xm, ym, loose=True).values()):
                continue
            vx, vy = values(e, xp, a, b, 1000), values(e, yp, a, b, 1000)
            if vx is None or vy is None or not all(check_scores(vx, vy).values()):
                continue
            identity = [stones(x), stones(y), row['player'], a, b]
            ident = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
            return dict(row, id=ident, B=b, edit=edit, history=history, X=vx, Y=vy,
                        board_X=stones(x), board_Y=stones(y), visits=1000,
                        protocol='full_history_single_historical_move_edit')
    return None


def pair_positions(record, moves):
    return [query_position(record['history'], side, moves) for side in ['X', 'Y']]


def choose_remote(e, r, roots, current_boards, bases, actor, leader, moves, blocked, variant):
    pool = shared_candidates(*roots, *current_boards, actor, r, blocked, limit=361)
    if not pool:
        return None
    result = e.query(bases[0], 96, [move for move, _ in pool[:24]])
    options = sorted(result['moveInfos'], key=lambda m: m['order'])
    choices = list(dict.fromkeys([v['move'] for v in options[:2]] +
        [pool[i][0] for i in [3, 8, 16, 31, 63, 127, len(pool)-1] if i < len(pool)]))
    estimates = []
    for candidate in choices:
        trial = moves + [[actor, candidate]]
        if len(trial) % 2:
            trial += [[leader, 'pass']]
        base = pair_positions(r, trial)[0]
        if legal(e.query(base, 16), r['B'], 19):
            vx = metric(e.query(base, 64, [r['B']]), r['B'])
            estimates.append((abs(vx['scoreLead']), candidate, vx, trial))
    shortlist = []
    for _, candidate, vx, trial in sorted(estimates, key=lambda v: v[0])[:4]:
        base = pair_positions(r, trial)[1]
        if not legal(e.query(base, 16), r['B'], 19):
            continue
        vy = metric(e.query(base, 64, [r['B']]), r['B'])
        penalty = 20 * sum(max(.38-v, 0, v-.62) for v in [vx['winrate'], vy['winrate']])
        penalty += .1 * max(abs(vx['scoreLead']), abs(vy['scoreLead']))
        penalty += max(abs(vx['scoreLead'] - vy['scoreLead']) - 3, 0)
        shortlist.append((penalty, candidate))
    if not shortlist:
        return None
    shortlist.sort(key=lambda v: v[0])
    move = shortlist[min(variant, len(shortlist)-1)][1]
    return next((m, pair) for m, pair in pool if m == move)


def balance_pair(e, r):
    """Try at most two continuations, with a shared two-minute search budget."""
    deadline = time.monotonic() + BALANCE_SECONDS
    orig = [make_board(r['board_X']), make_board(r['board_Y'])]
    blocked = remote_mask(r)
    limit = min(24, 149-r['turn'])
    limit -= limit % 2
    for variant in [0, 1]:
        if time.monotonic() >= deadline:
            break
        current_boards = [b.copy() for b in orig]
        moves = []
        lead = statistics.median([r['X']['A']['scoreLead'], r['X']['B']['scoreLead'], r['Y']['B']['scoreLead']])
        for ply in range(limit):
            if time.monotonic() >= deadline:
                break
            if ply % 2 == 0:
                leader = r['player'] if lead >= 0 else ('W' if r['player'] == 'B' else 'B')
            actor = r['player'] if ply % 2 == 0 else ('W' if r['player'] == 'B' else 'B')
            bases = pair_positions(r, moves)
            roots = [e.query(dict(base, includeOwnership=True), 32) for base in bases]
            if actor == leader:
                pool = filler_pool(*roots, *current_boards, actor, r, blocked)
                if not pool or not all(legal(root, 'pass', 19) for root in roots):
                    break
                pass_values = [metric(e.query(base, 96, ['pass']), 'pass') for base in bases]
                picked = None
                for _, move, pair in pool[:6]:
                    mv = [metric(e.query(base, 96, [move]), move) for base in bases]
                    if max(abs(a['scoreLead']-b['scoreLead']) for a, b in zip(mv, pass_values)) <= 2:
                        picked = move, pair
                        break
            else:
                picked = choose_remote(e, r, roots, current_boards, bases, actor, leader, moves, blocked, variant)
            if picked is None:
                break
            move, pair = picked
            current_boards = list(pair)
            moves.append([actor, move])
            if len(moves) % 2:
                continue
            bases = pair_positions(r, moves)
            xm, ym = [values(e, base, r['A'], r['B'], 128) for base in bases]
            if xm is None or ym is None:
                break
            lead = statistics.median([xm['A']['scoreLead'], xm['B']['scoreLead'], ym['B']['scoreLead']])
            print(f"Balancing {r['id']}: {len(moves)} moves added (attempt {variant+1}/2)", flush=True)
            good = [xm['A']['winrate'], xm['B']['winrate'], ym['B']['winrate']]
            if not all(.25 <= v <= .75 for v in good) or not all(check_scores(xm, ym, loose=True).values()):
                continue
            vx, vy = [values(e, base, r['A'], r['B'], 1000) for base in bases]
            if vx is None or vy is None:
                break
            lead = statistics.median([vx['A']['scoreLead'], vx['B']['scoreLead'], vy['B']['scoreLead']])
            if all(quality(vx, vy)[1].values()):
                history = dict(r['history'], X=r['history']['X']+moves, Y=r['history']['Y']+moves)
                return dict(r, turn=r['turn']+len(moves), original_turn=history['source_turn'],
                            appended_moves=r.get('appended_moves', [])+moves, history=history,
                            board_X=stones(current_boards[0]), board_Y=stones(current_boards[1]), X=vx, Y=vy)
    return None


def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2)+'\n')
    temp.replace(path)


def duration(seconds):
    minutes = max(0, int(seconds / 60))
    return f'{minutes // 60}h {minutes % 60:02d}m' if minutes >= 60 else f'{minutes}m'


def progress_line(state, sources, elapsed, session_elapsed, initial_done):
    statuses = [state['completed'][sha] for sha in sources if sha in state['completed']]
    done, total = len(statuses), len(sources)
    remaining = total - done
    saved = sum(status in ('direct', 'balanced', 'accepted') for status in statuses)
    recent_done = done - initial_done
    eta = 'warming up'
    if remaining == 0:
        eta = '0m'
    elif recent_done >= 5 and session_elapsed >= 60:
        eta = '~' + duration(session_elapsed / recent_done * remaining)
    return (f'Games {done}/{total} | Remaining {remaining} | Samples {saved} | '
            f'Elapsed {duration(elapsed)} | ETA (rough) {eta}')


def save_sample(record, out):
    """Publish the JSON and both full-history SGFs together."""
    out.mkdir(parents=True, exist_ok=True)
    temp = out/(record['id']+'.tmp')
    if temp.exists():
        shutil.rmtree(temp)
    export_one(record, temp)
    dest = out/record['id']
    if dest.exists():
        shutil.rmtree(temp)
    else:
        temp.replace(dest)


def process_pair(e, record, out):
    if all(quality(record['X'], record['Y'])[1].values()):
        result = record
        status = record.get('_result_status', 'direct')
    else:
        print(f"Found {record['id']}; balancing now", flush=True)
        try:
            result = balance_pair(e, record)
        except MissingMoveError as exc:
            print(f"Skipping balance for {record['id']}: {exc}", flush=True)
            result = None
        status = 'balanced' if result is not None else 'balance_failed'
    if result is not None:
        # Keep the final record until the sample is published and state is saved.
        # A restart can finish this transaction without rerunning the engine.
        atomic_json(out/'pending.json', dict(result, _result_status=status))
        save_sample(result, out)
    print(f"{record['id']}: {status}", flush=True)
    return status


def run(args):
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    with (out/'worker.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another process is already using this output folder')
        run_locked(args, out)


def run_locked(args, out):
    state_path = out/'state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else dict(completed={})
    state.setdefault('rows', {})
    paths = sorted(args.games_dir.resolve().rglob('*.sgf'))
    if not paths:
        raise SystemExit(f'No SGF games found in {args.games_dir}')
    random.Random(104).shuffle(paths)
    games = {}
    for path in paths:
        games.setdefault(hashlib.sha256(path.read_bytes()).hexdigest(), path)
    initial_done = sum(sha in state['completed'] for sha in games)
    previous_elapsed = state.get('elapsed_seconds', 0)
    started = time.monotonic()
    last_report = started

    def checkpoint(force=False):
        nonlocal last_report
        now = time.monotonic()
        state['elapsed_seconds'] = previous_elapsed + now - started
        atomic_json(state_path, state)
        if force or now - last_report >= 30:
            print(progress_line(state, games, state['elapsed_seconds'], now-started, initial_done), flush=True)
            last_report = now

    checkpoint(force=True)
    atomic_json(out/'engine.json', provenance())
    e = None
    pending = out/'pending.json'
    try:
        # A stopped balancing attempt resumes from its saved extracted pair.
        if pending.exists():
            record = json.loads(pending.read_text())
            sha = record['source_sha256']
            if sha not in state['completed']:
                if not all(quality(record['X'], record['Y'])[1].values()):
                    e = Evaluator(out)
                status = process_pair(e, record, out)
                state['completed'][sha] = status
                state['rows'].pop(sha, None)
                checkpoint()
            pending.unlink()
        queue = deque()
        for sha, path in games.items():
            if sha in state['completed']:
                continue
            queue.append((path, sha, None))
        # Round-robin games, as in the original search: a difficult game should
        # not delay trying promising positions from every other game.
        while queue:
            path, sha, rows = queue.popleft()
            if rows is None:
                try:
                    rows = candidate_rows(path, random.Random(sha))
                except (ValueError, IndexError) as exc:
                    print(f'Skipping invalid SGF: {path.name}: {exc}', flush=True)
                    state['completed'][sha] = 'invalid_sgf'
                    state['rows'].pop(sha, None)
                    checkpoint()
                    continue
            row_index = state['rows'].get(sha, 0)
            status = 'no_pair'
            if row_index < len(rows):
                print(f'{path.name}: candidate {row_index+1}/{len(rows)}', flush=True)
                if e is None:
                    e = Evaluator(out)
                try:
                    record = extract_pair(e, rows[row_index])
                except MissingMoveError as exc:
                    print(f'Skipping candidate in {path.name}: {exc}', flush=True)
                    record = None
                if record is None:
                    state['rows'][sha] = row_index+1
                    checkpoint()
                    queue.append((path, sha, rows))
                    continue
                atomic_json(pending, record)
                status = process_pair(e, record, out)
            state['completed'][sha] = status
            state['rows'].pop(sha, None)
            checkpoint()
            pending.unlink(missing_ok=True)
        print('All input games processed. Add more SGFs and run the same command to continue.', flush=True)
    finally:
        if e is not None:
            e.close()
        checkpoint(force=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--games-dir', type=Path, default=PROJECT/'datasets/games')
    parser.add_argument('--out', type=Path, default=PROJECT/'outputs')
    args = parser.parse_args()
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        run(args)
    except KeyboardInterrupt:
        print('\nStopped. Run the same command to continue.', flush=True)


if __name__ == '__main__':
    main()
