/* PRISM 2.0 browser session bridge.
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

  async function mountSession() {
    try {
      const response = await nativeFetch("/auth/me", { credentials: "same-origin", cache: "no-store" });
      if (!response.ok) { window.location.replace("/login"); return; }
      const session = await response.json();
      window.PRISM_SESSION = session;
      const actions = document.querySelector("#topbar-actions");
      if (!actions || document.querySelector("#prism-session")) return;
      const user = session.user || {};
      const tenant = session.tenant || {};
      const wrap = document.createElement("div");
      wrap.id = "prism-session";
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
