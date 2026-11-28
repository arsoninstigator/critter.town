"""relay: the room the org talks in.

the relay itself is vendored whole in `vendor/relay_workspace` and run as its published image. this
package is our half: which rooms the org has, and who holds which role in each. a channel scoped to a
crew, a department, a bloc or a wing is a projection of the org. its roster is read off the org every
time and is never typed by hand, because a room whose members drifted from the department it names is
how a boundary gets crossed without anyone deciding to cross it.
"""
from ingredients.relay.rooms import (
    RELAY_ROLE, ROLES, SCOPES, Rooms, desired_rooms, overview, plan,
)

__all__ = ["RELAY_ROLE", "ROLES", "SCOPES", "Rooms", "desired_rooms", "overview", "plan"]
