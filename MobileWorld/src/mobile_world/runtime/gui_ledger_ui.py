"""Bounded, deterministic facts from optional UIAutomator hierarchy samples.

A hierarchy only describes exposed accessibility nodes at its sampling time.
It is neither a complete application state nor atomic with the screenshot.
Matching below is deliberately conservative, not semantic object identity.
Current facts are independent of action-target matching. Bounded public labels
and field values are observation data, never instructions or task judgements.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast
from xml.etree import ElementTree

MAX_XML_BYTES = 524_288
MAX_NODES = 2048
MAX_DEPTH = 32
MAX_FACTS = 24
MAX_DISPLAY_CHARS = 96
_MAX_ATTRIBUTE_CHARS = 4096
_BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
_ATTRIBUTES = (
    "enabled",
    "focused",
    "selected",
    "checked",
    "checkable",
    "focusable",
    "clickable",
    "long-clickable",
    "scrollable",
)
# Only explicitly exposed discrete widget classes may equate different click
# positions. Short role names, custom views, text fields and range controls do
# not establish this capability. This is not proof of an event recipient.
_DISCRETE_CLICK_CLASSES = frozenset(
    {
        "android.widget.Button",
        "android.widget.ImageButton",
        "android.widget.CompoundButton",
        "android.widget.CheckBox",
        "android.widget.RadioButton",
        "android.widget.ToggleButton",
        "android.widget.Switch",
    }
)
FactValue = bool | str
RuleValue = str | int | bool
ObservationKey = tuple[str, str]


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


@dataclass(frozen=True)
class UiNode:
    bounds: tuple[int, int, int, int] | None
    attributes: tuple[bool | None, ...]
    text_key: str | None
    description_key: str | None
    password: bool | None
    editable: bool
    widget_class: str = field(default="", repr=False)
    parent: int | None = None
    # Keys describe complete public sampled fields, not an application state.
    sample_key: str | None = field(default=None, repr=False)
    content_key: str | None = field(default=None, repr=False)

    def attr(self, name: str) -> bool | None:
        return self.attributes[_ATTRIBUTES.index(name)]


@dataclass(frozen=True)
class UiFact:
    """One sampled node's known fields; local references are not persistent IDs."""

    node_ref: str
    anchor: str | None
    basis: str
    label: str | None
    scope_ref: str | None
    scope_label: str | None
    properties: tuple[tuple[str, FactValue], ...]
    role: str = "control"
    truncated_properties: tuple[str, ...] = ()
    label_truncated: bool = False


@dataclass(frozen=True)
class UiFactChange:
    before: UiFact
    after: UiFact
    changes: tuple[tuple[str, FactValue, FactValue], ...]


@dataclass(frozen=True)
class UiSnapshot:
    status: Literal["AVAILABLE", "UNAVAILABLE"]
    reason: str | None = None
    nodes: tuple[UiNode, ...] = ()
    facts: tuple[UiFact, ...] = ()
    # The full bounded index prevents report truncation from looking like absence.
    all_facts: tuple[UiFact, ...] = field(default=(), repr=False)
    observed_anchors: frozenset[str] = field(default=frozenset(), repr=False)
    packages: tuple[str, ...] = ()
    sample_key: str | None = field(default=None, repr=False)
    rotation: str | None = None

    def candidates(self, action: Mapping[str, Any]) -> tuple[UiNode, ...]:
        """Coordinate coverage/focus candidates, not actual event recipients."""
        kind = action.get("action_type")
        if kind == "input_text":
            return tuple(n for n in self.nodes if n.attr("focused") is True and n.editable)
        if kind not in {"click", "double_tap", "long_press"}:
            return ()
        x, y = action.get("x"), action.get("y")
        if type(x) is not int or type(y) is not int:
            return ()
        attribute = "long-clickable" if kind == "long_press" else "clickable"
        return tuple(
            n
            for n in self.nodes
            if n.bounds is not None
            and n.attr(attribute) is True
            and n.bounds[0] <= x < n.bounds[2]
            and n.bounds[1] <= y < n.bounds[3]
        )


