/* WayPoint console hardening patch.
 * Loaded after the legacy bundle. The legacy stream emits a lightweight
 * `verdict` event before the persisted `investigation_result`; this patch
 * deliberately waits for the latter so the browser never presents an
 * abbreviated event as a completed investigation.
 */
(() => {
  "use strict";

  const originalFinish = window.finishPipeline;

  window.startPipeline = async function startPipeline(payload) {
    const pipeline = document.querySelector("#pipeline");
    const stream = document.querySelector("#log-stream");
    if (!pipeline || !stream) return;

    pipeline.innerHTML = [
      ["Persist incident", "create · postgres"],
      ["Dispatch agents", "orchestrator · dynamic tree"],
      ["Causal graph", "builder · findings"],
      ["Confidence", "propagate ×5"],
      ["Consensus", "quorum · reliability"],
      ["Explanation", "explainability engine"],
      ["Learning", "confirmation-gated"],
    ].map(([label, sub]) => `
      <li class="pipeline-step" data-step>
        <span class="pipeline-dot"><svg class="ic"><use href="#i-check"/></svg></span>
        <span><span class="pipe-label">${esc(label)}</span><span class="pipe-sub">${esc(sub)}</span></span>
      </li>`).join("");
    stream.innerHTML = "";

    const started = Date.now();
    const timer = setInterval(() => {
      const s = Math.floor((Date.now() - started) / 1000);
      const clockEl = document.querySelector("#run-clock");
      if (clockEl) clockEl.textContent = `0${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    }, 500);

    const setStep = (idx) => {
      $$("#pipeline .pipeline-step").forEach((el, i) => {
        el.classList.toggle("done", i < idx);
        el.classList.toggle("active", i === idx);
      });
    };
    const log = (text, cls = "") => {
      const line = document.createElement("div");
      line.innerHTML = `<span class="ls-time">[${clock()}]</span> <span class="${cls}">${esc(text)}</span>`;
      stream.appendChild(line);
      stream.scrollTop = stream.scrollHeight;
    };
    const fail = (message) => {
      clearInterval(timer);
      if (typeof failPipeline === "function") failPipeline(new Error(message));
    };

    try {
      setStep(0);
      log("→ POST /incidents/investigate/stream · SSE connected", "ls-acc");
      const response = await fetch(BASE_API_URL + "/incidents/investigate/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error((await response.text()) || `HTTP ${response.status}`);

      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";
      let eventName = null;
      let finalResult = null;

      const handle = (event, data) => {
        switch (event) {
          case "pipeline_started":
            setStep(0); log("pipeline started", "ls-acc"); break;
          case "incident_persisted":
            setStep(1); log(`incident persisted · id=${String(data.incident_id || "").slice(0, 10)}…`, "ls-ok"); break;
          case "agents_dispatched":
            setStep(1); log(`dispatching agents for ${data.incident_type || "incident"}`, "ls-acc"); break;
          case "agent_completed":
            setStep(1);
            log(`${data.agent || "agent"} ▸ [${data.finding_type || "analysis"}] conf=${pct(data.confidence)}%`, data.finding_type === "error" ? "ls-warn" : "ls-ok");
            if (data.summary) log(`  ↳ ${data.summary}`, "ls-dim");
            break;
          case "causal_graph_built":
            setStep(2); log(`causal graph · nodes=${data.nodes_count} · edges=${data.edges_count}`, "ls-acc"); break;
          case "confidence_propagated":
            setStep(3); log(`confidence propagated · candidates=${data.candidate_count}`, "ls-ok"); break;
          case "consensus_reached":
            setStep(4); log(`inferred root cause: "${data.root_cause || "undetermined"}" · support=${pct(data.confidence)}%`, "ls-acc"); break;
          case "explanation_built":
            setStep(5); log("explanation assembled", "ls-ok"); break;
          case "learning_deferred":
            setStep(6); log("learning deferred until a confirmed root cause exists", "ls-warn"); break;
          case "learning_recorded":
            setStep(6); log("confirmed learning recorded", "ls-warn"); break;
          case "verdict":
            // Intentionally ignored as a terminal event. It is a legacy
            // compatibility summary, not the complete persisted result.
            log("received preliminary verdict summary · waiting for final result", "ls-dim");
            break;
          case "investigation_result":
            finalResult = data;
            break;
          case "error":
            throw new Error(data.error || "Investigation stream error");
        }
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split("\n\n");
        buffer = frames.pop() || "";
        for (const frame of frames) {
          let frameEvent = null;
          let frameData = "";
          for (const line of frame.split("\n")) {
            if (line.startsWith("event:")) frameEvent = line.slice(6).trim();
            else if (line.startsWith("data:")) frameData += line.slice(5).trim();
          }
          if (!frameEvent) continue;
          let data = {};
          try { data = JSON.parse(frameData || "{}"); } catch (_) { data = {message: frameData}; }
          handle(frameEvent, data);
        }
      }

      if (!finalResult || !finalResult.incident_id || !finalResult.root_cause) {
        throw new Error("Investigation stream ended without a complete investigation_result event");
      }

      clearInterval(timer);
      $$("#pipeline .pipeline-step").forEach((el) => { el.classList.remove("active"); el.classList.add("done"); });
      originalFinish(finalResult);

      const wrap = document.querySelector("#verdict-wrap");
      if (wrap && !wrap.querySelector(".waypoint-result-truth")) {
        const confirmed = Boolean(finalResult.resolution?.confirmed_root_cause);
        const degraded = finalResult.status === "completed_with_degraded_agents";
        const banner = document.createElement("div");
        banner.className = "waypoint-inference-banner waypoint-result-truth";
        banner.textContent = confirmed
          ? "Confirmed root cause: supported by explicit ground-truth confirmation."
          : degraded
            ? "Inference only. Some agents degraded or failed. Review evidence before confirming the root cause."
            : "Inference only. This root cause is PRISM's supported conclusion, not confirmed ground truth. Confirm it after service recovery or external verification.";
        wrap.prepend(banner);
      }
    } catch (error) {
      clearInterval(timer);
      fail(error.message || "Investigation failed");
    }
  };
})();
