from celery import Celery

app = Celery("worker")
app.conf.task_always_eager = True


@app.task
def ping() -> str:
    return "pong"
