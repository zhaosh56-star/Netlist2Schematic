"""Electrical-neutral supply artwork, attached to existing native contacts."""
from copy import deepcopy
import re

from .profile import transform


VERSION = 'native-supply-glyph-v1'
INK_ID = re.compile(r'n2s-supply-ink-[0-9a-f]{18}\Z')


class SupplyGlyphs:
    def __init__(self, snapshot, document, edits, id_for, supply_nets):
        self.snapshot, self.document, self.edits, self.id_for = snapshot, document, edits, id_for
        self.records = []
        # Re-layout replaces only this adapter's own artwork and local supply
        # labels. User drawings, authored labels and formal declarations stay.
        for item in (document.get('drafting') or {}).get('objects', []):
            if item['kind'] == 'arrow' and INK_ID.fullmatch(item['id']):
                edits.append(dict(kind='remove_drafting_object', objectId=item['id']))
        self.removed_labels = {a['id'] for a in document['annotations']
            if a.get('kind') == 'net-label' and a['id'].startswith('n2s-label-')
            and a.get('netId') in supply_nets
            and a.get('anchor', {}).get('objectId', '').startswith('n2s-junction-')}
        for ident in sorted(self.removed_labels):
            edits.append(dict(kind='remove_schematic_annotation', annotationId=ident))
        # The native remove-annotation edit also removes its owned name claim.

    def add(self, item, net_id, endpoint, owner_position):
        """Draw native-library supply strokes without adding a power-domain claim.

        Draft arrows with both arrowheads disabled are anchored vector lines;
        unlike unbound construction lines they follow a moved naming owner.
        Artwork alone has no pins and cannot change a Cell's exported contract.
        """
        negative = item['symbol'] == 'vee-port' or item['net'].casefold() in ('vss', 'vss!')
        symbol_id = 'vdd-port' if negative else item['symbol']
        rotation = 180 if negative else 0
        definition = self.snapshot['markerSymbols'][symbol_id]['definition']
        pin = definition['pins'][0]
        pin_offset = transform((pin['at']['x'], pin['at']['y']), rotation, 'none')
        contact = dict(zip(('x', 'y'), item['anchor']))
        origin = {axis: contact[axis]-value for axis, value in zip(('x','y'), pin_offset)}
        owner_id = endpoint['instanceId'] if endpoint['kind'] == 'terminal' else endpoint['junctionId']

        def anchor(position):
            return dict(kind='object', objectId=owner_id,
                localOffset={a:position[a]-owner_position[a] for a in ('x','y')},
                fallbackPosition=position)

        def world(raw):
            local = transform((raw['x'], raw['y']), rotation, 'none')
            # Native drafting coordinates are integer page units. Only artwork
            # is rounded; the actual pin/contact and every conductor stay exact.
            return {a:round(origin[a]+v) for a,v in zip(('x','y'), local)}

        gid = self.id_for('supply-glyph', [net_id, item['component_id'], item['pin']])
        strokes = []
        for primitive in definition['primitives']:
            scale = 1.
            if primitive['kind'] == 'line':
                a,b = primitive['from'],primitive['to']
            elif primitive['kind'] == 'polygon':
                # The native VDD cap is a thin filled rectangular bar. A bold
                # vector line inherits the document foreground in both themes.
                xs,ys = {p['x'] for p in primitive['points']},{p['y'] for p in primitive['points']}
                if len(xs) != 2 or len(ys) != 2:
                    raise ValueError('Unsupported native supply cap artwork')
                y = (min(ys)+max(ys))/2
                a,b = dict(x=min(xs),y=y),dict(x=max(xs),y=y)
                scale = 2.
            else:
                raise ValueError('Unsupported native supply artwork: '+primitive['kind'])
            a,b = world(a),world(b)
            if a == b:
                raise ValueError('Native supply artwork collapsed on the page grid')
            ident = self.id_for('supply-ink', [gid, len(strokes)])
            obj = dict(id=ident,kind='arrow',locked=False,zIndex=0,
                anchor=anchor(contact),from_=anchor(a),to=anchor(b),
                styleOverride=dict(arrowHead='none',arrowStart='none',arrowEnd='none',strokeScale=scale))
            obj['from'] = obj.pop('from_')
            self.edits.append(dict(kind='upsert_drafting_object',object=obj))
            strokes.append(dict(objectId=ident,fromOffset=obj['from']['localOffset'],
                toOffset=obj['to']['localOffset'],strokeScale=scale))
        if not strokes:
            raise ValueError('Native supply symbol has no drawable artwork')
        self.records.append(dict(id=gid,netId=net_id,owner=deepcopy(endpoint),
            anchorOffset={a:contact[a]-owner_position[a] for a in ('x','y')},
            style='negative-cap' if negative else 'ground' if symbol_id=='ground' else 'positive-cap',
            sourceSymbol=symbol_id,rotation=rotation,strokes=strokes))
        return dict(x=round(origin['x']+14),y=round(origin['y']+(4 if negative or symbol_id=='ground' else 10)))

    def move_formal_label(self, ident, net_id, owner_position, label_position):
        declaration = next(t for t in self.document['netlist']['terminals']
                           if ident in t['interfaceInstanceIds'] and t['netId'] == net_id)
        binding = dict(kind='cell-terminal-name',terminalId=declaration['id'])
        existing = [a for a in self.document['annotations'] if a.get('binding') == binding]
        if not existing:
            existing = [dict(id=self.id_for('label',[ident,'formal-supply']),kind='instance-label',
                             binding=binding,locked=False)]
        for original in existing:
            label = deepcopy(original)
            label.update(anchor=dict(kind='object',objectId=ident,
                localOffset={a:label_position[a]-owner_position[a] for a in ('x','y')},
                fallbackPosition=label_position),alignment='start',rotation=0)
            self.edits.append(dict(kind='upsert_schematic_annotation',annotation=label))

    def report(self):
        return dict(version=VERSION,glyphs=self.records)
