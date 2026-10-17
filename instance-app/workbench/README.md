# Learning Workbench

This directory is the tracked source for the browser workbench. Make future
workbench changes here. `../frontend` owns reusable readers, PDF.js resources,
invitation entry and lease lifecycle modules; it is not the workbench page.

## Layout

- `index.html`, `busy.html`: entry and capacity views.
- `workspace.html`: document reader, knowledge graph, explanations and narration.
- `how.html`, `terms.html`, `design.html`: supporting pages.
- `assets/`: page scripts, styles, QR library, PDF view and drawing tools.
- `scripts/dev-server.py`: same-origin static server and local API proxy.
- `scripts/browser-smoke.cjs`: browser integration test using a disposable backend.

## Local Development

Start the backend from the repository root:

```powershell
.\scripts\dev-instance-app.ps1 -Port 8091
```

Install the locked PDF.js dependency in `instance-app/frontend` with `npm ci`,
then start the workbench from the repository root:

```powershell
.\.venv\Scripts\python.exe instance-app/workbench/scripts/dev-server.py --port 8092 --api http://127.0.0.1:8091
```

Open `http://127.0.0.1:8092/`. The local demo invite is `TLM-DEV-LOCAL`.
The server also mounts `/static/reader/` and `/static/pdfjs/` from `../frontend`.
Pass `--control <origin>` to proxy a separately running control plane.
This standard-library server is for local development, not production hosting.

Production gateways must serve these pages and assets, plus the reader modules
at `/static/reader/` and built PDF.js resources at `/static/pdfjs/`, on one origin.
Tracking these files does not build or deploy them automatically.

## Current Integration Boundaries

Business data uses the actual `/api` endpoints and requires an application Bearer.
PDF rendering uses authenticated original-file reads and the shared Reader modules.
Drawing and view preferences are browser-local rather than server project content.
System invitation admission, durable project selection and sharing/import are not
fully connected to the control-plane workflow yet. Application project IDs must
not be substituted for control-plane workspace UUIDs.

For the browser smoke test, set `PLAYWRIGHT_MODULE` to a Playwright installation,
`APP_URL` to the local workbench and `APP_INVITE` to the demo invite, then run:

```powershell
node instance-app/workbench/scripts/browser-smoke.cjs
```

The test creates temporary project content. Use a disposable local backend.
Screenshots and test output under `.work/` are ignored. The optional cover image
is `assets/cover.jpg`; the page displays a placeholder when it is absent.
