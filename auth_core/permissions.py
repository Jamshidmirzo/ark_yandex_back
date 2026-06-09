"""Permission checks and the ``HasPermission`` DRF factory.

Verbatim behaviour from ark-backend so codenames and the hierarchy
(``administrator`` ⊇ everything, ``X`` ⊇ ``X_own``, ``X_all`` ⊇ ``X``) match.
"""

from rest_framework.permissions import BasePermission

from auth_core.models import UserAccessGroup


def expand_permission_codename(codename: str) -> set[str]:
    """Return the set of codenames that satisfy ``codename`` via the hierarchy."""
    codenames = {codename}
    if codename != "administrator":
        codenames.add("administrator")
    if codename.endswith("_own"):
        base = codename[:-4]
        codenames.add(base)
        codenames.add(f"{base}_all")
    elif not codename.endswith("_all"):
        codenames.add(f"{codename}_all")
    return codenames


def user_has_permission(user, codename: str) -> bool:
    """True if ``user`` holds ARK permission ``codename`` via any access group.

    Superusers and holders of ``administrator`` pass every check.
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return UserAccessGroup.objects.filter(
        user=user,
        group__permissions__codename__in=expand_permission_codename(codename),
    ).exists()


def user_permission_codenames(user) -> set[str]:
    """Flat set of every codename granted to ``user`` (for /me/permissions/)."""
    if not user or not user.is_authenticated:
        return set()
    return set(
        UserAccessGroup.objects.filter(user=user)
        .values_list("group__permissions__codename", flat=True)
        .distinct()
    )


class HasPermission:
    """Factory returning a DRF permission *class* bound to one ARK codename.

    Usage inside ``get_permissions`` (note the trailing call to instantiate)::

        return [IsAuthenticated(), HasPermission("car_order:create")()]
    """

    def __new__(cls, codename: str):
        class _HasPermission(BasePermission):
            _codename = codename
            message = f"Requires permission: {codename}"

            def has_permission(self, request, view):
                return bool(
                    request.user
                    and request.user.is_authenticated
                    and user_has_permission(request.user, self._codename)
                )

        _HasPermission.__name__ = f"HasPermission_{codename}"
        _HasPermission.__qualname__ = _HasPermission.__name__
        return _HasPermission
