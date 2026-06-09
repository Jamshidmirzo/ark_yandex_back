from django.http import JsonResponse


def health(request):
    """Simple liveness probe."""
    return JsonResponse({"status": "ok"})