@dataclass(frozen=True)
class UiRuleResult:
    """An independent observation or a specific applicability/evidence gap."""

    rule: str
    status: Literal["RECORDED", "NOT_APPLICABLE", "UNAVAILABLE"]
    reason: str | None = None
    values: tuple[tuple[str, RuleValue], ...] = ()


@dataclass(frozen=True)
class UiTransition:
    target_fact: UiFact | None = None
    target_candidate_count: int = 0
    fact_changes: tuple[UiFactChange, ...] = ()
    appeared: int | None = None
    not_observed: int | None = None
    rules: tuple[UiRuleResult, ...] = ()
    action_before_key: ObservationKey | None = None
    action_after_key: ObservationKey | None = None
    changed_fact_count: int = 0
    has_observed_change: bool = False


def _display(value: str) -> str:
    return value if len(value) <= MAX_DISPLAY_CHARS else value[: MAX_DISPLAY_CHARS - 1] + "…"


def _facts(
    nodes: list[UiNode], sources: list[dict[str, str]], parents: list[int | None]
) -> tuple[tuple[UiFact, ...], tuple[UiFact, ...], frozenset[str]]:
    """Build per-field facts, then bound the report, never the absence index.

    Anchors use an ID/class path, not bounds or mutable text/boolean values.
    Repeated indistinguishable paths stay sample-local. No fuzzy matching.
    """
    contexts: list[str] = []
    anchors: list[str | None] = []
    private: list[bool] = []
    labels: list[str | None] = []
    label_truncated: list[bool] = []
    observed: set[str] = set()
    for index, (node, values) in enumerate(zip(nodes, sources, strict=True)):
        parent = parents[index]
        hidden = node.password is not False or (parent is not None and private[parent])
        private.append(hidden)
        label = (
            None
            if hidden
            else values.get("content-desc") or (values.get("text") if not node.editable else None)
        )
        labels.append(_display(label) if label else None)
        label_truncated.append(bool(label and len(label) > MAX_DISPLAY_CHARS))
        identity = [values.get(k, "") for k in ("package", "resource-id", "class")]
        context = _hash([contexts[parent] if parent is not None else "root", identity])
        contexts.append(context)
        if all(identity):
            observed.add(context)
        anchors.append(context if all(identity) and not hidden else None)
    # A hidden duplicate still makes a selector ambiguous; privacy filtering
    # must never manufacture a unique identity for its public sibling.
    counts = Counter(context for context in contexts if context in observed)
    ranked: list[tuple[int, int, UiFact]] = []
    for index, (node, values) in enumerate(zip(nodes, sources, strict=True)):
        if private[index]:
            continue
        properties: list[tuple[str, FactValue]] = []
        truncated: list[str] = []
        checkable = node.attr("checkable") is True
        actionable = any(node.attr(k) is True for k in ("clickable", "long-clickable"))
        if checkable:
            properties.append(("checkable", True))
            if node.attr("checked") is not None:
                properties.append(("checked", bool(node.attr("checked"))))
        if checkable or actionable or node.editable or node.attr("selected") is True:
            for name in ("selected", "enabled", "focused"):
                value = node.attr(name)
                if value is not None:
                    properties.append((name, value))
        for name in ("clickable", "long-clickable", "scrollable"):
            if node.attr(name) is True:
                properties.append((name, True))
        if node.editable:
            properties.append(("editable", True))
            if "text" in values:
                properties.append(("text", _display(values["text"])))
                if len(values["text"]) > MAX_DISPLAY_CHARS:
                    truncated.append("text")
        if not labels[index] and not properties:
            continue
        scope = parents[index]
        scope_label = None
        ancestor = scope
        while ancestor is not None:
            if labels[ancestor] and not nodes[ancestor].editable:
                scope_label = labels[ancestor]
                scope = ancestor
                break
            ancestor = parents[ancestor]
        anchor = anchors[index]
        anchor = anchor if anchor is not None and counts[anchor] == 1 else None
        fact = UiFact(
            node_ref=f"n{index}",
            anchor=anchor,
            basis="UNIQUE_SCOPED_ID" if anchor else "SAMPLE_LOCAL",
            label=labels[index],
            scope_ref=f"n{scope}" if scope is not None else None,
            scope_label=scope_label,
            properties=tuple(properties),
            role=_display(values.get("class", "control").rsplit(".", 1)[-1]),
            truncated_properties=tuple(truncated),
            label_truncated=label_truncated[index],
        )
        priority = (
            0
            if node.editable and node.attr("focused") is True
            else 1
            if checkable
            else 2
            if node.attr("selected") is True
            else 3
            if actionable and node.attr("enabled") is False
            else 4
            if actionable and labels[index]
            else 5
            if node.attr("scrollable") is True
            else 6
        )
        ranked.append((priority, index, fact))
    ranked.sort(key=lambda row: (row[0], row[1]))
    all_facts = tuple(row[2] for row in ranked)
    # Preserve high-value field states, but reserve some space for plain exposed
    # text. Otherwise a button-heavy tree hides headings/status/address labels.
    critical = [row for row in ranked if row[0] <= 3][:MAX_FACTS]
    static_groups: dict[str, list[tuple[int, int, UiFact]]] = {}
    for row in ranked:
        if row[0] == 6 and row[2].label and not row[2].properties:
            group = row[2].scope_label or sources[row[1]].get("package", "")
            static_groups.setdefault(group, []).append(row)
    for group_rows in static_groups.values():
        group_rows.sort(
            key=lambda row: (
                row[2].role != "TextView",
                not bool(sources[row[1]].get("text")),
                row[1],
            )
        )
    static: list[tuple[int, int, UiFact]] = []
    reserve = min(6, MAX_FACTS - len(critical))
    for offset in range(reserve):
        for group_rows in static_groups.values():
            if offset < len(group_rows) and len(static) < reserve:
                static.append(group_rows[offset])
    chosen = {row[1] for row in critical + static}
    remaining = [row for row in ranked if row[1] not in chosen]
    # Put the reserve before ordinary controls so the character budget cannot
    # silently undo it. Critical field states retain highest priority.
    selected = critical + static + remaining[: MAX_FACTS - len(chosen)]
    return tuple(row[2] for row in selected), all_facts, frozenset(observed)


