"""Claude proposes and implements source changes in isolated circuit candidates.

The same checked JSON-edit protocol is used for HTTP models and Claude Code.
Only the runner applies edits; the CLI is invoked with tools disabled. This is
an edit boundary, not an OS sandbox for subsequently executed Python code.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request


class CodingAgentError(RuntimeError):
    pass


# The reference verifier, score, tests, assets and netlists live outside this
# allowlist. In particular, candidate code cannot edit the frozen rule helpers.
ALLOWED_ENGINE_FILES = frozenset({
    'candidate_entry.py', 'run_flow.py', 'netlist_core.py', 'parse-netlist.py',
    'place-and-route.py', 'place-layered.py', 'device_leveling.py', 'vf2_match.py',
    'structured_placement.py', 'structure_selection.py', 'circuit_structures.py',
    'layout_evidence.py', 'layout_intent.py', 'layout_relations.py',
    'branch_levels.py', 'branch_alignment.py', 'attachment_placement.py',
    'passive_placement.py', 'symmetry_layout.py', 'orthogonal_router.py',
    'fallback_bus.py', 'local_rescue.py', 'spacing_retry.py',
    'simplify_routes.py', 'simplify_budget.py', 'route_patterns.py',
    'route_tracks.py', 'route_reattachment.py', 'candidate_budget.py',
    'placement_preflight.py', 'presentation.py', 'sa_render.py',
    'layout_quality.py', 'aesthetic_flow.py',
})


def _relative_source_path(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        return False
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('', '.', '..') for p in value.split('/')):
        return False
    parts = path.parts
    reserved = {'CON', 'PRN', 'AUX', 'NUL'} | {f'{prefix}{i}' for prefix in ('COM', 'LPT') for i in range(1, 10)}
    if any(part.split('.')[0].upper() in reserved or part.endswith((' ', '.')) for part in parts):
        return False
    if path.suffix != '.py':
        return False
    if len(parts) == 2 and parts[0] == 'engine':
        return True
    if len(parts) >= 3 and parts[:2] == ('engine', 'strategies') and path.suffix == '.py':
        return all(part.replace('_', '').replace('-', '').replace('.', '').isalnum() for part in parts[2:])
    return False


def allowed_path(value):
    """Write permission; reading a rule helper never grants edit permission."""
    if not _relative_source_path(value):
        return False
    path = PurePosixPath(value)
    return len(path.parts) > 2 or path.name in ALLOWED_ENGINE_FILES


def _source_file(workspace, relative):
    """Resolve a read-only Python source under engine, excluding data/tests."""
    if not _relative_source_path(relative):
        raise CodingAgentError('只允许读取 engine/*.py 或 engine/strategies/**/*.py')
    root = Path(workspace).resolve()
    source = root.joinpath(*PurePosixPath(relative).parts)
    if not source.resolve().is_relative_to(root) or source.is_symlink():
        raise CodingAgentError('源码路径越出工作区或指向符号链接')
    for parent in source.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise CodingAgentError('源码路径经过符号链接')
    if not source.is_file():
        raise CodingAgentError('请求的源码文件不存在')
    return source


def _object(text, what):
    try:
        result = json.loads(text)
    except (ValueError, TypeError):
        raise CodingAgentError(f'{what} 未返回有效的纯 JSON 对象') from None
    if not isinstance(result, dict):
        raise CodingAgentError(f'{what} 必须返回 JSON 对象')
    return result


def _number(config, key, default, minimum, maximum, integer=False):
    value = config.get(key, default)
    types = (int,) if integer else (int, float)
    if type(value) not in types or not math.isfinite(value) or not minimum <= value <= maximum:
        raise CodingAgentError(f'{key} 必须在 {minimum} 到 {maximum} 之间')
    return value


def apply_edits(workspace, edits, *, max_files=8, max_file_bytes=220000, max_total_bytes=600000):
    """Validate a complete patch before writing; return hash/diff evidence."""
    root = Path(workspace).resolve()
    if not (root / 'engine' / 'candidate_entry.py').is_file():
        raise CodingAgentError('候选工作区必须包含 engine/candidate_entry.py')
    if not isinstance(edits, list) or not 1 <= len(edits) <= max_files:
        raise CodingAgentError(f'候选 edits 必须含 1 到 {max_files} 个文件')
    seen, prepared, total = set(), [], 0
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {'path', 'content'}:
            raise CodingAgentError('每个 edit 必须且只能包含 path 和 content')
        relative, content = edit['path'], edit['content']
        if not allowed_path(relative) or relative in seen:
            raise CodingAgentError('候选修改了禁止路径、重复路径或不支持的文件')
        if not isinstance(content, str) or '\x00' in content:
            raise CodingAgentError('候选文件必须为不含 NUL 的文本')
        encoded = content.encode('utf-8')
        total += len(encoded)
        if len(encoded) > max_file_bytes or total > max_total_bytes:
            raise CodingAgentError('候选代码超过允许的文件或总字节数')
        destination = root.joinpath(*PurePosixPath(relative).parts)
        resolved = destination.resolve()
        if not resolved.is_relative_to(root) or destination.is_symlink():
            raise CodingAgentError('候选路径越出工作区或指向符号链接')
        for ancestor in destination.parents:
            if ancestor == root:
                break
            if ancestor.is_symlink():
                raise CodingAgentError('候选路径经过符号链接')
        if destination.exists() and not destination.is_file():
            raise CodingAgentError('候选目标不是普通文件')
        try:
            ast.parse(content, filename=relative)
        except SyntaxError as exc:
            raise CodingAgentError(f'候选 Python 语法错误：{relative}:{exc.lineno}') from None
        before_bytes = destination.read_bytes() if destination.exists() else None
        before = before_bytes.decode('utf-8-sig') if before_bytes is not None else ''
        seen.add(relative)
        prepared.append((relative, destination, content, encoded, before, before_bytes))
    if all(before == content for _, _, content, _, before, _ in prepared):
        raise CodingAgentError('候选没有实际代码变化')
    written = []
    try:
        for _, destination, _, encoded, _, before_bytes in prepared:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(encoded)
            written.append((destination, before_bytes))
    except OSError:
        for destination, before_bytes in reversed(written):
            if before_bytes is None:
                destination.unlink(missing_ok=True)
            else:
                destination.write_bytes(before_bytes)
        raise CodingAgentError('候选写入失败；已恢复已写文件') from None
    diff, records = [], []
    for relative, _, content, encoded, before, before_bytes in prepared:
        diff.extend(difflib.unified_diff(before.splitlines(True), content.splitlines(True),
                                         fromfile='parent/' + relative, tofile='candidate/' + relative))
        records.append({'path': relative,
                        'before_sha256': hashlib.sha256(before_bytes).hexdigest() if before_bytes is not None else None,
                        'after_sha256': hashlib.sha256(encoded).hexdigest()})
    return {'files': records, 'diff': ''.join(diff)}


_ANALYST = '''你是模拟电路图生成方法的 RSI 分析 Agent。根据搜索样例中实际生成的图、连通/几何错误、布局和布线指标以及当前代码，找出一个可实现的改进机制。
你可以提出新增算法、重组流程、增加候选、结构识别、摆件、朝向、SA目标/动作、布线调度/避障、失败恢复或安全简化，不局限于参数选择。
你不能改变评测标准、原始网表、器件定义或测试集。只返回纯 JSON：{"failure_mode_desc":"具体失败/缺点","change_hypothesis":"单一可检验举措和预期作用","ref":"简短标识"}。不声称尚未测量的改进已成功。'''
_IMPLEMENTER = '''你是模拟电路图 workflow 的实现 Agent。依据假设卡和当前代码，实现一个实际源码改进，而不是只写建议。
返回纯 JSON：{"summary":"实现内容及机制","edits":[{"path":"engine/某文件.py","content":"修改后的完整 Python 文件内容"}]}。
允许编辑的现有文件清单和可新增的 engine/strategies/*.py 会在输入中提供；只做与卡片直接相关的有限改动。不得修改或绕开固定评测/验收/网表/符号资源，不得编造测量，不得把失败输出标成功。保留现有 CLI/JSON 输出接口以及正确的原始引脚与线路。不得使用外部命令、联网、访问候选目录外文件或添加第三方依赖。读取提供的文件全文，再返回完整替换文件。'''
_REVIEWER = '''你是模拟电路图 RSI 的机制评审，只根据提供的搜索证据、假设卡和代码 diff 说明实施是否符合假设以及潜在退化。
只返回纯 JSON：{"comment":"简短评审意见"}。评审不能取代固定几何验收和配对评分，不能宣称未测量的效果。'''

_SOURCE_PROTOCOL = '''
你可以在输出最终分析卡或实现结果之前，单独返回纯 JSON：{"read_files":["engine/place-and-route.py","engine/strategies/某文件.py"]}，请求读取当前版本的完整源码。
仅可读 available_source_files 中的 Python 文件，包含冻结规则辅助模块供理解，但读取权限不授予修改权限。可以分回合读取依赖与之前新增的 helper。
程序将把完整文件加入 code_files 后再次请求；不会静默截断文件。source_read_error 表示本次读取请求失败，需减少/更换请求，不能把错误当作文件内容。
达到源码读取回合上限后必须返回最终分析卡或实现结果。'''


class CodingAgent:
    def __init__(self, config=None, mode='api'):
        self.config = dict(config or {})
        if mode not in ('api', 'claude', 'smoke'):
            raise CodingAgentError('Agent mode 必须为 api、claude 或 smoke')
        self.mode = mode
        self.synthetic = mode == 'smoke'
        self.requests = []
        self.source_reads = []
        self.runtime = None
        self.recorder = None

    def bind_runtime(self, runtime, recorder=None):
        self.runtime, self.recorder = runtime, recorder

    def _record(self, event):
        if self.recorder:
            self.recorder(event)

    def _repair_feedback(self, context, error, response=None):
        payload = dict(context)
        text = json.dumps(response, ensure_ascii=False) if response is not None else ''
        payload['repair_feedback'] = {'error': str(error), 'previous_response': text[:20000],
                                      'response_truncated': len(text) > 20000,
                                      'instruction': '修复本次格式或代码错误，重新提交完整有效响应；不要把失败当作成功。'}
        return payload

    def _can_retry(self, exc):
        return not any(term in str(exc) for term in ('401', '403', '密钥', 'provider 必须', 'base_url 必须', '找不到 Claude'))

    def _role(self, name):
        roles = self.config.get('roles', {})
        role = roles.get(name, roles.get('analyst'))
        if not isinstance(role, dict):
            raise CodingAgentError(f'未配置模型角色 {name}')
        return role

    def _call(self, role_name, instruction, payload, cwd=None):
        role = self._role(role_name)
        timeout = _number(self.config, 'timeout_seconds', 180, 1, 1800)
        if self.runtime:
            timeout = self.runtime.bound(timeout)
        try:
            if self.mode == 'claude':
                result = self._cli(role, instruction, payload, timeout, cwd)
            else:
                result = self._http(role, instruction, payload, timeout)
        except CodingAgentError as exc:
            self._record({'role': role_name, 'status': 'response_error', 'error': str(exc)})
            raise
        self._record({'role': role_name, 'status': 'response', 'response': result})
        self.requests.append({'role': role_name, 'mode': self.mode,
                              'provider': role.get('provider', 'claude_cli'), 'model': role.get('model'),
                              'synthetic': False})
        return result

    def _source_loop(self, role_name, instruction, context, workspace=None):
        """Let a model retrieve full current source before its final response.

        The model can request only Python source within the candidate engine.
        Read requests never invoke candidate code or grant shell/file tools.
        """
        payload = dict(context)
        initial = payload.get('code_files', {})
        if not isinstance(initial, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                 for k, v in initial.items()):
            raise CodingAgentError('code_files 必须是路径到完整源码文本的对象')
        payload['code_files'] = dict(initial)
        max_rounds = _number(self.config, 'max_source_rounds', 4, 0, 16, integer=True)
        max_chars = _number(self.config, 'max_context_chars', 400000, 1000, 2000000, integer=True)
        if workspace is not None:
            root = Path(workspace).resolve()
            sources = list((root / 'engine').glob('*.py'))
            strategies = root / 'engine' / 'strategies'
            if strategies.is_dir():
                sources += list(strategies.rglob('*.py'))
            payload['available_source_files'] = sorted(
                p.relative_to(root).as_posix() for p in sources
                if _relative_source_path(p.relative_to(root).as_posix()))
        else:
            payload['available_source_files'] = sorted(initial)
        payload['source_read_limits'] = {'max_rounds': max_rounds, 'max_context_chars': max_chars,
                                         'full_files_only': True}

        def size(value):
            return len(json.dumps(value, ensure_ascii=False, allow_nan=False))

        if size(payload) > max_chars:
            raise CodingAgentError('初始 Agent 上下文超过 max_context_chars；不静默截断文件')
        for turn in range(max_rounds + 1):
            payload['source_read_limits']['remaining_rounds'] = max_rounds - turn
            if size(payload) > max_chars:
                raise CodingAgentError('Agent 上下文超过 max_context_chars；不发送截断或超限内容')
            response = self._call(role_name, instruction + _SOURCE_PROTOCOL, payload, workspace)
            if 'read_files' not in response:
                return response
            if turn == max_rounds:
                raise CodingAgentError('模型超过允许的源码读取回合数，尚未提交最终响应')
            request = response.get('read_files')
            event = {'role': role_name, 'round': turn + 1,
                     'requested': request if isinstance(request, list) else [], 'status': 'error'}
            try:
                if set(response) != {'read_files'} or not isinstance(request, list) or not 1 <= len(request) <= 32:
                    raise CodingAgentError('源码请求只能包含 1 到 32 个 read_files 路径')
                if any(not isinstance(name, str) for name in request) or len(set(request)) != len(request):
                    raise CodingAgentError('源码请求路径必须是不重复的文本')
                requested = {}
                for relative in request:
                    if workspace is None:
                        if relative not in initial or not _relative_source_path(relative):
                            raise CodingAgentError('当前调用未绑定源码工作区，无法补读该文件')
                        requested[relative] = initial[relative]
                    else:
                        source = _source_file(workspace, relative)
                        if source.stat().st_size > max_chars * 4:
                            raise CodingAgentError('请求的完整源码过大，请选择更少或更小的文件')
                        requested[relative] = source.read_text(encoding='utf-8-sig')
                trial = dict(payload)
                trial['code_files'] = {**payload['code_files'], **requested}
                trial.pop('source_read_error', None)
                if size(trial) > max_chars:
                    raise CodingAgentError('加入完整源码后超过 max_context_chars，请选择更少或更小的文件')
                payload = trial
                event['status'] = 'read'
            except (CodingAgentError, OSError, UnicodeError) as exc:
                # Return the request error to the model for another source choice.
                # Never return host filesystem paths or partial source content.
                message = str(exc) if isinstance(exc, CodingAgentError) else '源码文件读取失败'
                payload['source_read_error'] = message
                event['error'] = message
            self.source_reads.append(event)
        raise CodingAgentError('源码读取未产生最终响应')

    def _http(self, role, instruction, payload, timeout):
        provider = role.get('provider', 'anthropic')
        model, key_env = role.get('model'), role.get('api_key_env')
        if not isinstance(model, str) or not model or not isinstance(key_env, str) or not key_env:
            raise CodingAgentError('API 角色必须设置 model 和 api_key_env')
        key = os.environ.get(key_env)
        if not key:
            raise CodingAgentError(f'缺少模型密钥环境变量 {key_env}')
        tokens = _number(self.config, 'max_output_tokens', 16000, 1000, 64000, integer=True)
        user = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if provider == 'anthropic':
            base = role.get('base_url', 'https://api.anthropic.com')
            suffix = '/v1/messages'
            body = {'model': model, 'max_tokens': tokens, 'system': instruction,
                    'messages': [{'role': 'user', 'content': user}]}
            headers = {'x-api-key': key, 'anthropic-version': '2023-06-01'}
        elif provider == 'openai_compatible':
            base, suffix = role.get('base_url'), '/chat/completions'
            body = {'model': model, 'max_tokens': tokens,
                    'messages': [{'role': 'system', 'content': instruction}, {'role': 'user', 'content': user}]}
            headers = {'Authorization': 'Bearer ' + key}
        else:
            raise CodingAgentError('provider 必须是 anthropic 或 openai_compatible')
        if not isinstance(base, str) or not base.startswith(('https://', 'http://')):
            raise CodingAgentError('base_url 必须是 HTTP(S) API 根地址')
        headers['Content-Type'] = 'application/json'
        try:
            if self.runtime:
                envelope = {'url': base.rstrip('/') + suffix, 'body': body, 'headers': headers, 'timeout': timeout}
                completed = self.runtime.run([sys.executable, '-B', str(Path(__file__).with_name('model_http_worker.py'))],
                    input=json.dumps(envelope, ensure_ascii=False), timeout=timeout,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
                result = json.loads(completed.stdout)
                if result.get('error'):
                    raise CodingAgentError(result['error'])
                data = result['response']
            else:
                request = urllib.request.Request(base.rstrip('/') + suffix,
                    data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8'), headers=headers, method='POST')
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw = response.read(3000001)
                if len(raw) > 3000000:
                    raise CodingAgentError('模型响应过大')
                data = json.loads(raw.decode('utf-8'))
        except urllib.error.HTTPError as exc:
            raise CodingAgentError(f'模型服务返回 HTTP {exc.code}') from None
        except (socket.timeout, TimeoutError, subprocess.TimeoutExpired):
            raise CodingAgentError('模型请求超时') from None
        except urllib.error.URLError:
            raise CodingAgentError('无法连接模型服务') from None
        except (ValueError, UnicodeError):
            raise CodingAgentError('模型响应不是有效 JSON') from None
        try:
            if provider == 'anthropic':
                text = ''.join(p['text'] for p in data['content'] if p.get('type') == 'text')
            else:
                text = data['choices'][0]['message']['content']
        except (KeyError, TypeError, IndexError, AttributeError):
            raise CodingAgentError('模型响应缺少文本') from None
        self._record({'role': 'model_response', 'status': 'raw_text', 'text': text})
        return _object(text, '模型')

    def _cli(self, role, instruction, payload, timeout, cwd):
        executable = self.config.get('claude_executable', 'claude')
        if not isinstance(executable, str) or not executable or not shutil.which(executable):
            raise CodingAgentError('找不到 Claude Code；请安装、登录或设置 claude_executable')
        command = [executable, '--print', '--output-format', 'json', '--tools', '', '--system-prompt', instruction]
        if role.get('model'):
            command.extend(['--model', role['model']])
        try:
            if self.runtime:
                completed = self.runtime.run(command, input=json.dumps(payload, ensure_ascii=False),
                    cwd=str(cwd) if cwd else None, text=True, encoding='utf-8',
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
            else:
                completed = subprocess.run(command, input=json.dumps(payload, ensure_ascii=False),
                    cwd=str(cwd) if cwd else None, text=True, encoding='utf-8', capture_output=True,
                    timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise CodingAgentError('Claude Code 请求超时') from None
        except OSError:
            raise CodingAgentError('无法启动 Claude Code') from None
        if completed.returncode != 0:
            raise CodingAgentError(f'Claude Code 非正常退出（{completed.returncode}）；检查登录、模型和 CLI 配置')
        self._record({'role': 'claude_response', 'status': 'raw_text', 'text': completed.stdout})
        envelope = _object(completed.stdout, 'Claude Code')
        if envelope.get('is_error'):
            raise CodingAgentError('Claude Code 报告请求失败')
        if isinstance(envelope.get('structured_output'), dict):
            return envelope['structured_output']
        return _object(envelope.get('result'), 'Claude Code result')

    def analyse(self, context, workspace=None):
        if not isinstance(context, dict):
            raise CodingAgentError('analyse context 必须是对象')
        if self.synthetic:
            return {'failure_mode_desc': 'Synthetic smoke：基础自由 SA 未使用现有层级结构和安全简化。',
                    'change_hypothesis': 'Synthetic smoke：将候选入口的默认基础 profile 改为 reference，接入已随包提供的现有完整流程。',
                    'ref': 'synthetic-reference-profile', 'synthetic': True}
        payload = dict(context)
        repairs = _number(self.config, 'repair_attempts', 2, 0, 5, integer=True)
        for attempt in range(repairs + 1):
            result = None
            try:
                result = self._source_loop('analyst', _ANALYST, payload, workspace)
                required = {'failure_mode_desc', 'change_hypothesis'}
                if not required <= result.keys() or not set(result) <= required | {'ref'}:
                    raise CodingAgentError('分析卡必须包含 failure_mode_desc 和 change_hypothesis，可选 ref')
                if any(not isinstance(result[k], str) or not result[k].strip() for k in required):
                    raise CodingAgentError('分析卡描述和假说必须为非空文本')
                if len(result['failure_mode_desc']) > 3000 or len(result['change_hypothesis']) > 8000 or (isinstance(result.get('ref'), str) and len(result['ref']) > 120):
                    raise CodingAgentError('分析卡过长，请精简成一个可检验的机制')
                if 'ref' in result and not isinstance(result['ref'], str):
                    raise CodingAgentError('分析卡 ref 必须为文本')
                return result
            except CodingAgentError as exc:
                self._record({'role': 'analyst', 'status': 'repair_needed', 'attempt': attempt, 'error': str(exc), 'response': result})
                if attempt == repairs or not self._can_retry(exc):
                    raise
                payload = self._repair_feedback(context, exc, result)

    def implement(self, card, workspace, context):
        root = Path(workspace).resolve()
        if not isinstance(context, dict):
            raise CodingAgentError('implement context 必须是对象')
        if self.synthetic:
            entry = root / 'engine' / 'candidate_entry.py'
            before = entry.read_text(encoding='utf-8-sig')
            import re
            after, count = re.subn(r'(DEFAULT_PROFILE\s*=\s*)[\'"]baseline[\'"]', r'\1"reference"', before, count=1)
            if not count:
                after, count = re.subn(r'(default\s*=\s*)[\'"]baseline[\'"]', r'\1"reference"', before, count=1)
            if not count:
                if 'run(source, **common)' in before:
                    after = before.replace('run(source, **common)', "run(source, presentation='clean', **common)")
                    count = 1
                else:
                    after = before + '\n# Synthetic smoke: unchanged mechanism, expected to be rejected.\n'
            draft = {'summary': 'Synthetic smoke：在候选入口源码中启用随包 reference 流程，不是模型创新或真实 API 调用。',
                     'edits': [{'path': 'engine/candidate_entry.py', 'content': after}]}
        else:
            payload = dict(context)
            payload.update({'card': card, 'allowed_existing_files': ['engine/' + name for name in sorted(ALLOWED_ENGINE_FILES)],
                            'allowed_new_helpers': 'engine/strategies/*.py（可含子目录）',
                            'edit_format': {'summary': '说明代码机制', 'edits': [{'path': 'engine/candidate_entry.py', 'content': '完整 Python 文件'}]}})
            draft = None
        repairs = 0 if self.synthetic else _number(self.config, 'repair_attempts', 2, 0, 5, integer=True)
        initial_payload = dict(payload) if not self.synthetic else {}
        for attempt in range(repairs + 1):
            try:
                if not self.synthetic:
                    draft = self._source_loop('implementer', _IMPLEMENTER, payload, root)
                    if set(draft) != {'summary', 'edits'} or not isinstance(draft.get('summary'), str) or not draft['summary'].strip():
                        raise CodingAgentError('实现响应必须包含非空 summary 和 edits')
                report = apply_edits(root, draft['edits'],
                    max_files=_number(self.config, 'max_edit_files', 8, 1, 32, integer=True),
                    max_file_bytes=_number(self.config, 'max_file_bytes', 220000, 1000, 1000000, integer=True),
                    max_total_bytes=_number(self.config, 'max_total_bytes', 600000, 1000, 3000000, integer=True))
                break
            except CodingAgentError as exc:
                self._record({'role': 'implementer', 'status': 'repair_needed', 'attempt': attempt, 'error': str(exc), 'response': draft})
                if attempt == repairs or not self._can_retry(exc):
                    raise
                payload = self._repair_feedback(initial_payload, exc, draft)
        report.update({'summary': draft['summary'], 'synthetic': self.synthetic, 'mode': self.mode,
                       'requests': list(self.requests), 'source_reads': list(self.source_reads)})
        return report

    def review(self, context):
        if self.synthetic:
            return {'comment': 'Synthetic smoke：仅检查配对执行链路，不证明真实 Agent 能力。', 'synthetic': True}
        if not self.config.get('roles', {}).get('reviewer'):
            return {'comment': '未配置 reviewer；采用仍由本地固定评测决定。', 'skipped': True}
        result = self._call('reviewer', _REVIEWER, context)
        if set(result) != {'comment'} or not isinstance(result.get('comment'), str):
            raise CodingAgentError('评审响应必须为 comment 文本对象')
        return result
