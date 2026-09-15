"""Generate the synthetic eval corpus: the 'Aurora' homelab knowledge base.

Entirely fictional (TEST-NET IPs, example.com hosts, invented names) so the
corpus is safe to publish. Deterministic: no LLM, no randomness.

Produces:
  corpus/            the bundle (markdown notes, correction pairs, decoys)
  questions.json     factual questions with expected answer substrings
and simulates a usage history in corpus/.muninn/ (touches, outcomes,
supersessions, consolidations) so dynamics have something to work with.
"""

from __future__ import annotations

import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from muninn.dynamics import Dynamics  # noqa: E402
from muninn.store import Bundle  # noqa: E402

CORPUS = os.path.join(HERE, "corpus")

# (path, title, description, tags, body, supersedes)
NOTES = [
    # --- correction pairs: (old, new). Old and new share vocabulary; only
    # the key fact differs. New notes carry `supersedes` frontmatter. ------
    ("infra/db-port-old.md", "Aurora DB port (2025)", "postgres port config",
     "infra,db", "The Aurora postgres instance listens on the default port 5432. "
     "Configured in postgresql.conf on the primary.", ""),
    ("infra/db-port.md", "Aurora DB port", "current postgres port configuration",
     "infra,db", "The Aurora postgres instance listens on port 7433 (moved off the "
     "default in the Jan hardening pass). Configured in postgresql.conf on the "
     "primary. See [[Aurora DB host]] and [[Restore database runbook]].",
     "infra/db-port-old.md"),

    ("infra/redis-old.md", "Redis cache (initial setup)", "redis cache port",
     "infra,cache", "The aurora redis cache runs on the standard port 6379 "
     "with maxmemory 2gb.", ""),
    ("infra/redis.md", "Redis cache", "current cache configuration",
     "infra,cache", "The aurora redis cache runs on port 6390 (custom) with "
     "maxmemory 4gb and allkeys-lru eviction.", "infra/redis-old.md"),

    ("infra/vpn-old.md", "VPN endpoint (wireguard, original)", "vpn endpoint",
     "infra,network", "Remote access via wireguard at vpn.example.com:51820. "
     "Peers get a 10.8.0.0/24 address.", ""),
    ("infra/vpn.md", "VPN endpoint", "current remote-access endpoint",
     "infra,network", "Remote access via wireguard at wg.example.com:51821 "
     "(migrated after the ISP change). Peers get a 10.8.0.0/24 address.",
     "infra/vpn-old.md"),

    ("infra/backup-old.md", "Backup target (NAS)", "backups to the NAS",
     "infra,backup", "Nightly backups go to the NAS at 192.0.2.20 over NFS "
     "under /volume1/backups.", ""),
    ("infra/backup.md", "Backup target", "current backup destination",
     "infra,backup", "Nightly backups go to the minio object store at "
     "192.0.2.30:9000, bucket aurora-backups (moved off the NAS in March). "
     "Used by the [[Restore database runbook]].", "infra/backup-old.md"),

    ("infra/dns-old.md", "DNS resolver (dnsmasq)", "lan dns via dnsmasq",
     "infra,network", "LAN DNS is served by dnsmasq on the gateway with "
     "local zone aurora.lan.", ""),
    ("infra/dns.md", "DNS resolver", "current lan dns setup",
     "infra,network", "LAN DNS is served by unbound on the gateway (replaced "
     "dnsmasq for DNSSEC), local zone aurora.lan.", "infra/dns-old.md"),

    ("decisions/logs-old.md", "Log retention (initial)", "log retention policy",
     "decision,logging", "Service logs are retained for 30 days in loki, then "
     "dropped.", ""),
    ("decisions/logs.md", "Log retention", "current log retention policy",
     "decision,logging", "Service logs are retained for 90 days in loki "
     "(extended for the audit), then dropped.", "decisions/logs-old.md"),

    ("decisions/queue-old.md", "Queue choice (rabbitmq)", "message queue decision",
     "decision,queue", "Async jobs between aurora services go through rabbitmq; "
     "it was the team's prior experience.", ""),
    ("decisions/queue.md", "Queue choice", "current async queue decision",
     "decision,queue", "Async jobs between aurora services go through nats "
     "jetstream (replaced rabbitmq for simpler ops and k/v). Producers use "
     "subject aurora.jobs.*.", "decisions/queue-old.md"),

    # --- singles: infra ---------------------------------------------------
    ("infra/db-host.md", "Aurora DB host", "where postgres runs",
     "infra,db", "The PostgreSQL primary runs on the host at 192.0.2.10 "
     "(hostname db1.aurora.lan), 64gb ram, zfs mirror.", ""),
    ("infra/monitoring.md", "Monitoring stack", "prometheus + grafana",
     "infra,observability", "Prometheus scrapes every service on :9100/:9090 "
     "conventions; grafana at grafana.aurora.lan. Alertmanager routes to the "
     "oncall email.", ""),
    ("infra/bastion.md", "SSH bastion", "how to reach the lan",
     "infra,network", "All external ssh goes through the bastion at "
     "bastion.example.com port 2222, key-only, no agent forwarding.", ""),
    ("infra/tls.md", "TLS cert renewal", "certbot wildcard renewal",
     "infra,tls", "Wildcard cert for *.aurora.lan renews via certbot dns-01 "
     "against the cloud dns api, systemd timer weekly.", ""),
    ("infra/wifi.md", "WiFi VLANs", "ssid to vlan mapping",
     "infra,network", "SSID aurora-main maps to vlan 10, aurora-iot to vlan "
     "30 (isolated), guest to vlan 90.", ""),

    # --- decisions --------------------------------------------------------
    ("decisions/db-choice.md", "Database choice", "why postgres",
     "decision,db", "Postgres over mysql for aurora: jsonb, logical "
     "replication, team familiarity. Revisit only if multi-region.", ""),
    ("decisions/iac.md", "IaC tool", "terraform + ansible split",
     "decision,iac", "Terraform provisions cloud resources; ansible "
     "configures on-prem boxes. No pulumi.", ""),
    ("decisions/secrets.md", "Secrets manager", "where service credentials live",
     "decision,security", "All service secrets live in vault at "
     "vault.aurora.lan; nothing in .env files or the repo. Break-glass "
     "creds in the fireproof safe.", ""),
    ("decisions/access.md", "Access policy", "least privilege, sso everywhere",
     "decision,security", "Every internal service sits behind the sso proxy; "
     "direct port access is firewalled to the admin vlan only.", ""),

    # --- runbooks (link hubs) ----------------------------------------------
    ("runbooks/restore-db.md", "Restore database runbook", "how to restore postgres",
     "runbook,db", "1. Stop writers. 2. Pull the latest basebackup from the "
     "[[Backup target]]. 3. Restore onto [[Aurora DB host]]. 4. Point apps at "
     "[[Aurora DB port]]. 5. Verify with the smoke queries. Takes ~40 min.", ""),
    ("runbooks/rotate-certs.md", "Rotate certs runbook", "manual cert rotation",
     "runbook,tls", "Force-renew via certbot, then restart the ingress. See "
     "[[TLS cert renewal]] for the automated path.", ""),
    ("runbooks/onboard-service.md", "Onboard a service runbook", "new service checklist",
     "runbook", "New aurora service: register in the sso proxy, add "
     "prometheus scrape config, create a vault approle, add the nats subject "
     "to the allowlist, add a grafana dashboard.", ""),
    ("runbooks/incident.md", "Incident checklist", "what to do when things break",
     "runbook,oncall", "Declare in #incidents, snapshot dashboards, check "
     "[[Monitoring stack]] alerts, write the timeline as you go, blameless "
     "review within a week.", ""),
    ("runbooks/upgrade-node.md", "Upgrade a node runbook", "os upgrades",
     "runbook", "Drain the node, snapshot, apply updates, reboot, verify "
     "exporters are back before undraining.", ""),

    # --- projects ------------------------------------------------------------
    ("projects/aurora.md", "Aurora overview", "what aurora is",
     "project", "Aurora is the home platform: api, worker, and ml services "
     "behind the sso proxy. Data in [[Aurora DB host]] postgres, cache in "
     "[[Redis cache]], jobs via [[Queue choice]] nats, backups per "
     "[[Backup target]].", ""),
    ("projects/api.md", "Aurora API", "the http service",
     "project,service", "FastAPI service, uvicorn behind the ingress, "
     "connects to postgres and redis, publishes jobs to nats.", ""),
    ("projects/worker.md", "Aurora worker", "the job consumer",
     "project,service", "Consumes jobs from the message queue, writes results to "
     "postgres, pushes metrics.", ""),
    ("projects/ml.md", "Aurora ML experiments", "the gpu box workloads",
     "project,ml", "Fine-tuning runs on the gpu box; artifacts land in the "
     "minio bucket aurora-ml.", ""),
    ("projects/roadmap.md", "Roadmap 2026", "what's next",
     "project,planning", "Q3: move ingress to the new gateway. Q4: second "
     "postgres replica, evaluate object-storage tiering.", ""),

    # --- people / misc -------------------------------------------------------
    ("people/dana.md", "Dana preferences", "owner working preferences",
     "people", "Dana prefers small diffs, tests before merge, and no direct "
     "pushes to main. Review windows: mornings.", ""),
    ("people/oncall.md", "Oncall rotation", "who carries the pager",
     "people,oncall", "Weekly rotation between dana and robin, handoff "
     "mondays; escalation via the incident checklist.", ""),

    # --- decoys: heavy vocabulary overlap, no operative answers ------------
    ("notes/postgres-tuning.md", "Postgres tuning notes", "general pg tuning",
     "notes,db", "General postgres notes: shared_buffers 25% of ram, "
     "effective_cache_size 50-75%, watch checkpoint spikes, default port "
     "5432 unless hardened, wal compression on. Ports and listen_addresses "
     "belong in postgresql.conf.", ""),
    ("notes/network-cheatsheet.md", "Networking cheatsheet", "handy commands",
     "notes,network", "ss -tlnp for listening ports, dig +short for dns, wg "
     "show for wireguard peers, standard wireguard port is 51820, redis "
     "default port is 6379, nfs mounts via /etc/fstab.", ""),
    ("notes/redis-tips.md", "Redis tips", "general redis notes",
     "notes,cache", "Redis tips: keyspace notifications, maxmemory policies, "
     "default port 6379, use redis-cli --bigkeys to find hogs.", ""),
    ("notes/meeting-may.md", "Meeting notes May", "spring planning notes",
     "notes,meeting", "Discussed the backup migration options (NAS vs object "
     "store), queue simplification, and log retention for the audit. "
     "Decisions to be written up separately.", ""),
    ("notes/meeting-june.md", "Meeting notes June", "summer planning notes",
     "notes,meeting", "Reviewed the ingress gateway options and the postgres "
     "replica plan; dns migration confirmed done.", ""),
]

