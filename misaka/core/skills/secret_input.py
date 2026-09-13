"""Secure Skill setup UI: masked, not sent to model/history or the normal editor."""
from misaka.utils.async_lifecycle import run_in_thread


async def capture_secret(ctx, runtime, name, prompt, cancelled):
    from misaka.ui.tui.components.input import Input
    from misaka.ui.tui.utils import truncateToWidth

    from .runtime import blocked_env
    if blocked_env(name) or cancelled.is_set():
        return {"success": False, "skipped": True}

    def build(tui, theme, keybindings, done):
        class SecretInput(Input):
            def render(self, width):
                value = self.value
                try:
                    self.value = "*" * len(value)
                    return [truncateToWidth(f"Skill setup: {name} (Esc skips)", width), *super().render(width)]
                finally:
                    self.value = value

        component = SecretInput()
        component.onSubmit = lambda value: done(value)
        component.onEscape = lambda: done(None)
        return component

    value = await ctx.ui.custom(build)
    if not value or cancelled.is_set():
        return {"success": False, "skipped": True}
    await run_in_thread(runtime.store_secret, name, value)
    return {"success": True, "stored_as": name, "validated": False}