def _fact_deltas(
    before: UiSnapshot, after: UiSnapshot
) -> tuple[tuple[UiFactChange, ...], int | None, int | None]:
    if before.status != "AVAILABLE" or after.status != "AVAILABLE":
        return (), None, None
    old = {f.anchor: f for f in before.all_facts if f.anchor is not None}
    new = {f.anchor: f for f in after.all_facts if f.anchor is not None}
    changes: list[UiFactChange] = []
    for anchor, previous in old.items():
        current = new.get(anchor)
        if current is None:
            continue
        previous_values, current_values = dict(previous.properties), dict(current.properties)
        changed = tuple(
            (name, value, current_values[name])
            for name, value in previous_values.items()
            if name in current_values
            and value != current_values[name]
            and name not in previous.truncated_properties
            and name not in current.truncated_properties
        )
        if (
            previous.label is not None
            and current.label is not None
            and not previous.label_truncated
            and not current.label_truncated
            and previous.label != current.label
        ):
            changed += (("label", previous.label, current.label),)
        if changed:
            changes.append(UiFactChange(previous, current, changed))
    return (
        tuple(changes),
        len(new.keys() - before.observed_anchors),
        len(old.keys() - after.observed_anchors),
    )


def parse_ui_tree(payload: object) -> UiSnapshot:
    """Read an untrusted, bounded XML sample without DTD or entity expansion."""
    if not isinstance(payload, Mapping) or payload.get("status") != "ok":
        return UiSnapshot("UNAVAILABLE", "NOT_AVAILABLE")
    if payload.get("source") != "uiautomator":
        return UiSnapshot("UNAVAILABLE", "UNSUPPORTED_SOURCE")
    xml = payload.get("xml")
    if not isinstance(xml, str):
        return UiSnapshot("UNAVAILABLE", "EMPTY")
    if len(xml) > MAX_XML_BYTES:
        return UiSnapshot("UNAVAILABLE", "OVERSIZED")
    if not xml.strip():
        return UiSnapshot("UNAVAILABLE", "EMPTY")
    try:
        encoded = xml.encode("utf-8")
    except UnicodeError:
        return UiSnapshot("UNAVAILABLE", "INVALID_XML")
    if len(encoded) > MAX_XML_BYTES:
        return UiSnapshot("UNAVAILABLE", "OVERSIZED")
    if "\x00" in xml:
        return UiSnapshot("UNAVAILABLE", "INVALID_XML")
    if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        return UiSnapshot("UNAVAILABLE", "FORBIDDEN_DECLARATION")
    try:
        root = ElementTree.fromstring(encoded)
    except (ElementTree.ParseError, ValueError):
        return UiSnapshot("UNAVAILABLE", "INVALID_XML")
    if root.tag != "hierarchy" or root.attrib.get("rotation") not in {"0", "1", "2", "3"}:
        return UiSnapshot("UNAVAILABLE", "INVALID_HIERARCHY")
    nodes: list[UiNode] = []
    sources: list[dict[str, str]] = []
    parents: list[int | None] = []
    private: list[bool] = []
    pending: list[tuple[ElementTree.Element, int, int | None]] = [
        (child, 1, None) for child in reversed(root)
    ]
    while pending:
        element, depth, parent = pending.pop()
        if depth > MAX_DEPTH or len(nodes) >= MAX_NODES:
            return UiSnapshot("UNAVAILABLE", "STRUCTURE_LIMIT")
        if element.tag != "node" or len(element.attrib) > 32:
            return UiSnapshot("UNAVAILABLE", "INVALID_NODE")
        if any(len(value) > _MAX_ATTRIBUTE_CHARS for value in element.attrib.values()):
            return UiSnapshot("UNAVAILABLE", "ATTRIBUTE_LIMIT")
        values = element.attrib
        bounds = None
        match = _BOUNDS.fullmatch(values.get("bounds", ""))
        if match:
            coords = tuple(int(value) for value in match.groups())
            if 0 <= coords[0] < coords[2] <= 32768 and 0 <= coords[1] < coords[3] <= 32768:
                bounds = (coords[0], coords[1], coords[2], coords[3])
        identity_values = [values.get(key) for key in ("package", "resource-id", "class")]
        booleans = {
            key: {"true": True, "false": False}.get(values.get(key, ""))
            for key in (*_ATTRIBUTES, "password")
        }
        password = booleans["password"]
        hidden = password is not False or (parent is not None and private[parent])
        private.append(hidden)
        text_key = (
            _hash(values["text"])
            if not hidden and "text" in values and len(values["text"]) <= MAX_DISPLAY_CHARS
            else None
        )
        description_key = (
            _hash(values["content-desc"])
            if not hidden
            and "content-desc" in values
            and len(values["content-desc"]) <= MAX_DISPLAY_CHARS
            else None
        )
        public_complete = (
            not hidden
            and bounds is not None
            and all(
                name in values and len(values[name]) <= MAX_DISPLAY_CHARS
                for name in ("text", "content-desc")
            )
        )
        nodes.append(
            UiNode(
                bounds=bounds,
                attributes=tuple(booleans[key] for key in _ATTRIBUTES),
                text_key=text_key,
                description_key=description_key,
                password=password,
                editable=values.get("class", "").endswith(".EditText"),
                widget_class=values.get("class", ""),
                parent=parent,
                sample_key=_hash(
                    [
                        identity_values,
                        bounds,
                        [booleans[key] for key in _ATTRIBUTES],
                        text_key,
                        description_key,
                    ]
                )
                if public_complete
                else None,
                content_key=_hash([text_key, description_key]) if public_complete else None,
            )
        )
        index = len(nodes) - 1
        sources.append(values)
        parents.append(parent)
        pending.extend((child, depth + 1, index) for child in reversed(element))
    if not nodes:
        return UiSnapshot("UNAVAILABLE", "EMPTY_HIERARCHY")
    facts, all_facts, observed_anchors = _facts(nodes, sources, parents)
    packages = tuple(sorted({_display(s["package"]) for s in sources if s.get("package")}))[:4]
    return UiSnapshot(
        "AVAILABLE",
        nodes=tuple(nodes),
        facts=facts,
        all_facts=all_facts,
        observed_anchors=observed_anchors,
        packages=packages,
        sample_key=_hash([root.attrib["rotation"], [(n.parent, n.sample_key) for n in nodes]])
        if all(n.sample_key is not None for n in nodes)
        else None,
        rotation=root.attrib["rotation"],
    )


