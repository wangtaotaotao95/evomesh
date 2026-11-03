"""Deterministic context budgeting that preserves user turns and tool-call pairing."""

import copy
import json


class ContextBudgetExceeded(RuntimeError):
    pass


def request_size(messages, tools):
    return len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False))


def fit_context(messages, tools, budget):
    """Character budget, not a tokenizer estimate. Never truncate instructions or the task.

    Drop oldest completed user turns, then shorten tool results in the current turn.
    The caller keeps full history separately; this copy is only the inference input.
    """
    fitted = copy.deepcopy(messages)
    removed, shortened = 0, 0
    while request_size(fitted, tools) > budget:
        users = [i for i, message in enumerate(fitted) if message["role"] == "user"]
        if len(users) <= 1:
            break
        removed += users[1] - users[0]
        del fitted[users[0] : users[1]]
    if request_size(fitted, tools) > budget:
        for message in fitted:
            if message["role"] == "tool" and isinstance(message.get("content"), str):
                content = message["content"]
                if len(content) > 500:
                    message["content"] = (
                        content[:350] + "\n[Tool output shortened by context budget]"
                    )
                    shortened += 1
                if request_size(fitted, tools) <= budget:
                    break
    size = request_size(fitted, tools)
    if size > budget:
        raise ContextBudgetExceeded(
            "Current instructions, task and tool schemas exceed context budget"
        )
    return fitted, {
        "dropped_messages": removed,
        "shortened_results": shortened,
        "input_chars": size,
    }
