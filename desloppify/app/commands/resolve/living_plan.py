"""Living-plan update helpers used by resolve command."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import NamedTuple

from desloppify.app.commands.helpers.transition_messages import emit_transition_message
from desloppify.app.commands.resolve.plan_load import warn_plan_load_degraded_once
from desloppify.base.config import target_strict_score_from_config
from desloppify.base.exception_sets import PLAN_LOAD_EXCEPTIONS
from desloppify.base.output.terminal import colorize
from desloppify.engine._plan.cluster_semantics import EXECUTION_STATUS_DONE
from desloppify.engine._plan.refresh_lifecycle import (
    current_lifecycle_phase,
    invalidate_postflight_scan,
)
from desloppify.engine._plan.sync import live_planned_queue_empty, reconcile_plan
from desloppify.engine._state.progression import (
    maybe_append_entered_planning,
    maybe_append_execution_drain,
)
from desloppify.engine.plan_ops import (
    append_log_entry,
    auto_complete_steps,
    purge_ids,
)
from desloppify.engine.plan_state import (
    add_uncommitted_issues,
    has_living_plan,
    load_plan,
    plan_path_for_state,
    purge_uncommitted_ids,
    save_plan,
)

_logger = logging.getLogger(__name__)
_RESOLVED_STATUSES = frozenset({"fixed", "auto_resolved", "wontfix", "false_positive"})


def _step_ref_sets(
    state: dict | None, resolved_ids: list[str]
) -> tuple[set[str] | None, set[str] | None]:
    """Return resolved/open IDs plus review summary hashes used by action steps.

    Without state there is no hash index; callers fall back to suffix matching.
    """
    if not isinstance(state, dict):
        return None, None
    resolved_refs = set(resolved_ids)
    open_refs: set[str] = set()
    for issue_id, issue in (state.get("work_items") or {}).items():
        if not isinstance(issue, dict):
            continue
        refs = {issue_id}
        detail = issue.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("summary_hash"), str):
            refs.add(detail["summary_hash"])
        if issue_id in resolved_refs:
            resolved_refs.update(refs)
        if issue.get("status") == "open" and issue_id not in resolved_ids:
            open_refs.update(refs)
    return resolved_refs, open_refs


def _cluster_history_members(plan: dict) -> dict[str, list[str]]:
    """Return each cluster's historical members from the execution log."""
    members: dict[str, list[str]] = {}
    removed: dict[str, set[str]] = {}
    for entry in plan.get("execution_log", []) or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("cluster_name")
        action = entry.get("action")
        issue_ids = [
            issue_id
            for issue_id in entry.get("issue_ids", []) or []
            if isinstance(issue_id, str)
        ]
        if not isinstance(name, str) or not issue_ids:
            continue
        if action in {"cluster_create", "cluster_add", "cluster_update"}:
            bucket = members.setdefault(name, [])
            for issue_id in issue_ids:
                if issue_id not in bucket:
                    bucket.append(issue_id)
            removed.get(name, set()).difference_update(issue_ids)
        elif action == "cluster_remove" and entry.get("actor") == "user":
            removed.setdefault(name, set()).update(issue_ids)
    for name, gone in removed.items():
        members[name] = [
            issue_id for issue_id in members.get(name, []) if issue_id not in gone
        ]
    return members


def _restore_resolved_cluster_members(
    plan: dict, resolved_ids: list[str], cluster_names: list[str] | None = None
) -> list[str]:
    """Re-add resolved members so normalization keeps the packet's history."""
    history = _cluster_history_members(plan)
    targeted = (
        list(cluster_names)
        if cluster_names is not None
        else [
            name
            for name, members in history.items()
            if any(issue_id in members for issue_id in resolved_ids)
        ]
    )
    restored: dict[str, list[str]] = {}
    for name in targeted:
        cluster = (plan.get("clusters") or {}).get(name)
        if not isinstance(cluster, dict):
            continue
        existing = list(cluster.get("issue_ids") or [])
        candidates = [
            issue_id
            for issue_id in [*(history.get(name, [])), *resolved_ids]
            if issue_id not in existing
        ]
        missing: list[str] = []
        for issue_id in candidates:
            if issue_id in missing:
                continue
            missing.append(issue_id)
        if not missing:
            continue
        cluster["issue_ids"] = existing + missing
        restored[name] = missing
    for name, issue_ids in restored.items():
        append_log_entry(
            plan,
            "cluster_add",
            issue_ids=issue_ids,
            cluster_name=name,
            actor="system",
        )
    return list(restored)


