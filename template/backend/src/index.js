const app = require('./app');
const defaultPort = 3000;
const startPort = Number(process.env.PORT || defaultPort);
// ARC-Bench readiness uses PORT (normally 3000), while some official Web tests
// navigate to 3301. Serve the same application on both listeners.
// Local validation overrides the complete list with isolated ephemeral ports.
const ports = new Set((process.env.SPEC_BASE_PORTS || `${startPort},3301`)
  .split(',').map((value) => Number(value.trim())));

for (const port of ports) {
  app.listen(port, () => {
    console.log(`Backend listening at http://127.0.0.1:${port}`);
  });
}
