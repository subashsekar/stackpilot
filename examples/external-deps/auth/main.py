from fastapi import FastAPI

# Preferred listen port for StackPilot port detection.
PORT = 8006

app = FastAPI()


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def root():
    return {"service": "auth"}
