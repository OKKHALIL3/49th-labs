// Tiny TCP forwarder: 127.0.0.1:<listenPort> -> <host>:<targetPort>
const net = require("net");
const [listenPort, host, targetPort] = process.argv.slice(2);
net
  .createServer((c) => {
    const u = net.connect(+targetPort, host);
    c.pipe(u); u.pipe(c);
    const end = () => { c.destroy(); u.destroy(); };
    c.on("error", end); u.on("error", end); c.on("close", end); u.on("close", end);
  })
  .on("error", (e) => { console.error("fwd", listenPort, host, e.message); process.exit(1); })
  .listen(+listenPort, "127.0.0.1", () => console.log("fwd", listenPort, "->", host + ":" + targetPort));
