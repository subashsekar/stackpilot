from stackpilot import Stack, HttpHealthCheck

stack = Stack()

stack.service(
    name="app",
    path="./app",
    command="npm run dev",
    port=8000,
    reload=True,
    health_check=HttpHealthCheck(url="http://127.0.0.1:8000/"),
)

stack.run()
