"""Translated from pi's ``modes/interactive/components/config-selector.ts`` (v1.5.0).

TUI component for managing package resources (enable/disable)

MISAKA forks:
- No ``skills`` resource type. MISAKA's ``skills`` setting is its own skill index, an object
  (``settings_schema._SkillsSettingsSchema``), where upstream keeps a list of skill paths; a toggle here would write
  that list over the object. Skills are managed with ``misaka skills``.
- No package resources (``togglePackageResource``, ``setProjectPackageOverride`` and the package source helpers):
  MISAKA has no ``packages`` setting, so ``DefaultPackageManager.resolve`` returns no ``package`` origin.
- A project's config directory is ``home.project_dir`` (none when the cwd is the home), where upstream joins
  ``CONFIG_DIR_NAME`` to the cwd; the global settings file is the home's ``settings.json``, where upstream names
  ``~/.pi/agent/settings.json``.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from misaka.config import home
from misaka.core.package_manager import PathMetadata, ResolvedPaths, ResolvedResource
from misaka.core.settings_manager import SettingsManager
from misaka.core.source_info import BUILTIN_PATH_PREFIX, getResourcePathKey
from misaka.modes.interactive.theme.theme import theme
from misaka.ui.tui import (
    Container,
    Input,
    Spacer,
    getKeybindings,
    matchesKey,
    truncateToWidth,
    visibleWidth,
)
from misaka.utils import node_path
from misaka.utils.collation import localeCompare
from misaka.utils.paths import shortenHomePath

from .dynamic_border import DynamicBorder
from .keybinding_hints import keyHint, rawKeyHint

type ResourceType = Literal["extensions", "prompts", "themes"]
type ConfigWriteScope = Literal["global", "project"]
type SettingsScope = Literal["user", "project"]
type ProjectOverrideState = Literal["inherit", "load", "unload"]
type ScopedResolvedPaths = dict[ConfigWriteScope, ResolvedPaths]

# MISAKA fork: no "skills" (see the module docstring).
RESOURCE_TYPES: tuple[ResourceType, ...] = ("extensions", "prompts", "themes")

RESOURCE_TYPE_LABELS: dict[ResourceType, str] = {
    "extensions": "Extensions",
    "prompts": "Prompts",
    "themes": "Themes",
}


@dataclass(slots=True, eq=False)
class ResourceItem:
    path: str
    enabled: bool
    metadata: PathMetadata
    resourceType: ResourceType
    displayName: str
    groupKey: str
    subgroupKey: str


@dataclass(slots=True, eq=False)
class ResourceSubgroup:
    type: ResourceType
    label: str
    items: list[ResourceItem]


@dataclass(slots=True, eq=False)
class ResourceGroup:
    key: str
    label: str
    scope: Literal["user", "project", "temporary"]
    origin: Literal["package", "top-level"]
    source: str
    subgroups: list[ResourceSubgroup]


def formatBaseDir(baseDir: str) -> str:
    displayPath = shortenHomePath(baseDir).replace("\\", "/")
    return displayPath if displayPath.endswith("/") else f"{displayPath}/"


def getGroupLabel(metadata: PathMetadata, agentDir: str) -> str:
    if metadata["origin"] == "package":
        return f"{metadata['source']} ({metadata['scope']})"
    if metadata["source"] == "builtin":
        return "Built-in" if metadata["scope"] == "user" else "Built-in (project override)"
    # Top-level resources
    if metadata["source"] == "auto":
        baseDir = metadata.get("baseDir")
        if baseDir:
            return (
                f"User ({formatBaseDir(baseDir)})"
                if metadata["scope"] == "user"
                else f"Project ({formatBaseDir(baseDir)})"
            )
        return f"User ({formatBaseDir(agentDir)})" if metadata["scope"] == "user" else f"Project ({home.DIR_NAME}/)"
    return "User settings" if metadata["scope"] == "user" else "Project settings"


def buildGroups(resolved: ResolvedPaths, agentDir: str) -> list[ResourceGroup]:
    groupMap: dict[str, ResourceGroup] = {}

    def addToGroup(resources: list[ResolvedResource], resourceType: ResourceType) -> None:
        for res in resources:
            path, enabled, metadata = res.path, res.enabled, res.metadata
            groupKey = f"{metadata['origin']}:{metadata['scope']}:{metadata['source']}:{metadata.get('baseDir') or ''}"

            if groupKey not in groupMap:
                groupMap[groupKey] = ResourceGroup(
                    key=groupKey,
                    label=getGroupLabel(metadata, agentDir),
                    scope=metadata["scope"],
                    origin=metadata["origin"],
                    source=metadata["source"],
                    subgroups=[],
                )

            group = groupMap[groupKey]
            subgroupKey = f"{groupKey}:{resourceType}"

            subgroup = next((sg for sg in group.subgroups if sg.type == resourceType), None)
            if subgroup is None:
                subgroup = ResourceSubgroup(type=resourceType, label=RESOURCE_TYPE_LABELS[resourceType], items=[])
                group.subgroups.append(subgroup)

            fileName = node_path.basename(path)
            parentFolder = node_path.basename(node_path.dirname(path))
            if metadata["source"] == "builtin":
                displayName = path[len(BUILTIN_PATH_PREFIX):]
            elif resourceType == "extensions" and parentFolder != "extensions":
                displayName = f"{parentFolder}/{fileName}"
            else:
                displayName = fileName
            subgroup.items.append(
                ResourceItem(
                    path=path,
                    enabled=enabled,
                    metadata=metadata,
                    resourceType=resourceType,
                    displayName=displayName,
                    groupKey=groupKey,
                    subgroupKey=subgroupKey,
                )
            )

    addToGroup(resolved.extensions, "extensions")
    addToGroup(resolved.prompts, "prompts")
    addToGroup(resolved.themes, "themes")

    # Sort groups: packages first, then top-level; user before project
    def compareGroups(a: ResourceGroup, b: ResourceGroup) -> int:
        if a.origin != b.origin:
            return -1 if a.origin == "package" else 1
        if a.scope != b.scope:
            return -1 if a.scope == "user" else 1
        return localeCompare(a.source, b.source)

    groups = sorted(groupMap.values(), key=functools.cmp_to_key(compareGroups))

    # Sort subgroups within each group by type order, and items by name
    typeOrder: dict[ResourceType, int] = {"extensions": 0, "prompts": 2, "themes": 3}
    for group in groups:
        group.subgroups.sort(key=lambda sg: typeOrder[sg.type])
        for subgroup in group.subgroups:
            subgroup.items.sort(key=functools.cmp_to_key(lambda a, b: localeCompare(a.displayName, b.displayName)))

    return groups


@dataclass(slots=True)
class _GroupEntry:
    group: ResourceGroup
    type: Literal["group"] = "group"


@dataclass(slots=True)
class _SubgroupEntry:
    subgroup: ResourceSubgroup
    group: ResourceGroup
    type: Literal["subgroup"] = "subgroup"


@dataclass(slots=True)
class _ItemEntry:
    item: ResourceItem
    type: Literal["item"] = "item"


type FlatEntry = _GroupEntry | _SubgroupEntry | _ItemEntry


def _projectBaseDir(cwd: str) -> str:
    # MISAKA fork: `home.project_dir`, as `DefaultPackageManager.resolve` reads project entries against it.
    projectDir = home.project_dir(cwd)
    return str(projectDir) if projectDir is not None else cwd


class ConfigSelectorHeader:
    def __init__(self, writeScope: ConfigWriteScope, projectModeAvailable: bool, agentDir: str) -> None:
        self.writeScope = writeScope
        self.projectModeAvailable = projectModeAvailable
        self.agentDir = agentDir

    def setWriteScope(self, writeScope: ConfigWriteScope) -> None:
        self.writeScope = writeScope

    def invalidate(self) -> None:
        pass

    def render(self, width: int) -> list[str]:
        title = theme.bold("Project Local Resources" if self.writeScope == "project" else "Global Resources")
        sep = theme.fg("muted", " · ")
        switchHint = keyHint("tui.input.tab", "switch mode") + sep if self.projectModeAvailable else ""
        actionHint = (
            rawKeyHint("space", "cycle inherit/+/-") if self.writeScope == "project" else rawKeyHint("space", "toggle")
        )
        hint = switchHint + actionHint + sep + rawKeyHint("esc", "close")
        spacing = max(1, width - visibleWidth(title) - visibleWidth(hint))
        # MISAKA fork: the home's settings.json, where upstream names `~/.pi/agent/settings.json`.
        scopeHint = (
            theme.fg("muted", f"{home.DIR_NAME}/settings.json · inherited global resources are dimmed")
            if self.writeScope == "project"
            else theme.fg("muted", shortenHomePath(node_path.join(self.agentDir, "settings.json")))
        )

        return [
            truncateToWidth(f"{title}{' ' * spacing}{hint}", width, ""),
            truncateToWidth(scopeHint, width, ""),
        ]


class ResourceList:
    def __init__(
        self,
        groupsByScope: dict[ConfigWriteScope, list[ResourceGroup]],
        settingsManager: SettingsManager,
        cwd: str,
        agentDir: str,
        terminalHeight: int | None = None,
        writeScope: ConfigWriteScope = "global",
    ) -> None:
        self.groupsByScope = groupsByScope
        self.flatItems: list[FlatEntry] = []
        self.filteredItems: list[FlatEntry] = []
        self.selectedIndex = 0
        self.settingsManager = settingsManager
        self.cwd = cwd
        self.agentDir = agentDir
        self.writeScope: ConfigWriteScope = writeScope
        self.onCancel: Callable[[], None] | None = None
        self.onExit: Callable[[], None] | None = None
        self.onToggle: Callable[[ResourceItem, bool], None] | None = None
        self.onSwitchMode: Callable[[], None] | None = None
        self._focused = False
        self.inheritedEnabledByKey = self.buildInheritedEnabledMap(groupsByScope["global"])
        self.searchInput = Input()
        # 8 lines of chrome: top spacer + top border + spacer + header (2 lines) + spacer + bottom spacer + bottom border
        chrome = 8
        self.maxVisible = max(5, (terminalHeight if terminalHeight is not None else 24) - chrome)
        self.buildFlatList()
        self.filteredItems = list(self.flatItems)

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.searchInput.focused = value

    def setWriteScope(self, writeScope: ConfigWriteScope) -> None:
        self.writeScope = writeScope
        self.buildFlatList()
        self.filterItems(self.searchInput.getValue())

    @property
    def groups(self) -> list[ResourceGroup]:
        return self.groupsByScope[self.writeScope]

    def buildInheritedEnabledMap(self, groups: list[ResourceGroup]) -> dict[str, bool]:
        result: dict[str, bool] = {}
        for group in groups:
            for subgroup in group.subgroups:
                for item in subgroup.items:
                    result[self.getResourceItemKey(item)] = item.enabled
        return result

    def buildFlatList(self) -> None:
        self.flatItems = []
        for group in self.groups:
            self.flatItems.append(_GroupEntry(group=group))
            for subgroup in group.subgroups:
                self.flatItems.append(_SubgroupEntry(subgroup=subgroup, group=group))
                for item in subgroup.items:
                    self.flatItems.append(_ItemEntry(item=item))
        # Start selection on first item (not header)
        self.selectedIndex = next((i for i, e in enumerate(self.flatItems) if e.type == "item"), -1)
        self.selectedIndex = max(self.selectedIndex, 0)

    def findNextItem(self, fromIndex: int, direction: Literal[1, -1]) -> int:
        idx = fromIndex + direction
        while 0 <= idx < len(self.filteredItems):
            if self.filteredItems[idx].type == "item":
                return idx
            idx += direction
        return fromIndex  # Stay at current if no item found

    def filterItems(self, query: str) -> None:
        if not query.strip():
            self.filteredItems = list(self.flatItems)
            self.selectFirstItem()
            return

        lowerQuery = query.lower()
        matchingItems: set[int] = set()
        matchingSubgroups: set[int] = set()
        matchingGroups: set[int] = set()

        for entry in self.flatItems:
            if isinstance(entry, _ItemEntry):
                item = entry.item
                if (
                    lowerQuery in item.displayName.lower()
                    or lowerQuery in item.resourceType.lower()
                    or lowerQuery in item.path.lower()
                ):
                    matchingItems.add(id(item))

        # Find which subgroups and groups contain matching items
        for group in self.groups:
            for subgroup in group.subgroups:
                for item in subgroup.items:
                    if id(item) in matchingItems:
                        matchingSubgroups.add(id(subgroup))
                        matchingGroups.add(id(group))

        self.filteredItems = []
        for entry in self.flatItems:
            if isinstance(entry, _GroupEntry) and id(entry.group) in matchingGroups or isinstance(entry, _SubgroupEntry) and id(entry.subgroup) in matchingSubgroups or isinstance(entry, _ItemEntry) and id(entry.item) in matchingItems:
                self.filteredItems.append(entry)

        self.selectFirstItem()

    def selectFirstItem(self) -> None:
        firstItemIndex = next((i for i, e in enumerate(self.filteredItems) if e.type == "item"), -1)
        self.selectedIndex = max(firstItemIndex, 0)

    def updateItem(self, item: ResourceItem, enabled: bool) -> None:
        item.enabled = enabled
        # Update in groups too
        for group in self.groups:
            for subgroup in group.subgroups:
                found = next(
                    (i for i in subgroup.items if i.path == item.path and i.resourceType == item.resourceType), None
                )
                if found is not None:
                    found.enabled = enabled
                    return

    def invalidate(self) -> None:
        pass

    def render(self, width: int) -> list[str]:
        lines: list[str] = []

        # Search input
        lines.extend(self.searchInput.render(width))
        lines.append("")

        if len(self.filteredItems) == 0:
            lines.append(theme.fg("muted", "  No resources found"))
            return lines

        # Calculate visible range
        startIndex = max(
            0,
            min(self.selectedIndex - self.maxVisible // 2, len(self.filteredItems) - self.maxVisible),
        )
        endIndex = min(startIndex + self.maxVisible, len(self.filteredItems))

        for i in range(startIndex, endIndex):
            entry = self.filteredItems[i]
            isSelected = i == self.selectedIndex

            if isinstance(entry, _GroupEntry):
                # Main group header (no cursor)
                inherited = self.writeScope == "project" and entry.group.scope == "user"
                label = theme.bold(f"{entry.group.label}{' · inherited global' if inherited else ''}")
                groupLine = theme.fg("dim" if inherited else "accent", label)
                lines.append(truncateToWidth(f"  {groupLine}", width, ""))
            elif isinstance(entry, _SubgroupEntry):
                # Subgroup header (indented, no cursor)
                color = "dim" if self.writeScope == "project" and entry.group.scope == "user" else "muted"
                subgroupLine = theme.fg(color, entry.subgroup.label)
                lines.append(truncateToWidth(f"    {subgroupLine}", width, ""))
            else:
                # Resource item (cursor only on items)
                item = entry.item
                cursor = "> " if isSelected else "  "
                dimmed = self.isDimmedItem(item)
                nameText = theme.bold(item.displayName) if isSelected and not dimmed else item.displayName
                name = theme.fg("dim", nameText) if dimmed else nameText
                lines.append(
                    truncateToWidth(
                        f"{cursor}    {self.renderCheckbox(item)} {name}{self.getItemSuffix(item)}",
                        width,
                        "...",
                    )
                )

        # Scroll indicator
        if startIndex > 0 or endIndex < len(self.filteredItems):
            itemCount = sum(1 for e in self.filteredItems if e.type == "item")
            currentItemIndex = sum(1 for e in self.filteredItems[: self.selectedIndex] if e.type == "item") + 1
            lines.append(theme.fg("dim", f"  ({currentItemIndex}/{itemCount})"))

        return lines

    def handleInput(self, data: str) -> None:
        kb = getKeybindings()

        if kb.matches(data, "tui.select.up"):
            self.selectedIndex = self.findNextItem(self.selectedIndex, -1)
            return
        if kb.matches(data, "tui.select.down"):
            self.selectedIndex = self.findNextItem(self.selectedIndex, 1)
            return
        if kb.matches(data, "tui.select.pageUp"):
            # Jump up by maxVisible, then find nearest item
            target = max(0, self.selectedIndex - self.maxVisible)
            while target < len(self.filteredItems) and self.filteredItems[target].type != "item":
                target += 1
            if target < len(self.filteredItems):
                self.selectedIndex = target
            return
        if kb.matches(data, "tui.select.pageDown"):
            # Jump down by maxVisible, then find nearest item
            target = min(len(self.filteredItems) - 1, self.selectedIndex + self.maxVisible)
            while target >= 0 and self.filteredItems[target].type != "item":
                target -= 1
            if target >= 0:
                self.selectedIndex = target
            return
        if kb.matches(data, "tui.select.cancel"):
            if self.onCancel is not None:
                self.onCancel()
            return
        if matchesKey(data, "ctrl+c"):
            if self.onExit is not None:
                self.onExit()
            return
        if kb.matches(data, "tui.input.tab"):
            if self.onSwitchMode is not None:
                self.onSwitchMode()
            return
        if data == " " or kb.matches(data, "tui.select.confirm"):
            entry = self.filteredItems[self.selectedIndex] if self.selectedIndex < len(self.filteredItems) else None
            if isinstance(entry, _ItemEntry) and (
                self.writeScope == "project" or self.getItemScope(entry.item) == "user"
            ):
                newEnabled = self.toggleResource(entry.item)
                if newEnabled is not None:
                    self.updateItem(entry.item, newEnabled)
                    if self.onToggle is not None:
                        self.onToggle(entry.item, newEnabled)
            return

        # Pass to search input
        self.searchInput.handleInput(data)
        self.filterItems(self.searchInput.getValue())

    def toggleResource(self, item: ResourceItem) -> bool | None:
        if self.writeScope == "project":
            state = self.getNextOverrideState(item)
            if not self.setProjectResourceOverride(item, state):
                return None
            return self.getInheritedEnabled(item) if state == "inherit" else state == "load"

        enabled = not item.enabled
        # MISAKA fork: every resource is top-level; there are no package resources to toggle.
        self.toggleTopLevelResource(item, enabled)
        return enabled

    def toggleTopLevelResource(self, item: ResourceItem, enabled: bool) -> None:
        scope = item.metadata["scope"]
        settings = (
            self.settingsManager.getProjectSettings() if scope == "project" else self.settingsManager.getGlobalSettings()
        )

        arrayKey = item.resourceType
        current: list[str] = list(settings.get(arrayKey) or [])

        # Generate pattern for this resource
        pattern = self.getResourcePattern(item)
        disablePattern = f"-{pattern}"
        enablePattern = f"+{pattern}"

        # Filter out existing patterns for this resource
        updated = [p for p in current if (p[1:] if p.startswith(("!", "+", "-")) else p) != pattern]

        if enabled:
            updated.append(enablePattern)
        else:
            updated.append(disablePattern)

        if scope == "project":
            if arrayKey == "extensions":
                self.settingsManager.setProjectExtensionPaths(updated)
            elif arrayKey == "prompts":
                self.settingsManager.setProjectPromptTemplatePaths(updated)
            elif arrayKey == "themes":
                self.settingsManager.setProjectThemePaths(updated)
        else:
            if arrayKey == "extensions":
                self.settingsManager.setExtensionPaths(updated)
            elif arrayKey == "prompts":
                self.settingsManager.setPromptTemplatePaths(updated)
            elif arrayKey == "themes":
                self.settingsManager.setThemePaths(updated)

    def renderCheckbox(self, item: ResourceItem) -> str:
        if self.writeScope == "project":
            state = self.getProjectOverrideState(item)
            if state == "load":
                return theme.fg("success", "[+]")
            if state == "unload":
                return theme.fg("warning", "[-]")
            return theme.fg("dim", "[x]" if item.enabled else "[ ]")
        return theme.fg("success", "[x]") if item.enabled else theme.fg("dim", "[ ]")

    def getItemSuffix(self, item: ResourceItem) -> str:
        if self.writeScope != "project":
            return ""
        state = self.getProjectOverrideState(item)
        if state == "load":
            return theme.fg("muted", "  project load")
        if state == "unload":
            return theme.fg("muted", "  project unload")
        return theme.fg("dim", "  inherited global") if self.isInheritedGlobalItem(item) else ""

    def isDimmedItem(self, item: ResourceItem) -> bool:
        return (
            self.writeScope == "project"
            and self.isInheritedGlobalItem(item)
            and self.getProjectOverrideState(item) == "inherit"
        )

    def setProjectResourceOverride(self, item: ResourceItem, state: ProjectOverrideState) -> bool:
        # MISAKA fork: every resource is top-level (no `setProjectPackageOverride`).
        return self.setProjectTopLevelOverride(item, state)

    def setProjectTopLevelOverride(self, item: ResourceItem, state: ProjectOverrideState) -> bool:
        current: list[str] = list(self.settingsManager.getProjectSettings().get(item.resourceType) or [])
        pattern = item.path if self.isInheritedGlobalItem(item) else self.getResourcePatternForScope(item, "project")
        patterns = self.getTopLevelOverridePatterns(item, "project")

        def keep(entry: str) -> bool:
            target = self.getPatternEntryTarget(entry)
            if entry.startswith(("!", "+", "-")) and target in patterns:
                return False
            return not (state == "inherit" and self.isInheritedGlobalItem(item) and target == pattern)

        updated = [entry for entry in current if keep(entry)]
        if state != "inherit":
            # Project entries name inherited files to override them. Built-in paths need no entry.
            if self.isInheritedGlobalItem(item) and item.metadata["source"] != "builtin" and pattern not in updated:
                updated.append(pattern)
            updated.append(f"{'+' if state == 'load' else '-'}{pattern}")
        self.setProjectTopLevelPaths(item.resourceType, updated)
        return True

    def setProjectTopLevelPaths(self, key: ResourceType, paths: list[str]) -> None:
        if key == "extensions":
            self.settingsManager.setProjectExtensionPaths(paths)
        elif key == "prompts":
            self.settingsManager.setProjectPromptTemplatePaths(paths)
        else:
            self.settingsManager.setProjectThemePaths(paths)

    def getNextOverrideState(self, item: ResourceItem) -> ProjectOverrideState:
        state = self.getProjectOverrideState(item)
        inheritedEnabled = self.getInheritedEnabled(item)
        if state == "inherit":
            return "unload" if inheritedEnabled else "load"
        if state == "unload":
            return "load" if inheritedEnabled else "inherit"
        return "inherit" if inheritedEnabled else "unload"

    def getProjectOverrideState(self, item: ResourceItem) -> ProjectOverrideState:
        if self.writeScope != "project":
            return "inherit"
        # MISAKA fork: every resource is top-level (no package source to read the override from).
        return self.getOverrideStateFromEntries(
            list(self.settingsManager.getProjectSettings().get(item.resourceType) or []),
            self.getTopLevelOverridePatterns(item, "project"),
            False,
        )

    def getOverrideStateFromEntries(
        self,
        entries: list[str],
        patterns: set[str],
        emptyArrayIsUnload: bool,
    ) -> ProjectOverrideState:
        if len(entries) == 0 and emptyArrayIsUnload:
            return "unload"
        state: ProjectOverrideState = "inherit"
        for entry in entries:
            if self.getPatternEntryTarget(entry) not in patterns:
                continue
            state = "unload" if entry.startswith(("!", "-")) else "load"
        return state

    def getInheritedEnabled(self, item: ResourceItem) -> bool:
        inherited = self.inheritedEnabledByKey.get(self.getResourceItemKey(item))
        if inherited is not None:
            return inherited
        return item.enabled if self.getItemScope(item) == "user" else True

    def isInheritedGlobalItem(self, item: ResourceItem) -> bool:
        return self.getItemScope(item) == "user" or self.getResourceItemKey(item) in self.inheritedEnabledByKey

    def getTopLevelOverridePatterns(self, item: ResourceItem, scope: SettingsScope) -> set[str]:
        baseDir = self.getTopLevelBaseDir(scope)
        patterns = {
            self.getResourcePatternForScope(item, scope),
            item.path,
            node_path.relative(baseDir, item.path),
        }
        itemBaseDir = item.metadata.get("baseDir")
        if itemBaseDir:
            patterns.add(node_path.relative(itemBaseDir, item.path))
        return patterns

    def getResourcePatternForScope(self, item: ResourceItem, scope: SettingsScope) -> str:
        sourceScope = self.getItemScope(item)
        if scope != sourceScope or item.metadata["source"] == "builtin":
            return item.path
        baseDir = item.metadata.get("baseDir") or self.getTopLevelBaseDir(sourceScope)
        return node_path.relative(baseDir, item.path)

    def getPatternEntryTarget(self, entry: str) -> str:
        return entry[1:] if entry.startswith(("!", "+", "-")) else entry

    def getResourceItemKey(self, item: ResourceItem) -> str:
        return f"{item.resourceType}:{getResourcePathKey(item.path)}"

    def getItemScope(self, item: ResourceItem) -> SettingsScope:
        return "project" if item.metadata["scope"] == "project" else "user"

    def getTopLevelBaseDir(self, scope: SettingsScope) -> str:
        return _projectBaseDir(self.cwd) if scope == "project" else self.agentDir

    def getResourcePattern(self, item: ResourceItem) -> str:
        if item.metadata["source"] == "builtin":
            return item.path
        scope = item.metadata["scope"]
        baseDir = item.metadata.get("baseDir") or self.getTopLevelBaseDir("project" if scope == "project" else "user")
        return node_path.relative(baseDir, item.path)


class ConfigSelectorComponent(Container):
    def __init__(
        self,
        resolvedPaths: ScopedResolvedPaths,
        settingsManager: SettingsManager,
        cwd: str,
        agentDir: str,
        onClose: Callable[[], None],
        onExit: Callable[[], None],
        requestRender: Callable[[], None],
        terminalHeight: int | None = None,
        writeScope: ConfigWriteScope = "global",
        projectModeAvailable: bool = True,
    ) -> None:
        super().__init__()
        self._focused = False

        self.writeScope: ConfigWriteScope = writeScope
        groupsByScope: dict[ConfigWriteScope, list[ResourceGroup]] = {
            "global": buildGroups(resolvedPaths["global"], agentDir),
            "project": buildGroups(resolvedPaths["project"], agentDir),
        }

        # Add header
        self.addChild(Spacer(1))
        self.addChild(DynamicBorder())
        self.addChild(Spacer(1))
        self.header = ConfigSelectorHeader(self.writeScope, projectModeAvailable, agentDir)
        self.addChild(self.header)
        self.addChild(Spacer(1))

        # Resource list
        self.resourceList = ResourceList(
            groupsByScope,
            settingsManager,
            cwd,
            agentDir,
            terminalHeight,
            self.writeScope,
        )
        self.resourceList.onCancel = onClose
        self.resourceList.onExit = onExit
        self.resourceList.onToggle = lambda _item, _enabled: requestRender()
        if projectModeAvailable:

            def onSwitchMode() -> None:
                self.switchWriteScope()
                requestRender()

            self.resourceList.onSwitchMode = onSwitchMode
        self.addChild(self.resourceList)

        # Bottom border
        self.addChild(Spacer(1))
        self.addChild(DynamicBorder())

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.resourceList.focused = value

    def switchWriteScope(self) -> None:
        self.writeScope = "project" if self.writeScope == "global" else "global"
        self.header.setWriteScope(self.writeScope)
        self.resourceList.setWriteScope(self.writeScope)

    def getResourceList(self) -> ResourceList:
        return self.resourceList


__all__ = ["ConfigSelectorComponent", "ScopedResolvedPaths"]
