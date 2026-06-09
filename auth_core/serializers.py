from django.contrib.auth import authenticate, get_user_model
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from auth_core.permissions import user_permission_codenames

User = get_user_model()


def _display_name(user) -> str:
    """``User.name`` in ark-backend; full name / username as a fallback here."""
    return getattr(user, "name", "") or user.get_full_name() or user.get_username()


class UserBasicSerializer(serializers.ModelSerializer):
    """Compact user reference embedded in other payloads (created_by, driver…)."""

    name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "name"]

    def get_name(self, obj) -> str:
        return _display_name(obj)


class MeSerializer(serializers.ModelSerializer):
    """Current-user payload, including the flat list of granted codenames."""

    name = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "name", "username", "is_superuser", "permissions"]

    def get_name(self, obj) -> str:
        return _display_name(obj)

    def get_permissions(self, obj) -> list[str]:
        if obj.is_superuser:
            return ["administrator"]
        return sorted(user_permission_codenames(obj))


class LoginSerializer(serializers.Serializer):
    """Validate username + password, return the authenticated user."""

    username = serializers.CharField(help_text=_("Username."))
    password = serializers.CharField(
        write_only=True,
        style={"input_type": "password"},
    )

    def validate(self, attrs):
        user = authenticate(
            request=self.context.get("request"),
            username=attrs.get("username"),
            password=attrs.get("password"),
        )
        if user is None:
            raise serializers.ValidationError(
                _("Invalid credentials. Please check your username and password."),
                code="invalid_credentials",
            )
        if not user.is_active:
            raise serializers.ValidationError(
                _("This account has been deactivated."),
                code="account_disabled",
            )
        attrs["user"] = user
        return attrs
