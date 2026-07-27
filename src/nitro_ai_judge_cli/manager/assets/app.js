(() => {
  "use strict";
  const PAGE_SIZE = 25;
  const SUCCESS_DISMISS_MS = 5000;
  const DISMISSED_OPERATIONS_KEY = "naij-dismissed-operations";
  const state = {
    competitions: [],
    page: 1,
    loaded: false,
    loginRequired: false,
    refreshFailed: false,
    alertRetry: null,
    syncTimer: null,
    events: null,
    operationTimers: new Map(),
    successTimers: new Map(),
    successFrames: new Map(),
    dismissedOperations: loadOperationSet(DISMISSED_OPERATIONS_KEY),
    deleteRef: null,
  };
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const rows = document.querySelector("#competition-rows");
  const template = document.querySelector("#competition-row-template");
  const search = document.querySelector("#search");
  const imageFilter = document.querySelector("#image-filter");
  const workspaceFilter = document.querySelector("#workspace-filter");
  const empty = document.querySelector("#empty-state");
  const count = document.querySelector("#result-count");
  const paginations = [...document.querySelectorAll("[data-pagination]")];
  const dialog = document.querySelector("#delete-dialog");
  const confirmation = document.querySelector("#delete-confirmation");
  const removeImageDialog = document.querySelector("#remove-image-dialog");
  const loginDialog = document.querySelector("#nitro-login-dialog");
  const loginUsername = document.querySelector("#nitro-login-username");
  const loginPassword = document.querySelector("#nitro-login-password");
  const loginError = document.querySelector("#nitro-login-error");
  const liveAlert = document.querySelector("#live-alert");
  const liveAlertRetry = document.querySelector("#live-alert-retry");
  const emptyTitle = document.querySelector("#empty-title");
  const emptyMessage = document.querySelector("#empty-message");
  const emptyAction = document.querySelector("#empty-action");
  const disconnectDialog = document.querySelector("#disconnect-dialog");

  function loadOperationSet(key) {
    try {
      const values = JSON.parse(localStorage.getItem(key) || "[]");
      return new Set(Array.isArray(values) ? values.slice(-100) : []);
    } catch (_error) {
      return new Set();
    }
  }

  function saveOperationSet(key, values) {
    try {
      localStorage.setItem(key, JSON.stringify([...values].slice(-100)));
    } catch (_error) {
      // Storage can be disabled; the current page still behaves correctly.
    }
  }

  function cancelAutoDismiss(operationId) {
    clearTimeout(state.successTimers.get(operationId));
    state.successTimers.delete(operationId);
    const frame = state.successFrames.get(operationId);
    if (frame !== undefined) cancelAnimationFrame(frame);
    state.successFrames.delete(operationId);
  }

  function dismissOperation(operationId) {
    cancelAutoDismiss(operationId);
    state.dismissedOperations.add(operationId);
    saveOperationSet(DISMISSED_OPERATIONS_KEY, state.dismissedOperations);
    const operationRow = [...rows.querySelectorAll(".operation-row")].find(node => node.dataset.operationId === operationId);
    if (operationRow) operationRow.hidden = true;
  }

  function scheduleAutoDismiss(operation, operationRow) {
    if (state.successTimers.has(operation.id)) return;
    const deadline = Date.now() + SUCCESS_DISMISS_MS;
    const progress = operationRow.querySelector("progress");
    const tick = () => {
      const remaining = Math.max(0, deadline - Date.now());
      progress.value = remaining / SUCCESS_DISMISS_MS * 100;
      if (remaining > 0) state.successFrames.set(operation.id, requestAnimationFrame(tick));
      else state.successFrames.delete(operation.id);
    };
    tick();
    state.successTimers.set(
      operation.id,
      setTimeout(
        () => dismissOperation(operation.id),
        Math.max(0, deadline - Date.now()),
      ),
    );
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Accept": "application/json", "Content-Type": "application/json", "X-CSRF-Token": csrf, ...(options.headers || {}) },
    });
    const value = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(value.error?.message || `Request failed (${response.status})`);
    return value;
  }

  function showAlert(message, retry = null) {
    state.alertRetry = retry;
    document.querySelector("#live-alert-message").textContent = message;
    liveAlertRetry.hidden = !retry;
    liveAlert.hidden = false;
  }

  function hideAlert() {
    state.alertRetry = null;
    liveAlert.hidden = true;
  }

  function rememberRowFocus() {
    const active = document.activeElement;
    const row = active?.closest?.(".competition-row");
    if (!row || !active.dataset.action) return null;
    return { reference: row.dataset.reference, action: active.dataset.action };
  }

  function restoreRowFocus(value) {
    if (!value) return;
    const row = [...rows.querySelectorAll(".competition-row")].find(node => node.dataset.reference === value.reference);
    row?.querySelector(`[data-action="${value.action}"]`)?.focus({ preventScroll: true });
  }

  function status(node, value) {
    const text = value || "unknown";
    node.textContent = text;
    node.dataset.state = text;
  }

  function effectiveImageState(competition) {
    const operation = competition.operation;
    if (operation?.stage === "pulling") {
      if (["queued", "running"].includes(operation.status)) return "pulling";
      if (operation.status === "failed") return "error";
    }
    return competition.image_state;
  }

  function actionButton(label, action, kind = "") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `action-button ${kind}`.trim();
    button.textContent = label;
    button.dataset.action = action;
    if (action === "copy-link") button.setAttribute("aria-live", "polite");
    return button;
  }

  function jupyterUrl(reference) {
    const [org, competition] = reference.split("/");
    return new URL(`/nitro/competitions/${org}/${competition}/jupyter/`, window.location.href).href;
  }

  async function copyJupyterLink(button, reference) {
    if (!navigator.clipboard?.writeText) throw new Error("Clipboard access is unavailable in this browser.");
    try {
      await navigator.clipboard.writeText(jupyterUrl(reference));
    } catch (_error) {
      throw new Error("Could not copy the Jupyter link. Check browser clipboard permissions.");
    }
    button.textContent = "Copied";
    setTimeout(() => { if (button.isConnected) button.textContent = "Copy link"; }, 2000);
  }

  const filterClosers = new Map();

  function setupFilterMenu(menu) {
    const trigger = menu.querySelector(".filter-trigger");
    const listbox = menu.querySelector(".filter-options");
    const value = menu.querySelector(".filter-value");
    const mark = trigger.querySelector(".filter-state-mark");
    const options = [...menu.querySelectorAll(".filter-option")];
    let activeIndex = options.findIndex(option => option.getAttribute("aria-selected") === "true");

    function setActive(index) {
      activeIndex = (index + options.length) % options.length;
      const active = options[activeIndex];
      for (const option of options) option.classList.toggle("is-active", option === active);
      listbox.setAttribute("aria-activedescendant", active.id);
      active.scrollIntoView({ block: "nearest" });
    }

    function close(restoreFocus = false) {
      if (listbox.hidden) return;
      listbox.hidden = true;
      menu.classList.remove("is-open");
      trigger.setAttribute("aria-expanded", "false");
      listbox.removeAttribute("aria-activedescendant");
      if (restoreFocus) trigger.focus();
    }

    function open(edge = "selected") {
      for (const [other, closeOther] of filterClosers) {
        if (other !== menu) closeOther();
      }
      listbox.hidden = false;
      menu.classList.add("is-open");
      trigger.setAttribute("aria-expanded", "true");
      const selected = options.findIndex(option => option.getAttribute("aria-selected") === "true");
      setActive(edge === "first" ? 0 : edge === "last" ? options.length - 1 : Math.max(0, selected));
      listbox.focus({ preventScroll: true });
    }

    function choose(option) {
      activeIndex = options.indexOf(option);
      menu.dataset.value = option.dataset.value;
      value.textContent = option.querySelector(".filter-option-label").textContent;
      mark.dataset.state = option.dataset.state;
      for (const item of options) item.setAttribute("aria-selected", String(item === option));
      menu.dispatchEvent(new Event("input"));
      close(true);
    }

    trigger.addEventListener("click", () => {
      if (listbox.hidden) open();
      else close();
    });
    trigger.addEventListener("keydown", event => {
      if (event.key === "Escape" && !listbox.hidden) {
        event.preventDefault();
        close();
      } else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        open(event.key === "Home" ? "first" : event.key === "End" ? "last" : "selected");
      }
    });
    listbox.addEventListener("click", event => {
      const option = event.target.closest(".filter-option");
      if (!option) return;
      choose(option);
    });
    listbox.addEventListener("keydown", event => {
      if (event.key === "Escape") {
        event.preventDefault();
        close(true);
        return;
      }
      if (["Enter", " "].includes(event.key)) {
        event.preventDefault();
        choose(options[activeIndex]);
        return;
      }
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        setActive(event.key === "Home" ? 0 : event.key === "End" ? options.length - 1 : activeIndex + (event.key === "ArrowDown" ? 1 : -1));
        return;
      }
      if (event.key.length === 1 && !event.altKey && !event.ctrlKey && !event.metaKey) {
        const key = event.key.toLowerCase();
        const next = options.findIndex((option, index) => index > activeIndex && option.textContent.trim().toLowerCase().startsWith(key));
        const wrapped = next < 0 ? options.findIndex(option => option.textContent.trim().toLowerCase().startsWith(key)) : next;
        if (wrapped >= 0) {
          event.preventDefault();
          setActive(wrapped);
        }
      }
    });
    menu.addEventListener("focusout", event => {
      if (!menu.contains(event.relatedTarget)) close();
    });
    filterClosers.set(menu, close);
  }

  function render() {
    const priorFocus = rememberRowFocus();
    for (const timer of state.operationTimers.values()) clearInterval(timer);
    state.operationTimers.clear();
    for (const timer of state.successTimers.values()) clearTimeout(timer);
    state.successTimers.clear();
    for (const frame of state.successFrames.values()) cancelAnimationFrame(frame);
    state.successFrames.clear();
    const keywords = search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const matches = state.competitions.filter(item => {
      const haystack = `${item.reference} ${item.title || ""}`.toLowerCase();
      const match = keywords.every(keyword => haystack.includes(keyword));
      return match && (!imageFilter.dataset.value || effectiveImageState(item) === imageFilter.dataset.value) && (!workspaceFilter.dataset.value || item.workspace_state === workspaceFilter.dataset.value);
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
      const imageStatus = fragment.querySelector(".image-status");
      status(imageStatus, effectiveImageState(competition));
      const fallbackSources = Object.values(competition.images || {}).filter(image => image.fallback).map(image => image.fallback_source);
      if (fallbackSources.length) imageStatus.title = `Using fallback: ${fallbackSources.join(", ")}`;
      status(fragment.querySelector(".workspace-status"), competition.workspace_state);
      status(fragment.querySelector(".health-status"), competition.service_health);
      const actions = fragment.querySelector(".actions");
      if (competition.workspace_state === "running") {
        actions.append(actionButton("Open", "open", "primary"), actionButton("Copy link", "copy-link"), actionButton("Stop", "stop"), actionButton("Restart", "restart"));
      } else if (competition.workspace_state === "stopped") {
        actions.append(actionButton("Start", "start", "primary"), actionButton("Recreate", "recreate"));
      } else if (competition.workspace_state === "ready") {
        actions.append(actionButton("Play", "play", "primary"), actionButton("Recreate", "recreate"));
      } else {
        actions.append(actionButton("Play", "play", "primary"), actionButton("Pull", "pull"));
      }
      if (competition.image_state === "ready" && !["running", "stopped"].includes(competition.workspace_state)) {
        actions.append(actionButton("Remove images", "delete-image", "danger"));
      }
      if (competition.workspace_state !== "missing") actions.append(actionButton("Delete workspace", "delete-menu", "danger"));
      const busy = ["queued", "running"].includes(competition.operation?.status);
      for (const button of actions.querySelectorAll("button")) button.disabled = busy;
      rows.append(fragment);
      if (competition.operation) showOperation(competition.reference, competition.operation);
    }
    const end = Math.min(start + visible.length, matches.length);
    count.textContent = matches.length ? `${start + 1}–${end} of ${matches.length} competitions` : "0 competitions";
    empty.hidden = matches.length !== 0 || !state.loaded;
    if (!matches.length && state.loaded) {
      const filtered = Boolean(keywords.length || imageFilter.dataset.value || workspaceFilter.dataset.value);
      if (state.refreshFailed && !state.competitions.length) {
        emptyTitle.textContent = "Refresh failed";
        emptyMessage.textContent = "The saved competition list could not be refreshed.";
        emptyAction.textContent = "Retry";
        emptyAction.dataset.action = "retry";
        emptyAction.hidden = false;
      } else if (state.loginRequired && !state.competitions.length) {
        emptyTitle.textContent = "Nitro is disconnected";
        emptyMessage.textContent = "Sign in to load your competitions. Local workspaces remain available.";
        emptyAction.textContent = "Sign in";
        emptyAction.dataset.action = "sign-in";
        emptyAction.hidden = false;
      } else if (filtered) {
        emptyTitle.textContent = "No matching competitions";
        emptyMessage.textContent = "No saved competition matches the current filters.";
        emptyAction.textContent = "Clear filters";
        emptyAction.dataset.action = "clear-filters";
        emptyAction.hidden = false;
      } else {
        emptyTitle.textContent = "No competitions yet";
        emptyMessage.textContent = "Refresh to synchronize Nitro or start one with naij play.";
        emptyAction.hidden = true;
      }
    }
    for (const pagination of paginations) {
      pagination.hidden = pageCount === 1;
      pagination.querySelector('[data-page-action="previous"]').disabled = state.page === 1;
      pagination.querySelector('[data-page-action="next"]').disabled = state.page === pageCount;
      pagination.querySelector("[data-page-status]").textContent = `Page ${state.page} of ${pageCount}`;
    }
    restoreRowFocus(priorFocus);
  }

  async function load({ refresh = false, cached = false, silent = false } = {}) {
    if (!silent) document.querySelector("#manager-status").textContent = refresh ? "Refreshing" : "Loading cache";
    try {
      const query = refresh ? "?refresh=true" : cached ? "?cached=true" : "";
      const value = await api(`/nitro/api/v1/competitions${query}`);
      state.competitions = value.competitions;
      state.loaded = true;
      state.refreshFailed = false;
      state.loginRequired = value.login_sync_required;
      document.querySelector("#sync-notice").hidden = !value.login_sync_required;
      document.querySelector("#disconnect-nitro").disabled = value.login_sync_required;
      if (value.login_sync_required && !silent && !loginDialog.open) {
        loginDialog.showModal();
        loginUsername.focus();
      }
      if (!silent) document.querySelector("#manager-status").textContent = "Manager healthy";
      render();
    } catch (error) {
      state.loaded = true;
      state.refreshFailed = true;
      document.querySelector("#manager-status").textContent = "Refresh failed";
      showAlert(error.message, () => load({ refresh, cached, silent: false }));
      render();
    }
  }

  async function startAction(reference, action, options = {}) {
    const [org, competition] = reference.split("/");
    const value = await api(`/nitro/api/v1/competitions/${org}/${competition}/actions/${action}`, { method: "POST", body: JSON.stringify(options) });
    const current = state.competitions.find(item => item.reference === reference);
    if (!current) return;
    current.operation = value.operation;
    state.competitions = [current, ...state.competitions.filter(item => item !== current)];
    state.page = 1;
    render();
  }

  async function disconnectNitro() {
    await api("/nitro/api/v1/credentials", { method: "DELETE" });
    disconnectDialog.close();
    await load({ cached: true, silent: true });
    if (!loginDialog.open) loginDialog.showModal();
    loginUsername.focus();
  }

  async function logoutBrowser() {
    await api("/nitro/api/v1/logout", { method: "POST" });
    window.location.assign("/nitro/");
  }

  function updateOperation(operationRow, operation) {
    const active = ["queued", "running"].includes(operation.status);
    const failed = ["failed", "interrupted"].includes(operation.status);
    operationRow.hidden = false;
    operationRow.dataset.status = operation.status;
    operationRow.dataset.operationId = operation.id;
    operationRow.querySelector(".operation-stage").textContent = failed ? "Error" : operation.stage;
    const message = operationRow.querySelector(".operation-message");
    const elapsed = active && operation.message.match(/^(.*) \((\d+)s elapsed\)$/);
    if (elapsed) {
      if (operationRow.dataset.elapsedPrefix !== elapsed[1]) {
        operationRow.dataset.elapsedPrefix = elapsed[1];
        operationRow.dataset.elapsedBase = elapsed[2];
        operationRow.dataset.elapsedAt = Date.now();
      }
      updateElapsedClock(operationRow);
    } else {
      delete operationRow.dataset.elapsedPrefix;
      delete operationRow.dataset.elapsedBase;
      delete operationRow.dataset.elapsedAt;
      message.textContent = operation.message;
    }
    const progress = operationRow.querySelector("progress");
    if (active) progress.removeAttribute("value");
    else progress.value = 100;
    operationRow.querySelector(".operation-cancel").hidden = !active;
    operationRow.querySelector(".operation-dismiss").hidden = active;
    operationRow.querySelector(".operation-logs").textContent = (operation.error?.logs || []).join("\n") || "No diagnostic logs.";
  }

  function updateElapsedClock(operationRow) {
    const elapsedAt = Number(operationRow.dataset.elapsedAt);
    if (!elapsedAt) return;
    const elapsed = Number(operationRow.dataset.elapsedBase) + Math.floor((Date.now() - elapsedAt) / 1000);
    operationRow.querySelector(".operation-message").textContent = `${operationRow.dataset.elapsedPrefix} (${elapsed}s elapsed)`;
  }

  function updateElapsedClocks() {
    for (const operationRow of rows.querySelectorAll(".operation-row[data-elapsed-at]")) updateElapsedClock(operationRow);
  }

  function showOperation(reference, operation) {
    const operationRow = [...rows.querySelectorAll(".operation-row")].find(node => node.dataset.operationFor === reference);
    if (!operationRow || !operation) return;
    if (state.dismissedOperations.has(operation.id)) return;
    const competition = state.competitions.find(item => item.reference === reference);
    if (competition) competition.operation = operation;
    updateOperation(operationRow, operation);
    if (operation.status === "complete") scheduleAutoDismiss(operation, operationRow);
    if (["complete", "failed", "cancelled", "interrupted"].includes(operation.status)) return;
    const operationId = operation.id;
    if (state.operationTimers.has(operationId)) return;
    const timer = setInterval(async () => {
      try {
        const latest = await api(`/nitro/api/v1/operations/${operationId}`);
        if (state.operationTimers.get(operationId) !== timer) return;
        const previousImageState = competition && effectiveImageState(competition);
        if (competition) competition.operation = latest;
        const imageStateChanged = competition && previousImageState !== effectiveImageState(competition);
        if (imageStateChanged) render();
        else updateOperation(operationRow, latest);
        if (["complete", "failed", "cancelled", "interrupted"].includes(latest.status)) {
          clearInterval(timer);
          state.operationTimers.delete(operationId);
          if (latest.status === "complete" && !imageStateChanged) scheduleAutoDismiss(latest, operationRow);
          await load({ cached: true, silent: true });
        }
      } catch (error) {
        if (state.operationTimers.get(operationId) !== timer) return;
        clearInterval(timer);
        state.operationTimers.delete(operationId);
        operationRow.dataset.status = "failed";
        operationRow.querySelector(".operation-stage").textContent = "Error";
        operationRow.querySelector(".operation-message").textContent = error.message;
        operationRow.querySelector("progress").value = 100;
        operationRow.querySelector(".operation-dismiss").hidden = false;
        showAlert(error.message, () => load({ cached: true }));
      }
    }, 700);
    state.operationTimers.set(operationId, timer);
  }

  rows.addEventListener("click", async event => {
    const cancel = event.target.closest(".operation-cancel");
    if (cancel) {
      const operationRow = cancel.closest(".operation-row");
      cancel.disabled = true;
      delete operationRow.dataset.elapsedPrefix;
      delete operationRow.dataset.elapsedBase;
      delete operationRow.dataset.elapsedAt;
      operationRow.querySelector(".operation-stage").textContent = "Cancelling";
      operationRow.querySelector(".operation-message").textContent = "Stopping operation";
      try {
        await api(`/nitro/api/v1/operations/${operationRow.dataset.operationId}/cancel`, { method: "POST" });
      } catch (error) {
        cancel.disabled = false;
        const operationId = operationRow.dataset.operationId;
        showAlert(error.message, () => api(`/nitro/api/v1/operations/${operationId}/cancel`, { method: "POST" }));
      }
      return;
    }
    const dismiss = event.target.closest(".operation-dismiss");
    if (dismiss) {
      const operationRow = dismiss.closest(".operation-row");
      dismissOperation(operationRow.dataset.operationId);
      return;
    }
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const reference = button.closest(".competition-row").dataset.reference;
    button.disabled = true;
    try {
      if (button.dataset.action === "open") {
        window.open(
          jupyterUrl(reference),
          "_blank",
          "noopener",
        );
      } else if (button.dataset.action === "copy-link") {
        await copyJupyterLink(button, reference);
      } else if (button.dataset.action === "delete-menu") {
        state.deleteRef = reference;
        document.querySelector("#delete-reference").textContent = reference;
        confirmation.value = "";
        document.querySelector("#delete-error").textContent = "";
        dialog.showModal();
        confirmation.focus();
      } else if (button.dataset.action === "delete-image") {
        state.deleteRef = reference;
        document.querySelector("#remove-image-reference").textContent = reference;
        removeImageDialog.showModal();
      } else {
        await startAction(reference, button.dataset.action);
      }
    } catch (error) {
      const action = button.dataset.action;
      showAlert(error.message, action === "copy-link" ? () => copyJupyterLink(button, reference) : () => startAction(reference, action));
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
    const reference = state.deleteRef;
    dialog.close();
    try {
      await startAction(reference, "delete-workspace", { confirm_ref: reference });
    } catch (error) {
      showAlert(error.message, () => startAction(reference, "delete-workspace", { confirm_ref: reference }));
    }
  });

  document.querySelector("#remove-image-confirm").addEventListener("click", async () => {
    const reference = state.deleteRef;
    removeImageDialog.close();
    try {
      await startAction(reference, "delete-image");
    } catch (error) {
      showAlert(error.message, () => startAction(reference, "delete-image"));
    }
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
      await load({ refresh: true });
    } catch (error) {
      loginPassword.value = "";
      loginError.textContent = error.message;
      loginPassword.focus();
    }
  });

  [imageFilter, workspaceFilter].forEach(setupFilterMenu);
  document.addEventListener("pointerdown", event => {
    for (const [menu, close] of filterClosers) {
      if (!menu.contains(event.target)) close();
    }
  });
  [search, imageFilter, workspaceFilter].forEach(node => node.addEventListener("input", () => {
    state.page = 1;
    render();
  }));
  for (const pagination of paginations) {
    pagination.addEventListener("click", event => {
      const action = event.target.closest("[data-page-action]")?.dataset.pageAction;
      if (!action) return;
      state.page += action === "next" ? 1 : -1;
      render();
    });
  }
  document.querySelector("#refresh").addEventListener("click", () => load({ refresh: true }));
  emptyAction.addEventListener("click", () => {
    if (emptyAction.dataset.action === "clear-filters") {
      search.value = "";
      for (const filter of [imageFilter, workspaceFilter]) {
        filter.querySelector('.filter-option[data-value=""]').click();
      }
      render();
    } else if (emptyAction.dataset.action === "retry") {
      load({ refresh: true });
    } else if (emptyAction.dataset.action === "sign-in") {
      document.querySelector("#nitro-login-open").click();
    }
  });
  liveAlertRetry.addEventListener("click", async () => {
    const retry = state.alertRetry;
    if (!retry) return;
    liveAlertRetry.disabled = true;
    try {
      await retry();
      hideAlert();
    } catch (error) {
      showAlert(error.message, retry);
    } finally {
      liveAlertRetry.disabled = false;
    }
  });
  document.querySelector("#live-alert-dismiss").addEventListener("click", hideAlert);
  document.querySelector("#disconnect-nitro").addEventListener("click", () => disconnectDialog.showModal());
  document.querySelector("#disconnect-confirm").addEventListener("click", async () => {
    try {
      await disconnectNitro();
    } catch (error) {
      showAlert(error.message, disconnectNitro);
    }
  });
  document.querySelector("#browser-logout").addEventListener("click", async () => {
    try {
      await logoutBrowser();
    } catch (error) {
      showAlert(error.message, logoutBrowser);
    }
  });
  document.querySelector("#clear-operations").addEventListener("click", () => {
    for (const competition of state.competitions) {
      const operation = competition.operation;
      if (operation && ["complete", "failed", "cancelled", "interrupted"].includes(operation.status)) {
        dismissOperation(operation.id);
      }
    }
    render();
  });
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
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/nitro/assets/sw.js", { scope: "/nitro/" }).catch(() => {});
  }
  function connectEvents() {
    if (state.events) return;
    state.events = new EventSource("/nitro/api/v1/events");
    const synchronize = () => {
      clearTimeout(state.syncTimer);
      state.syncTimer = setTimeout(() => load({ cached: true, silent: true }), 120);
    };
    state.events.addEventListener("sync", synchronize);
    state.events.addEventListener("refresh", synchronize);
    state.events.addEventListener("error", () => {
      document.querySelector("#manager-status").textContent = "Reconnecting";
    });
    state.events.addEventListener("open", () => {
      document.querySelector("#manager-status").textContent = "Manager healthy";
    });
  }
  setInterval(updateElapsedClocks, 100);
  load().then(() => load({ refresh: true })).finally(connectEvents);
})();
