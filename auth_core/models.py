"""ARK access-control primitives.

This is a lean mirror of ``apps.auth_core`` in ark-backend: a codename-based
permission catalog (``Permission``), named bundles (``AccessGroup``) and a
membership table (``UserAccessGroup``). It is deliberately decoupled from
Django's built-in ``auth.Permission`` so the same codenames work across the
main CRM and this standalone block. See INTEGRATION.md for how this maps onto
ark-backend on merge.
"""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Permission(models.Model):
    """A module-level access flag identified by its codename.

    Examples: ``car_order:create``, ``driver:accept_order``, ``garage:list``.
    """

    codename = models.CharField(
        max_length=150,
        unique=True,
        verbose_name=_("Codename"),
        help_text=_("Permission codename, e.g. 'car_order:create'."),
    )
    description = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("Description"),
    )

    class Meta:
        ordering = ["codename"]
        verbose_name = _("Permission")
        verbose_name_plural = _("Permissions")

    def __str__(self):
        return self.codename


class AccessGroup(models.Model):
    """A named bundle of permissions assigned to users (e.g. 'Driver')."""

    name = models.CharField(max_length=255, unique=True, verbose_name=_("Name"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    permissions = models.ManyToManyField(
        Permission,
        blank=True,
        related_name="groups",
        verbose_name=_("Permissions"),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = _("Access group")
        verbose_name_plural = _("Access groups")

    def __str__(self):
        return self.name


class UserAccessGroup(models.Model):
    """Membership row: which user belongs to which access group."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="access_group_memberships",
        verbose_name=_("User"),
    )
    group = models.ForeignKey(
        AccessGroup,
        on_delete=models.CASCADE,
        related_name="memberships",
        verbose_name=_("Group"),
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_access_groups",
        verbose_name=_("Assigned by"),
    )
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("user", "group")]
        verbose_name = _("User access group")
        verbose_name_plural = _("User access groups")

    def __str__(self):
        return f"{self.user_id} -> {self.group.name}"
