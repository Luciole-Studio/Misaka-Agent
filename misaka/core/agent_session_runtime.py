"""Runtime host for replacing and managing coding-agent sessions."""

from __future__ import annotations

import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, NotRequired, Protocol, TypedDict

from misaka.core.agent_session_services import (
    AgentSessionRuntimeDiagnostic,
    AgentSessionServices,
    CreateAgentSessionFromServicesOptions,
    CreateAgentSessionServicesOptions,
    create_agent_session_from_services,
    create_agent_session_services,
)
from misaka.core.extensions.runner import emit_session_shutdown_event
from misaka.core.session_cwd import assert_session_cwd_exists
from misaka.core.session_manager import NewSessionOptions, SessionManager
from misaka.utils.paths import resolve_path


class ExtensionRunnerLike(Protocol):
    def hasHandlers(self, event: str) -> bool: ...

    def has_handlers(self, event: str) -> bool: ...

    async def emit(self, event: Any) -> Any: ...


class SessionManagerLike(Protocol):
    def getCwd(self) -> str: ...

    def getSessionDir(self) -> str: ...

    def getSessionFile(self) -> str | None: ...

    def isPersisted(self) -> bool: ...

    def newSession(self, options: NewSessionOptions | None = None) -> str | None: ...

    def createBranchedSession(self, leafId: str) -> str | None: ...

    def getEntry(self, id: str) -> dict[str, Any] | None: ...


class AgentStateLike(Protocol):
    messages: list[Any]


class AgentLike(Protocol):
    state: AgentStateLike


class AgentSessionLike(Protocol):
    extensionRunner: ExtensionRunnerLike
    sessionFile: str | None
    sessionManager: SessionManagerLike
    agent: AgentLike

    def dispose(self) -> None: ...

    def createReplacedSessionContext(self) -> Any: ...


@dataclass(slots=True)
class CreateAgentSessionRuntimeResult:
    session: AgentSessionLike
    services: AgentSessionServices
    diagnostics: list[AgentSessionRuntimeDiagnostic] = field(default_factory=list)
    extensionsResult: Any = None
    modelFallbackMessage: str | None = None


class CreateAgentSessionRuntimeOptions(TypedDict):
    cwd: str
    agentDir: str
    sessionManager: SessionManager
    sessionStartEvent: NotRequired[dict[str, Any]]


CreateAgentSessionRuntimeFactory = Callable[
    [CreateAgentSessionRuntimeOptions],
    Awaitable[CreateAgentSessionRuntimeResult],
]


class SessionImportFileNotFoundError(Exception):
    def __init__(self, filePath: str) -> None:
        super().__init__(f"File not found: {filePath}")
        self.name = "SessionImportFileNotFoundError"
        self.filePath = filePath


def extract_user_message_text(content: str | list[dict[str, Any]]) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        part["text"]
        for part in content
        if part.get("type") == "text" and isinstance(part.get("text"), str)
    )


