"""Repeatable behavioral checks against a fresh, operator-supplied app per case.

No LLM judge or automatic live-provider calls. A factory explicitly selects the model,
workspace and tools. Evaluation executes those tools; use a disposable environment.
"""

from __future__ import annotations

import hashlib
import json
import math


def validate_suite(cases):
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise ValueError("Suite must contain 1–100 cases")
    names = set()
    allowed = {
        "name",
        "prompt",
        "contains",
        "not_contains",
        "required_tools",
        "forbidden_tools",
        "max_model_calls",
        "max_tool_calls",
        "max_duration_seconds",
        "max_tool_errors",
    }
    for case in cases:
        if not isinstance(case, dict) or set(case) - allowed:
            raise ValueError("Unknown evaluation fields")
        for key in ("name", "prompt"):
            if not isinstance(case.get(key), str) or not 1 <= len(case[key]) <= 12000:
                raise ValueError("Cases require name and prompt")
        if case["name"] in names:
            raise ValueError("Case names must be unique")
        names.add(case["name"])
        for key in ("contains", "not_contains", "required_tools", "forbidden_tools"):
            if key in case and (
                not isinstance(case[key], list)
                or not case[key]
                or len(case[key]) > 100
                or any(not isinstance(v, str) or not v or len(v) > 12000 for v in case[key])
            ):
                raise ValueError("Expected nonempty string assertion arrays")
        for key in ("max_model_calls", "max_tool_calls", "max_tool_errors"):
            if key in case and (type(case[key]) is not int or case[key] < 0):
                raise ValueError("Call/error budgets must be nonnegative integers")
        if "max_duration_seconds" in case and (
            type(case["max_duration_seconds"]) not in (int, float)
            or not math.isfinite(case["max_duration_seconds"])
            or case["max_duration_seconds"] <= 0
        ):
            raise ValueError("Duration budget must be finite and positive")
        if not (set(case) - {"name", "prompt"}):
            raise ValueError("Every case needs explicit behavioral assertions or budgets")


def evaluate(factory, cases):
    validate_suite(cases)
    rows = []
    for case in cases:
        events, answer, error, metrics = [], "", None, {}
        try:
            with factory() as app:
                try:
                    answer = app.run(case["prompt"], session="evaluation", on_event=events.append)
                finally:
                    metrics = app.last_run_metrics or {}
        except Exception as exc:
            error = type(exc).__name__
        called = {e["tool"] for e in events if e["kind"] == "tool_started"}
        checks = {"completed": error is None and metrics.get("status") == "succeeded"}
        for key in ("contains", "not_contains", "required_tools", "forbidden_tools"):
            if key not in case:
                continue
            checks[key] = all(
                item in answer
                if key == "contains"
                else item not in answer
                if key == "not_contains"
                else item in called
                if key == "required_tools"
                else item not in called
                for item in case[key]
            )
        for key in ("model_calls", "tool_calls", "duration_seconds", "tool_errors"):
            if "max_" + key in case:
                checks["max_" + key] = key in metrics and metrics[key] <= case["max_" + key]
        rows.append(
            {
                "name": case["name"],
                "passed": all(checks.values()),
                "checks": checks,
                "error": error,
                "metrics": metrics,
            }
        )
    return {
        "suite_sha256": hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
        "passed": all(row["passed"] for row in rows),
        "cases": rows,
    }


def compare_reports(baseline, candidate):
    """Prevent comparing unrelated suites and fail a previously passing case's regression."""
    if baseline.get("suite_sha256") != candidate.get("suite_sha256"):
        raise ValueError("Baseline and candidate must use the identical evaluation suite")
    old = {row["name"]: row for row in baseline["cases"]}
    new = {row["name"]: row for row in candidate["cases"]}
    if old.keys() != new.keys():
        raise ValueError("Baseline and candidate case names differ")
    regressions = [name for name in old if old[name]["passed"] and not new[name]["passed"]]
    return {
        "no_regressions": not regressions,
        "regressions": regressions,
        "improved": [name for name in old if not old[name]["passed"] and new[name]["passed"]],
    }
