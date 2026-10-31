"""One role-scoped routing catalog in the system prompt, not task prose."""
import json
from contextlib import contextmanager

from misaka.core.network.roster import coordinator_profile, routing_catalog
from misaka.core.platform import owners, prompt_guard
from misaka.core.platform.vocabulary import DRIVER_OWNED_TOOLS


class SisterCapabilitiesPart:
    def __init__(self, workspace, catalog=None, *, workflow_context=False, sister_id=None):
        self.tools = []
        self.commands = []
        self.workspace = workspace
        self.sister_id = sister_id
        self.catalog = catalog
        self.workflow_context = workflow_context
        self.session = None

    def attach(self, session):
        self.session = session

    def project_tools(self, tools):
        if self.workflow_context and self.sister_id is None:
            return [tool for tool in tools if tool.name not in DRIVER_OWNED_TOOLS]
        return tools

    async def tool_call(self, event, ctx):
        if (self.workflow_context and self.sister_id is None
                and event["toolName"] in DRIVER_OWNED_TOOLS):
            return {"block": True, "reason": "The workflow's driver owns these cards; use the current phase's tools."}

    @contextmanager
    def snapshot(self, catalog=None):
        """Publish the workflow's context and, when supplied, the validator's exact catalog."""
        previous = self.catalog, self.workflow_context
        if catalog is not None:
            self.catalog = catalog
        self.workflow_context = True
        try:
            if self.session is not None and not previous[1]:
                self.session.refreshTools()
            yield
        finally:
            self.catalog, self.workflow_context = previous
            if self.session is not None and not previous[1]:
                self.session.refreshTools()

    async def before_agent_start(self, event, ctx):
        # Sections of the run's prompt, as pi's extensions add theirs: the workflow's context while one
        # runs, and the routing catalog. Returning a whole prompt instead would force it, and the
        # sections the extensions add after the parts (MCP's `mcp_servers`) would never reach the model.
        sections = event["systemPromptOptions"]["sections"]
        context = []
        if self.workflow_context:
            for owner in owners.registered():
                common = owner.system_context(self.session, self.session.getActiveToolNames(),
                                              "\n\n".join([event["systemPrompt"].rstrip(), *context]), self.sister_id)
                if common:
                    context.append(common)
        if context:
            sections["workflow"] = "\n\n".join(context)
        else:
            sections.pop("workflow", None)
        compact = ([coordinator_profile(entry) for entry in self.catalog]
                   if self.catalog is not None else routing_catalog())
        if self.sister_id is not None:
            compact = [entry for entry in compact if entry["id"] != self.sister_id]
        sections["sister_capabilities"] = (
            "## Sister capability profiles\n"
            "Choose collaborators by fit. profile_preview contains the first 200 characters of the introduction. "
            "Query live task state separately; this catalog is not a busy/idle report.\n"
            + prompt_guard.untrusted("sister-capabilities", json.dumps(compact, ensure_ascii=False)))


ROLES = {"last_order", "sisters"}
SESSION_KINDS = {"foreground", "dm", "card", "beast", "bare"}


def part(spec):
    from misaka.core.wiring import role_key
    is_lo = role_key(spec) == "last_order"
    if (is_lo and spec.kind not in {"foreground", "dm", "bare"}) or (not is_lo and spec.kind == "bare"):
        return None
    return SisterCapabilitiesPart(
        spec.workspace, spec.sister_catalog, workflow_context=spec.workflow_context,
        sister_id=None if is_lo else spec.role.rsplit("/", 1)[-1],
    )
