const http = require("http");

const port = Number(process.env.PORT || 8000);
const server = http.createServer((_req, res) => {
  res.writeHead(200, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ ok: true }));
});

server.listen(port, "0.0.0.0", () => {
  console.log(`nestjs example listening on ${port}`);
});