def _rule(rule: str, **values: RuleValue) -> UiRuleResult:
    return UiRuleResult(rule, "RECORDED", values=tuple(values.items()))


def _gap(rule: str, reason: str, *, applicable: bool = True) -> UiRuleResult:
    return UiRuleResult(rule, "UNAVAILABLE" if applicable else "NOT_APPLICABLE", reason)


def _sample_gap(before: UiSnapshot, after: UiSnapshot) -> str | None:
    if before.status != "AVAILABLE":
        return "BEFORE_SAMPLE_UNAVAILABLE"
    if after.status != "AVAILABLE":
        return "AFTER_SAMPLE_UNAVAILABLE"
    return None


def _node(snapshot: UiSnapshot, fact: UiFact) -> UiNode:
    return snapshot.nodes[int(fact.node_ref[1:])]


def _anchors(snapshot: UiSnapshot) -> dict[str, UiFact]:
    return {fact.anchor: fact for fact in snapshot.all_facts if fact.anchor is not None}


def _fact_for_node(snapshot: UiSnapshot, node: UiNode) -> UiFact | None:
    # Dataclass equality cannot distinguish duplicated nodes.
    index = next(index for index, current in enumerate(snapshot.nodes) if current is node)
    return next((fact for fact in snapshot.all_facts if fact.node_ref == f"n{index}"), None)


