"""Browser UI for Maestro."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

from maestro import __version__


def ui_router() -> APIRouter:
    """Return routes serving the browser UI shell and assets."""

    router = APIRouter(tags=["ui"])

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def root_ui() -> HTMLResponse:
        return HTMLResponse(
            content=_html_shell(),
            headers=_html_headers(),
        )

    @router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
    async def ui() -> HTMLResponse:
        return HTMLResponse(
            content=_html_shell(),
            headers=_html_headers(),
        )

    @router.get("/ui/styles.css", include_in_schema=False)
    async def styles() -> Response:
        return Response(
            content=STYLES,
            media_type="text/css; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/ui/app.js", include_in_schema=False)
    async def script() -> Response:
        return Response(
            content=APP_JS,
            media_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    return router


def _html_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'self'; "
            "connect-src 'self'; "
            "style-src 'self'; "
            "script-src 'self'; "
            "img-src 'self' data:; "
            "base-uri 'none'; "
            "form-action 'self'"
        ),
    }


def _html_shell() -> str:
    return HTML_SHELL.replace("__VERSION__", __version__)


HTML_SHELL = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Maestro</title>
  <link rel="stylesheet" href="/ui/styles.css">
  <script src="/ui/app.js" defer></script>
</head>
<body>
  <a class="skip-link" href="#main">Skip to main content</a>
  <div class="app-shell">
    <header class="topbar">
      <div class="brand">
        <button id="clear-data" type="button" class="clear-data-button">
          Clear Data
        </button>
        <span class="brand-mark" aria-hidden="true">M</span>
        <div>
          <h1>Maestro</h1>
          <p>v__VERSION__</p>
        </div>
      </div>
      <div class="topbar-actions">
        <span id="connection-state" class="status-pill">Offline</span>
        <button id="refresh-button" type="button" class="icon-button" title="Refresh">
          <span aria-hidden="true">R</span>
          <span class="sr-only">Refresh</span>
        </button>
      </div>
    </header>

    <div id="ui-error" class="error-banner" role="alert" hidden></div>

    <div class="workspace">
      <aside class="sidebar" aria-label="Projects">
        <div class="sidebar-header">
          <h2>Projects</h2>
          <span id="project-count" class="muted">0</span>
        </div>
        <form id="project-form" class="project-form">
          <label>
            <span>Name</span>
            <input id="project-name" name="name" autocomplete="off" required>
          </label>
          <label>
            <span>Description</span>
            <input id="project-description" name="description" autocomplete="off">
          </label>
          <label>
            <span>Repository Path</span>
            <input id="project-repository-path" name="repositoryPath"
              autocomplete="off">
          </label>
          <label>
            <span>Default Branch</span>
            <input id="project-default-branch" name="defaultBranch"
              autocomplete="off" value="main">
          </label>
          <button type="submit" class="primary-button">Create Project</button>
        </form>
        <div id="project-list" class="resource-list" aria-live="polite"></div>
      </aside>

      <main id="main" class="main" tabindex="-1">
        <section class="surface project-surface" aria-labelledby="project-title">
          <div class="section-header">
            <div>
              <p class="eyebrow">Project</p>
              <h2 id="project-title">No project selected</h2>
            </div>
            <span id="project-phase" class="phase-badge">Pending</span>
          </div>
          <dl id="project-metadata" class="meta-grid"></dl>
        </section>

        <section class="surface" aria-labelledby="new-execution-title">
          <div class="section-header">
            <div>
              <p class="eyebrow">Create</p>
              <h2 id="new-execution-title">New Execution</h2>
            </div>
          </div>
          <form id="execution-form" class="execution-form">
            <label>
              <span>Name</span>
              <input id="execution-name" name="name" autocomplete="off" required>
            </label>
            <label>
              <span>Goal</span>
              <input id="goal-summary" name="summary" autocomplete="off" required>
            </label>
            <label class="span-2">
              <span>Description</span>
              <textarea id="goal-description" name="description" rows="3"></textarea>
            </label>
            <label class="span-2">
              <span>Acceptance Criteria</span>
              <textarea id="goal-criteria" name="criteria" rows="3"></textarea>
            </label>
            <div class="form-actions span-2">
              <button type="submit" class="primary-button">Create Execution</button>
            </div>
          </form>
        </section>

        <section class="surface" aria-labelledby="execution-title">
          <div class="section-header">
            <div>
              <p class="eyebrow">Execution</p>
              <h2 id="execution-title">No execution selected</h2>
            </div>
            <div class="button-row">
              <button id="run-execution" type="button" class="primary-button">
                Start
              </button>
              <button id="cancel-execution" type="button" class="danger-button">
                Cancel
              </button>
            </div>
          </div>
          <div id="execution-list" class="resource-list compact"></div>
          <div id="execution-overview" class="overview-grid"></div>
        </section>

        <section id="user-input-panel" class="surface"
          aria-labelledby="user-input-title" hidden>
          <div class="section-header">
            <div>
              <p class="eyebrow">Input</p>
              <h2 id="user-input-title">Planner Questions</h2>
            </div>
          </div>
          <form id="user-input-form" class="question-form">
            <div id="user-input-list" class="resource-list"></div>
            <div class="form-actions">
              <button type="submit" class="primary-button">Submit Answers</button>
            </div>
          </form>
        </section>

        <section class="surface" aria-labelledby="plan-title">
          <div class="section-header">
            <div>
              <p class="eyebrow">Plan</p>
              <h2 id="plan-title">Plan</h2>
            </div>
          </div>
          <div id="plan-view"></div>
        </section>

        <section class="surface" aria-labelledby="activity-title">
          <div class="section-header">
            <div>
              <p class="eyebrow">Activity</p>
              <h2 id="activity-title">Timeline</h2>
            </div>
          </div>
          <ol id="event-timeline" class="timeline"></ol>
        </section>

        <section class="content-grid">
          <div class="surface" aria-labelledby="work-items-title">
            <div class="section-header">
              <div>
                <p class="eyebrow">Delivery</p>
                <h2 id="work-items-title">Work Items</h2>
              </div>
            </div>
            <div id="work-item-list" class="resource-list"></div>
          </div>

          <div class="surface" aria-labelledby="invocations-title">
            <div class="section-header">
              <div>
                <p class="eyebrow">Agents</p>
                <h2 id="invocations-title">Invocations</h2>
              </div>
            </div>
            <div id="invocation-list" class="resource-list"></div>
          </div>
        </section>

        <section class="content-grid">
          <div class="surface" aria-labelledby="artifacts-title">
            <div class="section-header">
              <div>
                <p class="eyebrow">Evidence</p>
                <h2 id="artifacts-title">Artifacts</h2>
              </div>
            </div>
            <div id="artifact-list" class="resource-list"></div>
          </div>

          <div class="surface artifact-preview"
            aria-labelledby="artifact-preview-title">
            <div class="section-header">
              <div>
                <p class="eyebrow">Content</p>
                <h2 id="artifact-preview-title">Artifact Preview</h2>
              </div>
            </div>
            <pre id="artifact-content" tabindex="0">No artifact selected.</pre>
          </div>
        </section>

        <section class="surface" aria-labelledby="reviews-title">
          <div class="section-header">
            <div>
              <p class="eyebrow">Review</p>
              <h2 id="reviews-title">Findings</h2>
            </div>
          </div>
          <div id="review-list" class="resource-list"></div>
        </section>

        <section class="surface" aria-labelledby="approval-title">
          <div class="section-header">
            <div>
              <p class="eyebrow">Human Decision</p>
              <h2 id="approval-title">Approvals</h2>
            </div>
          </div>
          <div id="approval-list" class="resource-list"></div>
        </section>
      </main>
    </div>
  </div>
</body>
</html>
"""


