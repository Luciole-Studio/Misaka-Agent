"""Interactive selector for package-backed resource configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from misaka.ui.tui import (
    Component,
    Container,
    Focusable,
    Input,
    Key,
    Spacer,
    getKeybindings,
    matchesKey,
    truncateToWidth,
    visibleWidth,
)

from misaka.config import CONFIG_DIR_NAME
from misaka.core.package_manager import PathMetadata, ResolvedPaths, ResolvedResource
from misaka.core.settings_manager import SettingsManager
from misaka.ui.tui.interactive.theme.theme import theme

from .dynamic_border import DynamicBorder
from .keybinding_hints import raw_key_hint

type ResourceType = Literal["extensions", "skills", "prompts", "themes"]

RESOURCE_TYPE_LABELS: dict[ResourceType, str] = {
    "extensions": "Extensions",
    "skills": "Skills",
    "prompts": "Prompts",
    "themes": "Themes",
}


@dataclass(slots=True)
class ResourceItem:
    path: str
    enabled: bool
    metadata: PathMetadata
    resourceType: ResourceType
    displayName: str
    groupKey: str
    subgroupKey: str


@dataclass(slots=True)
class ResourceSubgroup:
    type: ResourceType
    label: str
    items: list[ResourceItem]


@dataclass(slots=True)
class ResourceGroup:
    key: str
    label: str
    scope: str
    origin: str
    source: str
    subgroups: list[ResourceSubgroup]


def format_base_dir(base_dir: str) -> str:
    home_dir = str(Path.home())
    if base_dir == home_dir:
        display_path = "~"
    elif base_dir.startswith(home_dir):
        display_path = f"~{base_dir[len(home_dir):].replace(os.sep, '/')}"
    else:
        display_path = base_dir.replace(os.sep, "/")
    return display_path if display_path.endswith("/") else f"{display_path}/"


def get_group_label(metadata: PathMetadata) -> str:
    if metadata.get("origin") == "package":
        return f"{metadata['source']} ({metadata['scope']})"
    if metadata.get("source") == "auto":
        base_dir = metadata.get("baseDir")
        if base_dir:
            return (
                f"User ({format_base_dir(base_dir)})"
                if metadata.get("scope") == "user"
                else f"Project ({format_base_dir(base_dir)})"
            )
        return "User (~/.misaka/agent/)" if metadata.get("scope") == "user" else "Project (.misaka/)"
    return "User settings" if metadata.get("scope") == "user" else "Project settings"


def build_groups(resolved: ResolvedPaths) -> list[ResourceGroup]:
    group_map: dict[str, ResourceGroup] = {}

    def add_to_group(resources: list[ResolvedResource], resource_type: ResourceType) -> None:
        for resource in resources:
            metadata = resource.metadata
            group_key = (
                f"{metadata.get('origin')}:"
                f"{metadata.get('scope')}:"
                f"{metadata.get('source')}:"
                f"{metadata.get('baseDir', '')}"
            )
            if group_key not in group_map:
                group_map[group_key] = ResourceGroup(
                    key=group_key,
                    label=get_group_label(metadata),
                    scope=metadata.get("scope", ""),
                    origin=metadata.get("origin", ""),
                    source=metadata.get("source", ""),
                    subgroups=[],
                )

            group = group_map[group_key]
            subgroup_key = f"{group_key}:{resource_type}"
            subgroup = next((entry for entry in group.subgroups if entry.type == resource_type), None)
            if subgroup is None:
                subgroup = ResourceSubgroup(type=resource_type, label=RESOURCE_TYPE_LABELS[resource_type], items=[])
                group.subgroups.append(subgroup)

            file_name = os.path.basename(resource.path)
            parent_folder = os.path.basename(os.path.dirname(resource.path))
            if resource_type == "extensions" and parent_folder != "extensions":
                display_name = f"{parent_folder}/{file_name}"
            elif resource_type == "skills" and file_name == "SKILL.md":
                display_name = parent_folder
            else:
                display_name = file_name

            subgroup.items.append(
                ResourceItem(
                    path=resource.path,
                    enabled=resource.enabled,
                    metadata=metadata,
                    resourceType=resource_type,
                    displayName=display_name,
                    groupKey=group_key,
                    subgroupKey=subgroup_key,
                )
            )

    add_to_group(resolved.extensions, "extensions")
    add_to_group(resolved.skills, "skills")
    add_to_group(resolved.prompts, "prompts")
    add_to_group(resolved.themes, "themes")

    groups = list(group_map.values())
    groups.sort(key=lambda item: (0 if item.origin == "package" else 1, 0 if item.scope == "user" else 1, item.source))

    type_order = {"extensions": 0, "skills": 1, "prompts": 2, "themes": 3}
    for group in groups:
        group.subgroups.sort(key=lambda entry: type_order[entry.type])
        for subgroup in group.subgroups:
            subgroup.items.sort(key=lambda item: item.displayName)

    return groups


@dataclass(slots=True)
class FlatGroupEntry:
    type: Literal["group"]
    group: ResourceGroup


@dataclass(slots=True)
class FlatSubgroupEntry:
    type: Literal["subgroup"]
    subgroup: ResourceSubgroup
    group: ResourceGroup


@dataclass(slots=True)
class FlatItemEntry:
    type: Literal["item"]
    item: ResourceItem


type FlatEntry = FlatGroupEntry | FlatSubgroupEntry | FlatItemEntry


class ConfigSelectorHeader(Component):
    wantsKeyRelease = False

    def invalidate(self) -> None:
        return None

    def handleInput(self, data: str) -> None:
        return None

    def render(self, width: int) -> list[str]:
        title = theme.bold("Resource Configuration")
        separator = theme.fg("muted", " · ")
        hint = raw_key_hint("space", "toggle") + separator + raw_key_hint("esc", "close")
        spacing = max(1, width - visibleWidth(title) - visibleWidth(hint))
        return [
            truncateToWidth(f"{title}{' ' * spacing}{hint}", width, ""),
            theme.fg("muted", "Type to filter resources"),
        ]


class ResourceList(Component, Focusable):
    wantsKeyRelease = False

    def __init__(
        self,
        groups: list[ResourceGroup],
        settingsManager: SettingsManager,
        cwd: str,
        agentDir: str,
        terminalHeight: int | None = None,
    ) -> None:
        self.groups = groups
        self.settingsManager = settingsManager
        self.cwd = cwd
        self.agentDir = agentDir
        self.searchInput = Input()
        self.maxVisible = max(5, ((24 if terminalHeight is None else terminalHeight) - 8))
        self.flatItems: list[FlatEntry] = []
        self.filteredItems: list[FlatEntry] = []
        self.selectedIndex = 0
        self._focused = False

        self.onCancel = None
        self.onExit = None
        self.onToggle = None

        self.buildFlatList()
        self.filteredItems = list(self.flatItems)

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.searchInput.focused = value

    def buildFlatList(self) -> None:
        self.flatItems = []
        for group in self.groups:
            self.flatItems.append(FlatGroupEntry(type="group", group=group))
            for subgroup in group.subgroups:
                self.flatItems.append(FlatSubgroupEntry(type="subgroup", subgroup=subgroup, group=group))
                for item in subgroup.items:
                    self.flatItems.append(FlatItemEntry(type="item", item=item))

        self.selectedIndex = next((index for index, entry in enumerate(self.flatItems) if entry.type == "item"), 0)

    def findNextItem(self, fromIndex: int, direction: int) -> int:
        index = fromIndex + direction
        while 0 <= index < len(self.filteredItems):
            if self.filteredItems[index].type == "item":
                return index
            index += direction
        return fromIndex

    def filterItems(self, query: str) -> None:
        if not query.strip():
            self.filteredItems = list(self.flatItems)
            self.selectFirstItem()
            return

        lower_query = query.lower()
        matching_items: set[int] = set()
        matching_subgroups: set[int] = set()
        matching_groups: set[int] = set()

        for entry in self.flatItems:
            if entry.type != "item":
                continue
            item = entry.item
            if (
                lower_query in item.displayName.lower()
                or lower_query in item.resourceType.lower()
                or lower_query in item.path.lower()
            ):
                matching_items.add(id(item))

        for group in self.groups:
            for subgroup in group.subgroups:
                for item in subgroup.items:
                    if id(item) in matching_items:
                        matching_subgroups.add(id(subgroup))
                        matching_groups.add(id(group))

        self.filteredItems = []
        for entry in self.flatItems:
            if entry.type == "group" and id(entry.group) in matching_groups:
                self.filteredItems.append(entry)
            elif entry.type == "subgroup" and id(entry.subgroup) in matching_subgroups:
                self.filteredItems.append(entry)
            elif entry.type == "item" and id(entry.item) in matching_items:
                self.filteredItems.append(entry)
        self.selectFirstItem()

    def selectFirstItem(self) -> None:
        self.selectedIndex = next((index for index, entry in enumerate(self.filteredItems) if entry.type == "item"), 0)

    def updateItem(self, item: ResourceItem, enabled: bool) -> None:
        item.enabled = enabled
        for group in self.groups:
            for subgroup in group.subgroups:
                found = next(
                    (
                        entry
                        for entry in subgroup.items
                        if entry.path == item.path and entry.resourceType == item.resourceType
                    ),
                    None,
                )
                if found is not None:
                    found.enabled = enabled
                    return

    def invalidate(self) -> None:
        return None

    def render(self, width: int) -> list[str]:
        lines = [*self.searchInput.render(width), ""]
        if not self.filteredItems:
            return [*lines, theme.fg("muted", "  No resources found")]

        start_index = max(
            0,
            min(self.selectedIndex - (self.maxVisible // 2), len(self.filteredItems) - self.maxVisible),
        )
        end_index = min(start_index + self.maxVisible, len(self.filteredItems))

        for index in range(start_index, end_index):
            entry = self.filteredItems[index]
            is_selected = index == self.selectedIndex

            if entry.type == "group":
                lines.append(truncateToWidth(f"  {theme.fg('accent', theme.bold(entry.group.label))}", width, ""))
            elif entry.type == "subgroup":
                lines.append(truncateToWidth(f"    {theme.fg('muted', entry.subgroup.label)}", width, ""))
            else:
                cursor = "> " if is_selected else "  "
                checkbox = theme.fg("success", "[x]") if entry.item.enabled else theme.fg("dim", "[ ]")
                name = theme.bold(entry.item.displayName) if is_selected else entry.item.displayName
                lines.append(truncateToWidth(f"{cursor}    {checkbox} {name}", width, "..."))

        if start_index > 0 or end_index < len(self.filteredItems):
            item_count = sum(1 for entry in self.filteredItems if entry.type == "item")
            current_item_index = sum(
                1
                for entry in self.filteredItems[: self.selectedIndex + 1]
                if entry.type == "item"
            )
            lines.append(theme.fg("dim", f"  ({current_item_index}/{item_count})"))

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
            target = max(0, self.selectedIndex - self.maxVisible)
            while target < len(self.filteredItems) and self.filteredItems[target].type != "item":
                target += 1
            if target < len(self.filteredItems):
                self.selectedIndex = target
            return
        if kb.matches(data, "tui.select.pageDown"):
            target = min(len(self.filteredItems) - 1, self.selectedIndex + self.maxVisible)
            while target >= 0 and self.filteredItems[target].type != "item":
                target -= 1
            if target >= 0:
                self.selectedIndex = target
            return
        if kb.matches(data, "tui.select.cancel"):
            if callable(self.onCancel):
                self.onCancel()
            return
        if matchesKey(data, "ctrl+c"):
            if callable(self.onExit):
                self.onExit()
            return
        if data == " " or kb.matches(data, "tui.select.confirm"):
            entry = self.filteredItems[self.selectedIndex] if self.filteredItems else None
            if isinstance(entry, FlatItemEntry):
                new_enabled = not entry.item.enabled
                self.toggleResource(entry.item, new_enabled)
                self.updateItem(entry.item, new_enabled)
                if callable(self.onToggle):
                    self.onToggle(entry.item, new_enabled)
            return

        self.searchInput.handleInput(data)
        self.filterItems(self.searchInput.getValue())

    def toggleResource(self, item: ResourceItem, enabled: bool) -> None:
        if item.metadata.get("origin") == "top-level":
            self.toggleTopLevelResource(item, enabled)
        else:
            self.togglePackageResource(item, enabled)

    def toggleTopLevelResource(self, item: ResourceItem, enabled: bool) -> None:
        scope = str(item.metadata["scope"])
        settings = (
            self.settingsManager.getProjectSettings()
            if scope == "project"
            else self.settingsManager.getGlobalSettings()
        )
        array_key = item.resourceType
        current = list(settings.get(array_key) or [])
        pattern = self.getResourcePattern(item)
        updated = [
            entry
            for entry in current
            if (entry[1:] if entry.startswith(("!", "+", "-")) else entry) != pattern
        ]
        updated.append(f"+{pattern}" if enabled else f"-{pattern}")

        if scope == "project":
            if array_key == "extensions":
                self.settingsManager.setProjectExtensionPaths(updated)
            elif array_key == "skills":
                self.settingsManager.setProjectSkillPaths(updated)
            elif array_key == "prompts":
                self.settingsManager.setProjectPromptTemplatePaths(updated)
            else:
                self.settingsManager.setProjectThemePaths(updated)
        else:
            if array_key == "extensions":
                self.settingsManager.setExtensionPaths(updated)
            elif array_key == "skills":
                self.settingsManager.setSkillPaths(updated)
            elif array_key == "prompts":
                self.settingsManager.setPromptTemplatePaths(updated)
            else:
                self.settingsManager.setThemePaths(updated)

    def togglePackageResource(self, item: ResourceItem, enabled: bool) -> None:
        scope = str(item.metadata["scope"])
        settings = (
            self.settingsManager.getProjectSettings()
            if scope == "project"
            else self.settingsManager.getGlobalSettings()
        )
        packages = list(settings.get("packages") or [])
        pkg_index = next(
            (
                index
                for index, pkg in enumerate(packages)
                if (pkg if isinstance(pkg, str) else pkg.get("source")) == item.metadata["source"]
            ),
            -1,
        )
        if pkg_index < 0:
            return

        package = packages[pkg_index]
        if isinstance(package, str):
            package = {"source": package}
            packages[pkg_index] = package

        array_key = item.resourceType
        current = list(package.get(array_key) or [])
        pattern = self.getPackageResourcePattern(item)
        updated = [
            entry
            for entry in current
            if (entry[1:] if entry.startswith(("!", "+", "-")) else entry) != pattern
        ]
        updated.append(f"+{pattern}" if enabled else f"-{pattern}")
        if updated:
            package[array_key] = updated
        else:
            package.pop(array_key, None)

        has_filters = any(key in package for key in ("extensions", "skills", "prompts", "themes"))
        if not has_filters:
            packages[pkg_index] = package["source"]

        if scope == "project":
            self.settingsManager.setProjectPackages(packages)
        else:
            self.settingsManager.setPackages(packages)

    def getTopLevelBaseDir(self, scope: Literal["user", "project"]) -> str:
        return os.path.join(self.cwd, CONFIG_DIR_NAME) if scope == "project" else self.agentDir

    def getResourcePattern(self, item: ResourceItem) -> str:
        scope = item.metadata["scope"]
        base_dir = item.metadata.get("baseDir")
        if base_dir is None:
            base_dir = self.getTopLevelBaseDir("project" if scope == "project" else "user")
        return os.path.relpath(item.path, base_dir)

    def getPackageResourcePattern(self, item: ResourceItem) -> str:
        base_dir = item.metadata.get("baseDir")
        if base_dir is None:
            base_dir = os.path.dirname(item.path)
        return os.path.relpath(item.path, base_dir)


class ConfigSelectorComponent(Container, Focusable):
    wantsKeyRelease = False

    def __init__(
        self,
        resolvedPaths: ResolvedPaths,
        settingsManager: SettingsManager,
        cwd: str,
        agentDir: str,
        onClose,
        onExit,
        requestRender,
        terminalHeight: int | None = None,
    ) -> None:
        super().__init__()
        self._focused = False
        groups = build_groups(resolvedPaths)

        self.addChild(Spacer(1))
        self.addChild(DynamicBorder())
        self.addChild(Spacer(1))
        self.addChild(ConfigSelectorHeader())
        self.addChild(Spacer(1))

        self.resourceList = ResourceList(groups, settingsManager, cwd, agentDir, terminalHeight)
        self.resourceList.onCancel = onClose
        self.resourceList.onExit = onExit
        self.resourceList.onToggle = lambda *_args: requestRender()
        self.addChild(self.resourceList)

        self.addChild(Spacer(1))
        self.addChild(DynamicBorder())

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.resourceList.focused = value

    def getResourceList(self) -> ResourceList:
        return self.resourceList


__all__ = ["ConfigSelectorComponent"]
