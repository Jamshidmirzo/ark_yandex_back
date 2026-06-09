"""Local JWT auth (simplejwt). Login/refresh/me/permissions served by THIS
backend. To delegate auth to a remote backend instead, see the BFF note in
INTEGRATION.md and settings.UPSTREAM_API_BASE.
"""

from django.conf import settings
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from auth_core.permissions import user_permission_codenames
from auth_core.serializers import LoginSerializer, MeSerializer


def _token_error(message):
    return Response(
        {"error": {"code": "INVALID_TOKEN", "message": str(message), "details": {}}},
        status=status.HTTP_401_UNAUTHORIZED,
    )


class LoginView(APIView):
    """POST /api/v1/auth/login/  {username, password} -> {access, refresh, user}."""

    permission_classes = [AllowAny]
    serializer_class = LoginSerializer

    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": MeSerializer(user).data,
            },
            status=status.HTTP_200_OK,
        )


class RefreshView(APIView):
    """POST /api/v1/auth/refresh/  {refresh} -> {access[, refresh]}."""

    permission_classes = [AllowAny]

    def post(self, request):
        token_str = request.data.get("refresh")
        if not token_str:
            return Response(
                {
                    "error": {
                        "code": "INVALID_TOKEN",
                        "message": "refresh is required",
                        "details": {},
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            refresh = RefreshToken(token_str)
        except TokenError as exc:
            return _token_error(exc)

        data = {"access": str(refresh.access_token)}
        if settings.SIMPLE_JWT.get("ROTATE_REFRESH_TOKENS"):
            refresh.set_jti()
            refresh.set_exp()
            refresh.set_iat()
            data["refresh"] = str(refresh)
        return Response(data, status=status.HTTP_200_OK)


class MeView(APIView):
    """GET /api/v1/auth/me/ -> current user + permissions."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(MeSerializer(request.user).data)


class MyPermissionsView(APIView):
    """GET /api/v1/auth/me/permissions/ -> {"permissions": [...]}."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        if request.user.is_superuser:
            return Response({"permissions": ["administrator"]})
        return Response({"permissions": sorted(user_permission_codenames(request.user))})
