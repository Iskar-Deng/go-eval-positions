#!/usr/bin/env python3
"""Search X/Y pairs using A/B score leads or winrates and single-stone edits.

Local persistence is a source-selection heuristic, never an acceptance gate.
Full move histories are used by default. Values are for the initial player.
"""
import argparse
from collections import Counter, defaultdict, deque
import hashlib
import json
from pathlib import Path
import random
import shutil
import time

from sgfmill import sgf, sgf_moves
from find_atari import PROJECT, ataris, point_of
from engine_runtime import model_path, config_path, provenance
from search_pairs import (Evaluator, export_board, legal, make_board, metric,
                          position, relocate, stones)


CRITERIA = {
    'version': '2026-10-03-endpoints-v2',
    'X_AB_max_winrate_difference': .12,
    'Y_B_minus_A_min_winrate': .20,
    'each_of_XA_XB_YB_minus_YA_min_winrate': .20,
    'B_cross_position_max_winrate_difference': .30,
    'B_cross_position_preferred_winrate_difference': .20,
    'atari': {'X': {'A': True, 'B': False}, 'Y': {'A': False, 'B': False}},
    'edit': 'exactly one relocated stone, same color counts',
    'diagnostic_only': ['scoreLead', 'root winrate', 'local persistence', 'life', 'reply behavior'],
}

SCORE_CRITERIA = {
    'version': '2026-10-03-score-first-v1',
    'X_AB_max_score_difference': 2.,
    'Y_B_minus_A_min_score': 4.,
    'B_cross_position_max_score_difference': 3.,
    'increase_in_A_loss_vs_B_min_score': 3.,
    'atari': CRITERIA['atari'], 'edit': CRITERIA['edit'],
    'diagnostic_only': ['winrate', 'root value', 'local persistence', 'life', 'reply behavior'],
    'purpose': 'find local material for later common-context balancing; exploratory tolerances',
}


def check_scores(x, y, loose=False):
    tol, gap, drift, increase = (3.5, 2., 5., 1.5) if loose else (2., 4., 3., 3.)
    x_loss = x['B']['scoreLead'] - x['A']['scoreLead']
    y_loss = y['B']['scoreLead'] - y['A']['scoreLead']
    return {'X_AB_close': abs(x_loss) <= tol + 1e-12,
            'Y_B_better': y_loss >= gap - 1e-12,
            'B_stable': abs(x['B']['scoreLead'] - y['B']['scoreLead']) <= drift + 1e-12,
            'A_loss_increases': y_loss - x_loss >= increase - 1e-12}


def within_move_budget(row, max_move):
    # The candidate A/B itself occupies one move in the model input.
    return row['turn'] + 1 <= max_move


def check_values(x, y, loose=False):
    # Historical winrate screen; current searches default to check_scores.
    tol, gap, drift = (.20, .12, .40) if loose else (.12, .20, .30)
    checks = {
        'X_AB_close': abs(x['A']['winrate'] - x['B']['winrate']) <= tol + 1e-12,
        'Y_B_better': y['B']['winrate'] - y['A']['winrate'] >= gap - 1e-12,
        'three_good_above_YA': min(x['A']['winrate'], x['B']['winrate'], y['B']['winrate']) - y['A']['winrate'] >= gap - 1e-12,
        'B_stable': abs(x['B']['winrate'] - y['B']['winrate']) <= drift + 1e-12,
    }
    return checks


def check_structure(x, y, player, a, b):
    xs, ys = dict((p, c) for c, p in stones(x)), dict((p, c) for c, p in stones(y))
    diff = [p for p in xs.keys() | ys.keys() if xs.get(p) != ys.get(p)]
    same_counts = Counter(xs.values()) == Counter(ys.values())
    relocation = len(diff) == 2 and same_counts and sum(p in xs and p not in ys for p in diff) == 1
    xa, ya = ataris(x, player.lower()), ataris(y, player.lower())
    labels = {'X': {'A': a in xa, 'B': b in xa}, 'Y': {'A': a in ya, 'B': b in ya}}
    valid = x.side == y.side and a != b and relocation and labels == CRITERIA['atari']
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


