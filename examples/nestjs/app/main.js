const http = require("http");

const port = Number(process.env.PORT || 8005);
const server = http.createServer((req, res) => {
  const path = (req.url || "/").split("?", 1)[0];
  if (path === "/health" || path === "/") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ ok: true }));
    return;
  }
  res.writeHead(404, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: "not found" }));
});

server.listen(port, "0.0.0.0", () => {
  console.log(`nestjs example listening on ${port}`);
});
