from django.http import HttpResponse
from django.urls import path


def home(_request):
    return HttpResponse("ok")


urlpatterns = [
    path("", home),
]
