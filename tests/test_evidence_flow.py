"""Calibrated drawing intent reaches placement without changing reference seeds.

These fixtures use only the existing flat/selected-cell SPICE reader.  They do
not require local GT files, parser adapters, a simulator, or final A* routes.
"""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from netlist2schematic.recognition.circuit_structures import analyze_structures
from netlist2schematic.plugins.layout_shared.layout_evidence import build_layout_evidence
from netlist2schematic.plugins.layout_shared.layout_intent import build_intent
from netlist2schematic.plugins.layout_shared.layout_relations import build_layout_relations, placement_blocks
from netlist2schematic.netlist.netlist_core import parse_netlist, to_document
from netlist2schematic.plugins.structure.structure_selection import prepare_branch_context
from netlist2schematic.layout import structured_placement as structured


BANK_SOURCE = '''.subckt bank ip im bias pbias vdd vss out_left out_right
.model n NMOS
.model p PMOS
Ma left ip tail vss n
Mb right im tail vss n
Mra control_left left tail vss n
Mrb control_right right tail vss n
Mua out_left control_left left vss n
Mub out_right control_right right vss n
Mpa control_left pbias vdd vdd p
Mpb control_right pbias vdd vdd p
Mt tail bias vss vss n
.ends bank
'''


def differential_relations(relations):
    return [row for row in relations if row['kind'] in ('differential_pair', 'bjt_differential_pair')]


class EvidenceFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / 'bank.cir'
        self.source.write_text(BANK_SOURCE, encoding='utf-8')
        self.original_bytes = self.source.read_bytes()
        self.devices = parse_netlist(self.source, cell='bank')
        self.baseline = analyze_structures(self.devices)
        document = to_document(self.devices)
        self.comps, self.nets = document['components'], document['nets']
        self.index = {comp['id']: i for i, comp in enumerate(self.comps)}

    def tearDown(self):
        self.assertEqual(self.source.read_bytes(), self.original_bytes)

    def intent(self, phase=3):
        return build_intent(self.source, 'bank', self.devices, self.baseline,
                            self.comps, self.nets, self.index, phase=phase)

    def candidates(self, policy='auto', iterations=120, phase=3):
        with patch('netlist2schematic.routing.orthogonal_router.route_scene', side_effect=AssertionError('A* entered placement')):
            return list(structured.create_candidates(self.source, cell='bank', iterations=iterations,
                         seed=7921, deadline=None, structure_layout=policy, structure_phase=phase))

    def test_one_source_table_recovers_only_the_proven_input_pair(self):
        original = deepcopy((self.devices, self.baseline, self.comps, self.nets, self.index))
        self.assertFalse(any(row['kind'] == 'differential_pair' for row in self.baseline))
        evidence, relations = self.intent()
        pairs = differential_relations(relations)
        self.assertEqual([set(row['members']) for row in pairs], [{'Ma', 'Mb'}])
        self.assertTrue(pairs[0]['roles']['validated_shared_source'])
        self.assertEqual(set(pairs[0]['roles']['shared_tails']), {'Mt'})
        self.assertEqual(pairs[0]['roles']['exclusive_tails'], [])
        self.assertEqual(relations, build_layout_relations(self.devices, self.baseline,
                                                         validated_evidence=evidence))
        self.assertEqual((self.devices, self.baseline, self.comps, self.nets, self.index), original)
        json.dumps(evidence, allow_nan=False)

    def test_candidate_one_is_exactly_the_untouched_off_reference(self):
        old = self.candidates('off')[0]
        new = self.candidates('auto')[0]
        for before, after in ((old[0].cell_of, new[0].cell_of),
                              (old[0].orient, new[0].orient),
                              (old[0].column_x, new[0].column_x),
                              (old[0].row_y, new[0].row_y),
                              (old[0].rng.getstate(), new[0].rng.getstate()),
                              (old[4]['energy'], new[4]['energy']),
                              (old[4]['optimization_metrics'], new[4]['optimization_metrics'])):
            self.assertEqual(before, after)

    def test_auto_new_candidates_consume_calibrated_pairs_in_sa_and_local_owners(self):
        for pl, comps, nets, index, metadata in self.candidates()[1:]:
            with self.subTest(candidate=metadata['candidate_id']):
                evidence = metadata['layout_evidence']
                self.assertTrue(evidence.get('consumption'))
                self.assertEqual([set(row['members']) for row in differential_relations(
                    metadata['layout_relations'])], [{'Ma', 'Mb'}])
                self.assertIn({'Ma', 'Mb'}, [set(pair) for pair in metadata['sym_pairs']])
                owners = [set(comps[i]['id'] for i in group) for group in pl.blocks]
                self.assertIn({'Ma', 'Mb'}, owners)
                self.assertTrue(pl.blocks_contiguous())
                self.assertEqual(pl.row_of, [row for row, _ in pl.cell_of])
                self.assertEqual(len(set(pl.cell_of)), len(comps))
                json.dumps(evidence, allow_nan=False)

    def test_shared_tail_is_not_reintroduced_as_a_centering_target(self):
        from netlist2schematic.plugins.layout_shared.branch_alignment import _model
        for pl, comps, nets, index, metadata in self.candidates(iterations=0)[1:]:
            context = prepare_branch_context(self.source, 'bank', pl, comps, nets, index, metadata)
            pair = differential_relations(context['layout_relations'])[0]
            self.assertEqual(pair['roles']['exclusive_tails'], [])
            self.assertIn('Mt', pair['roles']['shared_tails'])
            model = _model(structured.par, pl, comps, nets, index, context)
            self.assertIn({'Ma', 'Mb'}, [set(row['devices']) for row in model['inputs']])
            self.assertFalse(any(row['tail'][0] == 'Mt' for row in model['tails']))

    def test_explicit_parenthood_and_low_level_ownership_remain_consistent(self):
        evidence, relations = self.intent()
        by_id = {row['id']: row for row in relations}
        owned = Counter(name for row in relations for name in row.get('owned_members', ()))
        self.assertTrue(all(count == 1 for count in owned.values()))
        for row in relations:
            parent = row.get('parent')
            if parent is not None:
                self.assertIn(parent, by_id)
                self.assertTrue(by_id[parent]['macro'])
                self.assertLessEqual(set(row['members']), set(by_id[parent]['members']))
                self.assertIn(row['id'], by_id[parent]['children'])
                seen, ancestor = {row['id']}, parent
                while ancestor is not None:
                    self.assertNotIn(ancestor, seen)
                    seen.add(ancestor)
                    ancestor = by_id[ancestor].get('parent')
        blocks, macros = placement_blocks(self.comps, self.index, relations)
        self.assertEqual(sorted(i for group in blocks for i in group), list(range(len(self.comps))))
        self.assertTrue(all(len(group) == len(set(group)) for group in macros))

    def test_reference_and_new_candidate_measurements_rebuild_the_same_source_relations(self):
        reference = self.candidates('off', iterations=0)[0]
        new = self.candidates('auto', iterations=0)[1]
        contexts = [prepare_branch_context(self.source, 'bank', pl, comps, nets, index, metadata)
                    for pl, comps, nets, index, metadata in (reference, new)]
        self.assertEqual(contexts[0]['layout_relations'], contexts[1]['layout_relations'])
        self.assertEqual(contexts[0]['layout_relations'], self.intent()[1])
        self.assertEqual(contexts[0]['layout_evidence']['version'], contexts[1]['layout_evidence']['version'])
        self.assertEqual(contexts[0]['external_control_ports'], contexts[1]['external_control_ports'])

    def test_off_does_not_enter_the_new_evidence_builder_or_astar(self):
        with patch('netlist2schematic.plugins.layout_shared.layout_intent.build_intent', side_effect=AssertionError('off entered new evidence')):
            rows = self.candidates('off', iterations=0)
        self.assertEqual([row[4]['candidate_id'] for row in rows], [1, 2, 3])
        self.assertTrue(all('layout_evidence' not in row[4] for row in rows))

    def test_zero_iteration_initialization_uses_no_final_router(self):
        with patch('netlist2schematic.routing.orthogonal_router.route_scene', side_effect=AssertionError('global A* invoked')):
            evidence, relations = self.intent()
            rows = self.candidates('auto', iterations=0)
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(differential_relations(relations)), 1)
        self.assertEqual(evidence['phase'], 3)

    def test_calibrated_rejection_cannot_be_overruled_by_legacy_positive_records(self):
        devices = deepcopy(self.devices)
        for name in ('Ma', 'Mb', 'Mra', 'Mrb'):
            devices[name]['s'] = 'vdd'
        legacy = deepcopy(self.baseline)
        legacy.append({'id': 'differential_pair:Ma|Mb', 'kind': 'differential_pair',
                       'devices': ['Ma', 'Mb'], 'confidence': 'high',
                       'roles': {'left': 'Ma', 'right': 'Mb', 'tail': ['Mt']},
                       'nets': {'source': 'vdd'}, 'parent': None, 'children': [], 'owner': {},
                       'layout': {'align_rows': [['Ma', 'Mb']]}})
        evidence = build_layout_evidence(devices, legacy, formal_ports=('ip', 'im'))
        relations = build_layout_relations(devices, legacy, validated_evidence=evidence)
        self.assertFalse(differential_relations(relations))
        self.assertTrue(evidence['diagnostics']['rejected_pairs'])
        self.assertFalse(any(set(pair) == {'Ma', 'Mb'} for row in relations
                             for pair in row.get('layout', {}).get('pairs', ())))

    def test_accepted_best_one_feedback_regroups_before_alignment_and_routing(self):
        """Only an already-compatible proposal is adopted; the guard stays intact."""
        from netlist2schematic.flow import aesthetic_flow
        from netlist2schematic.plugins.layout_shared import layout_relations
        candidates = self.candidates(iterations=0)
        row_plan = deepcopy(candidates[2][4]['branch_row_plan'])
        regroup = layout_relations.prepare_structure_placement
        events, aligned, route_count = [], {}, 0

        def feedback(pl, comps, nets, index, metadata, quality):
            self.assertEqual(metadata['candidate_id'], 1)
            self.assertNotIn({'Ma', 'Mb'}, [set(comps[i]['id'] for i in group) for group in pl.blocks])
            events.append(('feedback', 3))
            proposal = deepcopy((pl, comps, nets, index, metadata))
            # The real flow rejects incompatible row plans before adoption.
            # This test starts at the accepted-proposal boundary, retaining
            # candidate 1 geometry while explicitly providing a compatible plan.
            proposal[4]['branch_row_plan'] = deepcopy(row_plan)
            proposal[4]['feedback'] = {'source_candidate': '1', 'action': 'test_accepted_proposal'}
            return proposal

        def repack(pl, comps, index, relations):
            events.append(('regroup', 3))
            return regroup(pl, comps, index, relations)

        def align(par, pl, comps, nets, index, metadata, **kwargs):
            identifier = metadata['candidate_id']
            events.append(('post_alignment', identifier))
            if identifier == 3:
                self.assertIn({'Ma', 'Mb'}, [set(comps[i]['id'] for i in group) for group in pl.blocks])
                self.assertTrue(pl.blocks_contiguous())
                stages = metadata['layout_evidence']['consumption']['stages']
                self.assertIn('feedback_blocks', [row['stage'] for row in stages])
                aligned[3] = deepcopy(metadata)
            return pl, metadata

        def route(par, nets, pl, index, **kwargs):
            nonlocal route_count
            route_count += 1
            events.append(('final_route', route_count))
            if route_count == 3:
                self.assertIn({'Ma', 'Mb'}, [set(pl.comps[i]['id'] for i in group) for group in pl.blocks])
            return [], {'wirelength': 100, 'crossings': 0, 'bends': 0}, {'valid': True}

        quality_count = 0
        def quality(*args, **kwargs):
            nonlocal quality_count
            quality_count += 1
            return {'score': float(quality_count), 'per_net': {'detour': {
                'detour_factor': 3, 'excess_detour': 600}} if quality_count == 1 else {}}

        with patch('netlist2schematic.layout.structured_placement.create_candidates', return_value=iter(candidates)), \
             patch('netlist2schematic.layout.structured_placement.feedback_candidate', side_effect=feedback), \
             patch('netlist2schematic.plugins.layout_shared.layout_relations.prepare_structure_placement', side_effect=repack), \
             patch('netlist2schematic.plugins.layout_shared.branch_alignment.regularize_branch_layout', side_effect=align), \
             patch('netlist2schematic.flow.aesthetic_flow.final_route', side_effect=route), \
             patch('netlist2schematic.layout.layout_quality.evaluate_quality', side_effect=quality):
            result = aesthetic_flow.select_layout(self.source, cell='bank', iterations=0,
                        structure_layout='auto', candidate_id='3')
        self.assertEqual([row for row in events if row[1] == 3],
                         [('feedback', 3), ('regroup', 3), ('post_alignment', 3), ('final_route', 3)])
        metadata = result[4]
        self.assertEqual(metadata['feedback']['source_candidate'], '1')
        consumption = metadata['layout_evidence']['consumption']
        self.assertEqual((consumption['policy'], consumption['candidate_id']), ('candidate_layout', 3))
        stages = [row['stage'] for row in consumption['stages']]
        self.assertLess(stages.index('feedback_blocks'), stages.index('post_alignment'))
        self.assertIn(3, aligned)

    def test_missing_or_nonmacro_parent_is_rejected_without_changing_evidence(self):
        evidence, _ = self.intent()
        for parent in ('not-an-existing-relation', 'leaf-parent'):
            with self.subTest(parent=parent):
                changed = deepcopy(evidence)
                pair = differential_relations(changed['relations'])[0]
                pair['parent'] = parent
                if parent == 'leaf-parent':
                    changed['relations'].append({'id': parent, 'kind': 'ordinary_relation',
                        'members': list(pair['members']), 'macro': False, 'parent': None})
                original = deepcopy(changed)
                with self.assertRaisesRegex(ValueError, 'Invalid explicit layout parent'):
                    build_layout_relations(self.devices, validated_evidence=changed)
                self.assertEqual(changed, original)

    def test_cyclic_macro_parent_chain_is_rejected_before_ownership_resolution(self):
        evidence = {'relations': [
            {'id': 'macro-a', 'kind': 'paired_branches', 'members': ['Ma', 'Mb'],
             'macro': True, 'parent': 'macro-b'},
            {'id': 'macro-b', 'kind': 'paired_branches', 'members': ['Ma', 'Mb'],
             'macro': True, 'parent': 'macro-a'}]}
        original = deepcopy(evidence)
        with self.assertRaisesRegex(ValueError, 'Cyclic explicit layout parents'):
            build_layout_relations(self.devices, validated_evidence=evidence)
        self.assertEqual(evidence, original)


if __name__ == '__main__':
    unittest.main()
