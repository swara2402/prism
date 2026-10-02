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

  function mountStyles() {
    if (document.querySelector("#waypoint-session-styles")) return;
    const style = document.createElement("style");
    style.id = "waypoint-session-styles";
    style.textContent = `
      #waypoint-epistemic-legend{display:flex;gap:10px;align-items:stretch;flex-wrap:wrap;margin:0 0 14px;padding:11px 13px;border:1px solid var(--border,#263143);border-radius:12px;background:var(--panel,#0d121b);font-size:11px}
      #waypoint-epistemic-legend>span{display:flex;flex-direction:column;gap:2px;padding-right:12px;border-right:1px solid var(--border,#263143)}
      #waypoint-epistemic-legend>span:last-child{border-right:0}
      #waypoint-epistemic-legend .wp-legend-title{justify-content:center;color:var(--muted,#8d99aa);font-size:10px;letter-spacing:.08em;font-weight:700}
      #waypoint-epistemic-legend b{color:var(--text,#eef2f7)}
      #waypoint-epistemic-legend small{color:var(--muted,#8d99aa)}
      #waypoint-session{display:flex;align-items:center;gap:8px;margin-right:8px}
      .prism-session-copy{display:flex;flex-direction:column;text-align:right;line-height:1.2}
      .prism-session-copy b{font-size:11px}
      .prism-session-copy small{font-size:10px;color:var(--muted,#8d99aa)}
      @media(max-width:760px){#waypoint-epistemic-legend .wp-legend-title{width:100%;align-items:flex-start}#waypoint-epistemic-legend>span{flex:1;min-width:120px}.prism-session-copy{display:none}}
    `;
    document.head.appendChild(style);
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
    mountStyles();
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
