"""Failure evidence distinguishes exhausted search, capped work and interruptions."""
import sys
import itertools
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins'))
from test_native_grid import make_problem,add_net,wall
from autorouter.engine.native import router,astar


def problem():
    p=make_problem(layers=1,w=100,h=40)
    add_net(p,1,(5,20),(90,20))
    p.options.allow_vias=False
    p.options.rip_rounds=0
    p.options.retry_failed=False
    p.options.flat_first=False
    return p


def main():
    p=problem();p.options.max_expansions=1
    result=router.run(p)
    assert result.failed_detail[0]['verdict']=='expansion_limit',result.failed_detail
    assert result.failed_detail[0]['search']['expansions']>0
    p=problem();wall(p,0,50,0,39);p.options.max_expansions=100000
    result=router.run(p)
    assert result.failed_detail[0]['verdict']=='no_path_at_current_rules',result.failed_detail
    attempts=[]
    for rounds in (1,16):
        p=problem();wall(p,0,50,0,39);p.options.max_expansions=100000;p.options.rip_rounds=rounds
        result=router.run(p)
        attempts.append(result.failed_detail[0]['search']['attempts'])
    assert attempts[0]==attempts[1], 'unchanged rip-up rounds repeated without progress'
    p=problem();p.options.time_limit_s=1e-9
    result=router.run(p)
    assert result.failed_detail[0]['verdict']=='not_attempted_job_time_limit',result.failed_detail
    p=problem();result=router.run(p,cancelled=lambda:True)
    assert result.failed_detail[0]['verdict']=='not_attempted_cancelled',result.failed_detail
    p=make_problem(layers=1,w=400,h=200)
    add_net(p,1,(5,100),(390,100));wall(p,0,200,0,199)
    p.options.allow_vias=False;p.options.rip_rounds=0;p.options.flat_first=False
    p.options.conn_time_s=0.1;p.options.max_expansions=100000
    with patch.object(astar,'clock',side_effect=itertools.count()), patch.object(astar,'PROBE_AT',-1):
        result=router.run(p)
    assert result.failed_detail[0]['verdict']=='connection_time_limit',result.failed_detail
    p=problem();result=router.run(p)
    assert not result.failed and astar.LAST['outcome']=='found'
    assert not astar.LAST['capped'], 'stale previous-search limit'
    print('test_search_verdicts: capped, exhausted, untried timeout/cancel and success distinguished')


if __name__=='__main__':main()
