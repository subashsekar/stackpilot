DEBUG = True
SECRET_KEY = "examples-django-not-for-production"
# Preferred listen port for StackPilot sync / port detection.
PORT = 8003
ROOT_URLCONF = "project.urls"
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
]
MIDDLEWARE = []
ALLOWED_HOSTS = ["*"]
STATIC_URL = "static/"
