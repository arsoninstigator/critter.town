"""what a model name is, and what it is not.

an imported agent arrives saying which model it wants, and there is no list to check it
against: the platform talks to an openai-compatible endpoint, so a model name is a string
the vendor either serves or refuses at the first turn. inventing an allowlist would be a
worse lie than the current one, because it would refuse names that work and bless names
that do not.

so this module checks the one thing that is checkable: that the string is a model name at
all. `gpt-4o` is a name, even if this deployment answers with something else.
`gpt-4o, best for writing` is a sentence, and adopting it puts a sentence in a column that
is sent to a vendor. provider prefixes are stripped, because `openai/gpt-4o` and
`anthropic/claude-sonnet-4` are how other frameworks spell the same two names.
"""

from __future__ import annotations

import re

from core.errors import ValidationError

# a model name: letters, digits and the separators vendors use. no spaces, no commas, no
# paths, no urls, and nothing long enough to be a paragraph.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-/@]{0,79}$")

# prefixes other frameworks put in front of the same name
PREFIXES = ("openai/", "anthropic/", "google/", "azure/", "bedrock/", "litellm/",
            "openrouter/", "groq/", "mistral/", "cohere/", "ollama/", "vertex_ai/")

# what a name may not look like even if the pattern above accepts it
NOT_NAMES = ("http://", "https://", "~/", "/", "\\")


def normalize(raw: str) -> str:
    """the model name inside whatever the source wrote, or a refusal naming why not.

    never returns a guess. a source that says `gpt-4o (recommended)` gets refused rather
    than silently becoming `gpt-4o`, because the parenthetical is the source telling us
    something we would then be hiding.
    """
    name = (raw or "").strip()
    if not name:
        return ""
    if len(name) > 80:
        raise ValidationError(
            f"'{name[:40]}...' is too long to be a model name",
            why="model_not_a_name", length=len(name))
    lowered = name.lower()
    for prefix in PREFIXES:
        if lowered.startswith(prefix):
            name = name[len(prefix):]
            lowered = name.lower()
            break
    if any(bad in name for bad in NOT_NAMES) or not NAME_RE.match(name):
        raise ValidationError(
            f"'{name[:60]}' is not a model name: a name has no spaces, commas or paths",
            why="model_not_a_name", got=name[:60])
    return name


def advisory(name: str, deployment: str = "") -> str:
    """what to tell a person about a model the source asked for.

    the sentence exists because the column is not a promise. the deployment decides which
    model answers, and an imported name is a request that gets honoured only when the
    operator's endpoint serves it.
    """
    if not name:
        return ""
    if not deployment:
        return f"the source asks for model '{name}'; this deployment decides what answers"
    if name == deployment:
        return f"the source asks for model '{name}', which is what this deployment serves"
    return (f"the source asks for model '{name}', and this deployment answers with "
            f"'{deployment}': the name is kept as a request, the deployment decides")
