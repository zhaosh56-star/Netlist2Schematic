from copy import deepcopy
import unittest

from integrations.auto_canvas_huyangc.native_plan import build_plan
from integrations.auto_canvas_huyangc.profile import compile_snapshot
from integrations.auto_canvas_huyangc.tests.test_boundary import snapshot


def fixture(name='VDD'):
    source=snapshot();doc=source['project']['documents'][0]
    source['logicalNets'][0].update(name=name,powerDomain='none')
    shape=dict(id='port',viewBox=dict(x=-14,y=-7,width=18,height=14),primitives=[],
        pins=[dict(name='P',at=dict(x=0,y=0),direction='east')])
    source['symbols']['formal']=dict(definition=shape,variant=None,
        pins=[dict(shape['pins'][0],landing=dict(x=0,y=0))])
    doc['instances'].append(dict(id='formal',symbolId='port',placement=None))
    doc['nets'][0]['terminals'].append(dict(instanceId='formal',pinName='P'))
    doc['netlist']=dict(name='cell',formalParameters=[],terminals=[dict(
        id='rail-terminal',name=name,direction='passive',netId='a',interfaceInstanceIds=['formal'])])
    profile=compile_snapshot(source);alias=profile['net_names']['a']
    symbol='vee-port' if alias=='vee' else 'vdd-port' if alias in ('vdd','vcc') else 'ground'
    wire=dict(validation=dict(valid=True),metrics={},components=[dict(instance_id='R-original',
        position=dict(x=100,y=100),rotation=0,mirror='none',
        pins=[dict(name='1',net=alias,at=[100,80]),dict(name='2',net='output',at=[100,120])])],
        power=[dict(component_id='R1',pin='1',net=alias,symbol=symbol,anchor=[100,40],
                    origin=[100,20],segments=[[[100,80],[100,40]]])],routes={})
    return source,profile,wire


class SupplyGlyphTests(unittest.TestCase):
    def test_formal_supply_artwork_preserves_port_contract_and_source_data(self):
        for name,style,strokes in [('VDD','positive-cap',2),('VEE','negative-cap',2),
                                   ('VSS','negative-cap',2),('GND','ground',4),('0','ground',4)]:
            with self.subTest(name=name):
                source,profile,wire=fixture(name);before=deepcopy(source)
                plan=build_plan(source,profile,wire)
                self.assertEqual(source,before)
                self.assertEqual(plan['symbolOverrides'],[])
                self.assertFalse(any(e['kind']=='add_instance' for e in plan['edits']))
                self.assertFalse(any(e['kind']=='upsert_connectivity_evidence' and
                    'powerDomain' in e['evidence'] for e in plan['edits']))
                glyph=plan['powerPresentation']['glyphs'][0]
                self.assertEqual((glyph['style'],len(glyph['strokes'])),(style,strokes))
                self.assertEqual(glyph['owner'],dict(kind='terminal',instanceId='formal',pinName='P'))
                self.assertEqual(glyph['strokes'][0]['fromOffset'],glyph['anchorOffset'])
                labels=[e['annotation'] for e in plan['edits'] if e['kind']=='upsert_schematic_annotation']
                self.assertTrue(any(a['binding']==dict(kind='cell-terminal-name',terminalId='rail-terminal')
                                    for a in labels))

    def test_repeated_rail_has_one_glyph_per_local_named_connection(self):
        source,profile,wire=fixture()
        second=deepcopy(wire['power'][0]);second.update(component_id='R2',anchor=[200,40],
            origin=[200,20],segments=[[[200,80],[200,40]]])
        wire['power'].append(second)
        plan=build_plan(source,profile,wire)
        glyphs=plan['powerPresentation']['glyphs']
        self.assertEqual(len(glyphs),2)
        self.assertEqual([g['owner']['kind'] for g in glyphs],['terminal','junction'])
        claims=[e['evidence'] for e in plan['edits'] if e['kind']=='upsert_connectivity_evidence']
        self.assertEqual(len(claims),1)
        self.assertEqual(claims[0]['owner']['kind'],'net-label')
        self.assertNotIn('powerDomain',claims[0])

    def test_cleanup_replaces_only_adapter_artwork_and_its_managed_supply_labels(self):
        source,profile,wire=fixture();doc=source['project']['documents'][0]
        doc['drafting']=dict(objects=[dict(id='n2s-supply-ink-'+'a'*18,kind='arrow'),
                                     dict(id='user-line',kind='arrow')])
        doc['annotations']=[dict(id='n2s-label-old',kind='net-label',netId='a',
            anchor=dict(kind='object',objectId='n2s-junction-old')),
            dict(id='user-label',kind='net-label',netId='a',anchor=dict(kind='free',position=dict(x=0,y=0)))]
        plan=build_plan(source,profile,wire)
        removed_objects=[e['objectId'] for e in plan['edits'] if e['kind']=='remove_drafting_object']
        self.assertEqual(removed_objects,['n2s-supply-ink-'+'a'*18])
        removed_labels=[e['annotationId'] for e in plan['edits'] if e['kind']=='remove_schematic_annotation']
        self.assertEqual(removed_labels,['n2s-label-old'])

    def test_artwork_offsets_and_role_are_invariant_under_layout_translation(self):
        source,profile,wire=fixture('VEE');before=build_plan(source,profile,wire)
        shifted=deepcopy(wire)
        for c in shifted['components']:
            c['position']['x']+=300;c['position']['y']-=200
            for p in c['pins']:p['at']=[p['at'][0]+300,p['at'][1]-200]
        for p in shifted['power']:
            for key in ('anchor','origin'):p[key]=[p[key][0]+300,p[key][1]-200]
            p['segments']=[[[a[0]+300,a[1]-200],[b[0]+300,b[1]-200]] for a,b in p['segments']]
        after=build_plan(source,profile,shifted)
        self.assertEqual(before['powerPresentation'],after['powerPresentation'])

    def test_formal_label_keeps_authored_typography_and_binding(self):
        source,profile,wire=fixture();doc=source['project']['documents'][0]
        label=dict(id='original-name',kind='instance-label',locked=False,rotation=90,alignment='middle',
            binding=dict(kind='cell-terminal-name',terminalId='rail-terminal'),
            formatOverride=dict(runs=[dict(kind='text',value='VDD')]),
            anchor=dict(kind='object',objectId='formal',localOffset=dict(x=0,y=0),fallbackPosition=dict(x=0,y=0)))
        doc['annotations'].append(label)
        plan=build_plan(source,profile,wire)
        updated=next(e['annotation'] for e in plan['edits'] if e['kind']=='upsert_schematic_annotation'
                     and e['annotation']['id']==label['id'])
        self.assertEqual(updated['id'],label['id'])
        self.assertEqual(updated['binding'],label['binding'])
        self.assertEqual(updated['formatOverride'],label['formatOverride'])

    def test_unrenderable_supply_resource_fails_instead_of_claiming_a_visible_glyph(self):
        source,profile,wire=fixture()
        source['markerSymbols']['vdd-port']['definition']['primitives']=[]
        with self.assertRaisesRegex(ValueError,'no drawable artwork'):build_plan(source,profile,wire)


if __name__=='__main__':unittest.main()