def ordered_seeds(rows, seed):
    # Round-robin games rather than exhausting edits of a single local pattern.
    rng = random.Random(seed)
    groups = defaultdict(list)
    for row in rows:
        groups[row['source']].append(row)
    names = sorted(groups)
    rng.shuffle(names)
    queues = []
    for name in names:
        rng.shuffle(groups[name])
        queues.append(deque(groups[name]))
    while any(queues):
        for queue in queues:
            if queue:
                yield queue.popleft()


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


def save_sample(out, row, x, y, a, b, edit, xm, ym, visits, evidence=None,
                value_metric='winrate', max_move=None, history=None):
    structure = check_structure(x, y, row['player'], a, b)
    checker = check_scores if value_metric == 'scoreLead' else check_values
    criteria = dict(SCORE_CRITERIA if value_metric == 'scoreLead' else CRITERIA)
    checks = checker(xm, ym)
    if max_move is not None:
        assert within_move_budget(row, max_move)
        criteria['max_move_including_candidate'] = max_move
    assert structure['valid'] and all(checks.values())
    identity = [stones(x), stones(y), row['player'], a, b]
    ident = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    dest = out / ident
    dest.mkdir(exist_ok=True)
    drift = abs(xm['B']['winrate'] - ym['B']['winrate'])
    record = {'id': ident, 'status': 'score_candidate_needs_balance' if value_metric == 'scoreLead' else 'engine_validated', 'criteria': criteria,
              'source': row['source'], 'source_sha256': row['source_sha256'], 'turn': row['turn'],
              'player': row['player'], 'A': a, 'B': b, 'edit': edit,
              'X': xm, 'Y': ym, 'board_X': stones(x), 'board_Y': stones(y),
              'board_size': x.side, 'rules': 'chinese', 'komi': 7.5,
              'structure': structure, 'checks': checks, 'visits': visits,
              'drift_tier': 'preferred' if drift <= .20 + 1e-12 else 'extended',
              'B_winrate_drift': drift, 'perspective': 'initial player for all four values',
              'protocol': 'setup_snapshot_no_prior_history', 'previous_evidence': evidence}
    record.update(value_metric=value_metric, candidate_move_number=row['turn'] + 1,
                  moves_remaining_after_candidate=None if max_move is None else max_move - row['turn'] - 1)
    if value_metric == 'scoreLead':
        record['drift_tier'] = 'not_used_for_score_screen'
    if history is not None:
        record.update(history=history, protocol='full_history_single_historical_move_edit')
    (dest / 'sample.json').write_text(json.dumps(record, indent=2) + '\n')
    for label, board in [('X', x), ('Y', y)]:
        if history is not None:
            from full_history import write_sgf
            write_sgf(dest / f'{label}.sgf', history, label, labels={'A': a, 'B': b})
        else:
            export_board(dest / f'{label}.sgf', board, row['player'].lower(), a, b, 'chinese', 7.5,
                         f'{ident} {label}; {criteria["version"]}; {visits} visits; all values for initial player')
    return record


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', type=Path, default=PROJECT / 'outputs/auto_pairs/persistent-atari-100/single-stone-candidates.jsonl')
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--max-seeds', type=int, default=90)
    ap.add_argument('--per-seed', type=int, default=4)
    ap.add_argument('--max-bs', type=int, default=3)
    ap.add_argument('--screen-visits', type=int, default=96)
    ap.add_argument('--verify-visits', type=int, default=1000)
    ap.add_argument('--minutes', type=float, default=8)
    ap.add_argument('--max-passed', type=int, default=5)
    ap.add_argument('--seed', type=int, default=103)
    ap.add_argument('--cache-from', type=Path, action='append', default=[])
    ap.add_argument('--metric', choices=['scoreLead', 'winrate'], default='scoreLead')
    ap.add_argument('--max-move', type=int, default=150)
    ap.add_argument('--resume', action='store_true')
    ap.add_argument('--exclude-games', type=Path)
    ap.add_argument('--history-mode', choices=['full', 'snapshot'], default='full')
    args = ap.parse_args()
    out = args.out
    out.mkdir(parents=True, exist_ok=args.resume)
    existing = [json.loads(p.read_text()) for p in out.glob('*/sample.json')]
    if existing and not args.resume:
        raise ValueError('Existing results require --resume')
    excluded = set(json.loads(args.exclude_games.read_text())) if args.exclude_games else set()
    done_path = out / 'completed-seeds.jsonl'
    completed = {tuple(json.loads(line)) for line in done_path.read_text().splitlines()} if done_path.exists() else set()
    (out / 'config.json').write_text(json.dumps(vars(args), default=str, indent=2))
    criteria = dict(SCORE_CRITERIA if args.metric == 'scoreLead' else CRITERIA)
    criteria['max_move_including_candidate'] = args.max_move
    checker = check_scores if args.metric == 'scoreLead' else check_values
    if args.resume and (out / 'criteria.json').exists():
        assert json.loads((out / 'criteria.json').read_text()) == criteria, 'Resume criteria changed'
    (out / 'criteria.json').write_text(json.dumps(criteria, indent=2))
    for path in [Path(__file__), PROJECT / 'scripts/search_pairs.py', PROJECT / 'scripts/full_history.py', config_path()]:
        shutil.copy2(path, out / path.name)
    (out / 'provenance.json').write_text(json.dumps({**provenance(),
        'input_sha256': hashlib.sha256(args.input.read_bytes()).hexdigest(),
        'screen_is_heuristic': True,
        'protocol': 'full_history_single_historical_move_edit' if args.history_mode == 'full' else 'setup_snapshot_no_prior_history'}, indent=2))
    rows = [json.loads(line) for line in args.input.read_text().splitlines() if line.strip()]
    total_rows = len(rows)
    rows = [row for row in rows if within_move_budget(row, args.max_move)]
    counts, start, accepted_sources = Counter(), time.monotonic(), set()
    counts['input_rows'] = total_rows
    counts['within_move_budget'] = len(rows)
    counts['passed'] = len(existing)
    accepted_sources.update(r['source'] for r in existing)
    def timed_out():
        return args.minutes > 0 and time.monotonic() - start >= args.minutes * 60
    e = Evaluator(out)
    for cache_path in args.cache_from:
        for line in cache_path.read_text().splitlines():
            cached = json.loads(line)
            key = json.dumps([cached['request'], cached['visits'], cached['allowed']], sort_keys=True)
            e.cache[key] = cached['result']
    journal = (out / 'candidates.jsonl').open('a')
    try:
        for index, row in enumerate(ordered_seeds(rows, args.seed)):
            if (args.max_seeds > 0 and index >= args.max_seeds) or timed_out() or counts['passed'] >= args.max_passed:
                break
            seed_key = (row['source_sha256'], row['turn'], row['A'], tuple(row['target']['stones']))
            # JSON checkpoint uses a string for the target to keep keys hashable.
            seed_key = (seed_key[0], seed_key[1], seed_key[2], ','.join(seed_key[3]))
            if row['source'] in accepted_sources or row['source_sha256'] in excluded or seed_key in completed:
                continue
            counts['seeds'] += 1
            x = replay(row)
            player, a = row['player'].lower(), row['A']
            xp = position(x, player, 'chinese', 7.5)
            if args.history_mode == 'full':
                from full_history import source_history, board_stones, query_position, construct
                try:
                    initial, moves, first, _, _ = source_history(row)
                    xp = query_position(dict(initial_stones=board_stones(initial), initial_player=first, X=moves), 'X')
                except ValueError:
                    counts['invalid_X_history'] += 1
                    continue
            xr = e.query(xp, args.screen_visits)
            if not legal(xr, a, x.side):
                counts['X_A_illegal'] += 1
                continue
            xa = metric(e.query(xp, args.screen_visits, [a]), a)
            for edit in row['edits'][:args.per_seed]:
                if timed_out():
                    break
                y = relocate(x, point_of(edit['from']), point_of(edit['to']))
                if y is None:
                    continue
                counts['Y_examined'] += 1
                yp = position(y, player, 'chinese', 7.5)
                history = None
                if args.history_mode == 'full':
                    try:
                        history = construct(row, edit, x, y)
                        yp = query_position(history, 'Y')
                    except ValueError:
                        counts['invalid_Y_history'] += 1
                        continue
                yr = e.query(yp, args.screen_visits)
                if not legal(yr, a, y.side):
                    counts['Y_A_illegal'] += 1
                    continue
                ya = metric(e.query(yp, args.screen_visits, [a]), a)
                if args.metric == 'winrate' and xa['winrate'] - ya['winrate'] < .12:
                    counts['screen_small_A_contrast'] += 1
                    journal.write(json.dumps({'stage': 'heuristic_A_contrast', 'source': row['source'],
                        'turn': row['turn'], 'A': a, 'edit': edit, 'X_A': xa, 'Y_A': ya}) + '\n')
                    journal.flush()
                    continue
                # Low-budget root value gives a cheap estimate of the best attainable B.
                # This screen is not a certified bound and is saved for later false-negative audits.
                if metric(yr)[args.metric] - ya[args.metric] < (1. if args.metric == 'scoreLead' else .08):
                    counts['screen_small_Y_loss'] += 1
                    journal.write(json.dumps({'stage': 'heuristic_Y_loss', 'source': row['source'],
                        'turn': row['turn'], 'A': a, 'edit': edit, 'Y_root': metric(yr), 'Y_A': ya}) + '\n')
                    journal.flush()
                    continue
                for b in candidate_bs(xr, yr, x, y, player, a, args.max_bs):
                    structure = check_structure(x, y, player, a, b)
                    if not structure['valid']:
                        counts['structural_failure'] += 1
                        continue
                    xm = {'A': xa, 'B': metric(e.query(xp, args.screen_visits, [b]), b)}
                    ym = {'A': ya, 'B': metric(e.query(yp, args.screen_visits, [b]), b)}
                    checks = checker(xm, ym, loose=True)
                    counts['AB_screens'] += 1
                    event = {'source': row['source'], 'turn': row['turn'], 'A': a, 'B': b,
                             'edit': edit, 'X': xm, 'Y': ym, 'checks': checks, 'stage': 'screen'}
                    journal.write(json.dumps(event) + '\n')
                    journal.flush()
                    if not all(checks.values()):
                        counts.update('screen_' + k for k, v in checks.items() if not v)
                        continue
                    vx, vy = values(e, xp, a, b, args.verify_visits), values(e, yp, a, b, args.verify_visits)
                    counts['verified'] += 1
                    if vx is None or vy is None:
                        counts['verify_illegal'] += 1
                        continue
                    checks = checker(vx, vy)
                    event.update(stage='verify', X=vx, Y=vy, checks=checks)
                    journal.write(json.dumps(event) + '\n')
                    journal.flush()
                    if all(checks.values()):
                        record = save_sample(out, row, x, y, a, b, edit, vx, vy, args.verify_visits,
                                             value_metric=args.metric, max_move=args.max_move, history=history)
                        counts['passed'] += 1
                        accepted_sources.add(row['source'])
                        print(json.dumps({'passed': record['id'], 'A': a, 'B': b, 'edit': edit,
                            'turn': row['turn'], 'metric': args.metric,
                            'values': {k: {m: v[m][args.metric] for m in ['A', 'B']} for k, v in [('X', vx), ('Y', vy)]}}), flush=True)
                        break
                    counts.update('verify_' + k for k, v in checks.items() if not v)
                if row['source'] in accepted_sources:
                    break
            print(json.dumps({'progress': dict(counts), 'queries': e.queries,
                              'seconds': round(time.monotonic() - start, 1)}), flush=True)
            if not timed_out():
                with done_path.open('a') as done:
                    done.write(json.dumps(seed_key) + '\n')
                completed.add(seed_key)
    finally:
        summary = dict(counts, queries=e.queries, seconds=time.monotonic() - start,
                       distinct_passed_games=len(accepted_sources))
        (out / 'summary.json').write_text(json.dumps(summary, indent=2))
        journal.close()
        e.close()
        print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
