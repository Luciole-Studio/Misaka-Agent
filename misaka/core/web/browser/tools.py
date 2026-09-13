"""MISAKA tool schemas and artifact/image rendering around the browser owner."""
import hashlib
import json
import time
from pathlib import Path

from misaka.core.extensions.types import ToolDefinition
from misaka.core.platform.prompt_guard import untrusted
from misaka.core.tools._common import run_with_abort
from misaka.core.tools._web.evidence import save_page
from misaka.core.web import config
from misaka.core.web.browser import settings
from misaka.core.web.runtime import current_runtime
from misaka.utils.async_lifecycle import run_in_thread
from misaka.utils.atomic import write_bytes
from misaka.utils.image_process import process_image

# Protocol-declared binary fields are bytes, not text; redacting a coincidental
# key substring would corrupt CDP screenshots/PDFs. Runtime.evaluate cannot opt in.
_BINARY_FIELDS = {
    'Page.captureScreenshot': ('data',), 'Page.printToPDF': ('data',),
    'Network.streamResourceContent': ('bufferedData',),
    'HeadlessExperimental.beginFrame': ('screenshotData',),
    'CacheStorage.requestCachedResponse': ('response', 'body'),
}
_FLAGGED_FIELDS = {'Network.getResponseBody': 'body', 'Fetch.getResponseBody': 'body',
                   'IO.read': 'data', 'Network.getRequestPostData': 'postData'}


def redact_result(result, method=None, *, dialog=False):
    opaque = set()
    secrets = config._configured_secrets()
    for prefix, command, payload in [(('result',), method, result.get('result', {})),
            (('completed_action', 'result'), result.get('completed_action', {}).get('method') if dialog else None,
             result.get('completed_action', {}).get('result', {}))]:
        if command in _BINARY_FIELDS:
            opaque.add(prefix + _BINARY_FIELDS[command])
        if command in _FLAGGED_FIELDS and isinstance(payload, dict) and payload.get('base64Encoded') is True:
            opaque.add(prefix + (_FLAGGED_FIELDS[command],))
    def visit(value, path=()):
        if isinstance(value, str):
            if path not in opaque:
                for secret in secrets:
                    value = value.replace(secret, config.REDACTED)
            return value
        if isinstance(value, dict):
            return {visit(key): visit(item, (*path, key)) for key, item in value.items()}
        if isinstance(value, list):
            return [visit(item, (*path, i)) for i, item in enumerate(value)]
        return value
    return visit(result)


def _save_image(cwd, body):
    path = Path(cwd) / 'downloads' / 'browser' / (hashlib.sha256(body).hexdigest()[:24] + '.png')
    if not path.resolve().is_relative_to(Path(cwd).resolve()):
        raise ValueError('Browser artifact directory points outside the workspace')
    path.parent.mkdir(parents=True, exist_ok=True)
    write_bytes(str(path), body, mode=0o600)
    return str(path.relative_to(cwd))


async def image_content(body, question, ctx):
    processed = await process_image(body, 'image/png')
    if not processed.ok:
        raise ValueError(processed.message)
    image = {'type': 'image', 'data': processed.data, 'mimeType': processed.mimeType}
    model = getattr(ctx, 'model', None)
    if model is not None and 'image' in model.input:
        return [image]
    selected = settings.config().get('vision_model')
    if not selected:
        return [{'type': 'text', 'text': 'Screenshot saved. This model has no native vision; configure browser.vision_model for image analysis.'}]
    from misaka.ai.stream import complete_simple
    from misaka.ai.types import SimpleStreamOptions
    from misaka.ai.utils.headers import provider_headers_to_record
    from misaka.core.web.accounting import account_call

    provider, separator, model_id = selected.partition('/')
    registry = getattr(ctx, 'modelRegistry', None)
    model = registry.find(provider, model_id) if registry is not None and separator else None
    if model is None or 'image' not in model.input:
        raise ValueError('browser.vision_model must identify an available vision model as provider/model')
    auth = await registry.getAuth(model)
    if auth is None:
        raise ValueError('The selected browser vision model has no authentication')
    if auth.auth.baseUrl:
        model = model.model_copy(update={'baseUrl': auth.auth.baseUrl})
    options = SimpleStreamOptions(apiKey=auth.auth.apiKey, headers=provider_headers_to_record(auth.auth.headers),
                                  env=auth.env, signal=getattr(ctx, 'signal', None), maxTokens=2048)
    async with account_call('browser_vision', provider, question, unit='provider_operation') as facts:
        result = await complete_simple(model, {'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': question}, image], 'timestamp': int(time.time() * 1000)}]}, options)
        usage = getattr(result, 'usage', None)
        if usage is not None:
            facts['model_usage'] = usage.model_dump()
    if getattr(result, 'stopReason', None) in {'error', 'aborted'}:
        raise ValueError(getattr(result, 'errorMessage', None) or 'Browser vision model call failed')
    return [{'type': 'text', 'text': config.redact_secrets(''.join(getattr(item, 'text', '') for item in result.content))}]


