# Django example

Minimal Django project discovered by StackPilot.

## Layout

```text
django/
  Stackfile.py
  web/
    manage.py
    requirements.txt
    project/
      settings.py
      urls.py
```

## Setup

```bash
pip install -r web/requirements.txt
```

## Run

```bash
cd examples/django
stackpilot run
```

Health: `http://127.0.0.1:8003/`

## Re-sync

```bash
stackpilot sync --force
```
