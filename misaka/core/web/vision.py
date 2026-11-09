"""A picture answered in words, for a model that cannot see it.

The image goes to the vision model the user named (``provider/model``) with a question, and the
answer comes back as text. There is one such model for the whole system, ``vision.model`` in
settings.json -- not one per role, the way a Sister's chat model is chosen -- because the job is
the same for every role. ``browser.vision_model``, which named it for the browser alone before,
is read when it is not set. The browser asks it about a screenshot; the ``vision`` extension has
it read every image in a request to a model without vision. FrontierAgent's reader works the
same way through its READDOC_VISION_* gateway, and its transcription prompt is used here.
"""
import time

from misaka.core.web import config

# FrontierAgent, plugins/tools/_reader_core.py, _VISION_PROMPT.
TRANSCRIBE = (
    "Reproduce ALL content of this image faithfully and completely; do not summarize or guess. "
    "First, one line: what it is (chart/diagram/table/form/photo/screenshot). Then: transcribe text "
    "verbatim (exact numbers/units/labels); for any table preserve rows/columns and which cell each "
    "value belongs to; for a chart/diagram give title, axes, legend, series and the values/relationships "
    "it conveys; for purely visual elements describe only what carries information. Keep reading order. "
    "Use [illegible] rather than guessing."
)


def selected_model():
    """``(provider/model, the setting that named it)`` for reading images, or ``(None, None)``."""
    from misaka.config.product import setting
    selected = setting("vision", "model", None, str)
    if selected:
        return selected, "vision.model"
    try:
        from misaka.core.web.browser import settings as browser
        selected = browser.config().get("vision_model")
    except (ValueError, OSError):
        selected = None                     # an unreadable web configuration names no model
    return (selected, "browser.vision_model") if isinstance(selected, str) and selected else (None, None)


async def describe(image, question, ctx, selected, *, setting, purpose, max_tokens=2048):
    """The text the vision model ``selected`` answers ``question`` with about ``image`` (a content
    block). ``setting`` names the knob that chose the model, for the errors; ``purpose`` is the
    accounting label. Raises ValueError when the model cannot be used or the call fails."""
    from misaka.ai.compat import completeSimple
    from misaka.ai.types import SimpleStreamOptions
    from misaka.ai.utils.headers import providerHeadersToRecord
    from misaka.core.web.accounting import account_call

    provider, separator, model_id = selected.partition('/')
    registry = getattr(ctx, 'modelRegistry', None)
    model = registry.find(provider, model_id) if registry is not None and separator else None
    if model is None or 'image' not in model.input:
        raise ValueError(f'{setting} must identify an available vision model as provider/model')
    if not registry.hasConfiguredAuth(model):
        raise ValueError(f'The vision model {setting} names has no authentication')
    auth = await registry.getApiKeyAndHeaders(model)
    if not auth['ok']:
        raise ValueError(auth['error'])
    if auth.get('baseUrl'):
        model = model.model_copy(update={'baseUrl': auth['baseUrl']})
    options = SimpleStreamOptions(apiKey=auth.get('apiKey'), headers=providerHeadersToRecord(auth.get('headers')),
                                  env=auth.get('env'), signal=getattr(ctx, 'signal', None), maxTokens=max_tokens)
    async with account_call(purpose, provider, question, unit='provider_operation') as facts:
        result = await completeSimple(model, {'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': question}, image], 'timestamp': int(time.time() * 1000)}]}, options)
        usage = getattr(result, 'usage', None)
        if usage is not None:
            facts['model_usage'] = usage.model_dump()
    stop = getattr(result, 'stopReason', None)
    if stop in {'error', 'aborted'}:
        raise ValueError(getattr(result, 'errorMessage', None) or f'The vision model {setting} names failed')
    text = config.redact_secrets(''.join(getattr(item, 'text', '') for item in result.content))
    if stop == 'length':
        text = (text.rstrip() + '\n' if text.strip() else '') + CUT_OFF
    return text


# Said of a reading the vision model's output limit cut short (a reasoning model can spend all of
# it thinking); the vision bridge does not keep such a reading as the image's.
CUT_OFF = '[The reading stops here: the vision model reached its output limit.]'