def _move_resolved_cluster_members_to_end(
    plan: dict, resolved_ids: list[str]
) -> None:
    """Keep resolved packets' members as history, after the remaining work."""
    resolved = set(resolved_ids)
    for cluster in (plan.get("clusters") or {}).values():
        if not isinstance(cluster, dict):
            continue
        ids = cluster.get("issue_ids") or []
        if not any(issue_id in resolved for issue_id in ids):
            continue
        cluster["issue_ids"] = [
            issue_id for issue_id in ids if issue_id not in resolved
        ] + [issue_id for issue_id in ids if issue_id in resolved]


class ClusterContext(NamedTuple):
    cluster_name: str | None
    cluster_completed: bool
    cluster_remaining: int


def _affected_cluster_names(plan: dict, resolved_ids: list[str]) -> list[str]:
    """Return unique cluster names referenced by the resolved ids."""
    overrides = plan.get("overrides") or {}
    clusters = plan.get("clusters") or {}
    history = _cluster_history_members(plan)
    seen: set[str] = set()
    cluster_names: list[str] = []
    for resolved_id in resolved_ids:
        override = overrides.get(resolved_id)
        cluster_name = override.get("cluster") if isinstance(override, dict) else None
        candidates = [cluster_name] if cluster_name else [
            name
            for name, cluster in clusters.items()
            if isinstance(cluster, dict)
            and cluster.get("execution_status") != EXECUTION_STATUS_DONE
            and resolved_id in (cluster.get("issue_ids") or [])
        ]
        if not candidates:
            # A purged member only survives in the packet's logged history.
            candidates = [
                name
                for name, members in history.items()
                if resolved_id in members
                and not (clusters.get(name) or {}).get("execution_status")
                == EXECUTION_STATUS_DONE
            ]
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            cluster_names.append(candidate)
    return cluster_names


def _remaining_cluster_ids(
    cluster: dict, resolved_ids: list[str], state: dict | None
) -> set[str]:
    current_ids = set(cluster.get("issue_ids") or [])
    if state is None:
        return current_ids - set(resolved_ids)
    issues = state.get("work_items") or state.get("issues", {})
    return {
        issue_id
        for issue_id in current_ids
        if not isinstance(issues.get(issue_id), dict)
        or issues[issue_id].get("status") not in _RESOLVED_STATUSES
    }


def _completed_cluster_names(
    plan: dict, resolved_ids: list[str], *, state: dict | None = None
) -> list[str]:
    """Return affected clusters with no unresolved members in current state."""
    clusters = plan.get("clusters") or {}
    completed: list[str] = []
    for cluster_name in _affected_cluster_names(plan, resolved_ids):
        cluster = clusters.get(cluster_name)
        if not isinstance(cluster, dict) or cluster.get("execution_status") == EXECUTION_STATUS_DONE:
            continue
        if _remaining_cluster_ids(cluster, resolved_ids, state):
            continue
        completed.append(cluster_name)
    return completed


def capture_cluster_context(
    plan: dict, resolved_ids: list[str], *, state: dict | None = None
) -> ClusterContext:
    """Determine cluster membership for resolved issues before purge."""
    clusters = plan.get("clusters") or {}
    cluster_name = next(iter(_affected_cluster_names(plan, resolved_ids)), None)
    if not cluster_name or cluster_name not in clusters:
        return ClusterContext(
            cluster_name=None, cluster_completed=False, cluster_remaining=0
        )
    remaining = _remaining_cluster_ids(clusters[cluster_name], resolved_ids, state)
    return ClusterContext(
        cluster_name=cluster_name,
        cluster_completed=len(remaining) == 0,
        cluster_remaining=len(remaining),
    )


def _cluster_has_open_member(
    plan: dict, state: dict | None, cluster_name: str
) -> bool:
    """Return whether a cluster still contains an open state work item."""
    if state is None:
        return False
    cluster = (plan.get("clusters") or {}).get(cluster_name)
    if not isinstance(cluster, dict):
        return False
    issues = state.get("work_items") or state.get("issues", {})
    if not isinstance(issues, dict):
        return False
    return any(
        issues.get(issue_id, {}).get("status") == "open"
        if isinstance(issues.get(issue_id), dict)
        else True
        for issue_id in cluster.get("issue_ids") or []
    )


