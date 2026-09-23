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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sgf', type=Path)
    parser.add_argument('--output-dir', type=Path, default=PROJECT / 'outputs',
                        help='Output root; direct runs use output-dir/single/<run>/games/<game>')
    parser.add_argument('--resume-root', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--config', type=Path, default=PROJECT / 'configs/find-atari.json')
    parser.add_argument('--engine', type=Path, default=PROJECT / 'scripts/katago.sh')
    args = parser.parse_args()
    config = load_config(args.config)
    args.__dict__.update(vars(config))
    raw = args.sgf.read_bytes()
    game = sgf.Sgf_game.from_bytes(raw)
    if game.get_size() > 19:
        parser.error('This script supports boards up to 19x19')
    root = game.get_root()
    rules = args.rules or (root.get('RU') if root.has_property('RU') else None)
    komi = args.komi if args.komi is not None else (game.get_komi() if root.has_property('KM') else None)
    if rules is None or komi is None:
        parser.error('Missing SGF RU/KM: set rules and/or komi in the JSON config')
    rules = {'Japanese': 'japanese', 'Chinese': 'chinese', 'AGA': 'aga',
             'Korean': 'korean', 'New Zealand': 'new-zealand'}.get(rules, rules)
    board, plays = sgf_moves.get_setup_and_moves(game)
    nodes = game.get_main_sequence()
    if any(n.has_property('PL') for n in nodes[1:]):
        parser.error('Midgame PL edits unsupported; refusing inconsistent history')
    # Validate mainline before starting the expensive engine.
    check = board.copy()
    for color, move in plays:
        if move is not None:
            if check.get(*move) is not None:
                parser.error('Illegal occupied move in SGF')
            check.play(*move, color)
            if check.get(*move) != color:
                parser.error('Suicide SGF moves are unsupported')
    initial_player = root.get('PL') if root.has_property('PL') else (plays[0][0] if plays else 'b')
    for i, (color, _) in enumerate(plays):
        expected = initial_player if i % 2 == 0 else ('w' if initial_player == 'b' else 'b')
        if color != expected:
            parser.error('Non-alternating move history unsupported')
    metadata = game_metadata(root)
    digest = hashlib.sha256(raw).hexdigest()
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    if args.resume_root is None:
        run_root = args.output_dir / 'single' / run_id
        output = run_root / 'games' / output_name(metadata, digest)
    else:
        run_root = None
        output = args.resume_root / 'games' / output_name(metadata, digest)
    complete = output / '.complete'
    game_config = output / 'config.json'
    if (args.resume_root is not None and complete.is_file() and game_config.is_file()
            and json.loads(game_config.read_text()) == vars(config)):
        print(f'Skip complete: {output}', flush=True)
        return
    output.mkdir(parents=True, exist_ok=args.resume_root is not None)
    complete.unlink(missing_ok=True)
    (output / 'stop.json').unlink(missing_ok=True)
    if run_root is None:
        game_config.write_text(json.dumps(vars(config), ensure_ascii=False, indent=2) + '\n')
    else:
        (run_root / 'config.json').write_text(json.dumps(vars(config), ensure_ascii=False, indent=2) + '\n')
    (output / 'metadata.json').write_text(json.dumps(dict(sgf=metadata, source=str(args.sgf.resolve()),
                                                        source_sha256=digest, effective_rules=rules,
                                                        effective_komi=komi), ensure_ascii=False, indent=2) + '\n')
    initial = [[c.upper(), gtp(p)] for c, p in board.list_occupied_points()]
    history, records = [], []
    engine = Engine(args.engine.resolve(), output / 'engine.log', args.query_timeout_seconds, args.shutdown_timeout_seconds)
    try:
        with open(output / 'samples.jsonl', 'w') as samples:
            for node in nodes:
                color, move = node.get_move()
                if color is not None:
                    if move is not None:
                        board.play(*move, color)
                    history.append([color.upper(), gtp(move)])
                turn = len(history)
                if turn < args.start or (color is None and node is not root):
                    continue
                if args.end is not None and turn > args.end:
                    break
                # Do not sample finished games, including positions after two passes.
                if turn >= len(plays) or (turn >= 2 and history[-1][1] == history[-2][1] == 'pass'):
                    continue
                player = plays[turn][0]
                print(f'After move {turn}: checking position winrate', flush=True)
                base = {'moves': history, 'initialStones': initial, 'initialPlayer': initial_player.upper(),
                        'rules': rules, 'komi': komi, 'boardXSize': board.side, 'boardYSize': board.side,
                        'includePolicy': True, '_player': player.upper()}
                analysis = engine.query(base, args.visits)
                position_winrate = analysis['rootInfo']['winrate']
                if should_stop(position_winrate, config):
                    print(f'  Stop game: position winrate {position_winrate:.1%}; earlier samples retained', flush=True)
                    (output / 'stop.json').write_text(json.dumps({
                        'reason': 'stop_winrate_min', 'move_number': turn, 'player': player.upper(),
                        'position_winrate': position_winrate,
                        'lower_winrate': min(position_winrate, 1 - position_winrate),
                        'threshold': args.stop_winrate_min,
                        'samples_retained': len(records)}, indent=2) + '\n')
                    break
                if not in_winrate_range(position_winrate, config):
                    print(f'  Skip: position winrate {position_winrate:.1%}', flush=True)
                    continue
                all_atari = ataris(board, player)
                candidates = {m: [t for t in ts if len(t['stones']) >= args.min_target_stones]
                              for m, ts in all_atari.items()}
                candidates = {m: ts for m, ts in candidates.items() if ts}
                if not candidates:
                    continue
                # Policy is negative for illegal moves, including history-dependent ko bans.
                policy = analysis['policy']
                candidates = {m: ts for m, ts in candidates.items()
                              if policy[(board.side - 1 - point_of(m)[0]) * board.side + point_of(m)[1]] >= 0}
                if not candidates:
                    continue
                ranked = sorted(analysis['moveInfos'], key=lambda m: m['order'])
                top_move = ranked[0]['move']
                non_atari = [m['move'] for m in ranked if m['move'] not in all_atari and m['move'] != 'pass'
                             and any(distance(m['move'], a) >= args.min_ab_distance for a in candidates)]
                if not non_atari:
                    allowed = [gtp((r, c)) for r in range(board.side) for c in range(board.side)
                               if policy[(board.side-1-r)*board.side+c] >= 0
                               and gtp((r, c)) not in all_atari
                               and any(distance(gtp((r, c)), a) >= args.min_ab_distance for a in candidates)]
                    if not allowed:
                        continue
                    alternatives = engine.query(base, args.visits, allowed)
                    non_atari = [m['move'] for m in sorted(alternatives['moveInfos'], key=lambda m: m['order'])]
                # Give every A and B the same forced-root budget; never treat an unsearched A as bad.
                moves_to_check = list(dict.fromkeys([top_move] + list(candidates) + non_atari[:args.b_pool]))
                evaluated = {}
                for m in moves_to_check:
                    result = engine.query(base, args.visits, [m])
                    match = next((x for x in result['moveInfos'] if x['move'] == m), None)
                    if match is not None:
                        evaluated[m] = match
                if not evaluated:
                    continue
                aa, bb, best = choose(evaluated, candidates, top_move, args.max_loss, args.b_count,
                                      args.strict_not_top, args.min_ab_distance, all_atari)
                if not aa:
                    continue
                record = {'id': f'{len(records)+1:05d}', 'source': str(args.sgf.resolve()),
                          'source_sha256': hashlib.sha256(raw).hexdigest(), 'move_number': turn,
                          'game_metadata': metadata, 'config': vars(config),
                          'position_winrate': position_winrate, 'min_ab_distance': args.min_ab_distance,
                          'player': player.upper(), 'moves': list(history), 'initial_stones': initial,
                          'board_stones': [[c.upper(), gtp(p)] for c, p in board.list_occupied_points()],
                          'board_size': board.side, 'rules': rules, 'komi': komi,
                          'sgf_rules': root.get('RU') if root.has_property('RU') else None,
                          'visits_per_move': args.visits, 'max_loss': args.max_loss,
                          'top_move': top_move, 'score_perspective': 'player_to_move',
                          'unrestricted_analysis': ranked, 'evaluated_moves': evaluated,
                          'status': 'pending_manual_review'}
                for category, infos in [('A', aa), ('B', bb)]:
                    record[category] = [dict(info, loss_points=best-info['scoreLead'],
                                            targets=candidates.get(info['move'], [])) for info in infos]
                annotate(node, record)
                records.append(record)
                samples.write(json.dumps(record, ensure_ascii=False) + '\n')
                samples.flush()
                (output / 'annotated.sgf').write_bytes(game.serialise())
                print(f"  Saved {record['id']}: A={','.join(m['move'] for m in aa)} B={','.join(m['move'] for m in bb)}", flush=True)
                if args.max_samples and len(records) >= args.max_samples:
                    break
    finally:
        engine.close()
        (output / 'annotated.sgf').write_bytes(game.serialise())
    complete.write_text('')
    print(f'Done: {len(records)} positions -> {output}')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError) as exc:
        sys.exit(str(exc))
