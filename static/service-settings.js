(() => {
  "use strict";

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
  }[c]));

  function ensureStyles() {
    if (document.getElementById("wp-service-settings-style")) return;
    const style = document.createElement("style");
    style.id = "wp-service-settings-style";
    style.textContent = `
      #wp-service-settings { position:fixed; inset:0; z-index:10000; display:none; align-items:center; justify-content:center; background:rgba(0,0,0,.58); backdrop-filter:blur(8px); padding:24px; }
      #wp-service-settings.open { display:flex; }
      #wp-service-settings .wp-card { width:min(760px,96vw); max-height:90vh; overflow:auto; background:var(--panel,#111820); color:var(--text,#eef2f6); border:1px solid rgba(255,255,255,.1); border-radius:18px; box-shadow:0 24px 80px rgba(0,0,0,.45); padding:24px; }
      #wp-service-settings .wp-head { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; margin-bottom:20px; }
      #wp-service-settings h2 { margin:0 0 5px; }
      #wp-service-settings .wp-sub { opacity:.65; font-size:13px; }
      #wp-service-settings .wp-grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
      #wp-service-settings label { display:flex; flex-direction:column; gap:7px; font-size:12px; font-weight:600; }
      #wp-service-settings input,#wp-service-settings select,#wp-service-settings textarea { width:100%; box-sizing:border-box; border:1px solid rgba(255,255,255,.12); border-radius:9px; background:rgba(255,255,255,.045); color:inherit; padding:10px 11px; font:inherit; }
      #wp-service-settings textarea { min-height:120px; resize:vertical; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
      #wp-service-settings .wp-full { grid-column:1/-1; }
      #wp-service-settings .wp-actions { display:flex; gap:9px; justify-content:flex-end; margin-top:18px; flex-wrap:wrap; }
      #wp-service-settings .wp-status { margin-top:12px; min-height:18px; font-size:12px; opacity:.8; }
      #wp-service-settings .wp-onboarding { margin:0 0 18px; padding:13px; border:1px solid rgba(255,255,255,.09); border-radius:12px; background:rgba(255,255,255,.025); }
      #wp-service-settings .wp-steps { display:flex; gap:7px; flex-wrap:wrap; margin-top:9px; }
      #wp-service-settings .wp-step { padding:5px 8px; border-radius:999px; font-size:10px; border:1px solid rgba(255,255,255,.1); opacity:.65; }
      #wp-service-settings .wp-step.done { opacity:1; border-color:rgba(110,231,183,.45); color:#6ee7b7; }
      #wp-service-settings .wp-connectors { margin-top:18px; padding-top:18px; border-top:1px solid rgba(255,255,255,.08); }
      #wp-service-settings .wp-connector-list { display:grid; gap:8px; margin-top:10px; }
      #wp-service-settings .wp-connector-row { display:flex; justify-content:space-between; gap:10px; align-items:center; padding:9px 10px; border:1px solid rgba(255,255,255,.08); border-radius:10px; font-size:11px; }
      #wp-service-settings .wp-connector-meta { opacity:.62; }
      #wp-service-settings .wp-connector-actions { display:flex; gap:6px; flex-wrap:wrap; }
      #wp-service-settings .wp-note { font-size:11px; opacity:.58; line-height:1.45; }
      @media(max-width:650px){ #wp-service-settings .wp-grid{grid-template-columns:1fr;} #wp-service-settings .wp-full{grid-column:auto;} }
    `;
    document.head.appendChild(style);
  }

  function mount() {
    ensureStyles();
    if (document.getElementById("wp-service-settings")) return;

    const root = document.createElement("div");
    root.id = "wp-service-settings";
    root.innerHTML = `
      <div class="wp-card" role="dialog" aria-modal="true" aria-labelledby="wp-service-settings-title">
        <div class="wp-head">
          <div>
            <h2 id="wp-service-settings-title">Workspace configuration</h2>
            <div class="wp-sub">Tenant-scoped LLM credentials and customer schema mapping</div>
          </div>
          <button type="button" class="icon-btn" id="wp-settings-close" aria-label="Close">×</button>
        </div>
        <div class="wp-onboarding">
          <div><b>Customer onboarding</b><div class="wp-sub">Connect → understand schema → confirm mapping → ingest → investigate → learn</div></div>
          <div class="wp-steps" id="wp-onboarding-steps"></div>
        </div>
        <div class="wp-grid">
          <label>LLM provider
            <select id="wp-llm-provider">
              <option value="ollama">Ollama</option>
              <option value="openai">OpenAI</option>
              <option value="openai_compatible">OpenAI-compatible</option>
            </select>
          </label>
          <label>Model
            <input id="wp-llm-model" placeholder="llama3.1:8b">
          </label>
          <label class="wp-full">Base URL
            <input id="wp-llm-base" placeholder="https://api.openai.com/v1">
          </label>
          <label class="wp-full">API key
            <input id="wp-llm-key" type="password" autocomplete="new-password" placeholder="Leave blank to keep the saved key">
            <span class="wp-note">The key is encrypted server-side and is never returned to the browser after saving.</span>
          </label>
          <label class="wp-full">Canonical schema mapping
            <textarea id="wp-schema-map" spellcheck="false" placeholder='{"timestamp":"time","service":"application","message":"exception"}'></textarea>
            <span class="wp-note">Map WayPoint canonical fields to your customer's source fields. Use “Infer” on a sample payload first.</span>
          </label>
          <label class="wp-full">Sample payload for mapping inference
            <textarea id="wp-schema-sample" spellcheck="false" placeholder='{"time":"2026-10-02T10:00:00Z","application":"payments-api","exception":"DB timeout","duration":842}'></textarea>
          </label>
        </div>
        <div class="wp-connectors">
          <div><b>Evidence connectors</b><div class="wp-sub">Pull JSON from a customer endpoint or receive signed webhook events.</div></div>
          <div class="wp-grid" style="margin-top:10px">
            <label>Connector name<input id="wp-connector-name" placeholder="Production incident feed"></label>
            <label>Source type<select id="wp-connector-source"><option value="incidents">Incidents</option><option value="logs">Logs</option><option value="metrics">Metrics</option><option value="traces">Traces</option></select></label>
            <label>Connector type<select id="wp-connector-kind"><option value="webhook">Signed webhook</option><option value="http_json">HTTP JSON pull</option></select></label>
            <label>HTTP method<select id="wp-connector-method"><option>GET</option><option>POST</option></select></label>
            <label class="wp-full">Endpoint URL<input id="wp-connector-url" placeholder="https://customer.example.com/observability/incidents"></label>
            <label class="wp-full">Bearer token<input id="wp-connector-token" type="password" autocomplete="new-password" placeholder="Optional"></label>
            <label class="wp-full">Payload path<input id="wp-connector-path" placeholder="data.items"></label>
          </div>
          <div class="wp-actions">
            <button type="button" class="btn btn-ghost" id="wp-connector-test">Test selected connector</button>
            <button type="button" class="btn btn-primary" id="wp-connector-add">Add connector</button>
          </div>
          <div class="wp-connector-list" id="wp-connector-list"></div>
        </div>
        <div class="wp-actions">
          <button type="button" class="btn btn-ghost" id="wp-schema-infer">Infer mapping</button>
          <button type="button" class="btn btn-ghost" id="wp-schema-propose">AI propose mapping</button>
          <button type="button" class="btn btn-ghost" id="wp-llm-test">Test LLM connection</button>
          <button type="button" class="btn btn-primary" id="wp-settings-save">Save configuration</button>
        </div>
        <div class="wp-status" id="wp-settings-status" role="status"></div>
      </div>`;
    document.body.appendChild(root);

    const status = (msg, good=false) => {
      const el = document.getElementById("wp-settings-status");
      el.textContent = msg;
      el.style.opacity = "1";
      el.style.color = good ? "#6ee7b7" : "";
    };
    const jsonParse = (id) => {
      try { return JSON.parse(document.getElementById(id).value || "{}"); }
      catch { throw new Error("Sample payload or schema mapping is not valid JSON."); }
    };

    async function api(url, options={}) {
      const response = await fetch(url, { credentials:"same-origin", ...options, headers:{"Content-Type":"application/json", ...(options.headers||{})} });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "Request failed");
      return data;
    }

    async function loadOnboarding() {
      try {
        const data = await api("/service/onboarding");
        document.getElementById("wp-onboarding-steps").innerHTML = (data.stages || []).map(step =>
          `<span class="wp-step ${step.complete ? "done" : ""}">${step.complete ? "✓ " : ""}${esc(step.label)}</span>`
        ).join("");
        const list = document.getElementById("wp-connector-list");
        list.innerHTML = (data.connectors || []).length
          ? data.connectors.map(c => `<div class="wp-connector-row"><div><b>${esc(c.name)}</b><div class="wp-connector-meta">${esc(c.source_type)} · ${esc(c.kind)} · ${esc(c.last_status || "never synced")}</div></div><div class="wp-connector-actions">${c.kind === "http_json" ? `<button class="btn btn-ghost" data-connector-sync="${esc(c.id)}">Sync</button>` : ""}<button class="btn btn-ghost" data-connector-test-id="${esc(c.id)}">Test</button><button class="btn btn-ghost" data-connector-delete="${esc(c.id)}">Delete</button></div></div>`).join("")
          : '<div class="wp-note">No customer connector configured yet.</div>';
        list.querySelectorAll("[data-connector-sync]").forEach(btn => btn.onclick = async () => {
          try { const d = await api(`/service/connectors/${btn.dataset.connectorSync}/sync`, {method:"POST", body:"{}"}); status(`Sync complete: ${d.created} created, ${d.duplicates} duplicates.`, true); await loadOnboarding(); } catch(e) { status(e.message); }
        });
        list.querySelectorAll("[data-connector-test-id]").forEach(btn => btn.onclick = async () => {
          try { const d = await api(`/service/connectors/${btn.dataset.connectorTestId}/test`, {method:"POST", body:"{}"}); status(d.ok ? `Connector reachable. ${d.records} record(s) detected.` : d.error, d.ok); } catch(e) { status(e.message); }
        });
        list.querySelectorAll("[data-connector-delete]").forEach(btn => btn.onclick = async () => {
          if (!confirm("Delete this connector?")) return;
          try { await api(`/service/connectors/${btn.dataset.connectorDelete}`, {method:"DELETE"}); status("Connector deleted.", true); await loadOnboarding(); } catch(e) { status(e.message); }
        });
      } catch (e) { status(e.message); }
    }

    async function load() {
      try {
        const [llm, mapping] = await Promise.all([
          api("/service/llm"),
          api("/service/schema-mapping")
        ]);
        document.getElementById("wp-llm-provider").value = llm.provider || "ollama";
        document.getElementById("wp-llm-model").value = llm.model || "";
        document.getElementById("wp-llm-base").value = llm.base_url || "";
        document.getElementById("wp-schema-map").value = JSON.stringify(mapping.mapping || {}, null, 2);
        status(llm.configured ? "LLM configured for this workspace." : "LLM configuration is incomplete.");
        await loadOnboarding();
      } catch (e) { status(e.message); }
    }

    async function open() {
      root.classList.add("open");
      await load();
    }
    function close() { root.classList.remove("open"); }

    document.getElementById("wp-settings-close").onclick = close;
    root.addEventListener("click", (e) => { if (e.target === root) close(); });

    document.getElementById("wp-settings-save").onclick = async () => {
      try {
        const mapping = jsonParse("wp-schema-map");
        const body = {
          provider: document.getElementById("wp-llm-provider").value,
          model: document.getElementById("wp-llm-model").value.trim(),
          base_url: document.getElementById("wp-llm-base").value.trim() || null,
          api_key: document.getElementById("wp-llm-key").value || null
        };
        await api("/service/llm", {method:"PUT", body:JSON.stringify(body)});
        await api("/service/schema-mapping", {method:"PUT", body:JSON.stringify({mapping})});
        document.getElementById("wp-llm-key").value = "";
        status("Workspace configuration saved.", true);
      } catch (e) { status(e.message); }
    };

    document.getElementById("wp-schema-propose").onclick = async () => {
      try {
        const sample = jsonParse("wp-schema-sample");
        const data = await api("/service/schema-mapping/propose", {method:"POST", body:JSON.stringify({sample})});
        document.getElementById("wp-schema-map").value = JSON.stringify(data.mapping || {}, null, 2);
        status(data.source === "llm+deterministic" ? "AI proposal ready. Review and save it." : "Deterministic proposal ready. Review and save it.", true);
      } catch (e) { status(e.message); }
    };

    document.getElementById("wp-schema-infer").onclick = async () => {
      try {
        const sample = jsonParse("wp-schema-sample");
        const data = await api("/service/schema-mapping/infer", {method:"POST", body:JSON.stringify(sample)});
        document.getElementById("wp-schema-map").value = JSON.stringify(data.mapping || {}, null, 2);
        status("Mapping inferred. Review it and save it for this workspace.", true);
      } catch (e) { status(e.message); }
    };

    document.getElementById("wp-connector-add").onclick = async () => {
      try {
        const kind = document.getElementById("wp-connector-kind").value;
        const body = {
          name: document.getElementById("wp-connector-name").value.trim(),
          kind,
          source_type: document.getElementById("wp-connector-source").value,
          endpoint_url: kind === "http_json" ? document.getElementById("wp-connector-url").value.trim() || null : null,
          http_method: document.getElementById("wp-connector-method").value,
          auth_token: document.getElementById("wp-connector-token").value || null,
          payload_path: document.getElementById("wp-connector-path").value.trim() || null
        };
        if (!body.name) throw new Error("Connector name is required.");
        await api("/service/connectors", {method:"POST", body:JSON.stringify(body)});
        document.getElementById("wp-connector-token").value = "";
        status("Connector added.", true);
        await loadOnboarding();
      } catch (e) { status(e.message); }
    };

    document.getElementById("wp-connector-test").onclick = async () => {
      try {
        const kind = document.getElementById("wp-connector-kind").value;
        if (kind === "webhook") { status("Webhook is ready to receive signed events.", true); return; }
        const body = {
          name: document.getElementById("wp-connector-name").value.trim() || "Connection test",
          kind,
          source_type: document.getElementById("wp-connector-source").value,
          endpoint_url: document.getElementById("wp-connector-url").value.trim(),
          http_method: document.getElementById("wp-connector-method").value,
          auth_token: document.getElementById("wp-connector-token").value || null,
          payload_path: document.getElementById("wp-connector-path").value.trim() || null
        };
        const created = await api("/service/connectors", {method:"POST", body:JSON.stringify(body)});
        const result = await api(`/service/connectors/${created.id}/test`, {method:"POST", body:"{}"});
        await api(`/service/connectors/${created.id}`, {method:"DELETE"});
        status(result.ok ? `Connection verified. ${result.records} record(s) detected.` : result.error, result.ok);
      } catch (e) { status(e.message); }
    };

    document.getElementById("wp-llm-test").onclick = async () => {
      try {
        status("Testing connection…");
        const data = await api("/service/llm/test", {method:"POST", body:"{}"});
        status(data.ok ? "LLM connection verified." : "LLM connection failed.", data.ok);
      } catch (e) { status(e.message); }
    };

    const settingsButton = document.getElementById("btn-settings");
    if (settingsButton) {
      settingsButton.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopImmediatePropagation();
        open();
      }, true);
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
})();