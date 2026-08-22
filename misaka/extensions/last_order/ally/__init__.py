"""Allies: third-party agent CLIs (codex / claude / gemini / ...) running in panes.

How allies differ from Sisters: a Sister is a MISAKA agent running in the MISAKA engine
and speaks SendMessage natively. An ally is someone else's CLI and knows none of
MISAKA's protocols, so MISAKA does the one thing it cannot: start the process
asynchronously, collect its stdout, and drop the reply into messages.db. Last Order
then reads ally replies from the same mailbox it uses for Sisters.

No vendor knowledge lives here: Last Order supplies the full command line each time
and reads any session ID it needs out of the reply; MISAKA only executes.
"""

SESSION_KINDS = {"foreground", "dm"}


def activate(spec):
    from .extension import register
    return register