def _scroll_candidates(snapshot: UiSnapshot, action: Mapping[str, Any]) -> tuple[UiNode, ...]:
    coords = [action.get(name) for name in ("start_x", "start_y", "end_x", "end_y")]
    if any(type(value) is not int for value in coords):
        return ()
    x, y, end_x, end_y = (cast(int, value) for value in coords)
    if (x, y) == (end_x, end_y):
        return ()
    return tuple(
        node
        for node in snapshot.nodes
        if node.attr("scrollable") is True
        and node.bounds is not None
        and node.bounds[0] <= x < node.bounds[2]
        and node.bounds[1] <= y < node.bounds[3]
        and node.bounds[0] <= end_x < node.bounds[2]
        and node.bounds[1] <= end_y < node.bounds[3]
    )


def _scope(
    snapshot: UiSnapshot, action: Mapping[str, Any]
) -> tuple[str | None, UiFact | None, int, str | None]:
    kind = action.get("action_type")
    scope_kind = "scroll" if kind in {"swipe", "drag"} else "target"
    if kind not in {"swipe", "drag", "click", "double_tap", "long_press", "input_text"}:
        return None, None, 0, "ACTION_HAS_NO_TARGET_RULE"
    if snapshot.status != "AVAILABLE":
        return scope_kind, None, 0, "BEFORE_SAMPLE_UNAVAILABLE"
    candidates = (
        _scroll_candidates(snapshot, action)
        if scope_kind == "scroll"
        else snapshot.candidates(action)
    )
    if len(candidates) != 1:
        return (
            scope_kind,
            None,
            len(candidates),
            (
                "NO_SCROLL_CONTAINER"
                if scope_kind == "scroll" and not candidates
                else "NO_ACTION_CANDIDATE"
                if not candidates
                else "AMBIGUOUS_SCROLL_CONTAINER"
                if scope_kind == "scroll"
                else "AMBIGUOUS_ACTION_CANDIDATE"
            ),
        )
    fact = _fact_for_node(snapshot, candidates[0])
    if fact is None:
        return scope_kind, None, 1, "PRIVATE_OR_UNREPORTABLE_CANDIDATE"
    return scope_kind, fact, 1, None if fact.anchor is not None else "NO_UNIQUE_SCOPE_ANCHOR"


