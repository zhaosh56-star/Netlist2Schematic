"""Shared visible-port motifs for MOS and BJT; never convert electrical data.

The drain/control/source roles describe topology, not device equivalence.
Families and polarities stay distinct. CMOS-specific composites live in the
ordinary recognizer and are deliberately not inferred for bipolar devices.
"""
from collections import defaultdict
from itertools import combinations

FIELDS = {'MOS': ('d', 'g', 's'), 'BJT': ('c', 'b', 'e')}
PINS = {'MOS': ('D', 'G', 'S'), 'BJT': ('C', 'B', 'E')}
TYPES = {'MOS': ('NMOS', 'PMOS'), 'BJT': ('NPN', 'PNP')}
ALIASES = {'bjt_differential_pair': 'differential_pair',
           'bjt_current_mirror': 'current_mirror'}
CHANNELS = {'nmos': ('D', 'S'), 'pmos': ('D', 'S'),
            'npn': ('C', 'E'), 'pnp': ('C', 'E')}
CONTROLS = {'nmos': 'G', 'pmos': 'G', 'npn': 'B', 'pnp': 'B'}


def common_kind(kind):
    """Accept historical BJT tags at layout seams without rewriting saved JSON."""
    return ALIASES.get(kind, kind)


def channel_nets(device):
    fields = FIELDS.get(device.get('cls'))
    return tuple(device[f] for f in fields) if fields else None


def channel_pins(device):
    return PINS.get(device.get('cls'))


def basic_structures(devices, family, ordered, rails):
    """Return shared, conservative motif descriptions in deterministic order.

    ``rails`` comes from the caller's supply policy. Reference/output and tail
    identities remain explicit. Larger banks never become arbitrary pairs.
    """
    if family not in FIELDS:
        raise ValueError('Unknown transistor family: '+str(family))
    typed = {n: d for n, d in devices.items()
             if d.get('cls') == family and d.get('type') in TYPES[family]}
    ports = {n: channel_nets(d) for n, d in typed.items()}
    all_channels = {n: channel_nets(d) for n, d in devices.items() if d.get('cls') in FIELDS}
    sources, bias = defaultdict(list), defaultdict(list)
    for name, device in typed.items():
        drain, control, source = ports[name]
        sources[source].append(name)
        bias[(device['type'], source, control)].append(name)
    result = {kind: [] for kind in ('differential_pair', 'current_mirror',
        'common_bias_pair', 'common_source_bank', 'cross_coupled', 'series_branch')}

    def add(kind, members, roles, nets, confidence='high', layout=None):
        result[kind].append(dict(kind=kind, members=members, roles=roles, nets=nets,
                                 confidence=confidence, layout=layout))

    for source, names in sources.items():
        for polarity in TYPES[family]:
            pair = ordered(n for n in names if typed[n]['type'] == polarity)
            if source in rails or len(pair) != 2:
                continue
            left, right = pair
            a, b = ports[left], ports[right]
            if a[0] == b[0] or a[1] == b[1]:
                continue
            tails = ordered([n for n, p in all_channels.items() if n not in pair and p[0] == source]
                + [n for n, d in devices.items() if d.get('cls') == 'ISRC' and source in (d['p'], d['n'])])
            if not tails:
                continue
            add('differential_pair', pair, {'left': left, 'right': right, 'tail': tails},
                {'source': source, 'left_drain': a[0], 'right_drain': b[0]},
                layout={'align_rows': [pair], 'aligned': [], 'branches': [[left], [right]]})
    for (_, source, control), names in bias.items():
        names = ordered(names)
        if len(names) < 2:
            continue
        refs = ordered(n for n in names if ports[n][0] == control)
        outputs = ordered(n for n in names if ports[n][0] != control)
        if refs and outputs:
            add('current_mirror', names, {'reference': refs[0], 'references': refs, 'outputs': outputs},
                {'source': source, 'bias': control},
                layout={'align_rows': [names], 'aligned': [], 'branches': []})
        elif len({ports[n][0] for n in names}) > 1:
            add('common_bias_pair', names, {'members': names}, {'source': source, 'bias': control}, 'relation')
    for source, names in sources.items():
        if len(names) >= 3:
            add('common_source_bank', ordered(names), {'members': ordered(names)}, {'source': source}, 'relation')
    for left, right in combinations(ordered(typed), 2):
        a, b = ports[left], ports[right]
        if (typed[left]['type'] == typed[right]['type'] and a[2] == b[2] and a[0] != b[0]
                and a[1] == b[0] and b[1] == a[0]):
            add('cross_coupled', [left, right], {'left': left, 'right': right},
                {'left_output': a[0], 'right_output': b[0]},
                layout={'align_rows': [[left, right]], 'aligned': [], 'branches': []})
    for upstream, a in ports.items():
        for downstream in sources.get(a[0], ()):
            if upstream == downstream or typed[upstream]['type'] != typed[downstream]['type']:
                continue
            upper = upstream if typed[upstream]['type'] == TYPES[family][1] else downstream
            lower = downstream if typed[upstream]['type'] == TYPES[family][1] else upstream
            add('series_branch', [upstream, downstream], {'upper': upper, 'lower': lower},
                {'junction': a[0]}, 'relation',
                {'aligned': [[upstream, downstream]], 'align_columns': [[upstream, downstream]],
                 'branches': [[upper, lower]]})
    return result
