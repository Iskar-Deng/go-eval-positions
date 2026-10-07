#!/usr/bin/env python3
"""Find balanced positions where an atari and the local saving reply are both costly."""
import argparse
from collections import Counter
import fcntl
import hashlib
import json
from pathlib import Path
import random
import signal
import time

import generate as g


def candidate_rows(path):
    game = g.sgf.Sgf_game.from_bytes(path.read_bytes())
    if game.get_size() != 19:
        return []
    rows, seen = [], set()
    for record in g.scan(path, min_remote_moves=0, max_turn=148):
        for turn in record['sample_turns']:
            key = (turn, record['A'])
            if key in seen:
                continue
            seen.add(key)
            rows.append({k: record[k] for k in ['source', 'source_sha256', 'player', 'A', 'target']} | {'turn': turn})
    random.Random(hashlib.sha256(path.read_bytes()).hexdigest()).shuffle(rows)
    return rows


def best_move(result):
    return min(result['moveInfos'], key=lambda m: m['order'])['move']


def distance(move, anchors):
    point = g.point_of(move)
    return -1 if point is None else min(g.md(point, g.point_of(anchor)) for anchor in anchors)


def search(e, row, counts):
    initial, moves, first, board, _ = g.source_history(row)
    player = row['player']
    defender = 'W' if player == 'B' else 'B'
    a = row['A']
    targets = g.ataris(board, player.lower(), 1).get(a, [])
    if len(targets) != 1 or targets[0]['stone_count'] < 2:
        counts['complex_or_small_target'] += 1
        return None
    target = targets[0]
    c = target['liberties_after'][0]
    history = dict(initial_stones=g.board_stones(initial), initial_player=first, X=moves)
    saved, _, _ = g.replay_checked(initial, moves+[[player, a], [defender, c]], first)
    stones = [g.point_of(m) for m in target['stones']]
    if any(saved.get(*p) != defender.lower() for p in stones) or len(g.group(saved, stones[0])[1]) < 2:
        counts['reply_does_not_save'] += 1
        return None
    base = g.query_position(history, 'X')
    reply_base = g.query_position(history, 'X', [[player, a]])
    root = e.query(base, 64)
    if not .20 <= root['rootInfo']['winrate'] <= .80 or not g.legal(root, a, 19):
        counts['screen_unbalanced_or_illegal'] += 1
        return None
    b = best_move(root)
    va = g.metric(e.query(base, 64, [a]), a)
    if g.metric(root, b)['scoreLead'] - va['scoreLead'] < 3:
        counts['atari_not_bad_enough'] += 1
        return None
    reply = e.query(reply_base, 64)
    d = best_move(reply)
    anchors = target['stones'] + [a, c]
    if distance(d, anchors) < 4 or not g.legal(reply, c, 19):
        counts['best_reply_not_remote'] += 1
        return None
    vc = g.metric(e.query(reply_base, 64, [c]), c)
    if g.metric(reply, d)['scoreLead'] - vc['scoreLead'] < 3:
        counts['saving_not_bad_enough'] += 1
        return None

    # Final selection and every forced candidate are evaluated at 1,000 visits.
    root = e.query(base, 1000)
    b = best_move(root)
    vb = g.metric(e.query(base, 1000, [b]), b)
    if not .30 <= vb['winrate'] <= .70 or not .30 <= root['rootInfo']['winrate'] <= .70:
        counts['final_unbalanced'] += 1
        return None
    va = g.metric(e.query(base, 1000, [a]), a)
    if vb['scoreLead'] - va['scoreLead'] < 4:
        counts['final_atari_gap'] += 1
        return None
    reply = e.query(reply_base, 1000)
    d = best_move(reply)
    if distance(d, anchors) < 4 or not g.legal(reply, c, 19):
        counts['final_reply_not_remote'] += 1
        return None
    vc = g.metric(e.query(reply_base, 1000, [c]), c)
    vd = g.metric(e.query(reply_base, 1000, [d]), d)
    if vd['scoreLead'] - vc['scoreLead'] < 4:
        counts['final_reply_gap'] += 1
        return None
    ignored, _, _ = g.replay_checked(initial, moves+[[player, a], [defender, d]], first)
    if any(ignored.get(*p) != defender.lower() for p in stones) or g.group(ignored, stones[0])[1] != {g.point_of(c)}:
        counts['remote_move_removed_threat'] += 1
        return None
    captured, _, _ = g.replay_checked(initial, moves+[[player, a], [defender, d], [player, c]], first)
    if any(captured.get(*p) == defender.lower() for p in stones):
        counts['capture_failed'] += 1
        return None
    # Also replay the alternative B, so all exported choices are legal with history.
    g.replay_checked(initial, moves+[[player, b]], first)
    ident = hashlib.sha256(json.dumps([row['source_sha256'], row['turn'], a]).encode()).hexdigest()[:12]
    return dict(id=ident, source_sha256=row['source_sha256'], source_game=str(Path(row['source']).relative_to(g.PROJECT/'datasets/games'))
                if Path(row['source']).is_relative_to(g.PROJECT/'datasets/games') else Path(row['source']).name,
                move_number=row['turn'], player=player, defender=defender, moves={'A': a, 'B': b},
                replies={'save': c, 'best': d}, target_stones=target['stones'],
                before_A={'A': va, 'B': vb}, after_A={'save': vc, 'best': vd},
                score_gaps={'B_minus_A': vb['scoreLead']-va['scoreLead'], 'best_minus_save': vd['scoreLead']-vc['scoreLead']},
                remote_distance=distance(d, anchors), capture_after_ignored_atari=c,
                rules='Chinese', komi=7.5, visits=1000, history=history)


