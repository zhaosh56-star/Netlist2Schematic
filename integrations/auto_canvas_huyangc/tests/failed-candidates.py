"""Controlled routing-failure exercise, never a production routing strategy."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
from time import sleep
from unittest.mock import patch

parser=argparse.ArgumentParser()
parser.add_argument('--engine-root',type=Path,required=True)
parser.add_argument('--placement',type=Path,required=True)
parser.add_argument('--profile',type=Path,required=True)
parser.add_argument('--out-dir',type=Path,required=True)
parser.add_argument('--interrupt',action='store_true')
args=parser.parse_args()
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
sys.path.insert(0,str(args.engine_root))
from netlist2schematic.common.sa_saved import load_placement,saved_routing_options
from netlist2schematic.routing.routing_rules import build_scene
from netlist2schematic.routing.orthogonal_router import RoutingFailure
from integrations.auto_canvas_huyangc.engine_runner import run
data,source,par,placement,comps,nets,index=load_placement(args.placement)
options=saved_routing_options(data);scene=build_scene(par,nets,placement,index,options=options)

def failed_normal(*positional,**kwargs):
    identifier=str(positional[8])
    if args.interrupt and identifier=='3':sleep(30)
    metadata=deepcopy(data);metadata['candidate_id']=identifier
    routes=list(data['routes'].items())[:1 if identifier=='2' else 2]
    failure=RoutingFailure('Controlled A* failure for editable-result regression',{'reason':'no_path'})
    failure.retry_context=deepcopy((placement,comps,nets,index,metadata))
    failure.retry_options=options
    failure._rescue_context=dict(scene=scene,completed=dict(routes),failed_net='test',pending=[],margin=150,options=options)
    raise failure

def no_repair(rows,**kwargs):return None,{'status':'failed'},rows
def no_spacing(*args,**kwargs):raise RoutingFailure('Controlled spacing failure',{'reason':'no_path'})
args.seconds=20;args.iterations=0;args.seed=17
with patch('netlist2schematic.flow.run_flow._normal_result',failed_normal),\
     patch('netlist2schematic.recovery.primary_recovery.repair',no_repair),\
     patch('netlist2schematic.flow.run_flow._try_candidate_rescues',return_value=None),\
     patch('netlist2schematic.recovery.spacing_retry.retry_spacing',no_spacing):
    run(args)
