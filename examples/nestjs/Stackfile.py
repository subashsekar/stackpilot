from stackpilot import Stack, HttpHealthCheck

stack = Stack()

stack.service(
    name="app",
    path="./app",
    command="npm run start:dev",
    port=8005,
    reload=True,
    health_check=HttpHealthCheck(url="http://127.0.0.1:8005/health"),
)

stack.run()
