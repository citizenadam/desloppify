"""Direct regressions for consistent subjective queue thresholds."""

from __future__ import annotations

from types import SimpleNamespace

import desloppify.app.commands.plan.queue_render as plan_queue_mod
import desloppify.app.commands.resolve.queue_guard as queue_guard_mod


def test_plan_queue_context_uses_runtime_target_strict(monkeypatch) -> None:
    captured = {}

    def capture_build(_state, *, options):
        captured["context"] = options.context
        return {"items": []}

    monkeypatch.setattr(plan_queue_mod, "build_execution_queue", capture_build)
    plan_queue_mod._build_queue_items(
        state={},
        plan={"queue_order": []},
        include_skipped=False,
        effective_cluster=None,
        config={"target_strict_score": 85},
    )

    assert captured["context"].target_strict == 85


def test_resolve_guard_context_uses_project_target_strict(monkeypatch) -> None:
    plan = {"queue_order": ["review::first"], "clusters": {}, "active_cluster": None}
    access = SimpleNamespace(
        plan=plan,
        degraded=False,
        usable_plan=lambda **_kwargs: plan,
    )
    captured = {}

    monkeypatch.setattr(queue_guard_mod, "load_config", lambda: {"target_strict_score": 85})
    monkeypatch.setattr(queue_guard_mod, "load_resolve_plan_access", lambda: access)

    def capture_build(_state, *, options):
        captured["context"] = options.context
        return {"items": []}

    monkeypatch.setattr(queue_guard_mod, "build_work_queue", capture_build)
    assert queue_guard_mod._check_queue_order_guard({}, ["review::first"], "fixed") is False
    assert captured["context"].target_strict == 85