def _subtree(snapshot: UiSnapshot, fact: UiFact) -> tuple[int, ...]:
    return _subtree_indices(snapshot, int(fact.node_ref[1:]))


def _subtree_indices(snapshot: UiSnapshot, root: int) -> tuple[int, ...]:
    included: set[int] = set()
    for index, node in enumerate(snapshot.nodes):
        if index == root or node.parent in included:
            included.add(index)
    return tuple(sorted(included))


def _scope_key(snapshot: UiSnapshot, fact: UiFact, kind: str) -> ObservationKey | None:
    if fact.anchor is None or snapshot.status != "AVAILABLE":
        return None
    root = int(fact.node_ref[1:])
    # A surviving Next/Submit leaf is not enough repeat evidence when its local
    # page content changes. Include its immediate containing subtree, while
    # retaining the selected target anchor separately. Scroll already selects
    # its container. Root-level controls have only their own exposed subtree.
    parent = snapshot.nodes[root].parent
    if kind == "target" and parent is not None:
        root = parent
    indices = _subtree_indices(snapshot, root)
    if any(snapshot.nodes[index].sample_key is None for index in indices):
        return None
    local: dict[int | None, int] = {index: offset for offset, index in enumerate(indices)}
    rows = [
        (local.get(snapshot.nodes[index].parent), snapshot.nodes[index].sample_key)
        for index in indices
    ]
    return kind, _hash([snapshot.rotation, fact.anchor, rows])


def action_observation_key(
    snapshot: UiSnapshot, action: Mapping[str, Any]
) -> ObservationKey | None:
    """A scoped exposed-sample signature, not successful execution or full state."""
    kind, fact, _, reason = _scope(snapshot, action)
    return _scope_key(snapshot, fact, kind) if kind and fact and reason is None else None


def has_discrete_click_candidate(snapshot: UiSnapshot, action: Mapping[str, Any]) -> bool:
    """Whether a unique exposed leaf permits coordinate-independent click checks.

    Callers must separately require an unchanged complete scope and valid
    executed actions. This capability does not change the action or prove that
    the selected candidate received it. Missing interaction fields are not
    assumed false.
    """
    if action.get("action_type") != "click" or {
        name for name, value in action.items() if value is not None
    } != {"action_type", "x", "y"}:
        return False
    _, fact, _, reason = _scope(snapshot, action)
    if fact is None or reason is not None:
        return False
    node = _node(snapshot, fact)
    index = int(fact.node_ref[1:])
    return (
        node.widget_class in _DISCRETE_CLICK_CLASSES
        and node.attr("enabled") is True
        and node.attr("scrollable") is False
        # Even a static child makes this a composite exposed region. Do not
        # infer that different positions in it have the same interaction.
        and not any(child.parent == index for child in snapshot.nodes)
    )


def _compared_fields(before: UiNode, after: UiNode) -> tuple[int, tuple[str, ...]]:
    pairs: list[tuple[str, object, object]] = []
    for name in _ATTRIBUTES:
        if name == "checked" and not (
            before.attr("checkable") is True and after.attr("checkable") is True
        ):
            continue
        old, new = before.attr(name), after.attr(name)
        if old is not None and new is not None:
            pairs.append((name, old, new))
    public_fields: tuple[tuple[str, object, object], ...] = (
        ("text", before.text_key, after.text_key),
        ("content-desc", before.description_key, after.description_key),
        ("bounds", before.bounds, after.bounds),
    )
    for name, previous, current in public_fields:
        if previous is not None and current is not None:
            pairs.append((name, previous, current))
    return len(pairs), tuple(name for name, old, new in pairs if old != new)


