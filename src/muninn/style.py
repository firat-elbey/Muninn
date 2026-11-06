"""Adopt and enforce a user's style repository without owning its content.

Muninn defines a public contract. The adopted repository supplies one small
core, explicit document guides, and optional templates. Muninn embeds the core
in every global agent surface and routes only the files named by the contract.
Repeated feedback is stored in a separate bundle overlay; it never modifies
the adopted repository.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
import shutil
import subprocess

STYLE_BEGIN = ("<!-- muninn:style:begin "
               "(managed: muninn style adopt refreshes this block) -->")
STYLE_END = "<!-- muninn:style:end -->"
MANIFEST = "muninn-style.json"
MANIFEST_VERSION = 1
RECORD_VERSION = 2
MAX_ROUTES = 48
MAX_CORE_BYTES = 32 << 10
MAX_FIELD_CHARS = 240
STYLE_JSON = "style.json"
LEARNED_OVERLAY = "style-learned"
SKILL_REL = os.path.join(".claude", "skills", "style", "SKILL.md")
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class ContractError(Exception):
    """The repository does not satisfy the public style contract."""


class MountError(Exception):
    """Mounting or wiring would overwrite user-owned data."""


@dataclass(frozen=True)
class Guide:
    id: str
    path: str
    title: str
    description: str
    applies_to: tuple[str, ...]


@dataclass(frozen=True)
class Template:
    id: str
    path: str
    title: str
    description: str
    guide: str


@dataclass(frozen=True)
class StyleContract:
    repo: str
    core_path: str
    core_text: str
    guides: tuple[Guide, ...]
    templates: tuple[Template, ...]
    content_hash: str


def _contract_path(repo: str) -> str:
    return os.path.join(os.path.realpath(repo), MANIFEST)


def _field(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string")
    value = value.strip()
    if len(value) > MAX_FIELD_CHARS:
        raise ContractError(
            f"{field} exceeds the {MAX_FIELD_CHARS}-character limit")
    if "\n" in value or "\r" in value:
        raise ContractError(f"{field} must fit on one line")
    if "<!--" in value or "-->" in value:
        raise ContractError(f"{field} cannot contain an HTML comment marker")
    return value


def _id(value, field: str) -> str:
    value = _field(value, field)
    if not _ID.fullmatch(value):
        raise ContractError(
            f"{field} must use lowercase letters, numbers, dots, hyphens, "
            "or underscores")
    return value


def _resource(repo: str, value, field: str) -> tuple[str, bytes]:
    rel = _field(value, field).replace("\\", "/")
    if os.path.isabs(rel) or rel.startswith("../") or "/../" in rel:
        raise ContractError(f"{field} must stay inside the style repository")
    normalized = os.path.normpath(rel).replace(os.sep, "/")
    if normalized != rel or normalized in ("", "."):
        raise ContractError(f"{field} must be a normalized relative path")
    if not normalized.lower().endswith(".md"):
        raise ContractError(f"{field} must name a Markdown file")
    full = os.path.realpath(os.path.join(repo, normalized))
    try:
        inside = os.path.commonpath((os.path.realpath(repo), full))
    except ValueError:
        inside = ""
    if inside != os.path.realpath(repo):
        raise ContractError(f"{field} resolves outside the style repository")
    try:
        with open(full, "rb") as fh:
            content = fh.read()
    except OSError as exc:
        raise ContractError(f"{field} does not exist: {normalized}") from exc
    return normalized, content


def _keys(data: dict, allowed: set[str], field: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ContractError(f"{field} contains unknown field(s): "
                            + ", ".join(unknown))


def _entries(value, field: str) -> list[dict]:
    if not isinstance(value, list):
        raise ContractError(f"{field} must be a list")
    if not all(isinstance(item, dict) for item in value):
        raise ContractError(f"every {field} entry must be an object")
    return value


def load_contract(repo: str) -> StyleContract:
    """Load and validate one style contract. No file is inferred by name."""
    repo = os.path.realpath(os.path.abspath(os.path.expanduser(repo)))
    path = _contract_path(repo)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as exc:
        raise ContractError(
            f"{MANIFEST} is required at the repository root") from exc
    except (ValueError, UnicodeError) as exc:
        raise ContractError(f"{MANIFEST} is not valid UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise ContractError(f"{MANIFEST} must contain one JSON object")
    _keys(raw, {"version", "core", "guides", "templates"}, MANIFEST)
    if raw.get("version") != MANIFEST_VERSION:
        raise ContractError(
            f"{MANIFEST}.version must be {MANIFEST_VERSION}")

    core_path, core_bytes = _resource(repo, raw.get("core"), "core")
    if len(core_bytes) > MAX_CORE_BYTES:
        raise ContractError(
            f"core exceeds the {MAX_CORE_BYTES}-byte limit; reduce it before "
            "adoption")
    try:
        core_text = core_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError("core must be UTF-8 text") from exc
    if STYLE_BEGIN in core_text or STYLE_END in core_text:
        raise ContractError("core cannot contain Muninn's managed markers")

    guide_rows = _entries(raw.get("guides", []), "guides")
    template_rows = _entries(raw.get("templates", []), "templates")
    if len(guide_rows) + len(template_rows) > MAX_ROUTES:
        raise ContractError(
            f"the contract defines {len(guide_rows) + len(template_rows)} "
            f"routes; the explicit limit is {MAX_ROUTES}")

    ids: set[str] = set()
    resources: list[tuple[str, bytes]] = [(core_path, core_bytes)]
    parsed_guides: list[Guide] = []
    for index, row in enumerate(guide_rows):
        prefix = f"guides[{index}]"
        _keys(row, {"id", "path", "title", "description", "applies_to"},
              prefix)
        gid = _id(row.get("id"), f"{prefix}.id")
        if gid in ids:
            raise ContractError(f"duplicate route id: {gid}")
        ids.add(gid)
        rel, content = _resource(repo, row.get("path"), f"{prefix}.path")
        applies = row.get("applies_to")
        if (not isinstance(applies, list) or not applies
                or not all(isinstance(item, str) and item.strip()
                           for item in applies)):
            raise ContractError(
                f"{prefix}.applies_to must be a non-empty list of strings")
        apply_fields = tuple(_field(item, f"{prefix}.applies_to")
                             for item in applies)
        parsed_guides.append(Guide(
            gid, rel, _field(row.get("title"), f"{prefix}.title"),
            _field(row.get("description"), f"{prefix}.description"),
            apply_fields))
        resources.append((rel, content))

    guide_ids = {guide.id for guide in parsed_guides}
    parsed_templates: list[Template] = []
    for index, row in enumerate(template_rows):
        prefix = f"templates[{index}]"
        _keys(row, {"id", "path", "title", "description", "guide"}, prefix)
        tid = _id(row.get("id"), f"{prefix}.id")
        if tid in ids:
            raise ContractError(f"duplicate route id: {tid}")
        ids.add(tid)
        rel, content = _resource(repo, row.get("path"), f"{prefix}.path")
        guide = _id(row.get("guide"), f"{prefix}.guide")
        if guide not in guide_ids:
            raise ContractError(
                f"{prefix}.guide refers to unknown guide: {guide}")
        parsed_templates.append(Template(
            tid, rel, _field(row.get("title"), f"{prefix}.title"),
            _field(row.get("description"), f"{prefix}.description"), guide))
        resources.append((rel, content))

    normalized = {
        "version": MANIFEST_VERSION,
        "core": core_path,
        "guides": [guide.__dict__ for guide in parsed_guides],
        "templates": [template.__dict__ for template in parsed_templates],
    }
    digest = hashlib.sha256(json.dumps(
        normalized, sort_keys=True, separators=(",", ":")).encode())
    for rel, content in resources:
        digest.update(b"\0")
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(content)
    return StyleContract(repo, core_path, core_text, tuple(parsed_guides),
                         tuple(parsed_templates), digest.hexdigest())


def guides(repo: str) -> list[tuple[str, str, str]]:
    """Return the explicit guide index for status and compatibility."""
    contract = load_contract(repo)
    return [(guide.id, guide.title, guide.description)
            for guide in contract.guides]


def _base(repo: str, brain: str) -> str:
    mountp = os.path.join(os.path.abspath(brain), "style")
    if os.path.realpath(mountp) == os.path.realpath(repo):
        return mountp
    return os.path.realpath(repo)


def _route_path(base: str, rel: str) -> str:
    return os.path.join(base, *rel.split("/"))


def render_router(repo: str, brain: str,
                  contract: StyleContract | None = None) -> str:
    """Embed the mandatory core and render every declared route."""
    contract = contract or load_contract(repo)
    base = _base(repo, brain)
    brain = os.path.abspath(brain)
    head = (
        "## Writing style: mandatory rules and routes\n\n"
        "Apply the core rules below to every response. Before drafting a "
        "recognized document type, read the most specific matching guide. "
        "Use one guide. It may add structure, but it cannot override the "
        "core. A template is an optional starting form.\n\n"
        "### Mandatory core\n\n")
    text = head + contract.core_text
    if not text.endswith("\n"):
        text += "\n"
    text += "\n### Document guides\n\n"
    if contract.guides:
        for guide in contract.guides:
            kinds = ", ".join(guide.applies_to)
            path = _route_path(base, guide.path)
            text += (f"- {guide.id} ({kinds}): {guide.title}. "
                     f"{guide.description} File: {path}\n")
    else:
        text += "No document-specific guides are declared.\n"
    if contract.templates:
        text += "\n### Templates\n\n"
        for template in contract.templates:
            path = _route_path(base, template.path)
            text += (f"- {template.id} (guide: {template.guide}): "
                     f"{template.title}. {template.description} File: {path}\n")
    overlay = os.path.join(brain, LEARNED_OVERLAY)
    text += (
        "\n### Learned preferences\n\n"
        f"Repeated preferences are stored separately at {overlay}/. They may "
        "refine a response, but they cannot override the core or the selected "
        "guide.\n\n"
        "Log each correction or approval when it occurs:\n\n"
        f'`muninn --root {brain} feedback "<observed preference>" '
        "--domain <guide>`")
    return text


def router_hash(router: str) -> str:
    return hashlib.sha256(router.encode()).hexdigest()


def adoption_record(brain: str) -> dict:
    path = os.path.join(os.path.abspath(brain), ".muninn", STYLE_JSON)
    try:
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _record(brain: str, repo: str, contract: StyleContract | None = None,
            router: str | None = None) -> None:
    path = os.path.join(os.path.abspath(brain), ".muninn", STYLE_JSON)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    record = {"version": RECORD_VERSION, "repo": os.path.realpath(repo)}
    if contract is not None and router is not None:
        record.update({
            "manifest": MANIFEST,
            "content_hash": contract.content_hash,
            "router_hash": router_hash(router),
            "revision": source_revision(repo),
        })
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def record_adoption(brain: str, repo: str, contract: StyleContract,
                    router: str) -> None:
    os.makedirs(os.path.join(os.path.abspath(brain), LEARNED_OVERLAY),
                exist_ok=True)
    _record(brain, repo, contract, router)


def mounted_repo(brain: str) -> str | None:
    """Return the mounted repository or its no-symlink fallback record."""
    mountp = os.path.join(os.path.abspath(brain), "style")
    if os.path.islink(mountp):
        return os.path.realpath(mountp)
    repo = str(adoption_record(brain).get("repo", ""))
    return repo if repo and os.path.isdir(repo) else None


def _collision_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    candidate = f"{stem}-migrated{ext}"
    number = 2
    while os.path.exists(candidate):
        candidate = f"{stem}-migrated-{number}{ext}"
        number += 1
    return candidate


def _migrate_legacy_overlay(brain: str, mountp: str) -> None:
    legacy = os.path.join(mountp, "learned")
    if not os.path.isdir(legacy):
        return
    overlay = os.path.join(brain, LEARNED_OVERLAY)
    os.makedirs(overlay, exist_ok=True)
    for name in sorted(os.listdir(legacy)):
        shutil.move(os.path.join(legacy, name),
                    _collision_path(os.path.join(overlay, name)))
    os.rmdir(legacy)


def mount(brain: str, repo: str) -> str:
    """Mount a validated source without writing into it."""
    brain = os.path.abspath(brain)
    repo = os.path.realpath(os.path.abspath(repo))
    mountp = os.path.join(brain, "style")
    if os.path.islink(mountp):
        if os.path.realpath(mountp) == repo:
            _record(brain, repo)
            return "already mounted"
        raise MountError(
            f"style/ already mounts {os.path.realpath(mountp)}; remove the "
            "link explicitly before switching repositories")
    if os.path.isdir(mountp):
        _migrate_legacy_overlay(brain, mountp)
        remaining = sorted(os.listdir(mountp))
        if remaining:
            shown = ", ".join(remaining[:4])
            if len(remaining) > 4:
                shown += ", ..."
            raise MountError(
                f"style/ already contains user files ({shown}); move them "
                f"into {repo}, then run the command again")
        os.rmdir(mountp)
    os.makedirs(os.path.join(brain, LEARNED_OVERLAY), exist_ok=True)
    try:
        os.symlink(repo, mountp)
        verb = "mounted"
    except OSError:
        os.makedirs(mountp, exist_ok=True)
        verb = "recorded (symlinks unavailable; routes use the source path)"
    _record(brain, repo)
    return verb


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True,
                          text=True, timeout=30)


def source_revision(repo: str) -> dict:
    """Return local Git state. The function never contacts a remote."""
    try:
        inside = _git(repo, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.SubprocessError):
        return {"kind": "directory"}
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return {"kind": "directory"}
    commit = _git(repo, "rev-parse", "HEAD")
    branch = _git(repo, "branch", "--show-current")
    upstream = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
                    "@{upstream}")
    dirty = _git(repo, "status", "--porcelain", "--untracked-files=normal")
    state = {
        "kind": "git",
        "commit": commit.stdout.strip() if commit.returncode == 0 else "",
        "branch": branch.stdout.strip() if branch.returncode == 0 else "",
        "upstream": upstream.stdout.strip() if upstream.returncode == 0 else "",
        "ahead": 0,
        "behind": 0,
        "dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None,
    }
    if state["upstream"]:
        counts = _git(repo, "rev-list", "--left-right", "--count",
                      "HEAD...@{upstream}")
        if counts.returncode == 0:
            fields = counts.stdout.split()
            if len(fields) == 2:
                state["ahead"], state["behind"] = map(int, fields)
    return state


def pull_ff_only(repo: str) -> str:
    """Update a Git-backed style source only when a fast-forward is safe."""
    state = source_revision(repo)
    if state.get("kind") != "git":
        raise ContractError("--pull requires a Git repository")
    if not state.get("upstream"):
        raise ContractError("--pull requires a configured upstream branch")
    try:
        result = _git(repo, "pull", "--ff-only")
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContractError(f"Git pull failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ContractError(f"Git pull failed: {detail}")
    return result.stdout.strip() or "Already up to date."


def find_block(text: str, begin: str, end: str) -> tuple[int, int] | None:
    """Recognize exactly one ordered marker pair without guessing ownership."""
    if text.count(begin) != 1 or text.count(end) != 1:
        return None
    i = text.find(begin)
    j = text.find(end)
    if j < i + len(begin):
        return None
    return i, j + len(end)


def wire_block(path: str, router: str) -> str:
    """Replace only Muninn's valid managed block."""
    block = f"{STYLE_BEGIN}\n{router}\n{STYLE_END}"
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        text = ""
    span = find_block(text, STYLE_BEGIN, STYLE_END)
    if span:
        new = text[:span[0]] + block + text[span[1]:]
        verb = "refreshed"
    elif STYLE_BEGIN in text or STYLE_END in text:
        raise MountError(
            f"{path}: the muninn:style markers are damaged; repair or remove "
            "the old block, then run the command again")
    elif text:
        new = text + ("" if text.endswith("\n") else "\n") + "\n" + block + "\n"
        verb = "appended to"
    else:
        new = "# Global agent instructions\n\n" + block + "\n"
        verb = "wrote"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    return verb