QUESTIONS = [
    # correction questions: stale note is the lexical magnet
    {"q": "What port does the aurora postgres database listen on?",
     "expect": ["7433"], "kind": "correction"},
    {"q": "What port does the aurora redis cache run on?",
     "expect": ["6390"], "kind": "correction"},
    {"q": "What is the wireguard vpn endpoint (host and port) for remote access?",
     "expect": ["wg.example.com:51821", "51821"], "kind": "correction"},
    {"q": "Where do the nightly backups go?",
     "expect": ["minio", "192.0.2.30"], "kind": "correction"},
    {"q": "Which software serves LAN DNS on the gateway?",
     "expect": ["unbound"], "kind": "correction"},
    {"q": "How long are service logs retained?",
     "expect": ["90"], "kind": "correction"},
    {"q": "Which message queue do aurora services use for async jobs?",
     "expect": ["nats"], "kind": "correction"},
    # link/hub questions: answer sits one wikilink hop from the cue
    {"q": "What host (ip or hostname) runs the aurora postgres primary?",
     "expect": ["192.0.2.10", "db1.aurora.lan"], "kind": "link"},
    {"q": "Roughly how long does a full database restore take?",
     "expect": ["40"], "kind": "link"},
    {"q": "Which bucket do the ml experiment artifacts land in?",
     "expect": ["aurora-ml"], "kind": "link"},
    # pinned/safety questions
    {"q": "Where do aurora service secrets live?",
     "expect": ["vault"], "kind": "pinned"},
    {"q": "How is direct port access to internal services restricted?",
     "expect": ["admin vlan", "firewall"], "kind": "pinned"},
    # operational questions
    {"q": "Through which host and port does external ssh enter the lan?",
     "expect": ["bastion.example.com", "2222"], "kind": "ops"},
    {"q": "Which vlan is the iot wifi mapped to?",
     "expect": ["30"], "kind": "ops"},
    {"q": "What are the steps to onboard a new aurora service?",
     "expect": ["approle", "scrape"], "kind": "ops"},
    {"q": "Who is in the oncall rotation?",
     "expect": ["dana", "robin"], "kind": "ops"},
    # associative: answer lives one CO-USE hop from the cue's direct hits
    {"q": "What channel naming pattern does the worker consume from?",
     "expect": ["aurora.jobs"], "kind": "assoc"},
    {"q": "You are preparing the deploy of the aurora worker. Which host do "
          "you watch during rollout?",
     "expect": ["grafana"], "kind": "assoc"},
]