class AgentSessionRuntime:
    def __init__(
        self,
        session: AgentSessionLike,
        services: AgentSessionServices,
        createRuntime: CreateAgentSessionRuntimeFactory,
        diagnostics: list[AgentSessionRuntimeDiagnostic] | None = None,
        modelFallbackMessage: str | None = None,
    ) -> None:
        self._session = session
        self._services = services
        self.createRuntime = createRuntime
        self._diagnostics = list(diagnostics or [])
        self._modelFallbackMessage = modelFallbackMessage
        self.rebindSession: Callable[[AgentSessionLike], Awaitable[None]] | None = None
        self.beforeSessionInvalidate: Callable[[], None] | None = None

    @property
    def services(self) -> AgentSessionServices:
        return self._services

    @property
    def session(self) -> AgentSessionLike:
        return self._session

    @property
    def cwd(self) -> str:
        return self._services.cwd

    @property
    def diagnostics(self) -> tuple[AgentSessionRuntimeDiagnostic, ...]:
        return tuple(self._diagnostics)

    @property
    def modelFallbackMessage(self) -> str | None:
        return self._modelFallbackMessage

    def setRebindSession(
        self,
        rebindSession: Callable[[AgentSessionLike], Awaitable[None]] | None = None,
    ) -> None:
        self.rebindSession = rebindSession

    def setBeforeSessionInvalidate(
        self,
        beforeSessionInvalidate: Callable[[], None] | None = None,
    ) -> None:
        self.beforeSessionInvalidate = beforeSessionInvalidate

    async def emitBeforeSwitch(
        self,
        reason: str,
        targetSessionFile: str | None = None,
    ) -> dict[str, bool]:
        runner = self.session.extensionRunner
        if not runner.hasHandlers("session_before_switch"):
            return {"cancelled": False}

        result = await runner.emit(
            {
                "type": "session_before_switch",
                "reason": reason,
                "targetSessionFile": targetSessionFile,
            }
        )
        return {"cancelled": _result_flag(result, "cancel", False) is True}

    async def emitBeforeFork(
        self,
        entryId: str,
        options: dict[str, Any],
    ) -> dict[str, bool]:
        runner = self.session.extensionRunner
        if not runner.hasHandlers("session_before_fork"):
            return {"cancelled": False}

        result = await runner.emit(
            {
                "type": "session_before_fork",
                "entryId": entryId,
                **options,
            }
        )
        return {"cancelled": _result_flag(result, "cancel", False) is True}

    async def teardownCurrent(self, reason: str, targetSessionFile: str | None = None) -> None:
        await emit_session_shutdown_event(
            self.session.extensionRunner,
            {
                "type": "session_shutdown",
                "reason": reason,
                "targetSessionFile": targetSessionFile,
            },
        )
        if self.beforeSessionInvalidate is not None:
            self.beforeSessionInvalidate()
        self.session.dispose()

    def apply(self, result: CreateAgentSessionRuntimeResult) -> None:
        self._session = result.session
        self._services = result.services
        self._diagnostics = list(result.diagnostics)
        self._modelFallbackMessage = result.modelFallbackMessage

    async def finishSessionReplacement(
        self,
        withSession: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        if self.rebindSession is not None:
            await self.rebindSession(self.session)
        if withSession is not None:
            await withSession(self.session.createReplacedSessionContext())

    async def switchSession(
        self,
        sessionPath: str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, bool]:
        before_result = await self.emitBeforeSwitch("resume", sessionPath)
        if before_result["cancelled"]:
            return before_result

        previous_session_file = self.session.sessionFile
        session_manager = SessionManager.open(
            sessionPath,
            None,
            options.get("cwdOverride") if options else None,
        )
        assert_session_cwd_exists(session_manager, self.cwd)
        await self.teardownCurrent("resume", session_manager.getSessionFile())
        self.apply(
            await self.createRuntime(
                {
                    "cwd": session_manager.getCwd(),
                    "agentDir": self.services.agentDir,
                    "sessionManager": session_manager,
                    "sessionStartEvent": {
                        "type": "session_start",
                        "reason": "resume",
                        "previousSessionFile": previous_session_file,
                    },
                }
            )
        )
        await self.finishSessionReplacement(options.get("withSession") if options else None)
        return {"cancelled": False}

    async def newSession(self, options: dict[str, Any] | None = None) -> dict[str, bool]:
        before_result = await self.emitBeforeSwitch("new")
        if before_result["cancelled"]:
            return before_result

        previous_session_file = self.session.sessionFile
        session_dir = self.session.sessionManager.getSessionDir()
        session_manager = SessionManager.create(self.cwd, session_dir)
        if options and options.get("parentSession"):
            session_manager.newSession(NewSessionOptions(parentSession=options["parentSession"]))

        await self.teardownCurrent("new", session_manager.getSessionFile())
        self.apply(
            await self.createRuntime(
                {
                    "cwd": self.cwd,
                    "agentDir": self.services.agentDir,
                    "sessionManager": session_manager,
                    "sessionStartEvent": {
                        "type": "session_start",
                        "reason": "new",
                        "previousSessionFile": previous_session_file,
                    },
                }
            )
        )
        if options and callable(options.get("setup")):
            await options["setup"](self.session.sessionManager)
            self.session.agent.state.messages = self.session.sessionManager.buildSessionContext().messages
        await self.finishSessionReplacement(options.get("withSession") if options else None)
        return {"cancelled": False}

    async def fork(
        self,
        entryId: str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        position = options.get("position", "before") if options else "before"
        before_result = await self.emitBeforeFork(entryId, {"position": position})
        if before_result["cancelled"]:
            return {"cancelled": True}

        selected_entry = self.session.sessionManager.getEntry(entryId)
        if not selected_entry:
            raise ValueError("Invalid entry ID for forking")

        selected_text: str | None = None
        if position == "at":
            target_leaf_id = str(selected_entry["id"])
        else:
            if (
                selected_entry.get("type") != "message"
                or _message_role(selected_entry.get("message")) != "user"
            ):
                raise ValueError("Invalid entry ID for forking")
            target_leaf_id = selected_entry.get("parentId")
            selected_text = extract_user_message_text(_message_content(selected_entry.get("message")))

        previous_session_file = self.session.sessionFile
        if self.session.sessionManager.isPersisted():
            current_session_file = self.session.sessionFile
            if not current_session_file:
                raise RuntimeError("Persisted session is missing a session file")
            session_dir = self.session.sessionManager.getSessionDir()
            if not target_leaf_id:
                session_manager = SessionManager.create(self.cwd, session_dir)
                session_manager.newSession(NewSessionOptions(parentSession=current_session_file))
            else:
                session_manager = SessionManager.open(current_session_file, session_dir)
                forked_session_path = session_manager.createBranchedSession(str(target_leaf_id))
                if not forked_session_path:
                    raise RuntimeError("Failed to create forked session")

            await self.teardownCurrent("fork", session_manager.getSessionFile())
            self.apply(
                await self.createRuntime(
                    {
                        "cwd": session_manager.getCwd(),
                        "agentDir": self.services.agentDir,
                        "sessionManager": session_manager,
                        "sessionStartEvent": {
                            "type": "session_start",
                            "reason": "fork",
                            "previousSessionFile": previous_session_file,
                        },
                    }
                )
            )
            await self.finishSessionReplacement(options.get("withSession") if options else None)
            return {"cancelled": False, "selectedText": selected_text}

        session_manager = self.session.sessionManager
        if not target_leaf_id:
            session_manager.newSession(NewSessionOptions(parentSession=self.session.sessionFile))
        else:
            session_manager.createBranchedSession(str(target_leaf_id))
        await self.teardownCurrent("fork", session_manager.getSessionFile())
        self.apply(
            await self.createRuntime(
                {
                    "cwd": self.cwd,
                    "agentDir": self.services.agentDir,
                    "sessionManager": session_manager,
                    "sessionStartEvent": {
                        "type": "session_start",
                        "reason": "fork",
                        "previousSessionFile": previous_session_file,
                    },
                }
            )
        )
        await self.finishSessionReplacement(options.get("withSession") if options else None)
        return {"cancelled": False, "selectedText": selected_text}

    async def importFromJsonl(
        self,
        inputPath: str,
        cwdOverride: str | None = None,
    ) -> dict[str, bool]:
        resolved_path = resolve_path(inputPath)
        if not os.path.exists(resolved_path):
            raise SessionImportFileNotFoundError(resolved_path)

        session_dir = self.session.sessionManager.getSessionDir()
        if not os.path.exists(session_dir):
            os.makedirs(session_dir, exist_ok=True)

        destination_path = os.path.join(session_dir, os.path.basename(resolved_path))
        before_result = await self.emitBeforeSwitch("resume", destination_path)
        if before_result["cancelled"]:
            return before_result

        previous_session_file = self.session.sessionFile
        if os.path.abspath(destination_path) != resolved_path:
            shutil.copyfile(resolved_path, destination_path)

        session_manager = SessionManager.open(destination_path, session_dir, cwdOverride)
        assert_session_cwd_exists(session_manager, self.cwd)
        await self.teardownCurrent("resume", session_manager.getSessionFile())
        self.apply(
            await self.createRuntime(
                {
                    "cwd": session_manager.getCwd(),
                    "agentDir": self.services.agentDir,
                    "sessionManager": session_manager,
                    "sessionStartEvent": {
                        "type": "session_start",
                        "reason": "resume",
                        "previousSessionFile": previous_session_file,
                    },
                }
            )
        )
        await self.finishSessionReplacement()
        return {"cancelled": False}

    async def dispose(self) -> None:
        await emit_session_shutdown_event(
            self.session.extensionRunner,
            {
                "type": "session_shutdown",
                "reason": "quit",
            },
        )
        if self.beforeSessionInvalidate is not None:
            self.beforeSessionInvalidate()
        self.session.dispose()


async def create_agent_session_runtime(
    createRuntime: CreateAgentSessionRuntimeFactory,
    options: CreateAgentSessionRuntimeOptions,
) -> AgentSessionRuntime:
    assert_session_cwd_exists(options["sessionManager"], options["cwd"])
    result = await createRuntime(options)
    return AgentSessionRuntime(
        result.session,
        result.services,
        createRuntime,
        result.diagnostics,
        result.modelFallbackMessage,
    )


def _message_role(message: Any) -> str | None:
    if isinstance(message, dict):
        role = message.get("role")
    else:
        role = getattr(message, "role", None)
    return role if isinstance(role, str) else None


def _message_content(message: Any) -> str | list[dict[str, Any]]:
    if isinstance(message, dict):
        return message.get("content", "")
    return getattr(message, "content", "")


def _result_flag(result: Any, name: str, default: Any = None) -> Any:
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


createAgentSessionFromServices = create_agent_session_from_services
createAgentSessionServices = create_agent_session_services
createAgentSessionRuntime = create_agent_session_runtime

__all__ = [
    "AgentSessionRuntime",
    "AgentSessionRuntimeDiagnostic",
    "AgentSessionServices",
    "CreateAgentSessionFromServicesOptions",
    "CreateAgentSessionRuntimeFactory",
    "CreateAgentSessionRuntimeResult",
    "CreateAgentSessionServicesOptions",
    "SessionImportFileNotFoundError",
    "createAgentSessionFromServices",
    "createAgentSessionRuntime",
    "createAgentSessionServices",
]