def sample_json(record):
    values = record['before_A']
    stones = record['target_stones']
    return dict(
        source_sha256=record['source_sha256'],
        move_number=record['move_number'],
        moves={key: record['moves'][key] for key in ['A', 'B']},
        results={'X': {
            'winrate': {key: values[key]['winrate'] for key in ['A', 'B']},
            'score_lead': {key: values[key]['scoreLead'] for key in ['A', 'B']}}},
        X_A_targets=[dict(size=len(stones), stones=stones)])


def export(record, out):
    ident = record['id']
    dest = out/ident
    if dest.exists():
        return
    temp = out/(ident+'.tmp')
    temp.mkdir(exist_ok=True)
    g.write_sgf(temp/'X.sgf', record['history'], 'X')
    g.atomic_json(temp/'sample.json', sample_json(record))
    # A temporary folder may remain from the earlier export format.
    for name in ['after_A.sgf', 'review.sgf']:
        (temp/name).unlink(missing_ok=True)
    temp.replace(dest)


def read_sample(path):
    sample = json.loads(path.read_text())
    if 'before_A' in sample and 'after_A' in sample:
        # Simplify pilot outputs on resume without rerunning the engine.
        sample = sample_json(sample)
        g.atomic_json(path, sample)
    if set(sample.get('results', {})) != {'X'} or set(sample.get('moves', {})) != {'A', 'B'}:
        raise SystemExit('Output folder contains samples from another experiment')
    for name in ['after_A.sgf', 'review.sgf']:
        (path.parent/name).unlink(missing_ok=True)
    return sample


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
    path = out/'state.json'
    state = json.loads(path.read_text()) if path.exists() else dict(
        mode='addition', completed={}, rows={}, accepted={}, counts={}, seconds=0, queries=0)
    if state.get('mode') != 'addition':
        # Allow resuming the original pilot, whose state had no mode field.
        if 'mode' in state or not all(k in state for k in ['accepted', 'counts', 'seconds', 'queries']):
            raise SystemExit('Use a separate output folder for the additional experiment')
        state['mode'] = 'addition'
    paths = sorted(args.games_dir.resolve().rglob('*.sgf'))
    if not paths:
        raise SystemExit(f'No SGF games found in {args.games_dir}')
    print('Indexing input games...', flush=True)
    random.Random(104).shuffle(paths)
    games = {}
    for source in paths:
        games.setdefault(hashlib.sha256(source.read_bytes()).hexdigest(), source)

    # Recover samples published immediately before an interruption.
    for sample_path in out.glob('*/sample.json'):
        if sample_path.parent.name.endswith('.tmp'):
            continue
        sample = read_sample(sample_path)
        sha = sample['source_sha256']
        state['accepted'][sample_path.parent.name] = sha
        state['completed'][sha] = 'accepted'
        state['rows'].pop(sha, None)

    counts = Counter(state['counts'])
    started = last_report = time.monotonic()
    prior = state['seconds']
    initial_done = sum(sha in state['completed'] for sha in games)
    e = None
    pending = out/'pending.json'

    def checkpoint(force=False):
        nonlocal last_report
        now = time.monotonic()
        state['counts'] = dict(counts)
        state['seconds'] = prior + now - started
        g.atomic_json(path, state)
        if force or now - last_report >= 30:
            done = sum(sha in state['completed'] for sha in games)
            remaining = len(games) - done
            finished = done - initial_done
            eta = g.duration(remaining * (now-started) / finished) if finished >= 5 else 'warming up'
            if remaining == 0:
                eta = '0m'
            print(f'Games {done}/{len(games)} | Remaining {remaining} | Samples {len(state["accepted"])} | '
                  f'Elapsed {g.duration(state["seconds"])} | ETA (rough) {eta}', flush=True)
            last_report = now

    def publish(record):
        export(record, out)
        sha = record['source_sha256']
        state['accepted'][record['id']] = sha
        state['completed'][sha] = 'accepted'
        state['rows'].pop(sha, None)
        checkpoint()
        pending.unlink(missing_ok=True)

    checkpoint(force=True)
    try:
        if pending.exists():
            publish(json.loads(pending.read_text()))
        for sha, source in games.items():
            if sha in state['completed']:
                continue
            try:
                rows = candidate_rows(source)
            except (ValueError, IndexError, KeyError) as exc:
                print(f'Skipping invalid SGF: {source.name}: {exc}', flush=True)
                counts['invalid_sgf'] += 1
                state['completed'][sha] = 'invalid'
                state['rows'].pop(sha, None)
                checkpoint()
                continue
            first_index = state['rows'].get(sha, 0)
            for index in range(first_index, len(rows)):
                if index % 20 == 0 or index == first_index:
                    print(f'{source.name}: candidate {index+1}/{len(rows)}', flush=True)
                if e is None:
                    g.atomic_json(out/'engine.json', g.provenance())
                    e = g.Evaluator(out)
                queries = e.queries
                try:
                    result = search(e, rows[index], counts)
                except (g.EvaluationUnavailable, ValueError, IndexError, KeyError) as exc:
                    counts['unavailable_or_illegal'] += 1
                    print(f'Skipping candidate: {exc}', flush=True)
                    result = None
                finally:
                    state['queries'] += e.queries - queries
                counts['candidates'] += 1
                if result is not None:
                    g.atomic_json(pending, result)
                    publish(result)
                    print(f"Found {result['id']}: A={result['moves']['A']} B={result['moves']['B']} "
                          f"save={result['replies']['save']} best={result['replies']['best']} | "
                          f"gaps {result['score_gaps']['B_minus_A']:.1f}/{result['score_gaps']['best_minus_save']:.1f} points", flush=True)
                    break
                state['rows'][sha] = index+1
                checkpoint()
            state['completed'].setdefault(sha, 'no_sample')
            state['rows'].pop(sha, None)
            checkpoint()
        print('All input games processed. Add more SGFs and run the same command to continue.', flush=True)
    finally:
        if e is not None:
            e.close()
        checkpoint(force=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--games-dir', type=Path, default=g.PROJECT/'datasets/games')
    parser.add_argument('--out', type=Path, default=g.PROJECT/'outputs-addition')
    args = parser.parse_args()
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        run(args)
    except KeyboardInterrupt:
        print('Stopped. Run the same command to resume.', flush=True)


if __name__ == '__main__':
    main()