def build() -> None:
    if os.path.exists(CORPUS):
        shutil.rmtree(CORPUS)
    os.makedirs(CORPUS)
    b = Bundle(CORPUS)
    for path, title, desc, tags, body, supersedes in NOTES:
        meta = {"type": "fact", "title": title, "description": desc,
                "tags": [t.strip() for t in tags.split(",") if t.strip()]}
        if supersedes:
            meta["supersedes"] = [supersedes]
        b.write_note(path, meta, body)
    b = Bundle(CORPUS)  # reload so links resolve
    b.generate_index()

    d = Dynamics(CORPUS)
    # epoch 1: old world in use, lightly
    for path, *_ in NOTES:
        if path.endswith("-old.md") or "/logs-old" in path or "/queue-old" in path:
            d.touch(path)
    d.consolidate()
    # corrections land: supersede events (LTD) for every pair
    for path, _t, _d, _g, _b, sup in NOTES:
        if sup:
            d.supersede(sup, path)
    # epoch 2: current world used heavily
    hot = ["infra/db-port.md", "infra/redis.md", "infra/vpn.md",
           "infra/backup.md", "infra/dns.md", "decisions/logs.md",
           "decisions/queue.md", "runbooks/restore-db.md",
           "projects/aurora.md", "infra/db-host.md"]
    for _round in range(4):
        for path in hot:
            d.touch(path)
    # decoys got glanced at, twice
    for path in ["notes/postgres-tuning.md", "notes/network-cheatsheet.md",
                 "notes/redis-tips.md"]:
        d.touch(path)
        d.touch(path)
    # pins: safety notes hold a floor
    d.pin("decisions/secrets.md")
    d.pin("decisions/access.md")
    # an incident: strong negative outcome right after touching its precursors
    for path in ["runbooks/restore-db.md", "infra/db-port.md", "infra/backup.md"]:
        d.touch(path)
    d.outcome(-0.9, why="restore drill failed against the stale port")
    # Hebbian co-use: recurring "deploy" sessions wire worker+queue+monitoring
    # together, and "drill" sessions wire the restore cluster.
    for i in range(3):
        for path in ["projects/worker.md", "decisions/queue.md",
                     "infra/monitoring.md"]:
            d.touch(path, session=f"deploy-{i}")
    for i in range(2):
        for path in ["runbooks/restore-db.md", "infra/backup.md",
                     "infra/db-host.md"]:
            d.touch(path, session=f"drill-{i}")
    d.consolidate()

    with open(os.path.join(HERE, "questions.json"), "w", encoding="utf-8") as fh:
        json.dump(QUESTIONS, fh, indent=1)
    print(f"corpus: {len(NOTES)} notes, {len(QUESTIONS)} questions -> {CORPUS}")


if __name__ == "__main__":
    build()
