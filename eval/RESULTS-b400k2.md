# Experiment 1: recall quality at a fixed context budget

corpus: 40 notes; questions: 18; budget: 400 tokens; k=2; answerer: claude haiku

| condition | accuracy | unknown | wrong | avg pack tokens |
|---|---|---|---|---|
| none | 1/18 | 17 | 0 | 0 |
| naive | 15/18 | 3 | 0 | 223 |
| format | 15/18 | 3 | 0 | 230 |
| muninn | 16/18 | 2 | 0 | 245 |
| dump | 6/18 | 12 | 0 | 389 |

## By question kind (accuracy)

| condition | assoc | correction | link | ops | pinned |
|---|---|---|---|---|---|
| none | 1/2 | 0/7 | 0/3 | 0/4 | 0/2 |
| naive | 0/2 | 6/7 | 3/3 | 4/4 | 2/2 |
| format | 0/2 | 6/7 | 3/3 | 4/4 | 2/2 |
| muninn | 1/2 | 6/7 | 3/3 | 4/4 | 2/2 |
| dump | 1/2 | 3/7 | 0/3 | 0/4 | 2/2 |

## Failures (context-bearing conditions)

- **naive** [correction] How long are service logs retained? -> `UNKNOWN

The context references a current log retention policy file (`decisions/logs.md`), but the actual rete`
- **naive** [assoc] What channel naming pattern does the worker consume from? -> `UNKNOWN`
- **naive** [assoc] You are preparing the deploy of the aurora worker. Which host do you watch during rollout? -> `UNKNOWN`
- **format** [correction] How long are service logs retained? -> `UNKNOWN

The context references a log retention policy document (`decisions/logs.md`) but does not include the `
- **format** [assoc] What channel naming pattern does the worker consume from? -> `UNKNOWN

The context indicates the worker consumes jobs from a message queue (nats), but does not specify the `
- **format** [assoc] You are preparing the deploy of the aurora worker. Which host do you watch during rollout? -> `UNKNOWN

The context describes the Aurora DB host (192.0.2.10) and supporting services, but does not specify w`
- **muninn** [correction] How long are service logs retained? -> `UNKNOWN`
- **muninn** [assoc] You are preparing the deploy of the aurora worker. Which host do you watch during rollout? -> `UNKNOWN

The context provides the Aurora DB host (192.0.2.10 / db1.aurora.lan) but does not specify which host `
- **dump** [correction] What port does the aurora postgres database listen on? -> `UNKNOWN`
- **dump** [correction] What port does the aurora redis cache run on? -> `UNKNOWN`
- **dump** [correction] What is the wireguard vpn endpoint (host and port) for remote access? -> `UNKNOWN`
- **dump** [correction] Which software serves LAN DNS on the gateway? -> `UNKNOWN`
- **dump** [link] What host (ip or hostname) runs the aurora postgres primary? -> `UNKNOWN`
- **dump** [link] Roughly how long does a full database restore take? -> `UNKNOWN`
- **dump** [link] Which bucket do the ml experiment artifacts land in? -> `UNKNOWN`
- **dump** [ops] Through which host and port does external ssh enter the lan? -> `UNKNOWN`
- **dump** [ops] Which vlan is the iot wifi mapped to? -> `UNKNOWN`
- **dump** [ops] What are the steps to onboard a new aurora service? -> `UNKNOWN`
- **dump** [ops] Who is in the oncall rotation? -> `UNKNOWN`
- **dump** [assoc] You are preparing the deploy of the aurora worker. Which host do you watch during rollout? -> `UNKNOWN`
