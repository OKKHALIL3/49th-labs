// Tiny QM-publishable app: a stable /d/rev-viewer/ link that frames the live REV 3D viewer.
// Publish from QM:  apps({ action: "publish", dir: "rev-viewer-app", entrypoint: "node server.js",
//                          name: "rev-viewer", env: { REV_PUBLIC_URL: "https://<random>.trycloudflare.com" } })
// No dependencies. Listens on $PORT (QM's app runtime sets it).
const http = require("http");

const target = (process.env.REV_PUBLIC_URL || "").replace(/\/+$/, "");
const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const page = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>REV Enclosure Viewer</title>
<style>
  :root { --bg:#0f1115; --fg:#e8eaed; --muted:#9aa0a6; --line:#2a2e36; }
  html,body { margin:0; height:100%; background:var(--bg); color:var(--fg); font:14px/1.4 system-ui,sans-serif; }
  header { display:flex; gap:12px; align-items:center; padding:10px 16px; border-bottom:1px solid var(--line); }
  header b { font-weight:600; } header span { color:var(--muted); } a { color:#8ab4f8; margin-left:auto; }
  iframe { border:0; width:100%; height:calc(100% - 45px); display:block; }
  .empty { padding:24px 16px; color:var(--muted); }
</style></head><body>
<header><b>REV</b><span>Acme Devices / Sensor Hub enclosure</span>${target ? `<a href="${esc(target)}/" target="_blank" rel="noopener">Open full screen</a>` : ""}</header>
${target ? `<iframe src="${esc(target)}/" title="REV 3D enclosure viewer"></iframe>` : `<div class="empty">REV_PUBLIC_URL is not set. Republish with env { REV_PUBLIC_URL: "https://...trycloudflare.com" }.</div>`}
</body></html>`;

http
  .createServer((req, res) => {
    if (req.url === "/healthz") { res.writeHead(200, { "content-type": "text/plain" }); return res.end("ok"); }
    res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
    res.end(page);
  })
  .listen(Number(process.env.PORT) || 8080, () => console.log(`rev-viewer on :${process.env.PORT || 8080} -> ${target || "(unset)"}`));
