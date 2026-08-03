from stackpilot import Stack, HttpHealthCheck

stack = Stack()

stack.service(
    name="api",
    path="./api",
    command="python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000",
    port=8000,
    reload=True,
    health_check=HttpHealthCheck(url="http://127.0.0.1:8000/health"),
)

stack.run()
