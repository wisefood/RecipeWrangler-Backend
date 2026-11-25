"""Model estimate of how long the moved step takes, so a time row can say how many minutes the overlap saves.

The recipe text rarely states the minutes of the step that moves, so code cannot measure them. One call asks for a range; code checks it and clips it
to the wait window (the work cannot save more than the wait it runs inside). Any failure returns None and the row stays start-only."""
import json

MAX_MINUTES = 120

PROMPT = (
    "You estimate how long one recipe step takes an ordinary home cook, hands-on, with the ingredients already bought. "
    "Return exactly one JSON object: {\"minutes_low\": number, \"minutes_high\": number}. "
    "Count only this step, not waiting time that the step itself names (a step that says bake 20 minutes counts 20). "
    "Use whole minutes, minutes_low at most minutes_high, no text outside the JSON."
)


def estimate_minutes(title, steps, work_step, llm):
    """(low, high) minutes of steps[work_step], or None when the model fails or answers out of range."""
    messages = [{"role": "system", "content": PROMPT},
                {"role": "user", "content": json.dumps({"recipe": title, "steps": steps, "step_to_time": work_step + 1}, ensure_ascii=False)}]
    try:
        out = json.loads(llm(messages, "time_estimate", 200))
        low, high = float(out["minutes_low"]), float(out["minutes_high"])
    except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError):
        return None
    return (low, high) if 0 < low <= high <= MAX_MINUTES else None