STYLES = """
:root {
  color-scheme: light;
  --bg: #f7f8fa;
  --surface: #ffffff;
  --surface-subtle: #f1f5f2;
  --line: #d9ded9;
  --line-strong: #b9c2b8;
  --text: #18201b;
  --muted: #69736b;
  --accent: #146c54;
  --accent-strong: #0e533f;
  --warning: #a04b20;
  --danger: #9f2837;
  --info: #2d5f8f;
  --focus: #7a5cff;
  --shadow: 0 10px 30px rgba(24, 32, 27, 0.08);
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  min-width: 320px;
  background: var(--bg);
  color: var(--text);
  font-family:
    Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI",
    sans-serif;
  font-size: 15px;
  line-height: 1.45;
  letter-spacing: 0;
}

button,
input,
textarea {
  font: inherit;
  letter-spacing: 0;
}

button {
  min-height: 36px;
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  background: var(--surface);
  color: var(--text);
  cursor: pointer;
}

button:hover {
  border-color: var(--accent);
}

button:focus-visible,
input:focus-visible,
textarea:focus-visible,
[tabindex]:focus-visible {
  outline: 3px solid color-mix(in srgb, var(--focus) 35%, transparent);
  outline-offset: 2px;
}

button:disabled,
input:disabled,
textarea:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.skip-link {
  position: absolute;
  top: 8px;
  left: 8px;
  z-index: 20;
  transform: translateY(-140%);
  border-radius: 6px;
  background: var(--text);
  color: #fff;
  padding: 8px 10px;
}

.skip-link:focus {
  transform: translateY(0);
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
}

.app-shell {
  min-height: 100vh;
}

.topbar {
  position: sticky;
  top: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 64px;
  border-bottom: 1px solid var(--line);
  background: rgba(255, 255, 255, 0.94);
  padding: 10px 18px;
  backdrop-filter: blur(12px);
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
}

.clear-data-button {
  border-color: #f3b0b0;
  background: #fff5f5;
  color: #9f1d1d;
  font-size: 13px;
  font-weight: 700;
  padding: 0 10px;
}

.clear-data-button:hover {
  border-color: #d73535;
}

.brand-mark {
  display: grid;
  width: 38px;
  height: 38px;
  place-items: center;
  border-radius: 8px;
  background: var(--accent);
  color: #fff;
  font-weight: 800;
}

.brand h1 {
  margin: 0;
  font-size: 20px;
  line-height: 1.1;
}

.brand p {
  margin: 2px 0 0;
  color: var(--muted);
  font-size: 12px;
}

.topbar-actions,
.button-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.status-pill,
.phase-badge,
.tag {
  display: inline-flex;
  min-height: 28px;
  align-items: center;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface-subtle);
  padding: 3px 9px;
  color: var(--muted);
  font-size: 13px;
}

.phase-badge[data-phase="Ready"],
.phase-badge[data-phase="Completed"],
.phase-badge[data-phase="Approved"],
.phase-badge[data-phase="Succeeded"] {
  border-color: #99c6ad;
  color: var(--accent-strong);
}

.phase-badge[data-phase="Failed"],
.phase-badge[data-phase="Rejected"],
.phase-badge[data-phase="Cancelled"] {
  border-color: #e0a0a7;
  color: var(--danger);
}

.icon-button {
  width: 38px;
  padding: 0;
}

.primary-button {
  border-color: var(--accent);
  background: var(--accent);
  color: #fff;
  padding: 0 14px;
}

.danger-button {
  border-color: #d8a0a8;
  color: var(--danger);
  padding: 0 12px;
}

.error-banner {
  margin: 14px 18px 0;
  border: 1px solid #e3a1a9;
  border-radius: 8px;
  background: #fff4f5;
  color: var(--danger);
  padding: 10px 12px;
}

.workspace {
  display: grid;
  grid-template-columns: minmax(220px, 280px) minmax(0, 1fr);
  min-height: calc(100vh - 64px);
}

.sidebar {
  border-right: 1px solid var(--line);
  background: #fbfcfb;
  padding: 16px 12px;
}

.sidebar-header,
.section-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.sidebar h2,
.section-header h2 {
  margin: 0;
  font-size: 17px;
  line-height: 1.25;
}

.eyebrow {
  margin: 0 0 3px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
}

.muted {
  color: var(--muted);
}

.main {
  display: grid;
  gap: 14px;
  align-content: start;
  padding: 18px;
}

.surface {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  box-shadow: var(--shadow);
  padding: 14px;
}

.project-surface {
  box-shadow: none;
}

.content-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px;
}

.resource-list {
  display: grid;
  gap: 8px;
}

.resource-list.compact {
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
}

.resource-row {
  display: grid;
  gap: 6px;
  width: 100%;
  min-height: 72px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fff;
  padding: 10px;
  text-align: left;
}

.resource-row[aria-current="true"] {
  border-color: var(--accent);
  box-shadow: inset 3px 0 0 var(--accent);
}

.resource-row button {
  justify-self: start;
}

.subrow {
  display: grid;
  gap: 4px;
  border-top: 1px solid var(--line);
  padding-top: 8px;
}

.row-title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-weight: 700;
}

.row-meta {
  color: var(--muted);
  font-size: 13px;
  overflow-wrap: anywhere;
}

.meta-grid,
.overview-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 10px;
  margin: 0;
}

.meta-grid div,
.metric {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface-subtle);
  padding: 10px;
}

.meta-grid dt,
.metric span {
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
}

.meta-grid dd,
.metric strong {
  display: block;
  margin: 3px 0 0;
  overflow-wrap: anywhere;
}

.project-form {
  display: grid;
  gap: 10px;
  border-bottom: 1px solid var(--line);
  margin-bottom: 12px;
  padding-bottom: 12px;
}

.execution-form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.project-form label,
.execution-form label,
.question-form label {
  display: grid;
  gap: 6px;
  color: var(--muted);
  font-size: 13px;
  font-weight: 700;
}

.project-form input,
.execution-form input,
.execution-form textarea,
.question-form textarea {
  width: 100%;
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  background: #fff;
  color: var(--text);
  padding: 9px 10px;
  resize: vertical;
}

.span-2 {
  grid-column: 1 / -1;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
}

.question-form {
  display: grid;
  gap: 12px;
}

.question-row {
  display: grid;
  gap: 8px;
}

.timeline {
  display: grid;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.timeline li {
  display: grid;
  grid-template-columns: 52px minmax(0, 1fr);
  gap: 10px;
  border-bottom: 1px solid var(--line);
  padding: 8px 0;
}

.timeline li:last-child {
  border-bottom: 0;
}

.sequence {
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}

.event-payload {
  color: var(--text);
  display: block;
  font-size: 12px;
  margin-top: 3px;
}

.artifact-preview pre {
  min-height: 260px;
  max-height: 520px;
  margin: 0;
  overflow: auto;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #101712;
  color: #e9f1eb;
  padding: 12px;
  font-family: "SFMono-Regular", Consolas, monospace;
  font-size: 13px;
  line-height: 1.5;
  white-space: pre-wrap;
}

.empty-state {
  border: 1px dashed var(--line-strong);
  border-radius: 8px;
  color: var(--muted);
  padding: 14px;
}

@media (max-width: 900px) {
  .workspace,
  .content-grid,
  .execution-form {
    grid-template-columns: 1fr;
  }

  .sidebar {
    border-right: 0;
    border-bottom: 1px solid var(--line);
  }

  .main {
    padding: 12px;
  }
}
"""


