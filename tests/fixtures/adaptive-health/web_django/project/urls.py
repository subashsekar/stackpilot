from django.http import HttpResponse
from django.urls import path


def home(_request):
    return HttpResponse("ok")


def health(_request):
    return HttpResponse("healthy")


def ready(_request):
    return HttpResponse("ready")


urlpatterns = [
    path("health/", health),
    path("ready/", ready),
    path("", home),
]
