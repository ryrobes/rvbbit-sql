(() => {
  "use strict";

  const dashboard = window.RVBBIT_DASHBOARD || {};
  if (!dashboard.slug || dashboard.historical || window.self !== window.top) return;

  const AS_OF_PARAM = "rvbbit_as_of";
  const SCRUB_DEBOUNCE_MS = 1100;
  const openKey = `rvbbit-artifact-lens-open:${dashboard.slug}`;
  const scrollKey = `rvbbit-artifact-lens-scroll:${dashboard.slug}`;
  let activeAsOf = new URL(window.location.href).searchParams.get(AS_OF_PARAM);
  let timeline = null;
  let loading = null;
  let applyTimer = null;

  function sessionGet(key) {
    try {
      return window.sessionStorage.getItem(key);
    } catch {
      return null;
    }
  }

  function sessionSet(key, value) {
    try {
      if (value === null) window.sessionStorage.removeItem(key);
      else window.sessionStorage.setItem(key, value);
    } catch {
      // Storage is only a convenience; the URL remains the source of truth.
    }
  }

  function formatPoint(value, includeTime = true) {
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "Unknown time";
    return new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      year: parsed.getFullYear() === new Date().getFullYear() ? undefined : "numeric",
      hour: includeTime ? "numeric" : undefined,
      minute: includeTime ? "2-digit" : undefined,
    }).format(parsed);
  }

  function closestPoint(points, target) {
    if (!target || !points.length) return points.length - 1;
    const wanted = Date.parse(target);
    if (!Number.isFinite(wanted)) return points.length - 1;
    let best = 0;
    let distance = Number.POSITIVE_INFINITY;
    points.forEach((point, index) => {
      const next = Math.abs(Date.parse(point) - wanted);
      if (next < distance) {
        best = index;
        distance = next;
      }
    });
    return best;
  }

  function restoreScroll() {
    const raw = sessionGet(scrollKey);
    sessionSet(scrollKey, null);
    const y = Number(raw);
    if (!Number.isFinite(y) || y <= 0) return;
    window.addEventListener("load", () => {
      window.setTimeout(() => window.scrollTo({ top: y, behavior: "auto" }), 80);
    }, { once: true });
  }

  async function fetchTimeline() {
    const response = await fetch(
      `/api/d/${encodeURIComponent(dashboard.slug)}/time-travel`,
      { credentials: "same-origin", headers: { accept: "application/json" } },
    );
    const data = await response.json();
    if (!response.ok || data.error) {
      throw new Error(data.error?.message || `Timeline request failed (${response.status})`);
    }
    return data;
  }

  function mount() {
    if (document.querySelector("rvbbit-artifact-lens")) return;

    const host = document.createElement("rvbbit-artifact-lens");
    host.style.cssText = [
      "all:initial",
      "position:fixed",
      "z-index:2147483000",
      "right:max(18px,env(safe-area-inset-right))",
      "bottom:max(18px,env(safe-area-inset-bottom))",
      "width:auto",
      "height:auto",
      "pointer-events:none",
      "color-scheme:dark",
    ].join(";");
    const root = host.attachShadow({ mode: "open" });
    const stylesheet = document.createElement("link");
    stylesheet.rel = "stylesheet";
    stylesheet.href = "/theme/artifact-lens.css";
    root.appendChild(stylesheet);

    const shell = document.createElement("div");
    shell.className = "lens";
    shell.dataset.open = "false";
    shell.dataset.active = activeAsOf ? "true" : "false";
    shell.innerHTML = `
      <section class="panel" aria-label="Artifact data time" aria-hidden="true">
        <header>
          <span class="eyebrow">Artifact lens</span>
          <button class="close" type="button" aria-label="Minimize data time controls">×</button>
          <strong>Data time</strong>
          <small>View this artifact against a retained RVBBIT snapshot.</small>
        </header>
        <div class="loading" role="status">
          <i aria-hidden="true"></i>
          <span>Reading retained history…</span>
        </div>
        <div class="unavailable" hidden>
          <b>Time travel unavailable</b>
          <span class="unavailable-copy"></span>
        </div>
        <div class="controls" hidden>
          <div class="selection">
            <span>Viewing</span>
            <strong class="selection-value">Latest data</strong>
            <small class="selection-note">The authored dashboard is unchanged.</small>
          </div>
          <input class="scrubber" type="range" min="0" max="1" step="1" value="1"
            aria-label="Dashboard data time">
          <div class="range-labels">
            <span class="range-start">Earlier</span>
            <span>Latest</span>
          </div>
          <div class="actions">
            <span class="apply-state" aria-live="polite"></span>
            <button class="latest" type="button">Return to latest</button>
          </div>
          <p class="coverage"></p>
        </div>
        <footer>RVBBIT AS-OF · dashboard code and version stay fixed</footer>
      </section>
      <button class="trigger" type="button" aria-expanded="false" aria-label="Open data time controls">
        <span class="clock" aria-hidden="true">
          <svg viewBox="0 0 24 24">
            <path d="M12 7v5l3 2M5.6 5.7A8.5 8.5 0 1 1 3.5 12"/>
            <path d="M3.5 4.5v5h5"/>
          </svg>
        </span>
        <span class="trigger-copy">
          <b>Data time</b>
          <small>${activeAsOf ? formatPoint(activeAsOf, false) : "Live"}</small>
        </span>
        <i aria-hidden="true"></i>
      </button>`;
    root.appendChild(shell);
    document.body.appendChild(host);

    const panel = root.querySelector(".panel");
    const trigger = root.querySelector(".trigger");
    const close = root.querySelector(".close");
    const loadingNode = root.querySelector(".loading");
    const unavailable = root.querySelector(".unavailable");
    const unavailableCopy = root.querySelector(".unavailable-copy");
    const controls = root.querySelector(".controls");
    const scrubber = root.querySelector(".scrubber");
    const selectionValue = root.querySelector(".selection-value");
    const selectionNote = root.querySelector(".selection-note");
    const rangeStart = root.querySelector(".range-start");
    const applyState = root.querySelector(".apply-state");
    const latest = root.querySelector(".latest");
    const coverage = root.querySelector(".coverage");
    const triggerStatus = root.querySelector(".trigger-copy small");

    function setOpen(open) {
      shell.dataset.open = String(open);
      panel.setAttribute("aria-hidden", String(!open));
      trigger.setAttribute("aria-expanded", String(open));
      trigger.setAttribute(
        "aria-label",
        open ? "Minimize data time controls" : "Open data time controls",
      );
      sessionSet(openKey, open ? "1" : null);
      if (open) loadTimeline();
    }

    function setActive(value) {
      activeAsOf = value || null;
      dashboard.as_of = activeAsOf;
      shell.dataset.active = activeAsOf ? "true" : "false";
      triggerStatus.textContent = activeAsOf ? formatPoint(activeAsOf, false) : "Live";
    }

    function renderTimeline(data) {
      loadingNode.hidden = true;
      if (!data.eligible || !Array.isArray(data.points) || data.points.length < 2) {
        unavailable.hidden = false;
        unavailableCopy.textContent = data.message || "No common retained history was found.";
        controls.hidden = true;
        return;
      }
      timeline = data;
      unavailable.hidden = true;
      controls.hidden = false;
      scrubber.min = "0";
      scrubber.max = String(data.points.length - 1);
      const index = closestPoint(data.points, activeAsOf);
      scrubber.value = String(index);
      rangeStart.textContent = formatPoint(data.earliest, false);
      coverage.textContent = `${data.table_count} source${data.table_count === 1 ? "" : "s"} · `
        + `${data.point_count} retained data points`;
      renderSelection(data.points[index], Boolean(activeAsOf));
    }

    function renderSelection(point, historical) {
      if (historical) {
        selectionValue.textContent = formatPoint(point);
        selectionNote.textContent = "AS-OF snapshot · dashboard queries are pinned to this point";
      } else {
        selectionValue.textContent = "Latest data";
        selectionNote.textContent = timeline?.latest_refresh
          ? `Most recent retained refresh ${formatPoint(timeline.latest_refresh)}`
          : "The authored dashboard is unchanged.";
      }
    }

    async function loadTimeline() {
      if (timeline || loading) return loading;
      loadingNode.hidden = false;
      unavailable.hidden = true;
      controls.hidden = true;
      loading = (async () => {
        try {
          let data = await fetchTimeline();
          if (data.code === "NO_QUERY_SOURCES") {
            await new Promise((resolve) => window.setTimeout(resolve, 700));
            data = await fetchTimeline();
          }
          renderTimeline(data);
        } catch (error) {
          renderTimeline({
            eligible: false,
            message: error instanceof Error ? error.message : "The timeline could not be loaded.",
          });
        } finally {
          loading = null;
        }
      })();
      return loading;
    }

    async function applyAsOf(point) {
      window.clearTimeout(applyTimer);
      applyTimer = null;
      const next = new URL(window.location.href);
      if (point) next.searchParams.set(AS_OF_PARAM, point);
      else next.searchParams.delete(AS_OF_PARAM);
      if (next.href === window.location.href) {
        applyState.textContent = "";
        return;
      }

      applyState.textContent = "Refreshing all queries…";
      sessionSet(openKey, "1");
      sessionSet(scrollKey, String(window.scrollY));
      setActive(point);
      const refresh = window.RVBBIT_DASHBOARD?.refresh;
      if (typeof refresh === "function") {
        window.history.replaceState({}, "", next);
        try {
          await refresh({ as_of: point || null });
          applyState.textContent = "Updated";
          renderSelection(point || timeline.points[timeline.points.length - 1], Boolean(point));
          return;
        } catch {
          // A dashboard-provided refresh is optional; a full reload is the safe fallback.
        }
      }
      window.location.assign(next.href);
    }

    function scheduleAsOf(point) {
      window.clearTimeout(applyTimer);
      applyState.textContent = "Waiting for you to stop scrubbing…";
      applyTimer = window.setTimeout(() => applyAsOf(point), SCRUB_DEBOUNCE_MS);
    }

    trigger.addEventListener("click", () => setOpen(shell.dataset.open !== "true"));
    close.addEventListener("click", () => setOpen(false));
    scrubber.addEventListener("input", () => {
      if (!timeline) return;
      const point = timeline.points[Number(scrubber.value)];
      renderSelection(point, true);
      scheduleAsOf(point);
    });
    latest.addEventListener("click", () => {
      if (timeline) {
        scrubber.value = String(timeline.points.length - 1);
        renderSelection(timeline.points[timeline.points.length - 1], false);
      }
      applyAsOf(null);
    });
    root.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && shell.dataset.open === "true") {
        event.preventDefault();
        setOpen(false);
        trigger.focus();
      }
    });

    if (sessionGet(openKey) === "1") setOpen(true);
    restoreScroll();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount, { once: true });
  } else {
    mount();
  }
})();
