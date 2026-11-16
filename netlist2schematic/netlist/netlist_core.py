"""Shared structural SPICE reader for one flat circuit or an explicit --cell."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import warnings

SRC_ALIASES = frozenset({'vdd', 'vdd!', 'vpwr', 'vcc', 'vcc!', 'avdd', 'dvdd'})
SNK_ALIASES = frozenset({'0', 'gnd', 'gnd!', 'vss', 'vss!', 'gnds', 'agnd', 'dgnd'})


class NetlistError(ValueError):
    """An unsupported or malformed source statement, including its location."""


class NetlistCompatibilityWarning(UserWarning):
    """An explicit, source-located compatibility interpretation."""


@dataclass(frozen=True)
class SourceLine:
    path: Path
    number: int
    text: str

    def error(self, message: str) -> NetlistError:
        return NetlistError(f'{self.path}:{self.number}: {message}')


def _logical_lines(path: Path, ancestors: tuple[Path, ...] = ()) -> list[SourceLine]:
    path = path.resolve()
    if path in ancestors:
        chain = ' -> '.join(str(p) for p in (*ancestors, path))
        raise NetlistError(f'{path}: recursive .include: {chain}')
    if len(ancestors) >= 32:
        raise NetlistError(f'{path}: .include nesting exceeds 32 files')
    try:
        physical = path.read_text(encoding='utf-8-sig').splitlines()
    except (OSError, UnicodeError) as exc:
        raise NetlistError(f'{path}: cannot read UTF-8 netlist: {exc}') from exc
    logical: list[SourceLine] = []
    for number, raw in enumerate(physical, 1):
        text = raw.strip()
        if not text or text.startswith('*'):
            continue
        text = re.split(r'[$;]', text, maxsplit=1)[0].strip()
        if not text:
            continue
        if text.startswith('+'):
            if not logical:
                raise SourceLine(path, number, text).error('continuation has no preceding statement')
            previous = logical[-1]
            logical[-1] = SourceLine(path, previous.number, previous.text + ' ' + text[1:].strip())
        else:
            logical.append(SourceLine(path, number, text))
    expanded: list[SourceLine] = []
    include_re = re.compile(r'''^\.(?:include|inc)\s+(?:"([^"]+)"|'([^']+)'|(\S+))\s*$''', re.I)
    for line in logical:
        directive = line.text.split()[0].casefold()
        if directive not in ('.include', '.inc'):
            expanded.append(line)
            continue
        match = include_re.match(line.text)
        if not match:
            raise line.error('expected .include "local model file"')
        target = next(group for group in match.groups() if group is not None)
        include_path = Path(target)
        if not include_path.is_absolute():
            include_path = path.parent / include_path
        try:
            expanded.extend(_logical_lines(include_path, (*ancestors, path)))
        except NetlistError as exc:
            raise line.error(str(exc)) from exc
    return expanded


_IGNORED_DIRECTIVES = frozenset({
    '.param', '.parameters', '.global', '.end', '.title', '.op', '.dc', '.ac',
    '.tran', '.noise', '.save', '.probe', '.print', '.plot', '.option', '.options',
    '.temp', '.ic', '.nodeset', '.measure', '.meas', '.func', '.width',
    '.protect', '.unprotect',
})


def _partition(lines: list[SourceLine], *, root_path=None):
    top: list[SourceLine] = []
    cells: dict[str, list[SourceLine]] = {}
    active: str | None = None
    control = False
    has_definition = any(line.text.split()[0].casefold() == '.subckt' for line in lines)
    single_file = len({line.path for line in lines}) == 1
    for position, line in enumerate(lines):
        tokens = line.text.split()
        directive = tokens[0].casefold()
        if directive == '.control':
            control = True
            continue
        if directive == '.endc':
            control = False
            continue
        if control:
            continue
        if directive == '.subckt':
            if active is not None:
                raise line.error('nested .subckt definitions are not supported')
            if len(tokens) < 2:
                raise line.error('.subckt requires a name')
            active = tokens[1].casefold()
            if active in cells:
                raise line.error(f'duplicate subcircuit {tokens[1]!r}')
            cells[active] = []
            continue
        if directive == '.ends':
            if active is None:
                # Some flat exports finish with an unnamed .ends. Do not
                # hide missing/named definitions, trailing devices, or an
                # orphan terminator coming from an included model file.
                if (len(tokens) == 1 and not has_definition and single_file and top
                        and (root_path is None or line.path == Path(root_path).resolve())
                        and all(tail.text.casefold() == '.end' for tail in lines[position+1:])):
                    warnings.warn(str(line.error('interpreting terminal unnamed .ends as a flat-circuit terminator')),
                                  NetlistCompatibilityWarning, stacklevel=2)
                    continue
                raise line.error('.ends has no matching .subckt')
            if len(tokens) > 1 and tokens[1].casefold() != active:
                raise line.error(f'.ends {tokens[1]} does not match {active}')
            active = None
            continue
        (top if active is None else cells[active]).append(line)
    if active is not None:
        raise NetlistError(f'{lines[-1].path}: unclosed .subckt {active}')
    if control:
        raise NetlistError(f'{lines[-1].path}: unclosed .control block')
    return top, cells


def _model_types(lines: list[SourceLine]) -> dict[str, str]:
    models: dict[str, str] = {}
    for line in lines:
        if line.text.split()[0].casefold() != '.model':
            continue
        match = re.match(r'^\.model\s+(\S+)\s+([A-Za-z][A-Za-z0-9_]*)', line.text, re.I)
        if not match:
            raise line.error('malformed .model declaration')
        name, kind = match.groups()
        name, kind = name.casefold(), kind.upper()
        if name in models and models[name] != kind:
            raise line.error(f'conflicting types for model {name!r}')
        models[name] = kind
    return models


def _inferred_model(model: str, family: str) -> str | None:
    words = {'MOS': (('nmos', 'nfet'), ('pmos', 'pfet')),
             'BJT': (('npn',), ('pnp',))}[family]
    result = ('NMOS', 'PMOS') if family == 'MOS' else ('NPN', 'PNP')
    for candidates, kind in zip(words, result):
        for name in candidates:
            if re.search(r'(?:^|[_\-.])' + name + r'(?:$|[_\d\-.])', model, re.I):
                return kind
    return None


def _semiconductor_type(model: str, family: str, models: dict[str, str], line: SourceLine) -> str:
    allowed = {'MOS': {'NMOS', 'PMOS'}, 'BJT': {'NPN', 'PNP'}}[family]
    declared = models.get(model.casefold())
    if declared is not None:
        if declared not in allowed:
            raise line.error(f'model {model!r} has type {declared}, expected {family}')
        return declared
    inferred = _inferred_model(model, family)
    if inferred:
        return inferred
    raise line.error(f'cannot determine {family} polarity for model {model!r}; add a local .model declaration')


def classify_tokens(tokens: list[str], models: dict[str, str], line: SourceLine) -> dict:
    name = tokens[0]
    family = name[0].upper()
    net = lambda value: value.casefold()

    def need(count, shape):
        if len(tokens) < count:
            raise line.error(f'{name}: expected {shape}')

    if family == 'X':
        # X has variable node counts. The model precedes named parameters;
        # neither a final parameter nor a node is a reliable model position.
        end = next((i for i, token in enumerate(tokens[1:], 1)
                    if '=' in token or token.casefold() == 'params:'), len(tokens))
        head = tokens[:end]
        if len(head) < 3:
            raise line.error(f'{name}: expected X nodes model [parameters]')
        model, nodes = head[-1], head[1:-1]
        declared = models.get(model.casefold())
        if model.casefold() in ('npn', 'pnp') or declared in ('NPN', 'PNP'):
            if len(nodes) not in (3, 4):
                raise line.error(f'{name}: typed X BJT expects C B E [substrate] model [parameters]')
            kind = _semiconductor_type(model, 'BJT', models, line)
            result = {'cls': 'BJT', 'type': kind, 'c': net(nodes[0]),
                      'b': net(nodes[1]), 'e': net(nodes[2]), 'model': model}
            if len(nodes) == 4:
                result['substrate'] = net(nodes[3])
            return result
        if re.search(r'(?:^|[_\-.])(?:nfet|pfet)(?:$|[_\d\-.])', model, re.I):
            if len(nodes) != 4:
                raise line.error(f'{name}: X MOS expects D G S B model [parameters]')
            kind = _semiconductor_type(model, 'MOS', models, line)
            return {'cls': 'MOS', 'type': kind, 'd': net(nodes[0]), 'g': net(nodes[1]),
                    's': net(nodes[2]), 'bulk': net(nodes[3]), 'model': model}
        raise line.error(f'{name}: unsupported X instance {model!r}; supported mappings are nfet/pfet MOS and explicitly typed NPN/PNP BJT')
    if family == 'M':
        need(6, 'name D G S B model [parameters]')
        model = tokens[5]
        kind = _semiconductor_type(model, 'MOS', models, line)
        return {'cls': 'MOS', 'type': kind, 'd': net(tokens[1]), 'g': net(tokens[2]),
                's': net(tokens[3]), 'bulk': net(tokens[4]), 'model': model}
    if family == 'Q':
        need(5, 'name C B E [substrate] model [parameters]')
        model_index = 4
        if models.get(tokens[4].casefold()) not in ('NPN', 'PNP') and not _inferred_model(tokens[4], 'BJT'):
            if len(tokens) > 5 and (models.get(tokens[5].casefold()) in ('NPN', 'PNP') or _inferred_model(tokens[5], 'BJT')):
                model_index = 5
        model = tokens[model_index]
        kind = _semiconductor_type(model, 'BJT', models, line)
        result = {'cls': 'BJT', 'type': kind, 'c': net(tokens[1]), 'b': net(tokens[2]),
                  'e': net(tokens[3]), 'model': model}
        if model_index == 5:
            result['substrate'] = net(tokens[4])
        return result
    if family in ('R', 'C', 'L', 'D'):
        need(4, 'name node1 node2 value/model')
        if family == 'D' and tokens[3].casefold() in models and models[tokens[3].casefold()] != 'D':
            raise line.error(f'{name}: diode model {tokens[3]!r} has type {models[tokens[3].casefold()]}')
        return {'cls': 'PASSIVE', 'type': family, 'a': net(tokens[1]), 'b': net(tokens[2]),
                'value': ' '.join(tokens[3:])}
    if family in ('V', 'I'):
        need(4, 'name positive negative value')
        return {'cls': 'VSRC' if family == 'V' else 'ISRC', 'type': family,
                'p': net(tokens[1]), 'n': net(tokens[2]), 'value': ' '.join(tokens[3:])}
    if family == 'E':
        # A named black-box contract is different from a SPICE VCVS. Preserve
        # both supply connections, without inventing an out- node or gain.
        end = next((i for i, token in enumerate(tokens[1:], 1)
                    if '=' in token or token.casefold() == 'params:'), len(tokens))
        head = tokens[:end]
        if head[-1].casefold() == 'opamp' and len(head) in (5, 7):
            parameters = tokens[end:]
            if parameters and parameters[0].casefold() == 'params:':
                parameters = parameters[1:]
            if any('=' not in token for token in parameters):
                raise line.error(f'{name}: opamp parameters must use name=value')
            result = {'cls': 'OPAMP', 'type': 'E', 'model': head[-1],
                      'ip': net(head[-4]), 'in': net(head[-3]), 'op': net(head[-2]),
                      'parameters': ' '.join(parameters)}
            if len(head) == 7:
                result.update(vdd=net(head[1]), vss=net(head[2]))
            return result
        if head[-1].casefold() == 'opamp' and len(head) != 6:
            raise line.error(f'{name}: named opamp expects [VDD VSS] IN+ IN- OUT opamp [name=value]')
        need(6, 'name out+ out- control+ control- gain')
        if net(tokens[2]) not in SNK_ALIASES:
            raise line.error(f'{name}: E-source opamp proxy requires a grounded out-; got {tokens[2]!r}')
        if any(token.upper().startswith(('POLY', 'VALUE', 'TABLE', 'LAPLACE')) for token in tokens[3:]):
            raise line.error(f'{name}: only linear E-source opamp proxies are supported')
        return {'cls': 'VCVS', 'type': 'E', 'op': net(tokens[1]), 'on': net(tokens[2]),
                'ip': net(tokens[3]), 'in': net(tokens[4]), 'value': ' '.join(tokens[5:])}
    raise line.error(f'{name}: unsupported device prefix {family!r}')


def parse_netlist(path, cell=None) -> dict[str, dict]:
    """Read supported devices, retaining source locations and instance IDs."""
    path = Path(path).expanduser().resolve()
    top, cells = _partition(_logical_lines(path), root_path=path)
    if cell is not None:
        key = str(cell).casefold()
        if key not in cells:
            raise NetlistError(f'{path}: unknown subcircuit {cell!r}; available: {", ".join(sorted(cells)) or "none"}')
        selected = cells[key]
        models = _model_types(top)
        models.update(_model_types(selected))
    else:
        selected = top
        models = _model_types(top)
        if cells and not any(not line.text.startswith('.') for line in selected):
            raise NetlistError(f'{path}: select a subcircuit with --cell; available: {", ".join(sorted(cells))}')
    devices: dict[str, dict] = {}
    identifiers: set[str] = set()
    for line in selected:
        tokens = line.text.split()
        directive = tokens[0].casefold()
        if directive == '.model' or directive in _IGNORED_DIRECTIVES:
            continue
        if directive.startswith('.'):
            raise line.error(f'unsupported structural directive {tokens[0]!r}')
        name = tokens[0]
        if name.casefold() in identifiers:
            raise line.error(f'duplicate device ID {name!r} (IDs are case-insensitive)')
        device = classify_tokens(tokens, models, line)
        device['source_path'] = str(line.path)
        device['source_line'] = line.number
        devices[name] = device
        identifiers.add(name.casefold())
    if not devices:
        raise NetlistError(f'{path}: selected circuit contains no supported devices')
    return devices


def terminal_nets(device: dict) -> tuple[str, ...]:
    keys = {'MOS': ('d', 'g', 's', 'bulk'), 'BJT': ('c', 'b', 'e', 'substrate'),
            'PASSIVE': ('a', 'b'), 'VSRC': ('p', 'n'), 'ISRC': ('p', 'n'),
            'VCVS': ('op', 'on', 'ip', 'in'),
            'OPAMP': ('vdd', 'vss', 'ip', 'in', 'op')}[device['cls']]
    return tuple(device[key] for key in keys if key in device)


def to_document(devices: dict[str, dict]) -> dict:
    """Project canonical devices into visible symbol pins used by the SA model."""
    components = []
    for name, device in devices.items():
        family, kind = device['cls'], device['type']
        if family == 'MOS':
            symbol, pins = kind.casefold(), {'D': 'd', 'G': 'g', 'S': 's'}
        elif family == 'BJT':
            symbol, pins = kind.casefold(), {'C': 'c', 'B': 'b', 'E': 'e'}
        elif family == 'PASSIVE':
            symbol = {'R': 'resistor', 'C': 'capacitor', 'L': 'inductor', 'D': 'diode'}[kind]
            pins = {'A': 'a', 'K': 'b'} if kind == 'D' else {'1': 'a', '2': 'b'}
        elif family in ('VSRC', 'ISRC'):
            symbol = 'voltage-source' if family == 'VSRC' else 'current-source'
            pins = {'+': 'p', '-': 'n'}
        else:
            symbol, pins = 'opamp', {'OUT': 'op', 'IN+': 'ip', 'IN-': 'in'}
        component = {'id': name, 'symbol': symbol,
                     'nodes': {pin: device[key] for pin, key in pins.items()}}
        if family == 'OPAMP':
            hidden = {pin: device[key] for pin, key in (('VDD', 'vdd'), ('VSS', 'vss')) if key in device}
            if hidden:
                component['hidden_nodes'] = hidden
        components.append(component)
    nets: dict[str, list[tuple[str, str]]] = {}
    for component in components:
        for pin, net in component['nodes'].items():
            nets.setdefault(net, []).append((component['id'], pin))
    return {'components': components, 'nets': nets}
