from stackpilot import Stack, HttpHealthCheck, TcpHealthCheck

stack = Stack()

stack.service(
    name="api_none",
    path="./api_none",
    command="python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000",
    port=8000,
    health_check=TcpHealthCheck(host="127.0.0.1", port=8000),
)

stack.service(
    name="api_ready",
    path="./api_ready",
    command="python -m uvicorn main:app --reload --host 0.0.0.0 --port 8001",
    port=8001,
    health_check=HttpHealthCheck(url="http://127.0.0.1:8001/ready"),
)

stack.service(
    name="api_root",
    path="./api_root",
    command="python -m uvicorn main:app --reload --host 0.0.0.0 --port 8002",
    port=8002,
    health_check=HttpHealthCheck(url="http://127.0.0.1:8002/"),
)

stack.service(
    name="api_v1_health",
    path="./api_v1_health",
    command="python -m uvicorn main:app --reload --host 0.0.0.0 --port 8003",
    port=8003,
    health_check=HttpHealthCheck(url="http://127.0.0.1:8003/api/v1/health"),
)

stack.service(
    name="app_express",
    path="./app_express",
    command="npm run dev",
    port=8004,
    health_check=HttpHealthCheck(url="http://127.0.0.1:8004/internal/health"),
)

stack.service(
    name="app_nestjs",
    path="./app_nestjs",
    command="npm run start:dev",
    port=8005,
    health_check=HttpHealthCheck(url="http://127.0.0.1:8005/health"),
)

stack.service(
    name="web_django",
    path="./web_django",
    command="python manage.py runserver 0.0.0.0:8006",
    port=8006,
    health_check=HttpHealthCheck(url="http://127.0.0.1:8006/health"),
)

stack.service(
    name="web_flask",
    path="./web_flask",
    command="python app.py",
    port=8007,
    health_check=HttpHealthCheck(url="http://127.0.0.1:8007/api/health"),
)

stack.run()
