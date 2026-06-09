from django.urls import path

from auth_core.views import LoginView, MeView, MyPermissionsView, RefreshView

app_name = "auth_core"

urlpatterns = [
    path("login/", LoginView.as_view(), name="login"),
    path("refresh/", RefreshView.as_view(), name="refresh"),
    path("me/", MeView.as_view(), name="me"),
    path("me/permissions/", MyPermissionsView.as_view(), name="me-permissions"),
]
