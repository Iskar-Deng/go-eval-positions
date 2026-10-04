#!/usr/bin/env python3
"""Trailing side plays large remote moves; leading side fills owned space.

The filler is compared with pass, never presumed valueless from geometry alone.
Only real board moves are appended. Full common history and final pair checks remain.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import time

from find_atari import PROJECT, group, gtp, neighbors, point_of
from search_pairs import Evaluator, legal, make_board, metric, position
from search_endpoints import check_scores, values
from balance_append import play_pair, quality
from balance_rollouts import remote_mask, shared_candidates, save, pair_positions
from engine_runtime import config_path, provenance


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


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('samples',type=Path,nargs='+')
    ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--minutes',type=float,default=4)
    ap.add_argument('--total-minutes',type=float,default=0,help='Whole-batch budget; zero means unlimited')
    ap.add_argument('--max-added',type=int,default=20)
    ap.add_argument('--large-ranks',type=int,nargs='+',default=[0,1])
    ap.add_argument('--cache-from',type=Path,action='append',default=[])
    ap.add_argument('--size-control',action='store_true')
    ap.add_argument('--prefix-file',type=Path,help='JSON mapping source IDs to shared move prefixes')
    args=ap.parse_args()
    prefixes=json.loads(args.prefix_file.read_text()) if args.prefix_file else {}
    args.out.mkdir(parents=True,exist_ok=False)
    (args.out/'config.json').write_text(json.dumps(vars(args),default=str,indent=2))
    for p in [Path(__file__),PROJECT/'scripts/balance_rollouts.py',PROJECT/'scripts/balance_append.py',PROJECT/'scripts/search_pairs.py',PROJECT/'scripts/full_history.py',config_path()]:
        shutil.copy2(p,args.out/p.name)
    (args.out/'provenance.json').write_text(json.dumps({**provenance(),
        'protocol':'full source history when present, followed by identical appended moves',
        'ownership_perspective':'SIDETOMOVE','pass_equivalence_max_difference_points':2.,
        'policy_visits':32,'move_visits':96,'screen_visits':128,'verify_visits':1000},indent=2))
    e=Evaluator(args.out);reports=[]
    for path in args.cache_from:
        for line in path.read_text().splitlines():
            row=json.loads(line)
            key=json.dumps([row['request'],row['visits'],row['allowed']],sort_keys=True)
            e.cache[key]=row['result']
    journal=(args.out/'checkpoints.jsonl').open('a')
    decision=(args.out/'moves.jsonl').open('a')
    batch_start=time.monotonic()
    batch_deadline=batch_start+args.total_minutes*60 if args.total_minutes>0 else float('inf')
    try:
        for source in args.samples:
            if time.monotonic()>=batch_deadline:break
            r=json.loads(source.read_text());orig=[make_board(r['board_X']),make_board(r['board_Y'])]
            blocked=remote_mask(r);start=time.monotonic();qstart=e.queries
            sample_deadline=min(start+args.minutes*60,batch_deadline)
            limit=min(args.max_added,149-r['turn']);limit-=limit%2
            best=None;success=False;checked=0;deepest=0;reason='budget_or_trajectories_exhausted'
            for variant in args.large_ranks:
                reason='budget_or_trajectories_exhausted'
                boards=[b.copy() for b in orig];moves=[]
                lead=statistics.median([r['X']['A']['scoreLead'],r['X']['B']['scoreLead'],r['Y']['B']['scoreLead']])
                prefix=prefixes.get(r['id'],[])
                assert len(prefix)%2==0 and len(prefix)<=limit
                for actor,move in prefix:
                    expected=r['player'] if len(moves)%2==0 else ('W' if r['player']=='B' else 'B')
                    assert actor==expected and move!='pass' and move not in blocked
                    bases=pair_positions(r,orig,moves)
                    assert all(legal(e.query(base,16),move,19) for base in bases)
                    pair=play_pair(*boards,actor,move,r)
                    assert pair is not None
                    boards=list(pair);moves.append([actor,move])
                if moves:
                    bases=pair_positions(r,orig,moves)
                    px,py=[values(e,base,r['A'],r['B'],1000) for base in bases]
                    assert px is not None and py is not None
                    lead=statistics.median([px['A']['scoreLead'],px['B']['scoreLead'],py['B']['scoreLead']])
                for ply in range(len(moves),limit):
                    if time.monotonic()>=sample_deadline:break
                    if ply%2==0:
                        leader=r['player'] if lead>=0 else ('W' if r['player']=='B' else 'B')
                    actor=r['player'] if ply%2==0 else ('W' if r['player']=='B' else 'B')
                    bases=pair_positions(r,orig,moves)
                    roots=[e.query(dict(base,includeOwnership=True),32) for base in bases]
                    detail={}
                    if actor==leader:
                        pool=filler_pool(*roots,*boards,actor,r,blocked)
                        if not pool:
                            reason='no_safe_filler_candidate';break
                        if not all(legal(root,'pass',19) for root in roots):
                            reason='pass_reference_illegal';break
                        pass_values=[metric(e.query(base,96,['pass']),'pass') for base in bases]
                        picked=None
                        for _,move,pair in pool[:6]:
                            mv=[metric(e.query(base,96,[move]),move) for base in bases]
                            deltas=[a['scoreLead']-b['scoreLead'] for a,b in zip(mv,pass_values)]
                            if max(map(abs,deltas))<=2.:
                                picked=(move,pair);detail={'pass_score_differences':deltas,'own_space_threshold':.50};break
                        if picked is None:
                            reason='filler_not_close_to_pass';break
                        move,pair=picked;kind='own_space_pass_like_move'
                    else:
                        pool=shared_candidates(*roots,*boards,actor,r,blocked,limit=361 if args.size_control else 32)
                        if not pool:reason='no_remote_move';break
                        result=e.query(bases[0],96,[m for m,_ in pool[:24]])
                        options=sorted(result['moveInfos'],key=lambda m:m['order'])
                        if args.size_control:
                            choices=list(dict.fromkeys([v['move'] for v in options[:2]]+
                                [pool[i][0] for i in [3,8,16,31,63,127,len(pool)-1] if i<len(pool)]))
                            estimates=[]
                            for candidate in choices:
                                trial=moves+[[actor,candidate]]
                                if len(trial)%2:
                                    trial=trial+[[leader,'pass']]
                                trial_base=pair_positions(r,orig,trial)[0]
                                root=e.query(trial_base,16)
                                if not legal(root,r['B'],19):continue
                                v=metric(e.query(trial_base,64,[r['B']]),r['B'])
                                estimates.append((abs(v['scoreLead']),candidate,v))
                            if not estimates:reason='no_size_control_candidate';break
                            ranked=sorted(estimates,key=lambda v:v[0])
                            shortlist=[]
                            for _,candidate,vx in ranked[:4]:
                                trial=moves+[[actor,candidate]]
                                if len(trial)%2:trial=trial+[[leader,'pass']]
                                trial_base=pair_positions(r,orig,trial)[1]
                                root=e.query(trial_base,16)
                                if not legal(root,r['B'],19):continue
                                vy=metric(e.query(trial_base,64,[r['B']]),r['B'])
                                # Prefer a margin inside the winrate interval in both boards.
                                wr=[vx['winrate'],vy['winrate']]
                                penalty=20*sum(max(.38-v,0,v-.62) for v in wr)
                                penalty+=.1*max(abs(vx['scoreLead']),abs(vy['scoreLead']))
                                penalty+=max(abs(vx['scoreLead']-vy['scoreLead'])-3,0)
                                shortlist.append((penalty,candidate,vx,vy))
                            if not shortlist:reason='no_shared_size_control_candidate';break
                            shortlist.sort(key=lambda v:v[0])
                            _,move,vx,vy=shortlist[min(variant,len(shortlist)-1)]
                            detail={'size_control_B_score_proxy':[vx['scoreLead'],vy['scoreLead']],
                                    'proxy_uses_opponent_pass':ply%2==0}
                        else:
                            move=options[min(variant,len(options)-1)]['move']
                        pair=next(pair for m,pair in pool if m==move)
                        kind='large_remote_move'
                    boards=list(pair);moves.append([actor,move]);deepest=max(deepest,len(moves))
                    decision.write(json.dumps(dict(source_id=r['id'],variant=variant,ply=len(moves),actor=actor,
                        leader=leader,move=move,kind=kind,**detail))+'\n');decision.flush()
                    if len(moves)%2:continue
                    bases=pair_positions(r,orig,moves)
                    xm,ym=[values(e,base,r['A'],r['B'],128) for base in bases]
                    if xm is None or ym is None:reason='candidate_illegal';break
                    checked+=1;cost,checks=quality(xm,ym)
                    lead=statistics.median([xm['A']['scoreLead'],xm['B']['scoreLead'],ym['B']['scoreLead']])
                    event=dict(source_id=r['id'],variant=variant,moves=list(moves),stage='screen',X=xm,Y=ym,cost=cost,checks=checks)
                    journal.write(json.dumps(event)+'\n');journal.flush()
                    if best is None or cost<best['cost']:best=event
                    good=[xm['A']['winrate'],xm['B']['winrate'],ym['B']['winrate']]
                    print(json.dumps({'source':r['id'],'added':len(moves),'lead':round(lead,2),
                        'good_winrates':good,'cost':round(cost,3),'seconds':round(time.monotonic()-start,1)}),flush=True)
                    if all(.25<=v<=.75 for v in good) and all(check_scores(xm,ym,loose=True).values()):
                        vx,vy=[values(e,base,r['A'],r['B'],1000) for base in bases]
                        if vx is None or vy is None:reason='candidate_illegal';break
                        _,final_checks=quality(vx,vy)
                        lead=statistics.median([vx['A']['scoreLead'],vx['B']['scoreLead'],vy['B']['scoreLead']])
                        journal.write(json.dumps(dict(event,stage='verify',X=vx,Y=vy,checks=final_checks))+'\n');journal.flush()
                        print(json.dumps({'verified':r['id'],'added':len(moves),'moves':moves,'checks':final_checks,
                            'winrates':{p:{m:v[m]['winrate'] for m in ['A','B']} for p,v in [('X',vx),('Y',vy)]}}),flush=True)
                        if all(final_checks.values()):
                            save(args.out,source,r,orig,boards,moves,vx,vy,1000);success=True;reason='passed';break
                if success or time.monotonic()>=sample_deadline:break
            if not success and time.monotonic()>=sample_deadline:
                reason='batch_time_budget' if time.monotonic()>=batch_deadline else 'sample_time_budget'
            report=dict(id=r['id'],success=success,reason=reason,checkpoints=checked,max_added_reached=deepest,
                queries=e.queries-qstart,seconds=time.monotonic()-start,best_screen=best)
            reports.append(report);(args.out/'summary.json').write_text(json.dumps(reports,indent=2))
            print(json.dumps({k:v for k,v in report.items() if k!='best_screen'}),flush=True)
        (args.out/'batch-summary.json').write_text(json.dumps(dict(
            seconds=time.monotonic()-batch_start,provided=len(args.samples),attempted=len(reports),
            passed=sum(r['success'] for r in reports),
            stop_reason='batch_time_budget' if time.monotonic()>=batch_deadline else 'queue_exhausted'),indent=2))
    finally:
        journal.close();decision.close();e.close()


if __name__=='__main__':main()
