#!/usr/bin/env python3
"""Export verified full-history pairs using the cloud repository's accepted schema."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import shutil
from sgfmill import sgf, sgf_moves
from find_atari import ataris, gtp
from full_history import initial_board, replay_checked, board_key, write_sgf
from search_pairs import make_board
from balance_append import quality
from search_endpoints import check_structure


def export_one(r, destination, sources):
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
    source=Path(r['source']);raw=source.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=r['source_sha256']:raise ValueError('Source changed')
    sources.mkdir(parents=True,exist_ok=True)
    source_name=r['source_sha256']+'.sgf';(sources/source_name).write_bytes(raw)
    destination.mkdir(parents=True,exist_ok=False)
    for side in ['X','Y']:
        write_sgf(destination/f'{side}.sgf',history,side)
        game=sgf.Sgf_game.from_bytes((destination/f'{side}.sgf').read_bytes())
        board,moves=sgf_moves.get_setup_and_moves(game)
        if len(moves)!=r['turn']:raise ValueError('Export lost move history')
        for c,p in moves:
            if p is not None:board.play(*p,c)
        if board_key(board)!=board_key(boards[side]):raise ValueError('Export board mismatch')
    accepted=dict(source_sgf='datasets/sources/'+source_name,move_number=r['turn'],
        moves={m:r[m] for m in ['A','B']},
        results={s:dict(winrate={m:r[s][m]['winrate'] for m in ['A','B']},
                        score_lead={m:r[s][m]['scoreLead'] for m in ['A','B']}) for s in ['X','Y']},
        X_A_targets=[dict(size=t['stone_count'],stones=t['stones']) for t in ataris(boards['X'],r['player'].lower())[r['A']]])
    (destination/'sample.json').write_text(json.dumps(accepted,indent=2)+'\n')
    return dict(id=r['id'],player=r['player'],source_sha256=r['source_sha256'],
        move_number=r['turn'],changed_historical_move=history['changed_move_number'],
        source_turn=history['source_turn'],appended_moves=r.get('appended_moves',[]),visits=r['visits'],
        protocol='full_history_single_historical_move_edit',
        checksums={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in destination.iterdir()})


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input',type=Path,nargs='+',required=True)
    ap.add_argument('--out',type=Path,required=True,help='Dataset root, containing accepted/ and sources/')
    ap.add_argument('--count',type=int,default=0,help='Zero exports all verified pairs')
    ap.add_argument('--seed',type=int,default=104)
    args=ap.parse_args();records={}
    for directory in args.input:
        for f in sorted(directory.glob('*/sample.json')):
            r=json.loads(f.read_text())
            if 'history' in r and all(quality(r['X'],r['Y'])[1].values()):records[r['id']]=r
    selected=list(sorted(records));random.Random(args.seed).shuffle(selected)
    if args.count and len(selected)<args.count:raise ValueError(f'Need {args.count} verified pairs; only {len(selected)} available')
    if args.count:selected=selected[:args.count]
    args.out.mkdir(parents=True,exist_ok=False)
    metadata=[export_one(records[i],args.out/'accepted'/i,args.out/'sources') for i in selected]
    (args.out/'manifest.json').write_text(json.dumps(dict(count=len(metadata),seed=args.seed,
        schema_reference='Iskar-Deng/go-eval-positions@57a82bf99cf8062e384a5cdd24651ca7fca3f16a',
        candidates=['A','B'],rules='chinese',komi=7.5,samples=metadata),indent=2)+'\n')
    print(json.dumps({'exported':len(metadata),'out':str(args.out.resolve())}))


if __name__=='__main__':main()