APP_JS = """
const apiBase = "/api/v1";
const state = {
  projects: [],
  executions: [],
  plans: [],
  workItems: [],
  artifacts: [],
  reviews: [],
  approvals: [],
  invocations: [],
  events: [],
  selectedProjectId: null,
  selectedExecutionId: null,
  selectedArtifactId: null,
  eventSource: null,
  userInputDraft: {},
  loading: false,
};

const elements = {};

window.addEventListener("DOMContentLoaded", () => {
  bindElements();
  bindActions();
  startAutoRefresh();
  void loadAll();
});

function bindElements() {
  for (const id of [
    "connection-state",
    "clear-data",
    "refresh-button",
    "ui-error",
    "project-count",
    "project-form",
    "project-name",
    "project-description",
    "project-repository-path",
    "project-default-branch",
    "project-list",
    "project-title",
    "project-phase",
    "project-metadata",
    "execution-form",
    "execution-name",
    "goal-summary",
    "goal-description",
    "goal-criteria",
    "execution-list",
    "execution-title",
    "execution-overview",
    "run-execution",
    "cancel-execution",
    "user-input-panel",
    "user-input-form",
    "user-input-list",
    "plan-view",
    "event-timeline",
    "work-item-list",
    "invocation-list",
    "artifact-list",
    "artifact-content",
    "review-list",
    "approval-list",
  ]) {
    elements[id] = document.getElementById(id);
  }
}

function bindActions() {
  elements["clear-data"].addEventListener("click", () => {
    void clearData();
  });
  elements["refresh-button"].addEventListener("click", () => {
    void loadAll({force: true});
  });
  elements["project-form"].addEventListener("submit", event => {
    event.preventDefault();
    void createProject();
  });
  elements["execution-form"].addEventListener("submit", event => {
    event.preventDefault();
    void createExecution();
  });
  elements["run-execution"].addEventListener("click", () => {
    void runExecution();
  });
  elements["cancel-execution"].addEventListener("click", () => {
    void cancelExecution();
  });
  elements["user-input-form"].addEventListener("submit", event => {
    event.preventDefault();
    void submitUserInput();
  });
}

async function loadAll(options = {}) {
  if (!options.force && hasActiveFormControl()) {
    return;
  }
  if (state.loading) {
    return;
  }
  captureUserInputDraft();
  state.loading = true;
  setConnection("Loading");
  clearError();
  try {
    state.projects = await list("/projects");
    if (!state.selectedProjectId && state.projects.length > 0) {
      state.selectedProjectId = state.projects[0].metadata.id;
    }
    state.executions = await list("/executions");
    selectDefaultExecution();
    await loadExecutionResources();
    render();
    connectEvents();
    setConnection("Live");
  } catch (error) {
    showError(error);
    setConnection("Offline");
  } finally {
    state.loading = false;
  }
}

async function loadExecutionResources() {
  if (!state.selectedExecutionId) {
    state.plans = [];
    state.workItems = [];
    state.artifacts = [];
    state.reviews = [];
    state.approvals = [];
    state.invocations = [];
    state.events = [];
    return;
  }
  const query = `?executionId=${state.selectedExecutionId}&limit=100`;
  [
    state.plans,
    state.workItems,
    state.artifacts,
    state.reviews,
    state.approvals,
    state.invocations,
    state.events,
  ] = await Promise.all([
    list(`/plans${query}`),
    list(`/work-items${query}`),
    list(`/artifacts${query}`),
    list(`/reviews${query}`),
    list(`/approvals${query}`),
    list(`/role-invocations${query}`),
    list(`/executions/${state.selectedExecutionId}/events?limit=100`),
  ]);
}

function selectDefaultExecution() {
  const projectExecutions = executionsForSelectedProject();
  if (
    state.selectedExecutionId &&
    projectExecutions.some(item => item.metadata.id === state.selectedExecutionId)
  ) {
    return;
  }
  state.selectedExecutionId =
    projectExecutions.length > 0 ? projectExecutions[0].metadata.id : null;
}

async function list(path) {
  const payload = await request(path);
  return payload.items || [];
}

async function request(path, options = {}) {
  const response = await fetch(`${apiBase}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    ...options,
  });
  if (!response.ok) {
    const problem = await response.json().catch(() => ({}));
    throw new Error(problem.detail || problem.title || response.statusText);
  }
  if (response.status === 204) {
    return null;
  }
  return response.json();
}

function render() {
  renderProjectForm();
  renderProjects();
  renderProjectDetail();
  renderExecutionForm();
  renderExecutions();
  renderExecutionOverview();
  renderUserInput();
  renderPlan();
  renderEvents();
  renderWorkItems();
  renderInvocations();
  renderArtifacts();
  renderReviews();
  renderApprovals();
}

function renderProjectForm() {
  if (!elements["project-default-branch"].value) {
    elements["project-default-branch"].value = "main";
  }
}

function renderProjects() {
  elements["project-count"].textContent = String(state.projects.length);
  if (state.projects.length === 0) {
    elements["project-list"].innerHTML = empty("No projects.");
    return;
  }
  elements["project-list"].innerHTML = state.projects
    .map(project => {
      const phase = project.status.phase;
      return `
        <button class="resource-row" type="button"
          data-action="select-project" data-id="${escapeHtml(project.metadata.id)}"
          aria-current="${project.metadata.id === state.selectedProjectId}">
          <span class="row-title">
            ${escapeHtml(project.metadata.name)}
            ${phaseBadge(phase)}
          </span>
          <span class="row-meta">
            ${escapeHtml(project.spec.description || "Project")}
          </span>
        </button>
      `;
    })
    .join("");
  bindListAction("project-list", "select-project", id => {
    state.selectedProjectId = id;
    selectDefaultExecution();
    void loadExecutionResources().then(() => {
      render();
      connectEvents();
    });
  });
}

function renderProjectDetail() {
  const project = selectedProject();
  if (!project) {
    elements["project-title"].textContent = "No project selected";
    elements["project-phase"].textContent = "Pending";
    elements["project-phase"].dataset.phase = "Pending";
    elements["project-metadata"].innerHTML = "";
    return;
  }
  elements["project-title"].textContent = project.metadata.name;
  elements["project-phase"].textContent = project.status.phase;
  elements["project-phase"].dataset.phase = project.status.phase;
  const repositoryDetails = (project.status.repositories || []).map(repository => [
    `Repo ${repository.id}`,
    [
      repository.reachable ? "reachable" : "missing",
      repository.gitRepository ? "git" : "not git",
      repository.headRevision ? repository.headRevision.slice(0, 12) : "no HEAD",
      repository.clean ? "clean" : "dirty",
    ].join(", "),
  ]);
  elements["project-metadata"].innerHTML = metaGrid([
    ["Namespace", project.metadata.namespace],
    [
      "Workflow",
      `${project.spec.workflowRef.name}/${project.spec.workflowRef.version}`,
    ],
    ["Repositories", String(project.spec.repositories.length)],
    ...repositoryDetails,
    ["Resource Version", String(project.metadata.resourceVersion)],
  ]);
}

function renderExecutionForm() {
  const project = selectedProject();
  const disabled = !project || !["Ready", "Degraded"].includes(project.status.phase);
  for (const control of elements["execution-form"].elements) {
    control.disabled = disabled;
  }
  if (project && !elements["execution-name"].value) {
    elements["execution-name"].value = `execution-${Date.now().toString(36)}`;
  }
}

function renderExecutions() {
  const executions = executionsForSelectedProject();
  if (executions.length === 0) {
    elements["execution-list"].innerHTML = empty("No executions.");
    elements["execution-title"].textContent = "No execution selected";
    return;
  }
  elements["execution-list"].innerHTML = executions
    .map(execution => `
      <button class="resource-row" type="button"
        data-action="select-execution" data-id="${escapeHtml(execution.metadata.id)}"
        aria-current="${execution.metadata.id === state.selectedExecutionId}">
        <span class="row-title">
          ${escapeHtml(execution.metadata.name)}
          ${phaseBadge(execution.status.phase)}
        </span>
        <span class="row-meta">${escapeHtml(execution.spec.goal.summary)}</span>
      </button>
    `)
    .join("");
  const execution = selectedExecution();
  elements["execution-title"].textContent = execution
    ? execution.metadata.name
    : "No execution selected";
  bindListAction("execution-list", "select-execution", id => {
    state.selectedExecutionId = id;
    void loadExecutionResources().then(() => {
      render();
      connectEvents();
    });
  });
}

function renderExecutionOverview() {
  const execution = selectedExecution();
  elements["run-execution"].disabled = !execution || isTerminalExecution(execution);
  elements["run-execution"].textContent =
    execution && execution.status.phase === "Draft" ? "Start" : "Run";
  elements["cancel-execution"].disabled = !execution || !canCancelExecution(execution);
  if (!execution) {
    elements["execution-overview"].innerHTML = empty("No execution selected.");
    return;
  }
  elements["execution-overview"].innerHTML = [
    metric("Phase", execution.status.phase),
    metric("Current Step", execution.status.currentStep || "None"),
    metric("Goal", execution.spec.goal.summary),
    metric(
      "Iterations",
      `coding ${execution.status.iteration.coding}, ` +
        `review ${execution.status.iteration.review}`
    ),
  ].join("");
}

function renderUserInput() {
  captureUserInputDraft();
  const execution = selectedExecution();
  const questions = plannerQuestions();
  const waiting =
    execution &&
    execution.status.phase === "WaitingForUserInput" &&
    questions.length > 0;
  elements["user-input-panel"].hidden = !waiting;
  if (!waiting) {
    elements["user-input-list"].innerHTML = "";
    if (!execution || execution.status.phase !== "WaitingForUserInput") {
      state.userInputDraft = {};
    }
    return;
  }
  elements["user-input-list"].innerHTML = questions
    .map(question => {
      const value = state.userInputDraft[question.id] || "";
      return `
      <div class="resource-row question-row">
        <label>
          <span>${escapeHtml(question.question || question.id)}</span>
          <textarea rows="3" required
            data-question-id="${escapeHtml(question.id)}"
          >${escapeHtml(value)}</textarea>
        </label>
      </div>
    `;
    })
    .join("");
}

function renderPlan() {
  if (state.plans.length === 0) {
    elements["plan-view"].innerHTML = empty("No plan.");
    return;
  }
  const latest = state.plans[state.plans.length - 1];
  const workItems = latest.spec.workItems || [];
  elements["plan-view"].innerHTML = `
    <div class="resource-row">
      <div class="row-title">
        ${escapeHtml(latest.metadata.name)}
        ${phaseBadge(latest.status.phase)}
      </div>
      <div class="row-meta">${escapeHtml(latest.spec.summary)}</div>
      <div class="resource-list">
        ${workItems.map(item => `
          <div class="subrow">
            <div class="row-title">${escapeHtml(item.title)}</div>
            <div class="row-meta">${escapeHtml(item.objective)}</div>
          </div>
        `).join("")}
      </div>
    </div>
  `;
}

function renderEvents() {
  if (state.events.length === 0) {
    elements["event-timeline"].innerHTML = `<li>${empty("No events.")}</li>`;
    return;
  }
  elements["event-timeline"].innerHTML = state.events
    .slice()
    .reverse()
    .map(event => {
      const summary = eventSummary(event);
      return `
        <li>
          <span class="sequence">#${event.spec.sequence}</span>
          <span>
            <strong>${escapeHtml(event.spec.type)}</strong><br>
            <span class="row-meta">${escapeHtml(event.spec.producer)}
            - ${escapeHtml(event.spec.occurredAt)}</span>
            ${summary
              ? `<span class="event-payload">${escapeHtml(summary)}</span>`
              : ""}
          </span>
        </li>
      `;
    })
    .join("");
}

function renderWorkItems() {
  if (state.workItems.length === 0) {
    elements["work-item-list"].innerHTML = empty("No work items.");
    return;
  }
  elements["work-item-list"].innerHTML = state.workItems
    .map(item => `
      <div class="resource-row">
        <div class="row-title">
          ${escapeHtml(item.metadata.name)}
          ${phaseBadge(item.status.phase)}
        </div>
        <div class="row-meta">${escapeHtml(item.spec.objective)}</div>
        <div class="row-meta">attempt ${item.status.attempt}</div>
      </div>
    `)
    .join("");
}

function renderInvocations() {
  if (state.invocations.length === 0) {
    elements["invocation-list"].innerHTML = empty("No invocations.");
    return;
  }
  elements["invocation-list"].innerHTML = state.invocations
    .map(invocation => `
      <div class="resource-row">
        <div class="row-title">
          ${escapeHtml(invocation.spec.roleRef.name)}
          ${phaseBadge(invocation.status.phase)}
        </div>
        <div class="row-meta">
          ${escapeHtml(invocation.spec.agentRef.name || invocation.spec.agentRef.id)}
          - tools ${invocation.status.toolCallCount}
        </div>
      </div>
    `)
    .join("");
}

function renderArtifacts() {
  if (state.artifacts.length === 0) {
    elements["artifact-list"].innerHTML = empty("No artifacts.");
    return;
  }
  elements["artifact-list"].innerHTML = state.artifacts
    .map(artifact => `
      <button class="resource-row" type="button"
        data-action="select-artifact" data-id="${escapeHtml(artifact.metadata.id)}"
        aria-current="${artifact.metadata.id === state.selectedArtifactId}">
        <span class="row-title">
          ${escapeHtml(artifact.metadata.name)}
          <span class="tag">${escapeHtml(artifact.spec.type)}</span>
        </span>
        <span class="row-meta">${escapeHtml(artifact.spec.mediaType)}
        - ${artifact.spec.sizeBytes} bytes</span>
      </button>
    `)
    .join("");
  bindListAction("artifact-list", "select-artifact", id => {
    state.selectedArtifactId = id;
    void loadArtifactContent(id);
  });
}

function renderReviews() {
  if (state.reviews.length === 0) {
    elements["review-list"].innerHTML = empty("No reviews.");
    return;
  }
  elements["review-list"].innerHTML = state.reviews
    .map(review => {
      const findings = [
        ...(review.status.blockingFindings || []),
        ...(review.status.nonBlockingFindings || []),
      ];
      return `
        <div class="resource-row">
          <div class="row-title">
            ${escapeHtml(review.metadata.name)}
            ${phaseBadge(review.status.verdict || review.status.phase)}
          </div>
          <div class="row-meta">${escapeHtml(review.status.summary || "Review")}</div>
          ${findings.map(finding => `
            <div class="subrow">
              <div class="row-title">${escapeHtml(finding.id)}</div>
              <div class="row-meta">${escapeHtml(finding.issue)}</div>
            </div>
          `).join("")}
        </div>
      `;
    })
    .join("");
}

function renderApprovals() {
  if (state.approvals.length === 0) {
    elements["approval-list"].innerHTML = empty("No approvals.");
    return;
  }
  elements["approval-list"].innerHTML = state.approvals
    .map(approval => `
      <div class="resource-row">
        <div class="row-title">
          ${escapeHtml(approval.metadata.name)}
          ${phaseBadge(approval.status.phase)}
        </div>
        <div class="row-meta">
          ${escapeHtml(approval.spec.type)}
          - ${escapeHtml(approval.spec.subjectRef.kind)}
          v${approval.spec.subjectRef.resourceVersion}
        </div>
        ${approval.status.phase === "Pending" ? `
          <div class="button-row">
            <button type="button"
              data-action="approve"
              data-id="${escapeHtml(approval.metadata.id)}">
              Approve
            </button>
            <button type="button" class="danger-button"
              data-action="reject" data-id="${escapeHtml(approval.metadata.id)}">
              Reject
            </button>
          </div>
        ` : ""}
      </div>
    `)
    .join("");
  bindListAction("approval-list", "approve", id => void decideApproval(id, "approve"));
  bindListAction("approval-list", "reject", id => void decideApproval(id, "reject"));
}

async function createProject() {
  const repositoryPath = elements["project-repository-path"].value.trim();
  const defaultBranch = elements["project-default-branch"].value.trim() || "main";
  const repositories = repositoryPath
    ? [
        {
          id: "backend",
          path: repositoryPath,
          defaultBranch,
          type: "git",
        },
      ]
    : [];
  const payload = {
    name: slug(elements["project-name"].value),
    spec: {
      description: elements["project-description"].value,
      repositories,
      workflowRef: {
        name: "software-delivery",
        version: "v1alpha1",
      },
      roleBindings: {
        planner: {agentRef: {name: "planner-local"}},
        coding: {agentRef: {name: "coder-local"}},
        reviewer: {agentRef: {name: "reviewer-local"}},
      },
    },
  };
  try {
    const project = await request("/projects", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.selectedProjectId = project.metadata.id;
    state.selectedExecutionId = null;
    elements["project-form"].reset();
    elements["project-default-branch"].value = "main";
    await loadAll({force: true});
  } catch (error) {
    showError(error);
  }
}

async function clearData() {
  const confirmed = window.confirm(
    "Clear all Maestro local data for this test environment?"
  );
  if (!confirmed) {
    return;
  }
  try {
    await request("/admin/clear-data", {
      method: "POST",
      body: JSON.stringify({confirm: "CLEAR"}),
    });
    state.projects = [];
    state.executions = [];
    state.plans = [];
    state.workItems = [];
    state.artifacts = [];
    state.reviews = [];
    state.approvals = [];
    state.invocations = [];
    state.events = [];
    state.selectedProjectId = null;
    state.selectedExecutionId = null;
    state.selectedArtifactId = null;
    state.userInputDraft = {};
    elements["artifact-content"].textContent = "No artifact selected.";
    await loadAll({force: true});
  } catch (error) {
    showError(error);
  }
}

async function createExecution() {
  const project = selectedProject();
  if (!project) {
    return;
  }
  const criteria = splitLines(elements["goal-criteria"].value);
  const payload = {
    name: slug(elements["execution-name"].value || elements["goal-summary"].value),
    spec: {
      projectRef: {
        id: project.metadata.id,
        name: project.metadata.name,
      },
      goal: {
        summary: elements["goal-summary"].value,
        description: elements["goal-description"].value,
        acceptanceCriteria: criteria,
      },
      workflowRef: project.spec.workflowRef,
      requestedRoles: ["planner", "coding", "reviewer"],
    },
  };
  try {
    const execution = await request("/executions", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.selectedExecutionId = execution.metadata.id;
    elements["execution-form"].reset();
    await loadAll({force: true});
  } catch (error) {
    showError(error);
  }
}

async function runExecution() {
  const execution = selectedExecution();
  if (!execution) {
    return;
  }
  try {
    const started = await request(
      `/executions/${execution.metadata.id}/actions/run`,
      {
        method: "POST",
        body: JSON.stringify({
          resourceVersion: execution.metadata.resourceVersion,
        }),
      }
    );
    state.selectedExecutionId = started.metadata.id;
    await loadAll({force: true});
  } catch (error) {
    showError(error);
  }
}

async function submitUserInput() {
  const execution = selectedExecution();
  if (!execution) {
    return;
  }
  const answers = Array.from(
    elements["user-input-list"].querySelectorAll("textarea[data-question-id]")
  ).map(control => ({
    questionId: control.dataset.questionId,
    answer: control.value.trim(),
  }));
  if (answers.some(item => !item.answer)) {
    return;
  }
  try {
    const updated = await request(
      `/executions/${execution.metadata.id}/actions/respond`,
      {
        method: "POST",
        body: JSON.stringify({
          resourceVersion: execution.metadata.resourceVersion,
          answers,
          actor: "local-user",
          requestSource: "web-ui",
        }),
      }
    );
    state.selectedExecutionId = updated.metadata.id;
    state.userInputDraft = {};
    await loadAll({force: true});
  } catch (error) {
    showError(error);
  }
}

async function cancelExecution() {
  const execution = selectedExecution();
  if (!execution) {
    return;
  }
  const confirmed = window.confirm(
    `Cancel execution ${execution.metadata.name}? This cannot be undone.`
  );
  if (!confirmed) {
    return;
  }
  try {
    await request(`/executions/${execution.metadata.id}/actions/cancel`, {
      method: "POST",
      body: JSON.stringify({
        resourceVersion: execution.metadata.resourceVersion,
        actor: "local-user",
        requestSource: "web-ui",
      }),
    });
    await loadAll({force: true});
  } catch (error) {
    showError(error);
  }
}

async function decideApproval(id, decision) {
  const approval = state.approvals.find(item => item.metadata.id === id);
  if (!approval) {
    return;
  }
  try {
    await request(`/approvals/${id}/actions/${decision}`, {
      method: "POST",
      body: JSON.stringify({
        resourceVersion: approval.metadata.resourceVersion,
        subjectResourceVersion: approval.spec.subjectRef.resourceVersion,
        actor: "local-user",
        requestSource: "web-ui",
      }),
    });
    await loadAll({force: true});
  } catch (error) {
    showError(error);
  }
}

async function loadArtifactContent(id) {
  const artifact = state.artifacts.find(item => item.metadata.id === id);
  elements["artifact-content"].textContent = "Loading...";
  renderArtifacts();
  try {
    const response = await fetch(`${apiBase}/artifacts/${id}/content`);
    if (!response.ok) {
      const problem = await response.json().catch(() => ({}));
      throw new Error(problem.detail || problem.title || response.statusText);
    }
    const text = await response.text();
    const title = artifact ? `${artifact.metadata.name}\\n\\n` : "";
    elements["artifact-content"].textContent = `${title}${text}`;
  } catch (error) {
    showError(error);
    elements["artifact-content"].textContent = "Content unavailable.";
  }
}

function connectEvents() {
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
  if (!state.selectedExecutionId || !window.EventSource) {
    return;
  }
  const source = new EventSource(
    `${apiBase}/executions/${state.selectedExecutionId}/events/stream`
  );
  state.eventSource = source;
  source.onopen = () => setConnection("Live");
  source.onerror = () => setConnection("Polling");
  for (const eventName of [
    "GoalCreated",
    "ExecutionRunStarted",
    "ExecutionRunWaiting",
    "ExecutionRunCompleted",
    "ExecutionRunFailed",
    "ExecutionCancellationRequested",
    "PlannerRunStarted",
    "PlannerRunCompleted",
    "PlannerQuestionsProduced",
    "WorkspacePreparationStarted",
    "WorkspacePreparationFailed",
    "CodingRunStarted",
    "VerificationCompleted",
    "VerificationFailed",
    "VerificationSkipped",
    "ReviewerRunStarted",
    "ApprovalRequested",
    "UserInputProvided",
    "ExecutionPhaseChanged",
    "PlanPhaseChanged",
    "WorkItemPhaseChanged",
    "ReviewPhaseChanged",
    "ApprovalPhaseChanged",
    "ArtifactPhaseChanged",
    "ProviderPhaseChanged",
    "AgentPhaseChanged",
  ]) {
    source.addEventListener(eventName, () => {
      if (hasActiveFormControl()) {
        return;
      }
      void loadExecutionResources().then(render);
    });
  }
}

function selectedProject() {
  return state.projects.find(
    project => project.metadata.id === state.selectedProjectId
  );
}

function selectedExecution() {
  return state.executions.find(
    execution => execution.metadata.id === state.selectedExecutionId
  );
}

function captureUserInputDraft() {
  const controls = elements["user-input-list"]?.querySelectorAll(
    "textarea[data-question-id]"
  );
  if (!controls) {
    return;
  }
  for (const control of controls) {
    state.userInputDraft[control.dataset.questionId] = control.value;
  }
}

function plannerQuestions() {
  const questionEvent = state.events
    .slice()
    .reverse()
    .find(event => event.spec.type === "PlannerQuestionsProduced");
  const questions = questionEvent?.spec?.payload?.questions || [];
  return Array.isArray(questions) ? questions : [];
}

function executionsForSelectedProject() {
  return state.executions.filter(execution => {
    const projectRef = execution.spec.projectRef;
    return projectRef && projectRef.id === state.selectedProjectId;
  });
}

function canCancelExecution(execution) {
  return [
    "WaitingForUserInput",
    "WaitingForPlanApproval",
    "Executing",
    "WaitingForFinalApproval",
  ].includes(execution.status.phase);
}

function isTerminalExecution(execution) {
  return ["Completed", "Failed", "Cancelled", "Archived"].includes(
    execution.status.phase
  );
}

function hasActiveFormControl() {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement)) {
    return false;
  }
  return Boolean(
    active.closest(
      "#project-form, #execution-form, #user-input-form, #approval-list"
    )
  );
}

function startAutoRefresh() {
  window.setInterval(() => {
    const execution = selectedExecution();
    if (
      !execution ||
      isTerminalExecution(execution) ||
      state.loading ||
      hasActiveFormControl()
    ) {
      return;
    }
    void loadAll();
  }, 2500);
}

function bindListAction(containerId, action, handler) {
  elements[containerId]
    .querySelectorAll(`[data-action="${action}"]`)
    .forEach(button => {
      button.addEventListener("click", () => handler(button.dataset.id));
    });
}

function setConnection(value) {
  elements["connection-state"].textContent = value;
}

function clearError() {
  elements["ui-error"].hidden = true;
  elements["ui-error"].textContent = "";
}

function showError(error) {
  elements["ui-error"].hidden = false;
  elements["ui-error"].textContent = error.message || String(error);
}

function splitLines(value) {
  return value
    .split("\\n")
    .map(item => item.trim())
    .filter(Boolean);
}

function slug(value) {
  const normalized = value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 63);
  return normalized || `execution-${Date.now().toString(36)}`;
}

function metric(label, value) {
  return `
    <div class="metric">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
    </div>
  `;
}

function metaGrid(items) {
  return items
    .map(([label, value]) => `
      <div>
        <dt>${escapeHtml(label)}</dt>
        <dd>${escapeHtml(value)}</dd>
      </div>
    `)
    .join("");
}

function phaseBadge(value) {
  return (
    `<span class="phase-badge" data-phase="${escapeHtml(value)}">` +
    `${escapeHtml(value)}</span>`
  );
}

function eventSummary(event) {
  const payload = event.spec.payload || {};
  const parts = [];
  if (payload.fromPhase || payload.toPhase) {
    parts.push(`${payload.fromPhase || "?"} -> ${payload.toPhase || "?"}`);
  }
  for (const key of [
    "phase",
    "reason",
    "message",
    "error",
    "repository",
    "workspaceId",
    "planId",
    "approvalId",
    "workItemId",
    "agent",
    "reviewId",
    "decision",
    "actor",
  ]) {
    if (payload[key] !== undefined && payload[key] !== null && payload[key] !== "") {
      parts.push(`${key}: ${compactValue(payload[key])}`);
    }
  }
  return parts.join(" | ");
}

function compactValue(value) {
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

function empty(value) {
  return `<div class="empty-state">${escapeHtml(value)}</div>`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
"""
