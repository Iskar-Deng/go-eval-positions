#!/usr/bin/env python3
"""Mine main-line SGF positions; annotate hypothetical ataris and alternatives."""
import argparse
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path
import queue
import subprocess
import sys
import threading

from sgfmill import sgf, sgf_moves

PROJECT = Path(__file__).resolve().parents[1]


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
    def __init__(self, launcher, log, timeout, shutdown_timeout):
        self.timeout = timeout
        self.shutdown_timeout = shutdown_timeout
        self.log = open(log, 'w')
        self.process = subprocess.Popen([str(launcher), 'analysis'], stdin=subprocess.PIPE,
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


def distance(a, b):
    p, q = point_of(a), point_of(b)
    return abs(p[0]-q[0]) + abs(p[1]-q[1])


def choose(evaluated, candidates, top_move, gap, count_b, strict, min_distance=0,
           all_atari_moves=None):
    best_score = max(m['scoreLead'] for m in evaluated.values())
    good_b = sorted((m for name, m in evaluated.items()
                     if name not in (all_atari_moves if all_atari_moves is not None else candidates) and name != 'pass'
                     and best_score - m['scoreLead'] <= gap),
                    key=lambda m: -m['scoreLead'])
    good_a = sorted((m for name, m in evaluated.items()
                     if name in candidates and best_score - m['scoreLead'] <= gap
                     and (not strict or name != top_move)),
                    key=lambda m: (m['move'] == top_move, -m['scoreLead']))
    # Prefer a non-top A; every displayed A/B pair must meet the distance bound.
    for seed in good_a:
        bb = [b for b in good_b if distance(seed['move'], b['move']) >= min_distance][:count_b]
        if bb:
            aa = [a for a in good_a if all(distance(a['move'], b['move']) >= min_distance for b in bb)]
            return aa, bb, best_score
    return [], [], best_score


def in_winrate_range(winrate, config):
    lower = min(winrate, 1 - winrate)
    return lower >= config.winrate_min or math.isclose(lower, config.winrate_min, rel_tol=0, abs_tol=1e-12)


def should_stop(winrate, config):
    lower = min(winrate, 1 - winrate)
    return lower <= config.stop_winrate_min or math.isclose(lower, config.stop_winrate_min, rel_tol=0, abs_tol=1e-12)


def load_config(path):
    data = json.loads(path.read_text())
    required = {'rules', 'komi', 'visits', 'max_loss', 'b_count', 'b_pool', 'strict_not_top',
                'start', 'end', 'max_samples', 'min_ab_distance', 'min_target_stones',
                'winrate_min', 'stop_winrate_min', 'query_timeout_seconds', 'shutdown_timeout_seconds'}
    if set(data) != required:
        raise ValueError(f'Config fields: missing={required-set(data)}, unknown={set(data)-required}')
    for key in ('visits', 'b_count', 'b_pool', 'min_target_stones'):
        if type(data[key]) is not int or data[key] < 1:
            raise ValueError(f'{key} must be a positive integer')
    for key in ('start', 'min_ab_distance'):
        if type(data[key]) is not int or data[key] < 0:
            raise ValueError(f'{key} must be a nonnegative integer')
    for key in ('end', 'max_samples'):
        if data[key] is not None and (type(data[key]) is not int or data[key] < (1 if key == 'max_samples' else data['start'])):
            raise ValueError(f'Invalid {key}')
    for key in ('max_loss', 'winrate_min', 'stop_winrate_min', 'query_timeout_seconds', 'shutdown_timeout_seconds'):
        if type(data[key]) not in (int, float) or not math.isfinite(data[key]) or data[key] < 0:
            raise ValueError(f'Invalid {key}')
    if not 0 <= data['winrate_min'] <= 0.5:
        raise ValueError('Require 0 <= winrate_min <= 0.5')
    if not 0 <= data['stop_winrate_min'] < data['winrate_min']:
        raise ValueError('Require 0 <= stop_winrate_min < winrate_min')
    if min(data['query_timeout_seconds'], data['shutdown_timeout_seconds']) <= 0:
        raise ValueError('Timeouts must be positive')
    if type(data['strict_not_top']) is not bool:
        raise ValueError('strict_not_top must be boolean')
    if data['rules'] is not None and (not isinstance(data['rules'], str) or not data['rules'].strip()):
        raise ValueError('rules must be null or a nonempty string')
    if data['komi'] is not None and (type(data['komi']) not in (int, float) or not math.isfinite(data['komi'])):
        raise ValueError('komi must be null or a finite number')
    return SimpleNamespace(**data)


def game_metadata(root):
    return {key: root.get(key) if root.has_property(key) else None
            for key in ('DT', 'EV', 'RO', 'PB', 'PW', 'GN', 'RE', 'RU', 'KM')}


def output_name(metadata, digest):
    def safe(value):
        return re.sub(r'[^\w.-]+', '_', str(value), flags=re.UNICODE).strip('._')[:40] or 'unknown'
    return '__'.join(safe(metadata.get(key) or 'unknown') for key in ('DT', 'EV', 'RO', 'PB', 'PW')) + '__' + digest[:10]


def annotate(node, record):
    labels = dict(node.get('LB')) if node.has_property('LB') else {}
    lines = [f"ATARI_SAMPLE {record['id']} | after move {record['move_number']} | {record['player']} to play",
             f"KataGo top: {record['top_move']} | rules={record['rules']} komi={record['komi']}"]
    if 'position_winrate' in record:
        lines.append(f"Position winrate: {record['position_winrate']:.1%} | minimum A-B Manhattan distance: {record['min_ab_distance']}")
    for category in ('A', 'B'):
        for index, item in enumerate(record[category], 1):
            label = category if category == 'A' and len(record['A']) == 1 else f'{category}{index}'
            p = point_of(item['move'])
            labels[p] = label
            lines.append(f"{label}: {item['move']} | loss={item['loss_points']:.2f} points | "
                         f"winrate={item['winrate']:.1%} | visits={item['visits']}"
                         + (' | TOP MOVE (alternative exists)' if item['move'] == record['top_move'] else ''))
            if category == 'A':
                for target in item['targets']:
                    lines.append(f"  target {target['color']} ({target['stone_count']} stones): "
                                 + ','.join(target['stones']) + ': liberties '
                                 + ','.join(target['liberties_before']) + ' -> '
                                 + ','.join(target['liberties_after']))
    node.set('LB', list(labels.items()))
    old = node.get('C') if node.has_property('C') else ''
    node.set('C', old + ('\n\n' if old else '') + '\n'.join(lines))
