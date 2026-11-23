"""Model calls shared by the agents: one call + checked corrections, French style, no long dash.

Used by agent006, agent007, agent010. Moved out of agent006 on 2026-11-23 (modularity: an agent never imports another agent)."""
from __future__ import annotations

import json

from app.agent import RunContext
from app.agent import supervision as sv


FRENCH_STYLE = (" Write natural French typography: never use the long dash (—, –) nor English punctuation habits; "
                "use commas, colons or parentheses instead.")


def no_em_dash(value):
    """NO_EM_DASH (user request 2026-11-21: no English-style long dashes): the program fixes model texts itself."""
    if isinstance(value, str):
        v = value.replace(" — ", ", ").replace(" – ", ", ").replace("—", ", ").replace("–", "-")
        return v.replace(", ,", ",").replace(" ,", ",")
    if isinstance(value, list):
        return [no_em_dash(v) for v in value]
    if isinstance(value, dict):
        return {k: no_em_dash(v) for k, v in value.items()}
    return value


def ask_with_corrections(client, system: str, payload: dict, check, *, purpose: str, ctx: RunContext,
                         max_corrections: int) -> tuple[dict | None, list[str], int]:
    """One call + at most `max_corrections` re-sends naming the defect and the expected correction. Returns
    (answer or None, last problems, number of corrections)."""
    messages = [{"role": "system", "content": system + FRENCH_STYLE},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    problems: list[str] = []
    for attempt in range(max_corrections + 1):
        try:
            ans = client.chat_json(messages, purpose=purpose, run_id=ctx.run_id, task_id=ctx.task_id)
        except sv.LLMError as exc:
            return None, [f"model unavailable: {exc.category}"], attempt
        ans = no_em_dash(ans)
        problems = check(ans)
        if not problems:
            return ans, [], attempt
        messages += [{"role": "assistant", "content": json.dumps(ans, ensure_ascii=False)},
                     {"role": "user", "content": "Defects: " + "; ".join(problems) + ". Correct them: remove anything "
                      "absent from the candidate data and keep only the allowed values. Answer the full JSON again."}]
    return None, problems, max_corrections
