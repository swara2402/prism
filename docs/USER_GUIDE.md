# PRISM User Guide

PRISM is designed to answer one question quickly: **what is causing this incident, and what evidence supports that conclusion?**

## 1. Start PRISM

### Local development

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Open `http://localhost:8000`.

For local development, the API can run without an API key. If you configure `API_KEY`, paste it into **Settings** in the console.

### Docker

```bash
docker compose up --build
```

Then open `http://localhost:8000`.

## 2. Investigate an incident

1. Open **Investigate**.
2. Pick a scenario if you want a quick demo.
3. Enter a short incident title.
4. Select severity and incident type.
5. Add affected services.
6. Paste logs, metrics, traces, or context.
7. Click **Run investigation**.

PRISM streams the investigation so you can see which agents finish, when the causal graph is built, and when consensus is reached.

## 3. Read the result

Start with the **Root cause** card.

Then inspect:

- **Confidence**: how strongly the evidence supports the conclusion.
- **Causal chain**: the sequence of related nodes leading to the proposed cause.
- **Alternative hypotheses**: competing explanations considered by PRISM.
- **Agent findings**: what individual analyzers observed.
- **Explanation**: the evidence-based reasoning behind the result.

A high confidence value is not a guarantee. Treat it as a signal and verify the evidence before making a production change.

## 4. Resolve an incident

Open the incident and choose **Mark resolved**.

Record:

- the action taken
- the recovery steps
- whether the resolution was verified

Only confirmed resolutions should become authoritative learning data.

## 5. Explore the console

| Screen | Use it for |
|---|---|
| Overview | Current health and recent incidents |
| Investigate | Start a new investigation |
| Incidents | Browse and inspect incidents |
| Memory | Search historical investigation knowledge |
| Agents | Inspect agent reliability |
| Patterns | Review learned patterns awaiting approval |
| Predictions | Review recurrence/trend forecasts |
| Knowledge Graph | Explore service dependencies |

## 6. When something fails

### API unreachable

Check that the backend is running and that the browser is using the correct origin.

### Unauthorized

Open **Settings**, enter the configured `API_KEY`, and save it for the current browser session.

### Investigation is busy

PRISM deliberately limits concurrent investigations. Wait for an active investigation to finish or run the asynchronous investigation endpoint for queue-based workloads.

### No root cause yet

An investigation can finish in a degraded state if one or more agents fail. Inspect the agent status and evidence rather than treating a partial result as definitive.

## 7. Production rule of thumb

The console is the human-friendly layer. Production deployments should put PRISM behind HTTPS and a reverse proxy/load balancer, keep databases private, configure explicit CORS, protect credentials, and use real identity/RBAC before serving multiple organizations.
