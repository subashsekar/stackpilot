from fastapi import FastAPI

# Preferred listen port for StackPilot sync / port detection.
PORT = 8001

app = FastAPI()


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def root():
    return {"service": "api"}
