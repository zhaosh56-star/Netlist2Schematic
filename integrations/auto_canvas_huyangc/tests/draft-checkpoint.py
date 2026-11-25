"""Create a controlled partial-route test from a freshly calculated pose."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

parser=argparse.ArgumentParser()
parser.add_argument('--engine-root',type=Path,required=True)
parser.add_argument('--placement',type=Path,required=True)
parser.add_argument('--profile',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
sys.path.insert(0,str(args.engine_root))
from netlist2schematic.common.sa_saved import load_placement,saved_routing_options
from netlist2schematic.routing.routing_rules import build_scene
from integrations.auto_canvas_huyangc.drafts import make_draft
data,source,par,placement,comps,nets,index=load_placement(args.placement)
options=saved_routing_options(data)
scene=build_scene(par,nets,placement,index,options=options)
routes=list(data['routes'].items())
# Keep one real finished network, leaving the others as fixed pin exits. The
# fixture does not claim that the unmodified router failed on this circuit.
payload=make_draft(par,(placement,comps,nets,index,data),
    dict(scene=scene,completed=dict(routes[:1])),json.loads(args.profile.read_text(encoding='utf8')),options,'2')
payload['source_hash']=json.loads(args.profile.read_text(encoding='utf8'))['source']['sha256']
args.output.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps(payload['draft']))
