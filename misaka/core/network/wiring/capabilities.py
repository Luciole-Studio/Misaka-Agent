"""One role-scoped routing catalog in the system prompt, not task prose."""
import json
from contextlib import contextmanager

from misaka.core.network.roster import coordinator_profile, routing_catalog
from misaka.core.platform import prompt_guard


class SisterCapabilitiesPart:
    def __init__(self, workspace, catalog=None, *, research_context=False, sister_id=None):
        self.tools = []
        self.commands = []
        self.workspace = workspace
        self.sister_id = sister_id
        self.catalog = catalog
        self.research_context = research_context
        self.session = None

    def attach(self, session):
        self.session = session

    @contextmanager
    def snapshot(self, catalog=None):
        """Publish Research context and, when supplied, the validator's exact catalog."""
        previous = self.catalog, self.research_context
        if catalog is not None:
            self.catalog = catalog
        self.research_context = True
        try:
            yield
        finally:
            self.catalog, self.research_context = previous

    async def before_agent_start(self, event, ctx):
        system_prompt = event["systemPrompt"].rstrip()
        if self.research_context and self.sister_id is None:
            from misaka.core.research.prompting import system_context
            common = system_context(self.session, self.session.getActiveToolNames(), system_prompt)
            if common:
                system_prompt += "\n\n" + common
        compact = ([coordinator_profile(entry) for entry in self.catalog]
                   if self.catalog is not None else routing_catalog())
        if self.sister_id is not None:
            compact = [entry for entry in compact if entry["id"] != self.sister_id]
        text = ("\n\n## Sister capability profiles\n"
                "Choose collaborators by fit. profile_preview contains the first 200 characters of the introduction. "
                "Query live task state separately; this catalog is not a busy/idle report.\n"
                + prompt_guard.untrusted("sister-capabilities", json.dumps(compact, ensure_ascii=False)))
        return {"systemPrompt": system_prompt + text}


ROLES = {"last_order", "sisters"}
SESSION_KINDS = {"foreground", "dm", "card", "beast", "bare"}


def part(spec):
    from misaka.core.wiring import role_key
    is_lo = role_key(spec) == "last_order"
    if (is_lo and spec.kind not in {"foreground", "dm", "bare"}) or (not is_lo and spec.kind == "bare"):
        return None
    return SisterCapabilitiesPart(
        spec.workspace, spec.sister_catalog, research_context=spec.research_context,
        sister_id=None if is_lo else spec.role.rsplit("/", 1)[-1],
    )
