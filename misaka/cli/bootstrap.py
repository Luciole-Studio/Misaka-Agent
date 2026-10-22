"""What a process entry wires before any session is built.

Pi's ``main.ts`` composes its bundled extensions ahead of the caller's factories and
hands the list down into core, which never imports them. MISAKA also builds sessions
from inside core -- a card under the daemon, a sub-agent child -- where the bundled
package cannot be imported without core referring to it. So the entry assigns the
composition function into ``misaka.core.wiring.bundled`` instead, and core calls it
with each session's spec. Every process entry calls ``install`` first: the CLI
(``misaka.cli.app``), the sub-agent child (``misaka.cli.subagent_child``), the
research node (``misaka.cli.research_node``) and the panel daemon (``misaka.ui.panel.daemon``).
A process that never runs an entry -- a bare kernel run in a test -- builds sessions without
bundled extensions.

``install`` also registers the workflows that own cards (``misaka.core.platform.owners``), so the
board treats a research card as research's in every process without importing research itself.

``install`` is also where a process refuses to start on a settings.json knob it cannot
use, listing every one at once, so a bad value is found before any work begins rather
than by whichever long-running process reads it first.

``install`` also turns on theme validation (pi's ``main.ts`` does it first thing): the sessions
these processes build load the home's ``themes/``, and an invalid theme is then reported, not
half-loaded.
"""

from __future__ import annotations

from misaka import extensions
from misaka.config import product
from misaka.core import wiring


def install(*, check_settings: bool = True) -> None:
    # MISAKA fork: pi's only entry is `main()`; MISAKA's other entries build sessions without it.
    from misaka.modes.interactive.theme.theme import setThemeJsonValidator
    from misaka.modes.interactive.theme.theme_schema import validateThemeJson
    setThemeJsonValidator(validateThemeJson)
    if check_settings and (errors := product.validate_settings()):
        raise SystemExit("settings.json has values MISAKA cannot use; fix or remove them:\n  "
                         + "\n  ".join(errors))
    wiring.bundled = extensions.discover
    from misaka.core.platform import metering
    metering.install()          # every model request of a metered session is leased and recorded
    from misaka.core.research import owner
    owner.install()             # research answers for its cards wherever the board asks (platform.owners)


__all__ = ["install"]