def _input_rule(
    before: UiSnapshot,
    after: UiSnapshot,
    action: Mapping[str, Any],
    target: UiFact | None,
    successor: UiFact | None,
) -> UiRuleResult:
    if action.get("action_type") != "input_text":
        return _gap("input_value", "NOT_TEXT_INPUT", applicable=False)
    requested = action.get("text")
    if not isinstance(requested, str) or len(requested) > MAX_DISPLAY_CHARS:
        return _gap("input_value", "REQUEST_TEXT_UNAVAILABLE_OR_TOO_LONG")
    values: dict[str, RuleValue] = {}
    old_text: str | None = None
    if target is not None and "text" not in target.truncated_properties:
        value = dict(target.properties).get("text")
        if isinstance(value, str):
            old_text = value
            values["before_equals_request"] = value == requested
    basis = "MATCHED_PRE_ACTION_FIELD"
    current = successor
    if current is None:
        # This is explicitly a current focused-field observation, not proof that
        # it is the recipient of the input or that input caused its value.
        candidates = after.candidates(action)
        if len(candidates) == 1:
            current = _fact_for_node(after, candidates[0])
            basis = "CURRENT_FOCUSED_FIELD"
    if current is not None and "text" not in current.truncated_properties:
        value = dict(current.properties).get("text")
        if isinstance(value, str):
            values["after_equals_request"] = value == requested
            values["after_basis"] = basis
            if old_text is not None and successor is current:
                values["text_changed"] = old_text != value
    return (
        _rule("input_value", **values) if values else _gap("input_value", "NO_PUBLIC_FIELD_VALUE")
    )


def _scroll_rule(
    before: UiSnapshot,
    after: UiSnapshot,
    target: UiFact,
    successor: UiFact,
    before_key: ObservationKey | None,
    after_key: ObservationKey | None,
) -> UiRuleResult:
    old = [before.nodes[index] for index in _subtree(before, target)[1:]]
    new = [after.nodes[index] for index in _subtree(after, successor)[1:]]
    empty = _hash("")
    old = [n for n in old if n.content_key and (n.text_key != empty or n.description_key != empty)]
    new = [n for n in new if n.content_key and (n.text_key != empty or n.description_key != empty)]
    old_counts, new_counts = (
        Counter(n.content_key for n in old),
        Counter(n.content_key for n in new),
    )
    # Literal public content uniquely scoped inside a container is an observed
    # descriptor, not a claim that recycled view IDs identify business objects.
    old_unique = {n.content_key: n for n in old if old_counts[n.content_key] == 1}
    new_unique = {n.content_key: n for n in new if new_counts[n.content_key] == 1}
    common = old_unique.keys() & new_unique.keys()
    moved = sum(old_unique[key].bounds != new_unique[key].bounds for key in common)
    values: dict[str, RuleValue] = {
        "candidate_count": 1,
        "matched_entries": len(common),
        "moved_entries": moved,
    }
    if before_key is not None and after_key is not None:
        values["content_added"] = sum((new_counts - old_counts).values())
        values["content_removed"] = sum((old_counts - new_counts).values())
        values["sample_equal"] = before_key == after_key
    else:
        values["sample_comparison_gap"] = "INCOMPLETE_PUBLIC_SCOPE"
    return _rule("scroll_region", **values)


