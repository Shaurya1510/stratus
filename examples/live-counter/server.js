// A visitor counter that updates live in every open browser.
// Node's own http module only: nothing to install.
const http = require("http");

const port = Number(process.env.PORT || 8000);
const started = Date.now();
let visits = 0;

const page = `<!doctype html><meta charset=utf-8><title>Live Counter</title>
<style>body{font-family:system-ui,sans-serif;background:#0b1020;color:#e9ecf5;text-align:center;padding-top:80px}
h1{color:#9b8cff}.big{font-size:96px;font-weight:700;color:#7ee2a8}.dim{color:#7d86a8}</style>
<h1>Live Counter</h1><div class=big id=visits>0</div><p>visits since the server started</p>
<p class=dim>Server up for <span id=uptime>0</span> seconds · hosted on Stratus</p>
<script>
const live=new EventSource('/live');
live.onmessage=(event)=>{const s=JSON.parse(event.data);
document.getElementById('visits').textContent=s.visits;document.getElementById('uptime').textContent=s.uptime;};
</script>`;

const stats = () => JSON.stringify({ visits, uptime: Math.round((Date.now() - started) / 1000) });

http.createServer((request, response) => {
  if (request.url === "/live") {
    // Server-sent events: the browser keeps this one request open and the
    // server pushes the numbers down it every second.
    response.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
    response.write(`data: ${stats()}\n\n`);
    const timer = setInterval(() => response.write(`data: ${stats()}\n\n`), 1000);
    request.on("close", () => clearInterval(timer));
    return;
  }
  visits += 1;
  response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
  response.end(page);
}).listen(port, "0.0.0.0", () => console.log(`Live Counter on port ${port}`));
