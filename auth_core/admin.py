from django.contrib import admin

from auth_core.models import AccessGroup, Permission, UserAccessGroup


@admin.register(Permission)
class PermissionAdmin(admin.ModelAdmin):
    list_display = ("codename", "description")
    search_fields = ("codename", "description")


@admin.register(AccessGroup)
class AccessGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "description")
    search_fields = ("name",)
    filter_horizontal = ("permissions",)


@admin.register(UserAccessGroup)
class UserAccessGroupAdmin(admin.ModelAdmin):
    list_display = ("user", "group", "assigned_by", "assigned_at")
    search_fields = ("user__username", "group__name")
    autocomplete_fields = ("user", "group", "assigned_by")
