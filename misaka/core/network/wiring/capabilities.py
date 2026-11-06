"""One role-scoped routing catalog in the system prompt, not task prose."""
import json
from contextlib import contextmanager

from misaka.core.network.roster import coordinator_profile, routing_catalog
from misaka.core.platform import owners, prompt_guard
from misaka.core.platform.vocabulary import DRIVER_OWNED_TOOLS
from misaka.utils.values import read_field

# The session entry recording which board tools a workflow took off, as pi's plan mode keeps its own.
WORKFLOW_TOOLS_ENTRY = "workflow-tools"


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

    def _taken_tools(self):
        """What the branch's last entry says the workflow took off and has not given back."""
        entries = [entry for entry in self.session.sessionManager.getBranch()
                   if read_field(entry, "type") == "custom" and read_field(entry, "customType") == WORKFLOW_TOOLS_ENTRY]
        return list(read_field(read_field(entries[-1], "data"), "tools")) if entries else []

    def _settle_tools(self):
        """Last Order's board tools follow the workflow, the way pi's plan mode handles its tools: the
        owner of the mode takes them off with ``setActiveTools``, records what it took, and gives back
        that and no more. A resume finds the record, and ``/tree`` puts the branch's own record in force.
        Not at a run's start: a loadout set there rebuilds the options the run's sections are going into."""
        if self.session is None:
            return
        taken, active = self._taken_tools(), self.session.getActiveToolNames()
        if self.workflow_context:
            found = [name for name in active if name in DRIVER_OWNED_TOOLS]
            if found:
                self.session.setActiveToolsByName([name for name in active if name not in DRIVER_OWNED_TOOLS])
                if set(found) - set(taken):
                    self.session.sessionManager.appendCustomEntry(WORKFLOW_TOOLS_ENTRY, {"tools": [*taken, *found]})
        elif taken:
            self.session.setActiveToolsByName([*active, *taken])
            # The record goes only for what came back and is in the transcript: a session opened with
            # `--tools` cannot take them, and one killed before a running turn ends has not declared them.
            left = [name for name in taken if name not in self.session.getActiveToolNames()]
            if left != taken and self.session.recordLoadout():
                self.session.sessionManager.appendCustomEntry(WORKFLOW_TOOLS_ENTRY, {"tools": left})

    async def session_start(self, event, ctx):
        self._settle_tools()

    async def session_tree(self, event, ctx):
        self._settle_tools()

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
            if not previous[1]:
                self._settle_tools()
            yield
        finally:
            self.catalog, self.workflow_context = previous
            if not previous[1]:
                self._settle_tools()

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
