"""Manage one home knowledge base with per-project rooms.

The visible home directory defaults to ``~/muninn``. Project-specific
knowledge resides under ``projects/<slug>/``, while threads, lessons, and
style data apply across projects. The first qualifying project activity can
create a room.

Project adoption adds a short pointer without replacing existing agent
instructions. Absolute project paths and their room names remain in the
private ``.muninn/rooms.json`` registry so that shared knowledge does not
contain another machine's filesystem layout.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat

from .dynamics import Dynamics
from .store import Bundle, _atomic_write_text

HOME_ENV = "MUNINN_HOME"
ROOMS_DIR = "projects"
CROSS_DIRS = ("threads", "lessons", "style", "style-learned")
MARKER = "home.json"          # .muninn/home.json marks a home bundle.
REGISTRY = "rooms.json"       # .muninn/rooms.json: private, machine-local
POINTER_MARK = "<!-- muninn:home"
ROOM_KEYS = 512               # Maximum number of registered rooms.

HOME_NOTE = """\
Muninn organizes project knowledge in one home directory.

- `projects/<room>/` contains project-specific knowledge. Muninn creates a
  room after the first qualifying activity in that project.
- `threads/` contains current state and dated episodes across projects.
- `lessons/` contains corrective knowledge with optional path guards.
- `style/` contains the adopted user-owned style repository.
- `style-learned/` contains preferences promoted from repeated feedback. This
  local overlay does not modify the adopted repository. Raw evidence remains
  in the private `.muninn/` sidecar.

