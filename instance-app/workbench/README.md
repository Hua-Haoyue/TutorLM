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

## Markdown Reading

The reader and explanation cards support pipe tables (including column alignment),
nested ordered/unordered lists, read-only task checkboxes, links, strikethrough,
fenced code blocks, block quotes, heading levels 1-6 and the existing KaTeX math.
Tables scroll horizontally within the reader on narrow screens. Source ranges
are retained per text block and table cell for node creation and source lookup.
Raw HTML stays inert and remote images show alternative text without loading.
The bundled markdown-it 15.0.3 parser and its license are under `assets/markdown-it/`.

Run the focused regression with `node --test scripts/test-markdown.cjs` from this
directory. `scripts/fixtures/markdown-support.md` is a manual upload sample;
`scripts/browser-markdown-smoke.cjs` checks upload, rendering, table-cell source
resolution, reload, search and desktop/mobile layout against a disposable backend.

## Graph Relations And Layout

The graph toolbar's AI relation action proposes additions, direction/type changes
and removals for the current document. Previewing or rejecting suggestions leaves
positions unchanged. Accepting selected suggestions applies them first, then
animates a root-to-leaf, top-to-bottom layout and saves each node's position.
An empty suggestion result also organizes the current graph. The existing manual
layout button uses the same algorithm; cards can still be dragged afterwards.

Layout measures rendered cards, reserves expansion width, and uses the bundled
Dagre 3.1.1 engine for layered placement and crossing reduction. Learning ancestry
takes priority. Dependency-like edges point from prerequisite to dependent for
ranking only; stored edge directions and labels are not rewritten by layout.
Symmetric and cycle-closing relations remain visible without forcing invalid
hierarchy. Arbitrary cyclic/dense graphs cannot guarantee zero crossings.
Edges track card geometry during the transition; reduced-motion preferences skip
the animation. Saving positions is a bounded set of node updates, not an atomic
transaction with relation acceptance; a save failure is reported and layout can
be retried. Neither existing document content nor other documents are rearranged.

Run `node --test scripts/test-graph-layout.cjs` here for geometry regressions.
For the browser regression use a disposable local fake backend; from the repo
root, set `PYTHONPATH` to `instance-app`, `TUTORLM_PROVIDER=fake`,
`TUTORLM_RUN_MODE=demo`, `TUTORLM_INVITE_CODE=TLM-DEV-LOCAL`, and distinct
`TUTORLM_DATA_DIR` / `TUTORLM_SESSION_DATA_DIR` under `var/`. Start it with
`python -m uvicorn graph_layout_test_app:build_app --factory --app-dir instance-app/workbench/scripts --host 127.0.0.1 --port 8093`.
Run the workbench dev server on 8094 with `--api http://127.0.0.1:8093`, then
`node instance-app/workbench/scripts/browser-graph-layout.cjs` with
`PLAYWRIGHT_MODULE` set to a local Playwright installation if needed. The fixture
provider emits deterministic relation suggestions; no paid model is involved.

## Current Integration Boundaries

Business data uses the actual `/api` endpoints and requires an application Bearer.
PDF rendering uses authenticated original-file reads and the shared Reader modules.
Drawing and view preferences are browser-local rather than server project content.
Managed entry reads the control-plane access policy, authenticates a persistent
user, selects or creates a durable workspace and requests a lease. In open mode
the system invitation is optional; in invitation-only mode it is required even
when importing a share. Managed app sessions come from `/control/app-session`,
not a second application experience code. Standalone demo sessions are unchanged.
Application project IDs must not be substituted for control-plane workspace UUIDs.

The share button issues a unique copy string through the control plane, supports
copy and revocation, and does not generate QR codes or public project IDs. Import
creates an independent receiver-owned project before requesting compute for it.
Sharing may stop source compute for consistent copying. It never grants priority
or bypasses invitation-only allocation. See `../../contracts/instance-runtime-v4.md`.
User recovery credentials are returned at initial creation and are available
in that tab's account panel; save them privately for login recovery on the entry
page. Closing the workbench releases compute but preserves durable project data
until the separate idle retention deadline.

Test access flow contracts with:

```powershell
node --test instance-app/workbench/scripts/test-access.cjs
```

For the browser smoke test, set `PLAYWRIGHT_MODULE` to a Playwright installation,
`APP_URL` to the local workbench and `APP_INVITE` to the demo invite, then run:

```powershell
node instance-app/workbench/scripts/browser-smoke.cjs
```

The test creates temporary project content. Use a disposable local backend.

For an isolated managed Docker stack with offline `fake` models, the opt-in
`scripts/managed-smoke.py --run` checks real project copying, receiver edit
isolation, revocation and invitation priority through HTTP. It requires a
loopback gateway, a separate loopback admin origin and an explicit admin token.
`scripts/browser-access-smoke.cjs` tests the managed entry and share UI using
`APP_URL` and optionally `APP_INVITE` for invitation-only mode. These tests
create persistent test projects and can preempt test compute; never use a
production stack. Remove test volumes with the disposable stack afterwards.
Screenshots and test output under `.work/` are ignored. The optional cover image
is `assets/cover.jpg`; the page displays a placeholder when it is absent.