def register(harn, cwd):
    schemas = json.loads((Path(__file__).parent / 'schemas.json').read_text())
    schemas.extend([
        {'name': 'browser_cdp', 'description': 'Send a CDP command on this session-owned tab or an OOPIF frame from browser_snapshot. target_id and frame_id are exclusive. State persists on one connection; disconnected commands are not replayed. Shared browsers reject browser-wide mutations.',
         'parameters': {'type': 'object', 'properties': {'method': {'type': 'string'},
            'params': {'type': 'object', 'additionalProperties': True}, 'target_id': {'type': 'string'},
            'frame_id': {'type': 'string'}, 'session': {'type': 'string'}, 'timeout': {'type': 'number', 'minimum': 1, 'maximum': 300, 'default': 30}}, 'required': ['method']}},
        {'name': 'browser_dialog', 'description': 'Accept or dismiss a pending JavaScript dialog from browser_snapshot. prompt_text answers a prompt. Supply dialog_id when several dialogs are pending.',
         'parameters': {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['accept', 'dismiss']},
            'prompt_text': {'type': 'string'}, 'session': {'type': 'string'}, 'dialog_id': {'type': 'string'}}, 'required': ['action']}},
        {'name': 'browser_exec', 'description': 'Execute Python via the Browser Use CLI in a named, session-owned browser. Requires bash execution permission. Helpers: new_tab(url), goto_url(url), page_info(), js(expression), fill_input(selector,text), capture_screenshot(), cdp(method, **params). Use print for results. Python variables reset each call; the returned workspace persists. No implicit installation or personal-browser discovery.',
         'parameters': {'type': 'object', 'properties': {'code': {'type': 'string'}, 'session': {'type': 'string'},
            'timeout_s': {'type': 'integer', 'minimum': 5, 'maximum': 1800, 'default': 300}}, 'required': ['code']}},
    ])
    for schema in schemas:
        name = schema['name']
        async def execute(tool_call_id, raw, signal, on_update, ctx, *, name=name):
            args = dict(raw) if isinstance(raw, dict) else {}
            try:
                runtime = current_runtime()
                if runtime.browser is None:
                    from misaka.core.web.browser import BrowserManager
                    runtime.browser = BrowserManager(cwd)
                result, _ = await run_with_abort(runtime.browser.perform(name, args, tool_call_id), signal)
                body = result.pop('image_bytes', None)
                extra, saved_paths = [], []
                if body is not None:
                    if not body.startswith(b'\x89PNG'):
                        raise ValueError('Browser did not return a PNG screenshot')
                    if len(body) > 20 * 1024 * 1024:
                        raise ValueError('Browser screenshot exceeds the 20 MiB image budget')
                    path = await run_in_thread(_save_image, cwd, body)
                    saved_paths.append(path)
                    result.pop('path', None)  # The private scratch file is cleaned up with its owner.
                    result['screenshot_path'] = path
                    extra = await image_content(body, args.get('question', 'Describe this page'), ctx)
                if 'snapshot' in result:
                    result['content_kind'] = 'accessibility_snapshot'
                rendered = json.dumps(redact_result(result, args.get('method') if name == 'browser_cdp' else None, dialog=name == 'browser_dialog'), ensure_ascii=False)
                if name == 'browser_type' and args.get('text'):
                    rendered = rendered.replace(json.dumps(args['text'], ensure_ascii=False)[1:-1], '<typed>')
                if 'snapshot' in result or len(rendered) > 90_000:
                    path = await run_in_thread(save_page, cwd, {'provider': 'browser', 'content_kind': result.get('content_kind', 'browser_result')}, rendered)
                    if path:
                        saved_paths.append(path)
                    if len(rendered) > 15_000:
                        storage = ({'saved_path': path, 'read': {'path': path, 'offset': 1}} if path else
                                   {'storage_error': 'Full browser result could not be saved; omitted content is unavailable.'})
                        rendered = json.dumps({'success': result.get('success', True), 'preview': rendered[:10_000],
                                               'truncated': True, **storage,
                                               'pending_dialogs': result.get('pending_dialogs', [])}, ensure_ascii=False)
                return {'content': [{'type': 'text', 'text': untrusted('browser', rendered)}, *extra],
                        'details': {'saved_paths': saved_paths}, 'isError': result.get('success') is False}
            except Exception as error:  # noqa: BLE001 - tool or transport boundary reports the failure
                message = str(error)
                if name == 'browser_type' and args.get('text'):
                    message = message.replace(args['text'], '<typed>')
                return {'content': [{'type': 'text', 'text': untrusted('browser-error', config.redact_secrets(message)[:2048])}], 'details': {}, 'isError': True}
        harn.registerTool(ToolDefinition(name=name, label=name.replace('_', ' ').title(),
            description=schema['description'], parameters=schema['parameters'], execute=execute,
            promptSnippet=schema['description'].split('.')[0]))
