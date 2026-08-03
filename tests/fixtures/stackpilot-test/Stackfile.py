"""Deterministic multi-service fixture for dependency-system QA.

Registration order matches the expected topological sequence used by tests.
"""

from stackpilot import Stack

stack = Stack()

stack.service(name="postgres", path="./postgres", command="python main.py")
stack.service(name="redis", path="./redis", command="python main.py")
stack.service(
    name="users",
    path="./users",
    command="python main.py",
    depends_on=["postgres"],
)
stack.service(name="email", path="./email", command="python main.py")
stack.service(
    name="auth",
    path="./auth",
    command="python main.py",
    depends_on=["redis", "users", "email"],
)
stack.service(
    name="payments",
    path="./payments",
    command="python main.py",
    depends_on=["postgres", "redis"],
)
stack.service(
    name="analytics",
    path="./analytics",
    command="python main.py",
    depends_on=["postgres"],
)
stack.service(
    name="notifications",
    path="./notifications",
    command="python main.py",
    depends_on=["redis", "email"],
)
stack.service(
    name="gateway",
    path="./gateway",
    command="python main.py",
    depends_on=["auth", "payments", "analytics", "notifications"],
)

stack.run()
