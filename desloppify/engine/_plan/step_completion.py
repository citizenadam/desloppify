"""Auto-complete action steps from fixed resolve evidence."""

from __future__ import annotations


def _fixed_resolved_ids(plan: dict) -> set[str]:
    """Return IDs with a recorded fixed resolve, including legacy entries."""
    fixed_ids: set[str] = set()
    execution_log = plan.get("execution_log")
    if not isinstance(execution_log, list):
        return fixed_ids

    for entry in execution_log:
        if not isinstance(entry, dict) or entry.get("action") != "resolve":
            continue
        detail = entry.get("detail")
        if isinstance(detail, dict) and detail.get("status") != "fixed":
            continue
        issue_ids = entry.get("issue_ids")
        if isinstance(issue_ids, list):
            fixed_ids.update(
                issue_id for issue_id in issue_ids if isinstance(issue_id, str)
            )
    return fixed_ids


def auto_complete_steps(plan: dict) -> list[str]:
    """Mark steps done when all their issue_refs leave the living plan.

    Returns list of human-readable messages for completed steps.
    """
    messages: list[str] = []
    actionable_ids = set(plan.get("queue_order", []))
    actionable_ids.update((plan.get("skipped") or {}).keys())
    actionable_ids.update(plan.get("promoted_ids") or [])
    for cluster in (plan.get("clusters") or {}).values():
        if isinstance(cluster, dict):
            actionable_ids.update(cluster.get("issue_ids") or [])

    for name, cluster in plan.get("clusters", {}).items():
        cluster_issue_ids = set(cluster.get("issue_ids") or [])
        for i, step in enumerate(cluster.get("action_steps") or []):
            if not isinstance(step, dict) or step.get("done"):
                continue
            refs = step.get("issue_refs", [])
            if not refs:
                continue
            # Match by suffix: ref "abc123" matches "review::path::abc123"
            all_gone = all(
                not any(
                    issue_id.endswith(ref) or issue_id == ref
                    for issue_id in actionable_ids
                )
                for ref in refs
            )
            # Some triage runners emit summary hashes as step refs while the
            # living plan stores canonical issue IDs.  If the cluster still
            # has members, an unmatched ref is ambiguous rather than proof
            # that the step's finding was resolved.  Fail closed and leave
            # the step open until its cluster membership is drained.
            if all_gone and cluster_issue_ids:
                continue
            if all_gone:
                step["done"] = True
                messages.append(
                    f"  Step {i + 1} of '{name}' auto-completed: {step.get('title', '')}"
                )
    return messages


__all__ = ["auto_complete_steps"]
