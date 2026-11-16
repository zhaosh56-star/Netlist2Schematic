"""Joint real-pin axes for independently validated, untapped R/L paths."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from statistics import median
from time import perf_counter


def conduction_trials(pl, comps, index, plan, *, deadline=None, grid_step=None):
    from netlist2schematic.layout.structured_placement import par
    from netlist2schematic.plugins.layout_shared.branch_alignment import _bridge_relations, _grid_offset
    from netlist2schematic.plugins.attachment.attachment_integration import _install
    from netlist2schematic.routing.routing_rules import RoutingOptions
    step=RoutingOptions().grid_step if grid_step is None else grid_step
    bridges, _ = _bridge_relations(par,pl,comps,index,{'branch_row_plan':plan.get('branch_row_plan') or {}})
    adjacency, by_name = defaultdict(set), defaultdict(list)
    for bridge in bridges:
        names = [n for n in (bridge['upper'],*bridge['members'],bridge['lower']) if n is not None]
        for a,b in zip(names,names[1:]):
            adjacency[a].add(b);adjacency[b].add(a)
        for name in names:
            by_name[name].append(bridge)
    seen = set()
    for start in sorted(adjacency):
        if start in seen:
            continue
        names,pending = set(),[start]
        while pending:
            name=pending.pop()
            if name in names:
                continue
            names.add(name);pending.extend(adjacency[name]-names)
        seen.update(names)
        ids = [index[name] for name in sorted(names)]
        if len({pl.cell_of[i][0] for i in ids}) != len(ids):
            continue  # No random row repair or branch collapse in this pass.
        component_bridges = {b['id']:b for name in names for b in by_name[name]}
        roles = {name:role for b in component_bridges.values() for name,role in b['member_pins'].items()}
        refs = {}
        for bridge in component_bridges.values():
            for name,pin in ((bridge['upper'],bridge['upper_pin']),(bridge['lower'],bridge['lower_pin'])):
                if name is not None:
                    refs.setdefault(name,pin)
        center=median(pl.cell_of[i][1] for i in ids)
        columns=sorted(range(pl.cols),key=lambda c:(abs(c-center),c))[:8]
        for col in columns:
            if deadline is not None and perf_counter() >= deadline:
                return
            trial=deepcopy(pl)
            cells,orient=list(pl.cell_of),list(pl.orient)
            offsets=list(getattr(pl,'offset_x',[0.0]*len(comps)))
            valid=True
            for name,role in roles.items():
                i=index[name]
                choices=[]
                for o in trial.allowed_orientations(i):
                    trial.orient[i]=o
                    if trial.pin_dir(i,role['in'])=='north' and trial.pin_dir(i,role['out'])=='south':
                        choices.append(o)
                if not choices:
                    valid=False;break
                orient[i]=min(choices,key=lambda o:(o!=pl.orient[i],o))
            if not valid:
                continue
            local = {}
            trial.set_state(pl.cell_of,orient)
            for name in names:
                i=index[name]
                pin=refs.get(name) or roles[name]['in']
                local[name]=trial.pin_world(i,pin)[0]-trial.pos_of(i)[0]
            target_delta=median(local[n] for n in refs)
            # A fixed world pin is the axis when exactly one fixed anchor is
            # present. Other fixed positions still pass the caller's guard.
            fixed=set(getattr(pl,'fixed',())) | set(getattr(pl,'fixed_cells',{}))
            fixed_refs=[n for n in refs if index[n] in fixed]
            if fixed_refs:
                target_delta=median(pl.pin_world(index[n],refs[n])[0]-trial.column_x[col] for n in fixed_refs)
            for name in names:
                i=index[name]
                cells[i]=cells[i][0],col
            trial.set_state(cells,orient)
            trial.offset_x=offsets
            for name in names:
                i=index[name]
                value=_grid_offset(trial,i,target_delta-local[name],step)
                if value is None:
                    valid=False;break
                offsets[i]=value
            if not valid:
                continue
            trial.offset_x=offsets
            trial=_install(trial,comps,index,plan,required=pl.attachment_required_embeddings)
            if (cells==pl.cell_of and orient==pl.orient
                    and offsets==getattr(pl,'offset_x',[0.0]*len(comps))):
                continue
            yield trial,dict(kind='conduction_chain_axis',bridges=sorted(component_bridges),
                members=sorted(names),column=col,rows_preserved=True,axes_changed=False,
                actual_pins=True,columns_added=0)
