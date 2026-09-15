# Comparison with related systems

Muninn combines portable Markdown, deterministic source mapping, optional
model enrichment, and ranking that changes after independent use. Related
systems implement important subsets of this design. No comparison below
establishes universal superiority; each system serves a different operating
boundary.

## Comparison dimensions

The comparison uses six properties that affect adoption and retrieval:

1. Whether editable files or a database are authoritative.
2. Whether source structure can be extracted without a model.
3. Whether a model can add inferred concepts and relationships.
4. Whether observed use changes later retrieval.
5. Whether usage state can be separated by consumer.
6. Whether the system returns bounded context with selection reasons.

| System | Authoritative state | Deterministic mapping | Model enrichment | Independent use changes retrieval | Separate consumer state | Bounded explained context |
|---|---|---|---|---|---|---|
| Muninn | Markdown knowledge and append-only usage ledger | Yes | Optional | Yes, within fixed bounds | Yes | Yes |
| [GBrain](https://github.com/garrytan/gbrain) | Markdown with PGLite or Postgres retrieval | Partial | Yes | No documented access-based adjustment | No documented separate ranking state | Yes |
| [Graphify](https://github.com/Graphify-Labs/graphify) | Derived graph | Yes | Optional | Explicit outcomes can create a learning overlay | No | Yes |
| [Basic Memory](https://github.com/basicmachines-co/basic-memory) | Markdown with a derived SQLite or Postgres index | Link and relation extraction | Agent-authored memory | No documented access-based adjustment | No | Partial |
| [Graphiti](https://github.com/getzep/graphiti) | Graph database | No | Required for extraction | Temporal invalidation rather than access learning | No | Partial |

The table reflects the verified mechanisms recorded in the dated
[landscape review](LANDSCAPE-2026-08.md). A partial value means that the
system provides a related mechanism but not the complete property defined
above.

## Architectural differences

GBrain demonstrates that a large Markdown corpus can support graph-oriented
retrieval and scheduled model maintenance. Its local mode uses an embedded
PGLite database, while larger deployments use Postgres. Muninn instead keeps
ordinary knowledge recall in the standard-library process and stores usage evidence in a separate consumer sidecar.

Graphify provides the strongest overlap in deterministic source analysis.
Muninn can import Graphify output, but the tools use different authorities:
Graphify produces a derived graph, while Muninn retains Markdown knowledge and
the append-only ledger as authoritative inputs. Graphify can also turn explicit
query outcomes into a learning overlay. Muninn learns bounded ranking signals
from independently observed use.

Graphiti represents temporal facts directly in a graph database. Its
invalidation model is useful for recording when a claim became false. Muninn
implements corrections through `supersedes` and excludes obsolete notes during
serving. Muninn additionally adjusts preference after use, while Graphiti's
temporal invalidation addresses validity rather than access frequency.

Model-curated systems such as LLM Wiki can synthesize concepts and rewrite a
knowledge representation from source material. Muninn restricts this behavior
to an additive inferred layer. Deterministic and curated facts remain separate,
and a user can remove inferred notes without changing the base.

Hosted memory services reduce setup by making a remote store authoritative.
Muninn instead keeps knowledge and memory in user-controlled files. This choice
requires local installation but supports offline operation, direct inspection, and deletion without an export API.

## Current evidence

Muninn's strongest comparative evidence concerns its own retrieval choices,
not end-to-end competition with every system. On 300 SWE-bench Lite cases, its
BM25F-led source retrieval reached 44.7 percent hit@1 and 80.0 percent hit@10,
compared with 20.0 and 60.3 percent for the previous token-overlap method.
Direct graph fusion reduced hit@1 and therefore did not become the default.
On a 40-note synthetic knowledge task, a 400-token adaptive pack answered 16
of 18 questions, while a whole-corpus condition answered 6. These results do
not establish answer quality, patch correctness, or superiority over the
systems in the table.

The [evidence report](EVIDENCE.md) defines supported claims and negative
results. The [evaluation directory](../eval/README.md) records benchmark
sources and license treatment.
