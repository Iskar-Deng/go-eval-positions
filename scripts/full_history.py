"""Retain source move history; relocate one historical move for counterfactual Y."""
import hashlib
from pathlib import Path
from sgfmill import sgf, sgf_moves
from find_atari import gtp, point_of


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
