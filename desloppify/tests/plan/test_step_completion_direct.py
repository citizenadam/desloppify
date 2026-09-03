"""Direct coverage tests for plan step auto-completion helpers."""

from __future__ import annotations

from desloppify.engine._plan.step_completion import auto_complete_steps
from desloppify.engine.plan_ops import purge_ids, skip_items


def test_auto_complete_steps_marks_done_when_all_refs_have_fixed_resolves() -> None:
    plan = {
        "execution_log": [
            {
                "action": "resolve",
                "issue_ids": ["review::done::gone1", "review::done::gone2"],
                "detail": {"status": "fixed"},
            }
        ],
        "clusters": {
            "epic/cleanup": {
                "action_steps": [
                    {"title": "Fix abc", "issue_refs": ["abc123"]},
                    {"title": "Fix stale refs", "issue_refs": ["gone1", "gone2"]},
                ]
            }
        },
    }

    messages = auto_complete_steps(plan)

    steps = plan["clusters"]["epic/cleanup"]["action_steps"]
    assert steps[0].get("done") is not True
    assert steps[1]["done"] is True
    assert messages == ["  Step 2 of 'epic/cleanup' auto-completed: Fix stale refs"]


def test_auto_complete_steps_matches_exact_fixed_issue_ids() -> None:
    plan = {
        "execution_log": [
            {
                "action": "resolve",
                "issue_ids": ["review::gone"],
                "detail": {"status": "fixed"},
            }
        ],
        "clusters": {
            "epic/exact": {
                "action_steps": [
                    {"title": "Exact open", "issue_refs": ["review::still-open"]},
                    {"title": "Exact gone", "issue_refs": ["review::gone"]},
                ]
            }
        },
    }

    messages = auto_complete_steps(plan)

    steps = plan["clusters"]["epic/exact"]["action_steps"]
    assert steps[0].get("done") is not True
    assert steps[1]["done"] is True
    assert "Step 2" in messages[0]


def test_auto_complete_steps_accepts_legacy_resolve_entries() -> None:
    plan = {
        "execution_log": [
            {
                "action": "resolve",
                "issue_ids": ["review::legacy::fixed"],
            }
        ],
        "clusters": {
            "legacy": {
                "action_steps": [
                    {"title": "Finish legacy work", "issue_refs": ["legacy::fixed"]}
                ]
            }
        },
    }

    messages = auto_complete_steps(plan)

    assert plan["clusters"]["legacy"]["action_steps"][0]["done"] is True
    assert messages == ["  Step 1 of 'legacy' auto-completed: Finish legacy work"]


def test_auto_complete_steps_ignores_done_steps_and_invalid_step_shapes() -> None:
    plan = {
        "queue_order": [],
        "clusters": {
            "epic/mixed": {
                "action_steps": [
                    {"title": "Already done", "issue_refs": ["gone"], "done": True},
                    {"title": "No refs"},
                    "not-a-dict",
                ]
            }
        },
    }

    messages = auto_complete_steps(plan)

    assert messages == []
    assert plan["clusters"]["epic/mixed"]["action_steps"][0]["done"] is True


def test_auto_complete_steps_keeps_clustered_issue_outside_execution_queue_open() -> (
    None
):
    plan = {
        "queue_order": ["review::selected::issue-a"],
        "clusters": {
            "epic/selected": {
                "issue_ids": ["review::selected::issue-a"],
                "action_steps": [
                    {"title": "Fix selected", "issue_refs": ["issue-a"]},
                ],
            },
            "epic/not-selected": {
                "issue_ids": ["review::not-selected::issue-b"],
                "action_steps": [
                    {"title": "Fix later", "issue_refs": ["summary-hash-b"]},
                ],
            },
        },
    }

    messages = auto_complete_steps(plan)

    assert messages == []
    assert (
        plan["clusters"]["epic/not-selected"]["action_steps"][0].get("done") is not True
    )
