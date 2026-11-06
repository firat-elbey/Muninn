# Experiment 1: recall quality at a fixed context budget

corpus: 40 notes; questions: 18; budget: 900 tokens; k=5; answerer: claude haiku

| condition | accuracy | unknown | wrong | avg pack tokens |
|---|---|---|---|---|
| none | 0/18 | 18 | 0 | 0 |
| naive | 17/18 | 1 | 0 | 498 |
| format | 17/18 | 1 | 0 | 503 |
| muninn | 17/18 | 1 | 0 | 550 |
| dump | 13/18 | 5 | 0 | 898 |

## By question kind (accuracy)

| condition | assoc | correction | link | ops | pinned |
|---|---|---|---|---|---|
| none | 0/2 | 0/7 | 0/3 | 0/4 | 0/2 |
| naive | 1/2 | 7/7 | 3/3 | 4/4 | 2/2 |
| format | 1/2 | 7/7 | 3/3 | 4/4 | 2/2 |
| muninn | 1/2 | 7/7 | 3/3 | 4/4 | 2/2 |
| dump | 1/2 | 7/7 | 1/3 | 2/4 | 2/2 |

## Failures (context-bearing conditions)

- **naive** [assoc] You are preparing the deploy of the aurora worker. Which host do you watch during rollout? -> `UNKNOWN

The context describes Aurora's architecture and infrastructure components (database host, ports, back`
- **format** [assoc] You are preparing the deploy of the aurora worker. Which host do you watch during rollout? -> `UNKNOWN`
- **muninn** [assoc] You are preparing the deploy of the aurora worker. Which host do you watch during rollout? -> `UNKNOWN

The context describes aurora's infrastructure (DB host at 192.0.2.10, queue via nats, etc.) but does `
- **dump** [link] Roughly how long does a full database restore take? -> `UNKNOWN`
- **dump** [link] Which bucket do the ml experiment artifacts land in? -> `UNKNOWN`
- **dump** [ops] What are the steps to onboard a new aurora service? -> `UNKNOWN`
- **dump** [ops] Who is in the oncall rotation? -> `UNKNOWN`
- **dump** [assoc] You are preparing the deploy of the aurora worker. Which host do you watch during rollout? -> `UNKNOWN

The context provided contains infrastructure decisions about database, queues, monitoring, and variou`
