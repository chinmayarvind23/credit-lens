/* Each worker runs one isolated Python interpreter. All dependencies are hosted with the Space. */
importScripts("./runtime/pyodide.js");
let engine;
let demo;

/** Preserve startup failures as explicit UI errors rather than leaving a loading screen. */
async function initialize() {
  postMessage({ type: "progress", message: "Loading the Python evidence engine… First load may take a minute." });
  engine = await loadPyodide({ indexURL: new URL("./runtime/", self.location.href).href });
  await engine.loadPackage(["pydantic", "sqlalchemy"]);
  const response = await fetch("./python-bundle.json");
  if (!response.ok) throw new Error("Evidence engine files could not be loaded.");
  const bundle = await response.json();
  engine.FS.mkdirTree("/app/creditlens");
  for (const [name, source] of Object.entries(bundle.files)) {
    if (!/^(?:creditlens\/[a-z_]+\.py|bridge\.py|fixture\.json)$/.test(name)) throw new Error("Invalid engine package.");
    engine.FS.writeFile(`/app/${name}`, source);
  }
  await engine.runPythonAsync("import sys\nsys.path.insert(0, '/app')\nfrom bridge import initialize\nbrowser_demo = initialize()");
  demo = engine.globals.get("browser_demo");
  postMessage({ type: "ready" });
}

/** JSON crosses the language boundary as a value; no input is interpolated into executable code. */
function dispatch(event) {
  const { id, operation, payload } = event.data;
  let argument;
  try {
    if (!demo) throw new Error("The evidence engine is not ready.");
    argument = engine.toPy(payload);
    const result = demo.dispatch_json(operation, argument);
    postMessage({ type: "result", id, value: JSON.parse(result) });
  } catch (error) {
    postMessage({ type: "error", id, message: "The evidence request could not be accepted. Check the borrower, question and policy date." });
    console.error("CreditLens worker request failed", error.name);
  } finally {
    argument?.destroy();
  }
}

self.onmessage = dispatch;
initialize().catch(() => postMessage({ type: "fatal", message: "The evidence engine could not start. Refresh borrowers to retry loading the demo." }));