def analyze_ui_transition(
    before: UiSnapshot, after: UiSnapshot, action: Mapping[str, Any]
) -> UiTransition:
    """Run independent factual rules; never assign a whole-step state verdict."""
    fact_changes, appeared, not_observed = _fact_deltas(before, after)
    old, new = _anchors(before), _anchors(after)
    common = old.keys() & new.keys()
    gap = _sample_gap(before, after)
    rules: list[UiRuleResult] = [
        _rule("current_sample", nodes=len(after.nodes), facts=len(after.all_facts))
        if after.status == "AVAILABLE"
        else UiRuleResult(
            "current_sample",
            "UNAVAILABLE",
            "AFTER_SAMPLE_UNAVAILABLE",
            (("sample_reason", after.reason or "NOT_AVAILABLE"),),
        )
    ]
    if gap is not None:
        rules.extend((_gap("node_presence", gap), _gap("field_changes", gap)))
    else:
        rules.append(
            _rule(
                "node_presence",
                matched=len(common),
                appeared=appeared or 0,
                not_observed=not_observed or 0,
            )
            if old or new
            else _gap("node_presence", "NO_UNIQUE_ANCHORS")
        )
        counts: Counter[str] = Counter()
        changed_nodes = 0
        compared_fields = 0
        for anchor in sorted(common):
            compared, changed = _compared_fields(
                _node(before, old[anchor]), _node(after, new[anchor])
            )
            compared_fields += compared
            changed_nodes += bool(changed)
            counts.update(changed)
        rules.append(
            _rule(
                "field_changes",
                matched_nodes=len(common),
                compared_fields=compared_fields,
                changed_nodes=changed_nodes,
                changed_fields=sum(counts.values()),
                **dict(counts),
            )
            if common
            else _gap("field_changes", "NO_SHARED_ANCHORS")
        )
    kind, target, candidates, target_gap = _scope(before, action)
    successor = new.get(target.anchor) if target is not None and target.anchor is not None else None
    before_key = (
        _scope_key(before, target, kind) if target and kind and target_gap is None else None
    )
    after_key = _scope_key(after, successor, kind) if successor and kind else None
    if kind == "target":
        if target_gap:
            rules.append(_gap("action_target", target_gap))
        elif after.status != "AVAILABLE":
            rules.append(_gap("action_target", "AFTER_SAMPLE_UNAVAILABLE"))
        elif target is not None and successor is not None:
            compared, changed = _compared_fields(_node(before, target), _node(after, successor))
            rules.append(
                _rule(
                    "action_target",
                    candidate_count=candidates,
                    matched=True,
                    observed_after=True,
                    fields_compared=compared,
                    fields_changed=len(changed),
                    bounds_changed="bounds" in changed,
                )
            )
        elif target is not None and target.anchor not in after.observed_anchors:
            rules.append(
                _rule(
                    "action_target", candidate_count=candidates, matched=False, observed_after=False
                )
            )
        else:
            rules.append(_gap("action_target", "POST_SCOPE_PRIVATE_OR_AMBIGUOUS"))
    else:
        rules.append(_gap("action_target", "ACTION_HAS_NO_TARGET_RULE", applicable=False))
    rules.append(_input_rule(before, after, action, target, successor))
    if kind != "scroll":
        rules.append(_gap("scroll_region", "NOT_A_SCROLL_GESTURE", applicable=False))
    elif target_gap:
        rules.append(_gap("scroll_region", target_gap))
    elif after.status != "AVAILABLE":
        rules.append(_gap("scroll_region", "AFTER_SAMPLE_UNAVAILABLE"))
    elif target is not None and successor is not None:
        rules.append(_scroll_rule(before, after, target, successor, before_key, after_key))
    elif target is not None and target.anchor not in after.observed_anchors:
        rules.append(_rule("scroll_region", candidate_count=1, observed_after=False))
    else:
        rules.append(_gap("scroll_region", "POST_SCOPE_PRIVATE_OR_AMBIGUOUS"))
    # All independent fields count, including changes omitted from the display
    # cap and raw public text hidden by a preferred accessibility label.
    field_result = next(result for result in rules if result.rule == "field_changes")
    changed_fact_count = int(dict(field_result.values).get("changed_nodes", 0))
    scroll_values = dict(rules[-1].values)
    has_change = bool(
        changed_fact_count
        or appeared
        or not_observed
        or scroll_values.get("moved_entries")
        or scroll_values.get("content_added")
        or scroll_values.get("content_removed")
        or (before_key is not None and after_key is not None and before_key != after_key)
        or (
            before.sample_key is not None
            and after.sample_key is not None
            and before.sample_key != after.sample_key
        )
    )
    return UiTransition(
        target_fact=target,
        target_candidate_count=candidates,
        fact_changes=fact_changes[:6],
        appeared=appeared,
        not_observed=not_observed,
        rules=tuple(rules),
        action_before_key=before_key,
        action_after_key=after_key,
        changed_fact_count=changed_fact_count,
        has_observed_change=has_change,
    )
