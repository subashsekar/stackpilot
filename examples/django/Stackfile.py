from stackpilot import Stack, HttpHealthCheck

stack = Stack()

stack.service(
    name="web",
    path="./web",
    command="python manage.py runserver 0.0.0.0:8003",
    port=8003,
    reload=True,
    health_check=HttpHealthCheck(url="http://127.0.0.1:8003/"),
)

stack.run()
