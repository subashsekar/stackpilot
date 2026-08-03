from stackpilot import Stack

stack = Stack()

stack.service(
    name="worker",
    path="./worker",
    command="celery -A celery worker",
)

stack.run()
