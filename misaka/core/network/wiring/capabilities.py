"""One coordinator-owned Sister catalog in the system prompt, not phase prose."""
import json
from contextlib import contextmanager

from misaka.core.network.roster import capability_catalog
from misaka.core.platform import prompt_guard


class SisterCapabilitiesPart:
    def __init__(self, workspace, catalog=None, *, research_context=False):
        self.tools = []
        self.commands = []
        self.workspace = workspace
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
        if self.research_context:
            from misaka.core.research.prompting import system_context
            common = system_context(self.session, self.session.getActiveToolNames(), system_prompt)
            if common:
                system_prompt += "\n\n" + common
        entries = self.catalog if self.catalog is not None else capability_catalog(workspace=self.workspace)
        compact = [{k: v for k, v in entry.items() if k != "profile"} for entry in entries]
        text = ("\n\n## Sister capability profiles\n"
                "These are collaborators, not your own Skills. Read relevant full profiles at profile_path before "
                "assigning work. Listed Skills are indexed capabilities; readiness is checked when loaded. "
                "Query live task state separately; this catalog is not a busy/idle report.\n"
                + prompt_guard.untrusted("sister-capabilities", json.dumps(compact, ensure_ascii=False)))
        return {"systemPrompt": system_prompt + text}


ROLES = {"last_order"}
SESSION_KINDS = {"foreground", "dm", "bare"}


def part(spec):
    return SisterCapabilitiesPart(spec.workspace, spec.sister_catalog, research_context=spec.research_context)
