from stackpilot import Stack, ProcessHealthCheck

stack = Stack()

stack.service(
    name="app",
    path="./app",
    command="python main.py",
    health_check=ProcessHealthCheck(timeout=5.0, interval=0.2),
)

stack.run()
