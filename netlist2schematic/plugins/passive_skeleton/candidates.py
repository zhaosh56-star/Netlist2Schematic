"""Optimize an active skeleton first, then embed its typed passive edges."""
from copy import deepcopy
from bisect import bisect_left
from time import perf_counter

from netlist2schematic.plugins.passive_skeleton.plan import ACTIVE, PASSIVE, measure
from netlist2schematic.plugins.passive_skeleton.placement import pack


def _core_records(structs, names):
    return [deepcopy(s) for s in structs if set(s.get('devices', ())) <= names]


def _align_rl(par, core, comps, nets, index, plan):
    """Unique R/L conduction chains inherit a column, not an arbitrary side slot."""
    from netlist2schematic.layout.series_alignment import _fixed_ids
    from netlist2schematic.plugins.preflight.placement_preflight import _wire_estimate
    fixed = _fixed_ids(core, comps); actions=[]
    for group in plan['groups']:
        if group['role'] != 'conduction_inline':continue
        ports = group.get('conduction', {}).get('conduction_ports')
        terminals = (list(ports.values()) if ports else
                     [t for ts in group['anchors'].values() for t in ts
                      if t[0] in index and comps[index[t[0]]]['symbol'] in ACTIVE])
        if len(terminals)!=2:continue
        a,b=(index[n] for n,p in terminals)
        if core.cell_of[a][0]==core.cell_of[b][0]:continue
        columns=sorted({core.cell_of[a][1],core.cell_of[b][1]})
        best=None
        for column in columns:
            trial=deepcopy(core);cells=list(trial.cell_of)
            for i in (a,b):cells[i]=(cells[i][0],column)
            if len(set(cells))!=len(cells) or any(cells[i]!=core.cell_of[i] for i in fixed):continue
            trial.set_state(cells,trial.orient)
            if not trial.blocks_contiguous():continue
            if getattr(core, 'column_plan', {}).get('protected_plan') is not None:
                from netlist2schematic.plugins.columns.column_layout import preserves_columns
                if not preserves_columns(core, trial, index):
                    continue
            reversed_role=False
            for block in core.role_orders:
                for row in block:
                    for left,right in zip(row,row[1:]):
                        before=core.cell_of[left][1]-core.cell_of[right][1]
                        after=cells[left][1]-cells[right][1]
                        if before and before*after<=0:reversed_role=True
            if reversed_role:continue
            points=[trial.pin_world(index[n],p) for n,p in terminals]
            key=(abs(points[0][0]-points[1][0]),_wire_estimate(trial,nets,index,par),tuple(cells))
            if best is None or key<best[0]:best=key,trial
        if best is not None:
            _,trial=best
            if trial.cell_of!=core.cell_of:
                actions.append(dict(group=group['id'],members=[n for n,p in terminals],
                    before=[list(core.cell_of[i]) for i in (a,b)],after=[list(trial.cell_of[i]) for i in (a,b)]))
                core=trial
    return core,actions


def _control_sources(par,core, comps, index, plan):
    """A grounded source feeding a unique passive control chain is an accessory.

    Its old unconstrained SA slot must not occupy the horizontal input lane.
    Only singleton, unfixed sources move; MOS/BJT cells remain unchanged.
    """
    from statistics import median
    from netlist2schematic.layout.series_alignment import _fixed_ids
    current=deepcopy(core);actions=[];fixed=_fixed_ids(core,comps)
    pitch=median([b-a for a,b in zip(core.row_y,core.row_y[1:])]) if len(core.row_y)>1 else 120.
    for group in plan['groups']:
        if group['role']!='source_control_chain':continue
        ports=[t for ts in group['anchors'].values() for t in ts]
        sources=[t for t in ports if comps[index[t[0]]]['symbol'] in ('voltage-source','current-source')]
        hosts=[t for t in ports if comps[index[t[0]]]['symbol'] in ACTIVE and t[1] in ('G','B')]
        if len(sources)!=1 or len(hosts)!=1:continue
        name,pin=sources[0];host,hpin=hosts[0];i=index[name];j=index[host]
        if i in fixed or len(current.blocks[current.block_of[i]])!=1:continue
        if not any(par.power_marker_sym(n)=='ground' for n in comps[i]['nodes'].values()):continue
        row=len(current.row_y);current.row_y.append(current.row_y[-1]+pitch)
        col=current.cell_of[j][1]+(-1 if current.pin_dir(j,hpin)=='west' else 1)
        col=max(0,min(current.cols-1,col))
        before=list(current.cell_of[i]);current.cell_of[i]=(row,col);current.row_of[i]=row
        current.source_orientation_ids=set(getattr(current,'source_orientation_ids',()))|{i}
        candidates=[]
        for o in range(current.n_orients(i)):
            current.orient[i]=o
            wrong=sum(current.pin_dir(i,p)!='south' for p,n in comps[i]['nodes'].items() if par.power_marker_sym(n)=='ground')
            candidates.append((wrong,current.pin_dir(i,pin)!='north',o))
        current.orient[i]=min(candidates)[2]
        current.comp_at={cell:k for k,cell in enumerate(current.cell_of)}
        actions.append(dict(source=name,host=[host,hpin],group=group['id'],before=before,after=[row,col],
            evidence='grounded_singleton_source_via_unique_control_attachment'))
    return current,actions


