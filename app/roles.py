"""Roles and how Microsoft sign-in claims map onto them. No web framework imports."""
from enum import Enum


class Role(str, Enum):
    PENDING = "pending"
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return _RANKS[self]

    # str's own ordering would compare the text; compare by rank instead.
    def __ge__(self, other):
        if not isinstance(other, Role):
            return NotImplemented
        return self.rank >= other.rank

    def __gt__(self, other):
        if not isinstance(other, Role):
            return NotImplemented
        return self.rank > other.rank

    def __le__(self, other):
        if not isinstance(other, Role):
            return NotImplemented
        return self.rank <= other.rank

    def __lt__(self, other):
        if not isinstance(other, Role):
            return NotImplemented
        return self.rank < other.rank

    __hash__ = str.__hash__


_RANKS = {Role.PENDING: 0, Role.VIEWER: 1, Role.OPERATOR: 2, Role.ADMIN: 3}

ROLE_LABELS = {
    Role.PENDING: "Waiting for approval",
    Role.VIEWER: "Viewer",
    Role.OPERATOR: "Operator",
    Role.ADMIN: "Admin",
}

APP_ROLE_CLAIMS = {
    "DockerManager.Admin": Role.ADMIN,
    "DockerManager.Operator": Role.OPERATOR,
    "DockerManager.Viewer": Role.VIEWER,
}


def mapped_role(claims: dict, group_mappings: list[dict]) -> Role | None:
    """Highest role granted by the token's app roles and the configured group mappings."""
    found: list[Role] = []
    roles = claims.get("roles")
    if isinstance(roles, list):
        found += [APP_ROLE_CLAIMS[r] for r in roles if isinstance(r, str) and r in APP_ROLE_CLAIMS]
    groups = claims.get("groups")
    if isinstance(groups, list):
        for m in group_mappings:
            if m.get("group_id") in groups:
                try:
                    found.append(Role(m.get("role")))
                except ValueError:
                    continue
    return max(found, key=lambda r: r.rank) if found else None


def default_role_for_new(setting: str) -> tuple[Role, bool]:
    """Role for a newly signed-in Microsoft user, and whether it is pinned.
    `admin_legacy` is the upgrade default for installs that had no roles: everyone stays Admin."""
    if setting == "admin_legacy":
        return Role.ADMIN, True
    return Role(setting), False
