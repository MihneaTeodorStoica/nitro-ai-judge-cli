(() => {
  "use strict";
  const PAGE_SIZE = 25;
  const state = { competitions: [], page: 1, operationTimers: new Map(), deleteRef: null };
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const rows = document.querySelector("#competition-rows");
  const template = document.querySelector("#competition-row-template");
  const search = document.querySelector("#search");
  const imageFilter = document.querySelector("#image-filter");
  const workspaceFilter = document.querySelector("#workspace-filter");
  const empty = document.querySelector("#empty-state");
  const count = document.querySelector("#result-count");
  const pagination = document.querySelector("#pagination");
  const previousPage = document.querySelector("#previous-page");
  const nextPage = document.querySelector("#next-page");
  const pageStatus = document.querySelector("#page-status");
  const dialog = document.querySelector("#delete-dialog");
  const confirmation = document.querySelector("#delete-confirmation");
  const loginDialog = document.querySelector("#nitro-login-dialog");
  const loginUsername = document.querySelector("#nitro-login-username");
  const loginPassword = document.querySelector("#nitro-login-password");
  const loginError = document.querySelector("#nitro-login-error");

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Accept": "application/json", "Content-Type": "application/json", "X-CSRF-Token": csrf, ...(options.headers || {}) },
    });
    const value = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(value.error?.message || `Request failed (${response.status})`);
    return value;
  }

  function status(node, value) {
    const text = value || "unknown";
    node.textContent = text;
    node.dataset.state = text;
  }

  function actionButton(label, action, kind = "") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `action-button ${kind}`.trim();
    button.textContent = label;
    button.dataset.action = action;
    return button;
  }

  function render() {
    const query = search.value.trim().toLowerCase();
    const matches = state.competitions.filter(item => {
      const match = !query || `${item.reference} ${item.title || ""}`.toLowerCase().includes(query);
      return match && (!imageFilter.value || item.image_state === imageFilter.value) && (!workspaceFilter.value || item.workspace_state === workspaceFilter.value);
    });
    const pageCount = Math.max(1, Math.ceil(matches.length / PAGE_SIZE));
    state.page = Math.min(state.page, pageCount);
    const start = (state.page - 1) * PAGE_SIZE;
    const visible = matches.slice(start, start + PAGE_SIZE);
    rows.replaceChildren();
    for (const competition of visible) {
      const fragment = template.content.cloneNode(true);
      const row = fragment.querySelector(".competition-row");
      const operationRow = fragment.querySelector(".operation-row");
      row.dataset.reference = competition.reference;
      operationRow.dataset.operationFor = competition.reference;
      fragment.querySelector(".competition-title").textContent = competition.title || competition.competition;
      fragment.querySelector(".competition-ref").textContent = competition.reference;
      status(fragment.querySelector(".image-status"), competition.image_state);
      status(fragment.querySelector(".workspace-status"), competition.workspace_state);
      status(fragment.querySelector(".health-status"), competition.service_health);
      const actions = fragment.querySelector(".actions");
      if (competition.workspace_state === "running") {
        actions.append(actionButton("Open", "open", "primary"), actionButton("Stop", "stop"), actionButton("Restart", "restart"));
      } else if (competition.workspace_state === "stopped") {
        actions.append(actionButton("Start", "start", "primary"), actionButton("Recreate", "recreate"));
      } else if (competition.workspace_state === "ready") {
        actions.append(actionButton("Play", "play", "primary"), actionButton("Recreate", "recreate"));
      } else {
        actions.append(actionButton("Play", "play", "primary"), actionButton("Pull", "pull"));
      }
      if (competition.workspace_state !== "missing") actions.append(actionButton("Delete", "delete-menu", "danger"));
      rows.append(fragment);
    }
    const end = Math.min(start + visible.length, matches.length);
    count.textContent = matches.length ? `${start + 1}–${end} of ${matches.length} competitions` : "0 competitions";
    empty.hidden = matches.length !== 0;
    pagination.hidden = pageCount === 1;
    previousPage.disabled = state.page === 1;
    nextPage.disabled = state.page === pageCount;
    pageStatus.textContent = `Page ${state.page} of ${pageCount}`;
  }

  async function load(refresh = false) {
    document.querySelector("#manager-status").textContent = refresh ? "Refreshing" : "Loading cache";
    try {
      const value = await api(`/nitro/api/v1/competitions${refresh ? "?refresh=true" : ""}`);
      state.competitions = value.competitions;
      document.querySelector("#sync-notice").hidden = !value.login_sync_required;
      if (value.login_sync_required && !loginDialog.open) {
        loginDialog.showModal();
        loginUsername.focus();
      }
      document.querySelector("#manager-status").textContent = "Manager healthy";
      render();
    } catch (error) {
      document.querySelector("#manager-status").textContent = error.message;
      count.textContent = "Could not load competitions";
    }
  }

  async function startAction(reference, action, options = {}) {
    const [org, competition] = reference.split("/");
    const value = await api(`/nitro/api/v1/competitions/${org}/${competition}/actions/${action}`, { method: "POST", body: JSON.stringify(options) });
    showOperation(reference, value.operation_id);
  }

  function showOperation(reference, operationId) {
    const operationRow = [...rows.querySelectorAll(".operation-row")].find(node => node.dataset.operationFor === reference);
    if (!operationRow) return;
    operationRow.hidden = false;
    const timer = setInterval(async () => {
      try {
        const operation = await api(`/nitro/api/v1/operations/${operationId}`);
        operationRow.querySelector(".operation-stage").textContent = operation.stage;
        operationRow.querySelector(".operation-message").textContent = operation.message;
        const events = operation.events || [];
        operationRow.querySelector("progress").value = Math.min(95, events.length * 14);
        operationRow.querySelector(".operation-logs").textContent = (operation.error?.logs || []).join("\n") || "No diagnostic logs.";
        if (["complete", "failed", "cancelled", "interrupted"].includes(operation.status)) {
          clearInterval(timer);
          state.operationTimers.delete(operationId);
          operationRow.querySelector("progress").value = 100;
          await load();
        }
      } catch (error) {
        clearInterval(timer);
        operationRow.querySelector(".operation-message").textContent = error.message;
      }
    }, 700);
    state.operationTimers.set(operationId, timer);
  }

  rows.addEventListener("click", async event => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const reference = button.closest(".competition-row").dataset.reference;
    button.disabled = true;
    try {
      if (button.dataset.action === "open") {
        const [org, competition] = reference.split("/");
        window.open(
          `/nitro/competitions/${org}/${competition}/jupyter/`,
          "_blank",
          "noopener",
        );
      } else if (button.dataset.action === "delete-menu") {
        state.deleteRef = reference;
        document.querySelector("#delete-reference").textContent = reference;
        confirmation.value = "";
        document.querySelector("#delete-error").textContent = "";
        dialog.showModal();
        confirmation.focus();
      } else {
        await startAction(reference, button.dataset.action);
      }
    } catch (error) {
      document.querySelector("#manager-status").textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });

  document.querySelector("#delete-form").addEventListener("submit", async event => {
    if (event.submitter?.value !== "confirm") return;
    event.preventDefault();
    if (confirmation.value !== state.deleteRef) {
      document.querySelector("#delete-error").textContent = "The competition reference does not match.";
      confirmation.focus();
      return;
    }
    dialog.close();
    await startAction(state.deleteRef, "delete-workspace", { confirm_ref: state.deleteRef });
  });

  document.querySelector("#nitro-login-open").addEventListener("click", () => {
    loginError.textContent = "";
    loginDialog.showModal();
    loginUsername.focus();
  });
  document.querySelector("#nitro-login-cancel").addEventListener("click", () => {
    loginPassword.value = "";
    loginDialog.close();
  });
  document.querySelector("#nitro-login-form").addEventListener("submit", async event => {
    event.preventDefault();
    loginError.textContent = "Signing in…";
    try {
      await api("/nitro/api/v1/login", {
        method: "POST",
        body: JSON.stringify({ username: loginUsername.value.trim(), password: loginPassword.value }),
      });
      loginPassword.value = "";
      loginDialog.close();
      await load();
    } catch (error) {
      loginPassword.value = "";
      loginError.textContent = error.message;
      loginPassword.focus();
    }
  });

  [search, imageFilter, workspaceFilter].forEach(node => node.addEventListener("input", () => {
    state.page = 1;
    render();
  }));
  previousPage.addEventListener("click", () => { state.page -= 1; render(); });
  nextPage.addEventListener("click", () => { state.page += 1; render(); });
  document.querySelector("#refresh").addEventListener("click", () => load(true));
  document.querySelector("#theme-toggle").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme !== "dark";
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    localStorage.setItem("naij-theme", dark ? "dark" : "light");
    document.querySelector("#theme-toggle").textContent = dark ? "Light" : "Dark";
  });
  if (localStorage.getItem("naij-theme") === "dark") {
    document.documentElement.dataset.theme = "dark";
    document.querySelector("#theme-toggle").textContent = "Light";
  }
  load();
})();
