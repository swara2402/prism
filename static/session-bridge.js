/* WayPoint browser session bridge.
 * Loaded before the legacy console bundle so the UI uses the HttpOnly
 * workspace session instead of localStorage API keys.
 */
(() => {
  "use strict";
  try { localStorage.removeItem("prism_api_key"); } catch (_) {}

  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const opts = { ...init, credentials: "same-origin", headers: new Headers(init.headers || {}) };
    opts.headers.delete("X-API-Key");
    const response = await nativeFetch(input, opts);
    const url = typeof input === "string" ? input : input?.url || "";
    if (response.status === 401 && !url.includes("/auth/login") && !url.includes("/auth/me")) {
      window.location.replace("/login");
    }
    return response;
  };

  const esc = (value) => String(value ?? "").replace(/[&<>\"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'\"':"&quot;","'":"&#39;"}[c]));

  function applyBranding() {
    document.title = "WayPoint — Incident Command Console";
    document.querySelectorAll(".brand-name").forEach(el => { el.textContent = "WayPoint"; });
    document.querySelectorAll(".brand-sub").forEach(el => { el.textContent = "INCIDENT INTELLIGENCE"; });
  }

  function mountEpistemicLegend() {
    if (document.querySelector("#waypoint-epistemic-legend")) return;
    const content = document.querySelector(".content");
    if (!content) return;
    const legend = document.createElement("aside");
    legend.id = "waypoint-epistemic-legend";
    legend.setAttribute("aria-label", "WayPoint evidence certainty guide");
    legend.innerHTML = `
      <span class="wp-legend-title">HOW TO READ THIS INVESTIGATION</span>
      <span><b>Observed</b><small>Directly supplied or measured fact</small></span>
      <span><b>Evidence</b><small>Artifact supporting a finding</small></span>
      <span><b>Inference</b><small>Agent or model interpretation</small></span>
      <span><b>Confirmed</b><small>Human-verified outcome</small></span>`;
    content.prepend(legend);
  }

  async function mountSession() {
    applyBranding();
    mountEpistemicLegend();
    try {
      const response = await nativeFetch("/auth/me", { credentials: "same-origin", cache: "no-store" });
      if (!response.ok) { window.location.replace("/login"); return; }
      const session = await response.json();
      window.WAYPOINT_SESSION = session;
      window.PRISM_SESSION = session;
      applyBranding();
      mountEpistemicLegend();
      const actions = document.querySelector("#topbar-actions");
      if (!actions || document.querySelector("#waypoint-session")) return;
      const user = session.user || {};
      const tenant = session.tenant || {};
      const wrap = document.createElement("div");
      wrap.id = "waypoint-session";
      wrap.innerHTML = `<span class="prism-session-copy"><b>${esc(user.email || "User")}</b><small>${esc(tenant.name || "Workspace")} · ${esc(user.role || "viewer")}</small></span><button class="icon-btn" id="prism-logout" type="button" title="Sign out" aria-label="Sign out">↪</button>`;
      actions.prepend(wrap);
      const settings = document.querySelector("#btn-settings");
      if (settings) settings.style.display = "none";
      document.querySelector("#prism-logout")?.addEventListener("click", async () => {
        try { await nativeFetch("/auth/logout", { method: "POST", credentials: "same-origin" }); }
        finally { window.location.replace("/login"); }
      });
    } catch (_) {
      window.location.replace("/login");
    }
  }

  document.addEventListener("DOMContentLoaded", mountSession, { once: true });
})();
