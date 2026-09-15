"""Masked extension dialog using the existing TUI input/editor lifecycle."""
from misaka.ui.tui.components.input import Input
from misaka.ui.tui.interactive.components.extension_input import ExtensionInputComponent


class MaskedInput(Input):
    def render(self, width):
        value = self.value
        try:
            self.value = "*" * len(value)
            return super().render(width)
        finally:
            self.value = value


async def secret_input(ui, title):
    def factory(tui, theme, keys, done):
        component = ExtensionInputComponent(title, None, done, lambda: done(None))
        field = MaskedInput()
        component.children[component.children.index(component.input)] = field
        component.input = field
        dispose = component.dispose
        def clear():
            # No reuse of password text, paste buffers, kill ring or undo history.
            field.__init__()
            dispose()
        component.dispose = clear
        return component
    return await ui.custom(factory)
