"""Example Stackfile with application services and external infrastructure."""

from stackpilot import Stack, HttpHealthCheck

stack = Stack()

stack.external_dependency(
    name="postgres",
    type="postgresql",
    host="127.0.0.1",
    port=5432,
)

stack.external_dependency(
    name="redis",
    type="redis",
    host="127.0.0.1",
    port=6379,
)

stack.service(
    name="auth",
    path="./auth",
    command="python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000",
    port=8000,
    health_check=HttpHealthCheck(url="http://127.0.0.1:8000/health"),
    depends_on=["postgres", "redis"],
)

stack.service(
    name="gateway",
    path="./gateway",
    command="python -m uvicorn main:app --reload --host 0.0.0.0 --port 8001",
    port=8001,
    health_check=HttpHealthCheck(url="http://127.0.0.1:8001/health"),
    depends_on=["auth"],
)

stack.run()