def update_living_plan_after_resolve(
    *,
    args: argparse.Namespace,
    all_resolved: list[str],
    attestation: str | None,
    state: dict | None = None,
    state_file: Path | str | None = None,
) -> tuple[dict | None, ClusterContext]:
    """Apply resolve side effects to the living plan when it exists."""
    plan_path = plan_path_for_state(Path(state_file)) if state_file else None
    plan = None
    ctx = ClusterContext(
        cluster_name=None, cluster_completed=False, cluster_remaining=0
    )
    try:
        if not has_living_plan(plan_path):
            return None, ctx
        plan = load_plan(plan_path)
        if state is not None:
            _restore_resolved_cluster_members(plan, all_resolved)
        ctx = capture_cluster_context(plan, all_resolved, state=state)
        completed_clusters = _completed_cluster_names(plan, all_resolved, state=state)
        phase_before = current_lifecycle_phase(plan)
        purged = purge_ids(plan, all_resolved)
        if state is not None:
            # Resolved members stay as the packet's history; the appended
            # cluster_add entry keeps normalization from pruning them again.
            _restore_resolved_cluster_members(plan, all_resolved)
            _move_resolved_cluster_members_to_end(plan, all_resolved)
        append_log_entry(
            plan,
            "resolve",
            issue_ids=all_resolved,
            actor="user",
            note=getattr(args, "note", None),
            detail={"status": args.status, "attestation": attestation},
        )
        if args.status == "fixed":
            resolved_refs, open_refs = _step_ref_sets(state, all_resolved)
            step_messages = auto_complete_steps(
                plan, resolved_refs=resolved_refs, open_refs=open_refs
            )
            for msg in step_messages:
                print(colorize(msg, "green"))
        if completed_clusters:
            for cluster_name in completed_clusters:
                append_log_entry(
                    plan,
                    "cluster_done",
                    issue_ids=all_resolved,
                    cluster_name=cluster_name,
                    actor="user",
                )
                # Mark cluster as done so cluster_is_active() returns False
                plan["clusters"][cluster_name]["execution_status"] = (
                    EXECUTION_STATUS_DONE
                )
            # Clear focus when the active cluster is done
            if plan.get("active_cluster") in set(completed_clusters):
                plan["active_cluster"] = None
        elif (
            ctx.cluster_name
            and ctx.cluster_remaining > 0
            and _cluster_has_open_member(plan, state, ctx.cluster_name)
        ):
            # Auto-focus only while a state-open member remains in the cluster.
            plan["active_cluster"] = ctx.cluster_name
        if args.status == "fixed":
            add_uncommitted_issues(plan, all_resolved)
        elif args.status == "open":
            purge_uncommitted_ids(plan, all_resolved)
        transition_phase: str | None = None
        invalidated = invalidate_postflight_scan(
            plan, issue_ids=all_resolved, state=state
        )
        queue_drained = state is not None and live_planned_queue_empty(plan)
        if state is not None and (invalidated or queue_drained):
            target_strict = target_strict_score_from_config(state.get("config"))
            result = reconcile_plan(plan, state, target_strict=target_strict)
            if result.lifecycle_phase_changed:
                transition_phase = result.lifecycle_phase
        save_plan(plan, plan_path)

        # --- Progression: execution_drain (only when queue actually drained)
        #     + entered_planning_mode ---
        if queue_drained:
            try:
                maybe_append_execution_drain(
                    state or {},
                    plan,
                    trigger_action="resolve",
                    issue_ids=all_resolved,
                    cluster_name=ctx.cluster_name,
                    phase_before=phase_before,
                    source_command="resolve",
                )
                maybe_append_entered_planning(
                    state,
                    plan,
                    source_command="resolve",
                    trigger_action="resolve",
                    issue_ids=all_resolved,
                    phase_before=phase_before,
                )
            except Exception:
                _logger.warning(
                    "Failed to append progression event after resolve", exc_info=True
                )

        if purged:
            print(
                colorize(f"  Plan updated: {purged} item(s) removed from queue.", "dim")
            )
        if transition_phase:
            emit_transition_message(transition_phase)
    except PLAN_LOAD_EXCEPTIONS as exc:
        _logger.debug("plan update failed after resolve", exc_info=True)
        warn_plan_load_degraded_once(
            command_label="resolve",
            error_kind=exc.__class__.__name__,
            behavior="Living-plan queue metadata could not be updated after resolve.",
        )
    return plan, ctx


__all__ = [
    "ClusterContext",
    "capture_cluster_context",
    "update_living_plan_after_resolve",
]
