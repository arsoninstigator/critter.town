"""authority is an intersection with a trace, not a boolean.

an agent working inside its own crew holds what its own grants say, narrowed by every
ceiling above it. an agent seconded into another crew holds the host's ceiling and the
secondment's scope as well, and its spend starts at zero unless the secondment says
otherwise, because spending somebody else's budget has to be a line somebody wrote.

the trace is the part that matters when a run fails with "denied": it names the level that
dropped the tool, so the answer is "the host ceiling" and not "somewhere above you".
"""

from __future__ import annotations

DEFAULT_SPEND = 0


def normalize(grants: dict | None) -> dict:
    grants = grants or {}
    return {
        "tools": sorted({str(tool) for tool in (grants.get("tools") or [])}),
        "spend": float(grants.get("spend") or 0),
    }


def intersect(left: dict, right: dict) -> dict:
    """what both allow. spend is the smaller of the two, because a budget you cannot
    afford is not a budget."""
    left, right = normalize(left), normalize(right)
    return {
        "tools": sorted(set(left["tools"]) & set(right["tools"])),
        "spend": min(left["spend"], right["spend"]),
    }


def _trace(own: dict, levels: list[dict], kept: set[str]) -> list[dict]:
    out = []
    for tool in own["tools"]:
        if tool in kept:
            out.append({"tool": tool, "kept": True})
            continue
        blocker = next((level for level in levels if tool not in normalize(level["ceiling"])["tools"]),
                       None)
        out.append({"tool": tool, "kept": False,
                    "dropped_by": blocker["id"] if blocker else ""})
    return out


def effective_home(chain: list[dict], own: dict) -> dict:
    """root ceiling, then every ceiling down to this agent's own, then its own grants."""
    if not chain:
        raise ValueError("authority_requires_a_chain")
    grants = normalize(own)
    for level in chain:
        grants = intersect(grants, level["ceiling"])
    return {"grants": grants, "mode": "home",
            "trace": _trace(normalize(own), chain, set(grants["tools"]))}


def effective_in_host(host_chain: list[dict], own: dict, secondment: dict) -> dict:
    """the host's ceilings and the secondment's scope, on top of what the agent holds.

    there is no home ceiling term here: the home ceiling already bounds the agent's own
    grants, so applying it again would only make the trace lie about who dropped a tool.
    """
    if not host_chain:
        raise ValueError("authority_requires_a_chain")
    scope = normalize(secondment.get("scope"))
    grants = intersect(normalize(own), scope)
    for level in host_chain:
        grants = intersect(grants, level["ceiling"])
    grants = {"tools": grants["tools"], "spend": min(grants["spend"], scope["spend"])}
    levels = list(host_chain) + [{"id": "secondment", "ceiling": scope}]
    return {"grants": grants, "mode": "host",
            "trace": _trace(normalize(own), levels, set(grants["tools"]))}


def apply_elevation(base: dict, elevation: dict, context: dict) -> dict:
    """widening that somebody signed, and every refusal has a reason worth logging."""
    base = normalize(base)
    if context["now"] >= elevation.get("expires_at", ""):
        return {"grants": base, "applied": False, "reason": "expired"}
    if int(elevation.get("used", 0)) >= int(elevation.get("max_uses", 0)):
        return {"grants": base, "applied": False, "reason": "exhausted"}
    if elevation.get("issuer") not in context.get("target_chain", []):
        return {"grants": base, "applied": False, "reason": "issuer_not_ancestor"}
    issuer_tools = set(normalize(context.get("issuer_ceiling"))["tools"])
    beyond = [tool for tool in elevation.get("adds") or [] if tool not in issuer_tools]
    if beyond:
        return {"grants": base, "applied": False, "reason": f"issuer_lacks:{','.join(sorted(beyond))}"}
    window = elevation.get("window")
    if window:
        if context.get("day_of_week") not in (window.get("days") or []):
            return {"grants": base, "applied": False, "reason": "outside_window"}
        start, end = window.get("hours") or (0, 24)
        if not (start <= int(context.get("hour_utc", 0)) < end):
            return {"grants": base, "applied": False, "reason": "outside_window"}
    return {"grants": {"tools": sorted(set(base["tools"]) | set(elevation.get("adds") or [])),
                       "spend": base["spend"]}, "applied": True, "reason": ""}


def can_afford(grants: dict, amount: float) -> bool:
    """a spend limit that nobody set is not a limit, so an unset ceiling refuses a cost."""
    if amount <= 0:
        return True
    return float(normalize(grants)["spend"]) >= amount
