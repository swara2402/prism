/* WayPoint console hardening patch. Wait for the complete persisted investigation result. */
(() => {
  "use strict";

  const originalFinish = window.finishPipeline;
  if (typeof originalFinish !== "function") return;

  window.startPipeline = async function startPipeline(payload) {
    const pipeline = document.querySelector("#pipeline");
    const stream = document.querySelector("#log-stream");
    if (!pipeline || !stream) return;

    const escLocal = (value) => String(value ?? "").replace(/[&<>\"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'\"':"&quot;","'":"&#39;"}[c]));
    const pctLocal = (value) => Number.isFinite(Number(value)) ? Math.round(Number(value) * 100) : 0;
    const clockLocal = () => new Date().toLocaleTimeString([], {hour12:false});

    pipeline.innerHTML = [
      ["Persist incident", "database"], ["Dispatch agents", "investigation tree"],
      ["Causal graph", "findings"], ["Confidence", "propagation"],
      ["Consensus", "support"], ["Explanation", "evidence"], ["Learning", "confirmation-gated"],
    ].map(([label, sub]) => `<li class="pipeline-step"><span class="pipeline-dot"><svg class="ic"><use href="#i-check"/></svg></span><span><span class="pipe-label">${escLocal(label)}</span><span class="pipe-sub">${escLocal(sub)}</span></span></li>`).join("");
    stream.innerHTML = "";

    const started = Date.now();
    const timer = setInterval(() => {
      const clock = document.querySelector("#run-clock");
      if (clock) { const s = Math.floor((Date.now() - started) / 1000); clock.textContent = `0${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }
    }, 500);
    const setStep = (idx) => document.querySelectorAll("#pipeline .pipeline-step").forEach((el, i) => { el.classList.toggle("done", i < idx); el.classList.toggle("active", i === idx); });
    const log = (text, cls = "") => { const line = document.createElement("div"); line.innerHTML = `<span class="ls-time">[${clockLocal()}]</span> <span class="${cls}">${escLocal(text)}</span>`; stream.appendChild(line); stream.scrollTop = stream.scrollHeight; };
    const fail = (message) => { clearInterval(timer); if (typeof window.failPipeline === "function") window.failPipeline(new Error(message)); };

    try {
      setStep(0);
      const response = await fetch((window.BASE_API_URL || "") + "/incidents/investigate/stream", { method:"POST", headers:{"Content-Type":"application/json"}, credentials:"same-origin", body:JSON.stringify(payload) });
      if (!response.ok) throw new Error((await response.text()) || `HTTP ${response.status}`);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let finalResult = null;
      const handle = (event, data) => {
        if (event === "verdict") { log("preliminary verdict received · waiting for persisted result", "ls-dim"); return; }
        if (event === "investigation_result") { finalResult = data; return; }
        if (event === "pipeline_started") { setStep(0); return; }
        if (event === "incident_persisted") { setStep(1); log("incident persisted", "ls-ok"); return; }
        if (event === "agents_dispatched") { setStep(1); log("agents dispatched", "ls-acc"); return; }
        if (event === "agent_completed") { setStep(1); log(`${data.agent || "agent"} · ${data.finding_type || "analysis"} · support ${pctLocal(data.confidence)}%`, data.finding_type === "error" ? "ls-warn" : "ls-ok"); return; }
        if (event === "causal_graph_built") { setStep(2); return; }
        if (event === "confidence_propagated") { setStep(3); return; }
        if (event === "consensus_reached") { setStep(4); log(`inferred root cause · support ${pctLocal(data.confidence)}%`, "ls-acc"); return; }
        if (event === "explanation_built") { setStep(5); return; }
        if (event === "learning_deferred") { setStep(6); log("learning deferred until root cause confirmation", "ls-warn"); return; }
        if (event === "error") throw new Error(data.error || "Investigation stream error");
      };
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream:true});
        const frames = buffer.split("\n\n");
        buffer = frames.pop() || "";
        for (const frame of frames) {
          let event = null, raw = "";
          for (const line of frame.split("\n")) { if (line.startsWith("event:")) event = line.slice(6).trim(); else if (line.startsWith("data:")) raw += line.slice(5).trim(); }
          if (!event) continue;
          let data = {}; try { data = JSON.parse(raw || "{}"); } catch (_) { data = {message:raw}; }
          handle(event, data);
        }
      }
      if (!finalResult || !finalResult.incident_id || !finalResult.root_cause) throw new Error("Stream ended without a complete investigation_result");
      clearInterval(timer);
      document.querySelectorAll("#pipeline .pipeline-step").forEach(el => { el.classList.remove("active"); el.classList.add("done"); });
      originalFinish(finalResult);
      const wrap = document.querySelector("#verdict-wrap");
      if (wrap && !wrap.querySelector(".waypoint-result-truth")) {
        const banner = document.createElement("div");
        banner.className = "waypoint-inference-banner waypoint-result-truth";
        banner.textContent = finalResult.status === "completed_with_degraded_agents" ? "Inference only. Some agents degraded or failed. Review evidence before confirmation." : "Inference only. This root cause is a supported conclusion, not confirmed ground truth.";
        wrap.prepend(banner);
      }
    } catch (error) { fail(error.message || "Investigation failed"); }
  };
})();
