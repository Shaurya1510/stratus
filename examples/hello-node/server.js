// Stratus gives every application a port in $PORT and a writable directory
// in $STRATUS_DATA_DIR. Serve HTTP on that port and you are hosted.
const http = require("http");
const port = Number(process.env.PORT || 3000);
http.createServer((request, response) => {
  response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
  response.end("<h1>Stratus Node deployment works.</h1>");
}).listen(port, "0.0.0.0", () => console.log(`listening on ${port}`));