def create_candidate(devices, comps, nets, index, levels, att, structs, seed, variant,
                     plan, options, *, iterations=0, deadline=None, columns=True):
    """Return a complete static-legal layout or an explicit fallback report.

    Only the geometric estimator sees the auxiliary edges. Exported components,
    real nets, model declarations and original current levels are untouched.
    """
    from netlist2schematic.layout import structured_placement as sp
    from netlist2schematic.netlist.netlist_core import to_document
    from netlist2schematic.plugins.layout_shared.branch_levels import plan_branch_rows
    from netlist2schematic.plugins.preflight.placement_preflight import repair_static_ports
    from netlist2schematic.plugins.columns.column_layout import build_column_plan,refine_columns
    from netlist2schematic.layout.series_alignment import refine_series_alignment
    from netlist2schematic.layout.structure_hierarchy import hierarchy_report
    started=perf_counter()
    core_devices={n:deepcopy(d) for n,d in devices.items() if d['cls']!='PASSIVE'}
    document=to_document(core_devices);cc,cn=document['components'],document['nets']
    ci={c['id']:i for i,c in enumerate(cc)}
    core_structs=_core_records(structs,set(ci))
    inline_bridges = [g['conduction'] for g in plan['groups']
                      if g.get('conduction', {}).get('conduction_ports')]
    # Derive directed R/L evidence on the real circuit first; compress away
    # passive reservation rows before building the ordinary active grid.
    original_rows,_=sp.legacy._allocate_rows(devices,comps,index,levels['net_levels'],levels['device_levels'],att)
    row_options = ({'bridges': [g['conduction'] for g in plan['groups']
                               if g.get('conduction')]} if inline_bridges else {})
    planning_rows = original_rows
    terminal_rows = []
    if inline_bridges:
        # Old passive reservation gaps must not become new active tiers when
        # an additional branch is inserted. Pack later reserves real space.
        active_rows = sorted({original_rows[index[name]] for name in ci})
        planning_rows = [bisect_left(active_rows, row) for row in original_rows]
        from netlist2schematic.recognition.device_leveling import out_terminal
        for bridge in inline_bridges:
            lower = bridge['conduction_ports'].get(bridge['end'])
            if lower is None:
                continue
            name = lower[0]
            sink = out_terminal(devices[name])
            if sp.par.power_marker_sym(sink) != 'ground':
                continue
            peers = [planning_rows[index[other]] for other in ci
                     if out_terminal(devices[other]) == sink]
            before = planning_rows[index[name]]
            planning_rows[index[name]] = max(peers)
            if planning_rows[index[name]] != before:
                terminal_rows.append(dict(device=name, net=sink, before=before,
                    after=planning_rows[index[name]], reason='shared_sink_branch_end'))
    _,rows=plan_branch_rows(devices,levels,planning_rows,[],comps,index,**row_options)
    occupied=sorted({rows['rows_before_attachments'][n] for n in ci})
    row_map={old:r for r,old in enumerate(occupied)}
    core_rows=[row_map[rows['rows_before_attachments'][c['id']]] for c in cc]
    core_att={n:deepcopy(v) for n,v in att.items() if n in ci}
    core,alignment,pairs=sp._initial_placement(core_devices,cc,cn,ci,levels,core_att,
        core_structs,seed,variant,passive_layout='fixed',row_override=core_rows)
    estimate_nets=deepcopy(cn);aux=[]
    for k,g in enumerate(plan['groups']):
        terminals=[tuple(t) for ts in g['anchors'].values() for t in ts if t[0] in ci]
        if len([ts for ts in g['anchors'].values() if ts])==2 and len(terminals)>1:
            key='__layout_attachment_edge_'+str(k)
            while key in estimate_nets:key+='_'  # Never overwrite a real net.
            estimate_nets[key]=terminals;aux.append(dict(id=key,group=g['id'],terminals=[list(t) for t in terminals],projection_only=True))
        if g['role']=='conduction_inline':
            ports = g.get('conduction', {}).get('conduction_ports')
            active = ([ci[n] for n,p in ports.values()] if ports else
                      [ci[n] for n,p in terminals if cc[ci[n]]['symbol'] in ACTIVE])
            if len(active)==2:alignment.append((*active,3.))
    local_deadline=min(deadline,perf_counter()+12) if deadline is not None else None
    energy,estimate,annealing=sp._anneal(core,estimate_nets,ci,alignment,pairs,iterations,local_deadline)
    role_actions=sp._orient_latch_clock_access(core,core_devices,core_structs,ci)
    sp._enforce_geometry_floor(core)
    core,preflight=repair_static_ports(sp.par,core,cc,cn,ci,options,deadline=deadline)
    context=dict(structs=core_structs,alignment_relations=[dict(devices=[cc[a]['id'],cc[b]['id']],weight=w) for a,b,w in alignment],
        sym_pairs=[(cc[a]['id'],cc[b]['id']) for a,b,_ in pairs])
    # Preserve the default active finalizers while passive guests are absent.
    # They cannot be repelled by a parked resistor or a remote capacitor.
    until=min(deadline,perf_counter()+1) if deadline is not None else perf_counter()+1
    if columns:
        column_plan=build_column_plan(sp.par,core,cc,cn,ci,core_structs,
            **({'inline_bridges': inline_bridges} if inline_bridges else {}))
        core,context=refine_columns(sp.par,core,cc,cn,ci,context,column_plan,options,deadline=until,
            seconds=max(0,until-perf_counter()),max_trials=128)
    used=context.get('layout_columns',{}).get('trials',0)
    core,context=refine_series_alignment(sp.par,core,cc,cn,ci,context,options,deadline=until,
        seconds=max(0,until-perf_counter()),max_trials=max(0,128-used))
    core,rl_actions=_align_rl(sp.par,core,cc,estimate_nets,ci,plan)
    core,source_actions=_control_sources(sp.par,core,cc,ci,plan)
    used+=context.get('series_alignment',{}).get('trials',0)
    packed,report=pack(sp.par,core,comps,nets,index,plan,options,deadline=until,
        seconds=max(0,until-perf_counter()),max_trials=max(0,128-used))
    report['shared_local_budget']=dict(seconds_limit=1.,max_trials=128,active_trials=used,
        packing_trials=report['trials'],completed_trials=used+report['trials'])
    report.update(core_only_sa=True,core_ids=list(ci),auxiliary_estimator_edges=aux,
        original_electrical_levels=deepcopy(levels['device_levels']),
        display_rows={n:core.cell_of[i][0] for n,i in ci.items()},
        rl_row_evidence=deepcopy(rows['rl_bridges']),rl_skipped=deepcopy(rows['skipped_bridges']),
        active_column_actions=rl_actions,source_attachment_actions=source_actions,core_preflight=preflight,
        core_finalizers={k:deepcopy(context[k]) for k in ('layout_columns','series_alignment') if k in context},
        generation_seconds=round(perf_counter()-started,6))
    if packed is None:return None,report
    metadata=dict(source_note=levels['source_note'],levels=deepcopy(levels['device_levels']),attach=deepcopy(att),
        structs=deepcopy(structs),sym_pairs=context['sym_pairs'],energy=energy,optimization_metrics=estimate,
        optimization_metrics_stage='active_skeleton_before_passive_embedding',
        layout_grid=dict(column_x=list(packed.column_x),row_y=list(packed.row_y)),
        candidate_id=variant+1,candidate_seed=seed,layout_style='structured',annealing=annealing,
        role_adjustments=role_actions,alignment_relations=context['alignment_relations'],
        layout_hierarchy=hierarchy_report(core),passive_skeleton=report,
        passive_layout_policy='skeleton',passive_layout=dict(version=plan['version'],policy='skeleton',
            core_only_sa=True,post_sa_local_coordinates=True))
    report['current']=measure(packed,comps,index,report['plan'])
    if inline_bridges:
        report['inline_port_policy'] = 'unique-conduction-hosts-with-control-taps-v1'
        report['terminal_row_preferences'] = terminal_rows
        metadata.update({key: deepcopy(context[key]) for key in ('layout_columns','series_alignment')
                         if key in context})
    return (packed,comps,nets,index,metadata),report
