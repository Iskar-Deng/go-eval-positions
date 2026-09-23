#!/usr/bin/env python3
"""Manual selection -> analysis -> explicit acceptance; no prompts are generated."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

from sgfmill import sgf, sgf_moves
from find_atari import (PROJECT, Engine, ataris, game_metadata, gtp, point_of,
                        load_config, output_name)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def now():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')


def other(color):
    return 'w' if color == 'b' else 'b'


def checked_point(value, size):
    if not isinstance(value, str):
        raise ValueError('Fill A/B/C with coordinates, e.g. "N5"')
    value = value.strip().upper()
    try:
        p = point_of(value)
        if p is None or not all(0 <= x < size for x in p) or gtp(p) != value:
            raise ValueError()
    except (ValueError, IndexError):
        raise ValueError(f'Invalid board coordinate: {value!r}')
    return p


def read_source(path, turn):
    raw = path.read_bytes()
    game = sgf.Sgf_game.from_bytes(raw)
    board, plays = sgf_moves.get_setup_and_moves(game)
    if not 2 <= board.side <= 19:
        raise ValueError('Only square boards of size 2..19 are supported')
    if type(turn) is not int or not 0 <= turn <= len(plays):
        raise ValueError(f'move_number must be in 0..{len(plays)} (moves already played)')
    root = game.get_root()
    first = root.get('PL') if root.has_property('PL') else (plays[0][0] if plays else 'b')
    player = plays[turn][0] if turn < len(plays) else (other(plays[-1][0]) if plays else first)
    return raw, game, board, plays[:turn], first, player


def init_selection(args):
    raw, game, board, plays, first, player = read_source(args.sgf, args.move_number)
    root = game.get_root()
    folder = args.sample_dir or PROJECT / 'reviews' / (output_name(game_metadata(root), digest(raw)) + f'__m{args.move_number}__{now()}')
    folder.mkdir(parents=True, exist_ok=False)
    selection = {
        'source_sgf': str(args.sgf.resolve()), 'move_number': args.move_number,
        'moves': {'A': None, 'B': None, 'C': None},
        'Y': {'edits': [
            {'from': None, 'to': None},
            {'from': None, 'to': None},
            {'from': None, 'to': None},
        ]},
    }
    save_json(folder / 'selection.json', selection)
    print(f'Fill this file in VS Code:\n{folder / "selection.json"}')
    return folder / 'selection.json'


def board_key(board):
    return tuple(board.get(r, c) for r in range(board.side) for c in range(board.side))


def normalize_rules(rules):
    aliases = {'Japanese': 'japanese', 'Chinese': 'chinese', 'Korean': 'korean',
               'AGA': 'aga', 'New Zealand': 'new-zealand'}
    if not isinstance(rules, str):
        raise ValueError('Fill rules with a KataGo rules name')
    return aliases.get(rules, rules)


def reconstruct(initial, moves, track_origins=False):
    """Replay moves to obtain the board; optionally track which move placed each current stone."""
    board = initial.copy()
    origins = {p: None for color, p in initial.list_occupied_points()}
    for number, (color, p) in enumerate(moves, 1):
        if p is not None:
            board.play(*p, color)
            occupied = {q for _, q in board.list_occupied_points()}
            origins = {q: source for q, source in origins.items() if q in occupied}
            if p in occupied:
                origins[p] = number
    return (board, origins) if track_origins else board


def prepare(selection, selection_path, config):
    required = {'source_sgf', 'move_number', 'moves'}
    allowed = required | {'Y'}
    if not required <= set(selection) or set(selection) - allowed:
        raise ValueError('Required: source_sgf, move_number, moves.A, moves.B; other fields are optional')
    if not isinstance(selection['source_sgf'], str) or not selection['source_sgf'].strip():
        raise ValueError('source_sgf is required')
    source = Path(selection['source_sgf'])
    if not source.is_absolute():
        source = PROJECT / source
    raw, game, initial, moves, first, player = read_source(source, selection['move_number'])
    root = game.get_root()
    rules = normalize_rules(config.rules or (root.get('RU') if root.has_property('RU') else None))
    komi = config.komi if config.komi is not None else (game.get_komi() if root.has_property('KM') else None)
    if type(komi) not in (int, float) or not math.isfinite(komi) or not -400 <= komi <= 400 or komi * 2 != int(komi * 2):
        raise ValueError('Fill komi with a finite integer or half-integer in [-400,400]')
    if not isinstance(selection['moves'], dict) or not {'A', 'B'} <= set(selection['moves']) or set(selection['moves']) - {'A', 'B', 'C'}:
        raise ValueError('moves requires A and B; C is optional')
    points = {label: checked_point(selection['moves'].get(label), initial.side) for label in ('A', 'B')}
    if selection['moves'].get('C') is not None:
        points['C'] = checked_point(selection['moves']['C'], initial.side)
    if len(set(points.values())) != len(points):
        raise ValueError('Selected moves must be different coordinates')
    xboard, origins = reconstruct(initial, moves, track_origins=True)
    positions = {'X': (list(moves), xboard)}
    if selection.get('Y') is not None:
        y = selection['Y']
        if not isinstance(y, dict) or set(y) != {'edits'} or not isinstance(y['edits'], list):
            raise ValueError('Y must be null or {"edits": [{"from": ..., "to": ...}]}')
        edits = []
        for edit in y['edits']:
            if not isinstance(edit, dict) or set(edit) != {'from', 'to'}:
                raise ValueError('Each Y edit needs from and to')
            if edit['from'] is None and edit['to'] is None:
                continue
            if edit['from'] is None or edit['to'] is None:
                raise ValueError('Each used Y edit must fill both from and to')
            edits.append(edit)
        if not edits:
            return raw, game, initial, first, player, rules, komi, points, positions
        occupied = {p for _, p in xboard.list_occupied_points()}
        edited_moves = list(moves)
        changed_move_numbers = set()
        destinations = set()
        for edit in edits:
            old = checked_point(edit['from'], initial.side)
            new = checked_point(edit['to'], initial.side)
            if old not in occupied or new in occupied:
                raise ValueError('Y edit: from must contain a stone and to must be empty')
            if new in destinations:
                raise ValueError('Two Y edits cannot use the same destination')
            move_number = origins.get(old)
            if move_number is None:
                raise ValueError(f'Y edit {gtp(old)}: current stone comes from initial setup, not a historical move')
            if move_number in changed_move_numbers:
                raise ValueError('Two Y edits refer to the same historical move')
            color, source_point = edited_moves[move_number - 1]
            edited_moves[move_number - 1] = (color, new)
            changed_move_numbers.add(move_number)
            destinations.add(new)
        yboard = reconstruct(initial, edited_moves)
        positions['Y'] = (edited_moves, yboard)
        if board_key(xboard) == board_key(yboard):
            raise ValueError('Y edits produce no final board difference')
    return raw, game, initial, first, player, rules, komi, points, positions


def make_sgf(source_game, initial, moves, first, player, rules, komi, points, comment):
    # Clean prefix: no future moves, outcome, candidate labels, comments or variations leak.
    game = sgf.Sgf_game(initial.side)
    root = game.get_root()
    source_root = source_game.get_root()
    for key in ('DT', 'EV', 'RO', 'PB', 'PW', 'BR', 'WR'):
        if source_root.has_property(key):
            root.set(key, source_root.get(key))
    root.set('RU', rules)
    root.set('KM', komi)
    root.set('PL', first)
    stones = initial.list_occupied_points()
    root.set_setup_stones([p for c, p in stones if c == 'b'], [p for c, p in stones if c == 'w'])
    node = root
    for color, move in moves:
        node = node.new_child()
        node.set_move(color, move)
    node.set('LB', [(p, label) for label, p in points.items()])
    node.set('C', comment)
    return game.serialise()


def summary(report):
    lines = ['| Position | Move | Legal | Winrate | Score lead | Atari | Target stones |',
             '| --- | --- | --- | --- | --- | --- | --- |']
    for name, position in report['positions'].items():
        for label, item in position['candidates'].items():
            wr = f"{item['winrate']:.2%}" if item['legal'] else '—'
            score = f"{item['score_lead']:+.2f}" if item['legal'] else '—'
            sizes = ', '.join(str(t['stone_count']) for t in item['targets']) or '—'
            lines.append(f"| {name} | {label}={item['move']} | {item['legal']} | {wr} | {score} | {item['is_atari']} | {sizes} |")
    return '\n'.join(lines) + '\n'


def atari_candidates(report):
    return {(position_name, label)
            for position_name, position in report['positions'].items()
            for label, item in position['candidates'].items()
            if item['is_atari']}


def workspace_for(selection_path):
    return PROJECT / 'manual_reviews' if selection_path.parent == PROJECT / 'scripts' else selection_path.parent


def check_selection(args):
    selection_path = args.selection.resolve()
    selection_raw = selection_path.read_bytes()
    selection = json.loads(selection_raw)
    config = load_config(args.config)
    source_raw, game, initial, first, player, rules, komi, points, positions = prepare(selection, selection_path, config)
    workspace = workspace_for(selection_path)
    folder = workspace / 'runs' / now()
    folder.mkdir(parents=True, exist_ok=False)
    save_json(folder / 'selection.json', selection)
    save_json(folder / 'config.json', vars(config))
    report = {'schema_version': 1, 'status': 'pending_manual_review',
              'selection_sha256': digest(selection_raw), 'source_sha256': digest(source_raw),
              'player': player.upper(), 'rules': rules, 'komi': komi,
              'analysis_mode': 'full_history', 'history_checked': False,
              'move_number': selection['move_number'], 'game_metadata': game_metadata(game.get_root()),
              'score_perspective': 'player_to_move', 'visits_per_search': config.visits,
              'positions': {}, 'board_diff': [], 'warnings': [], 'all_moves_legal': True}
    if 'Y' in positions:
        xb, yb = positions['X'][1], positions['Y'][1]
        report['board_diff'] = [{'point': gtp((r, c)), 'X': xb.get(r, c), 'Y': yb.get(r, c)}
                                for r in range(xb.side) for c in range(xb.side) if xb.get(r, c) != yb.get(r, c)]
        if len(report['board_diff']) != 2:
            report['warnings'].append('Final board differs at more/fewer than two intersections; inspect extra captures or downstream effects.')
    engine = Engine(args.engine.resolve(), folder / 'engine.log', config.query_timeout_seconds, config.shutdown_timeout_seconds)
    try:
        for name, (history, board) in positions.items():
            print(f'Analyzing {name} with {len(history)} historical moves...', flush=True)
            base = {'moves': [[c.upper(), gtp(p)] for c, p in history],
                    'initialStones': [[c.upper(), gtp(p)] for c, p in initial.list_occupied_points()],
                    'initialPlayer': first.upper(), '_player': player.upper(), 'rules': rules,
                    'komi': komi, 'boardXSize': board.side, 'boardYSize': board.side,
                    'includePolicy': True}
            result = engine.query(base, config.visits)
            structural = ataris(board, player)
            position = {'moves': base['moves'], 'initial_stones': base['initialStones'],
                        'board_size': board.side, 'board_stones': [[c.upper(), gtp(p)] for c, p in board.list_occupied_points()],
                        'root_info': result['rootInfo'], 'candidates': {}}
            for label, p in points.items():
                move = gtp(p)
                legal = result['policy'][(board.side-1-p[0])*board.side+p[1]] >= 0
                item = {'move': move, 'legal': legal, 'is_atari': None,
                        'targets': [], 'winrate': None, 'score_lead': None}
                if legal:
                    analyzed = engine.query(base, config.visits, [move])
                    info = next((m for m in analyzed['moveInfos'] if m['move'] == move), None)
                    if info is None:
                        raise RuntimeError(f'{name}/{label}: engine did not return the forced move')
                    item.update(is_atari=move in structural, targets=structural.get(move, []),
                                winrate=info['winrate'], score_lead=info['scoreLead'], engine_info=info)
                else:
                    report['all_moves_legal'] = False
                    report['warnings'].append(f'{name}/{label}={move} is illegal; cannot accept this sample.')
                position['candidates'][label] = item
            report['positions'][name] = position
            (folder / f'{name}.sgf').write_bytes(make_sgf(game, initial, history, first, player, rules,
                                                       komi, points, f'Manual review: {name}; full history retained; historical naturalness not checked'))
    finally:
        engine.close()
    if not report['positions']['X']['candidates']['A']['is_atari']:
        report['warnings'].append('X/A does not create atari.')
    if 'Y' in report['positions'] and report['positions']['Y']['candidates']['A']['is_atari']:
        report['warnings'].append('Y/A still creates atari; inspect the targets.')
    if atari_candidates(report) != {('X', 'A')}:
        report['warnings'].append('Acceptance requires X/A to be the only atari candidate.')
    save_json(folder / 'analysis.json', report)
    summary_text = summary(report)
    (folder / 'summary.md').write_text(summary_text, encoding='utf-8')
    # Only point to complete successful analysis. Every rerun keeps the earlier results.
    artifacts = {p.name: digest(p.read_bytes()) for p in folder.iterdir() if p.name != 'engine.log'}
    save_json(workspace / 'latest.json', {'run': str(folder.relative_to(workspace)),
                                                    'selection_sha256': digest(selection_raw), 'artifacts': artifacts})
    print(f'\n{summary_text}', end='')
    print(f'Saved: {folder / "summary.md"}\nNot added to the final dataset.')
    return folder


def accept_selection(args):
    selection_path = args.selection.resolve()
    raw = selection_path.read_bytes()
    workspace = workspace_for(selection_path)
    latest = json.loads((workspace / 'latest.json').read_text())
    if digest(raw) != latest['selection_sha256']:
        raise ValueError('Selection changed after checking; run check again before accepting')
    run = (workspace / latest['run']).resolve()
    if not run.is_relative_to((workspace / 'runs').resolve()):
        raise ValueError('Invalid analysis run path')
    for name, expected in latest['artifacts'].items():
        if Path(name).name != name or digest((run / name).read_bytes()) != expected:
            raise ValueError(f'Analysis artifact changed: {name}; run check again')
    report = json.loads((run / 'analysis.json').read_text())
    if report['selection_sha256'] != digest(raw) or not report['all_moves_legal']:
        raise ValueError('Cannot accept a stale analysis or illegal candidate moves')
    if atari_candidates(report) != {('X', 'A')}:
        raise ValueError('Cannot accept: X/A must be the only candidate that creates atari')
    selection = json.loads(raw)
    sample_id = report['source_sha256'][:12] + f"-m{selection['move_number']}-" + digest(raw)[:12]
    destination = args.dataset / sample_id
    destination.mkdir(parents=True, exist_ok=False)
    # Store human labels/evaluation apart from model input to avoid target leakage.
    for name, position in report['positions'].items():
        source = sgf.Sgf_game.from_bytes((run / f'{name}.sgf').read_bytes())
        last = source.get_main_sequence()[-1]
        for key in ('LB', 'C'):
            if last.has_property(key):
                last.unset(key)
        (destination / f'{name}.sgf').write_bytes(source.serialise())
    accepted = {
        'source_sgf': selection['source_sgf'],
        'move_number': selection['move_number'],
        'moves': {label: move for label, move in selection['moves'].items() if move is not None},
        'results': {
            name: {
                'winrate': {label: item['winrate'] for label, item in position['candidates'].items()},
                'score_lead': {label: item['score_lead'] for label, item in position['candidates'].items()},
            }
            for name, position in report['positions'].items()
        },
        'X_A_targets': [
            {'size': target['stone_count'], 'stones': target['stones']}
            for target in report['positions']['X']['candidates']['A']['targets']
        ],
    }
    save_json(destination / 'sample.json', accepted)
    print(f'Accepted by explicit command:\n{destination}')
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='Create a blank JSON form for a selected position')
    init.add_argument('sgf', type=Path)
    init.add_argument('--move-number', type=int, required=True, help='Moves already played')
    init.add_argument('--sample-dir', type=Path)
    init.add_argument('--config', type=Path, default=PROJECT / 'configs/find-atari.json')
    init.set_defaults(func=init_selection)
    check = commands.add_parser('check', help='Analyze X/A,B,C and optionally Y/A,B,C')
    check.add_argument('selection', nargs='?', type=Path, default=PROJECT / 'scripts/selection.json')
    check.add_argument('--config', type=Path, default=PROJECT / 'configs/find-atari.json')
    check.add_argument('--engine', type=Path, default=PROJECT / 'scripts/katago.sh')
    check.set_defaults(func=check_selection)
    accept = commands.add_parser('accept', help='Explicitly approve the latest checked selection')
    accept.add_argument('selection', nargs='?', type=Path, default=PROJECT / 'scripts/selection.json')
    accept.add_argument('--dataset', type=Path, default=PROJECT / 'datasets/accepted')
    accept.set_defaults(func=accept_selection)
    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        sys.exit(str(exc))
