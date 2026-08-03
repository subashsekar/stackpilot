const http = require("http");

const port = Number(process.env.PORT || 8000);
const server = http.createServer((req, res) => {
  const url = req.url || "/";
  if (url === "/health" || url === "/health/") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ ok: true, service: "app_nestjs" }));
    return;
  }
  if (url === "/ping") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ pong: true }));
    return;
  }
  res.writeHead(200, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ service: "app_nestjs" }));
});

server.listen(port, "0.0.0.0", () => {
  console.log(`nestjs adaptive-health demo on ${port}`);
});