Run {prime} to retrieve current context.
"""


def home_root() -> str:
    """Return `$MUNINN_HOME`, or `~/muninn` when the variable is absent."""
    env = os.environ.get(HOME_ENV, "").strip()
    return os.path.abspath(os.path.expanduser(env or os.path.join("~", "muninn")))


def is_home(root: str) -> bool:
    """Return whether a bundle carries the home sidecar marker."""
    return os.path.isfile(os.path.join(os.path.abspath(root),
                                       ".muninn", MARKER))


def init_home(root: str) -> bool:
    """Create missing home directories without replacing existing content.

    The return value is true only when the call creates the home marker.
    """
    root = os.path.abspath(os.path.expanduser(root))
    fresh = not is_home(root)
    os.makedirs(root, exist_ok=True)
    _ensure_gitignore(root)
    for d in (ROOMS_DIR,) + CROSS_DIRS:
        os.makedirs(os.path.join(root, d), exist_ok=True)
    b = Bundle(root)
    if "home.md" not in b.notes:
        b.write_note("home.md", {"type": "note", "title": "Muninn home knowledge base",
                                 "pinned": True, "provenance": "curated"},
                     HOME_NOTE.format(prime=_code_span(f"muninn --root {shlex.quote(root)} prime")))
    b.generate_index()
    Dynamics(root)._save()
    marker = os.path.join(root, ".muninn", MARKER)
    if fresh:
        with open(marker, "w", encoding="utf-8") as fh:
            json.dump({"version": 1}, fh)
            fh.write("\n")
    return fresh


def _ensure_gitignore(root: str) -> None:
    """Exclude private state without reading or modifying linked files."""
    gi = os.path.join(root, ".gitignore")
    try:
        descriptor = os.open(gi, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        existing = ""
    else:
        with os.fdopen(descriptor, encoding="utf-8") as fh:
            status = os.fstat(fh.fileno())
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                raise ValueError(".gitignore must be a regular file with one hard link")
            existing = fh.read()
    if ".muninn/" not in existing.splitlines():
        text = existing + ("\n" if existing and not existing.endswith("\n") else "")
        _atomic_write_text(root, [".gitignore"], text + ".muninn/\n")


# -- rooms --------------------------------------------------------------------

def _registry_path(root: str) -> str:
    return os.path.join(os.path.abspath(root), ".muninn", REGISTRY)


def rooms(root: str) -> dict[str, str]:
    """The registry: real project path -> room slug. Empty when none.
    A torn/corrupt file is QUARANTINED (renamed .corrupt), never
    half-read: acting on a partial registry could hand one project's
    room to another (the disk-existence check in room_for is the second
    line of that defense)."""
    path = _registry_path(root)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError:
        return {}
    except ValueError:
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            pass
        return {}
    r = data.get("rooms", {}) if isinstance(data, dict) else {}
    return {str(k): str(v) for k, v in r.items()} if isinstance(r, dict) else {}


def _save_rooms(root: str, reg: dict[str, str]) -> None:
    path = _registry_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"  # atomic replace: a crash mid-write must never
    with open(tmp, "w", encoding="utf-8") as fh:  # tear the registry
        json.dump({"version": 1, "rooms": reg}, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _toplevel(workdir: str) -> tuple[str, bool]:
    """(project root, is_git_repo): the git toplevel when there is one,
    else the folder itself: so every file under one repo lands in one
    room. The flag lets ambient callers refuse non-repo folders."""
    from .observe import git_out  # observe imports store only; no cycle
    wd = os.path.abspath(os.path.expanduser(workdir))
    if not os.path.isdir(wd):
        wd = os.path.dirname(wd) or wd
    top = git_out(wd, "rev-parse", "--show-toplevel")
    return os.path.realpath(top or wd), bool(top)


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48]
    return s or "project"


def room_for(root: str, workdir: str, create: bool = False,
             ambient: bool = False) -> str | None:
    """The room a project belongs to, as a bundle-relative prefix
    (``projects/<slug>``). Unknown project + ``create`` registers it :
    the organic-growth entry point (first activity builds the room).
    The brain itself (and anything inside it) is never a room.
    ``ambient`` callers (hooks) only mint rooms for real git projects:
    a stray dotfile or download read must not become a room. Explicit
    adopt may room any folder."""
    root = os.path.abspath(root)
    top, is_repo = _toplevel(workdir)
    brain = os.path.realpath(root)
    if top == brain or top.startswith(brain + os.sep):
        return None
    reg = rooms(root)
    slug = reg.get(top)
    if slug is None:
        if (not create or (ambient and not is_repo)
                or len(reg) >= ROOM_KEYS):
            return None
        slug = _slug(os.path.basename(top))
        # a taken slug OR an existing room dir forces the hash suffix :
        # the disk check means a lost/quarantined registry can never
        # hand one project's populated room to another (the hash is
        # path-derived, so the SAME project re-finds the same suffix)
        if (slug in reg.values()
                or os.path.isdir(os.path.join(root, ROOMS_DIR, slug))):
            slug += "-" + hashlib.sha1(
                top.encode("utf-8", "replace")).hexdigest()[:6]
        reg[top] = slug
        _save_rooms(root, reg)
        os.makedirs(os.path.join(root, ROOMS_DIR, slug), exist_ok=True)
    return f"{ROOMS_DIR}/{slug}"


# -- adopt: the two-line pointer ----------------------------------------------

def _code_span(command: str) -> str:
    """Use a Markdown delimiter that cannot occur inside the command."""
    fence = "`" * (1 + max((len(run) for run in re.findall(r"`+", command)), default=0))
    return f"{fence}{command}{fence}"


def pointer_lines(home: str, room_rel: str | None = None) -> str:
    """Return the two-line pointer appended to an existing instruction file."""
    where = f" (room: {room_rel})" if room_rel else ""
    prime = _code_span(f"muninn --root {shlex.quote(home)} prime")
    return (f"{POINTER_MARK}: this workspace is organized by a muninn "
            "home knowledge base -->\n"
            f"Start every session with {prime}: "
            f"knowledge, threads, and lessons live in {home}{where}; log "
            "reactions with `muninn feedback`, milestones with `muninn "
            "journal`.\n")


def pointer_matches(text: str, root: str) -> bool:
    """Verify that one complete home pointer selects the requested root."""
    lines = text.splitlines()
    positions = [i for i, line in enumerate(lines) if POINTER_MARK in line]
    if len(positions) != 1:
        return False
    index = positions[0]
    if (lines[index] != pointer_lines(root).splitlines()[0]
            or index + 1 >= len(lines)):
        return False
    match = re.match(r"Start every session with (?P<fence>`+)(?!`)"
                     r"(?P<command>.*?)(?P=fence): ", lines[index + 1])
    if match is None:
        return False
    try:
        command = shlex.split(match.group("command"))
    except ValueError:
        return False
    return (len(command) == 4 and command[:2] == ["muninn", "--root"]
            and command[3] == "prime"
            and os.path.realpath(os.path.expanduser(command[2]))
            == os.path.realpath(root))


def adopt(home: str, project_dir: str) -> tuple[str | None, str]:
    """Register a project room and add the home pointer to its `AGENTS.md`.

    The returned status distinguishes a new file, an appended pointer, and an
    idempotent repeat.
    """
    home = os.path.abspath(os.path.expanduser(home))
    proj = os.path.abspath(os.path.expanduser(project_dir))
    agents = os.path.join(proj, "AGENTS.md")
    try:
        with open(agents, encoding="utf-8") as fh:
            existing = fh.read()
    except OSError:
        existing = ""
    if POINTER_MARK in existing and not pointer_matches(existing, home):
        return None, "conflicting pointer"
    room = room_for(home, proj, create=True)
    if room is None:  # the brain itself, or the room cap: a refused
        return None, "not adopted"  # adopt must not leave a pointer behind
    if POINTER_MARK in existing:
        return room, "already wired"
    with open(agents, "a", encoding="utf-8") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        if existing:
            fh.write("\n")
        fh.write(pointer_lines(home, room))
    return room, ("appended to" if existing else "wrote")