def current_block(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    span = find_block(text, STYLE_BEGIN, STYLE_END)
    if span is None:
        return None
    inner = text[span[0] + len(STYLE_BEGIN):span[1] - len(STYLE_END)]
    return inner.strip()


def write_skill(repo: str, brain: str, router: str | None = None) -> str:
    """Write the identical core and route block as a discoverable skill."""
    router = router or render_router(repo, brain)
    path = os.path.expanduser(os.path.join("~", SKILL_REL))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "---\n"
            "name: style\n"
            "description: >-\n"
            "  Apply the user's adopted writing core and the one guide that\n"
            "  matches the document. Use for every response and prose edit.\n"
            "---\n\n"
            "# Writing style\n\n" + router + "\n")
    return path


def verify_adoption(brain: str, repo: str, canonical: str) -> list[str]:
    """Return exact reasons why an adopted style source is not current."""
    issues: list[str] = []
    try:
        contract = load_contract(repo)
        router = render_router(repo, brain, contract)
    except ContractError as exc:
        return [str(exc)]
    if current_block(canonical) != router.strip():
        issues.append("the canonical style block does not match the source")
    record = adoption_record(brain)
    if record.get("version") != RECORD_VERSION:
        issues.append("the adoption record is missing or obsolete")
    if os.path.realpath(str(record.get("repo", ""))) != os.path.realpath(repo):
        issues.append("the adoption record names a different repository")
    if record.get("content_hash") != contract.content_hash:
        issues.append("the declared style content changed after the last refresh")
    if record.get("router_hash") != router_hash(router):
        issues.append("the recorded route block changed after the last refresh")
    current_revision = source_revision(repo)
    recorded_revision = record.get("revision", {})
    for field in ("kind", "commit", "branch", "upstream"):
        if recorded_revision.get(field) != current_revision.get(field):
            issues.append("the source revision changed after the last refresh")
            break
    if int(current_revision.get("behind", 0) or 0) > 0:
        issues.append(
            f"the source branch is {current_revision['behind']} commit(s) "
            "behind its local upstream reference")
    skill_path = os.path.expanduser(os.path.join("~", SKILL_REL))
    try:
        with open(skill_path, encoding="utf-8") as fh:
            skill_text = fh.read()
    except OSError:
        skill_text = ""
    if router not in skill_text:
        issues.append("the generated style skill is missing or stale")
    return issues
