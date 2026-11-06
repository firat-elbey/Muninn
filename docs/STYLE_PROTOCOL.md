# Style repository protocol

Muninn consumes a user's style repository without owning its content. The
repository defines the writing standard. Muninn validates, embeds, routes, and
checks that standard.

## Manifest

Place `muninn-style.json` at the repository root:

```json
{
  "version": 1,
  "core": "core.md",
  "guides": [
    {
      "id": "email",
      "path": "guides/email.md",
      "title": "Email",
      "description": "Structure and length rules for email.",
      "applies_to": ["email"]
    }
  ],
  "templates": [
    {
      "id": "project-brief",
      "path": "templates/project-brief.md",
      "title": "Project brief",
      "description": "A decision-oriented project brief.",
      "guide": "email"
    }
  ]
}
```

`version` must be `1`. `core` must name one UTF-8 Markdown file no larger than
32 KiB. Each guide requires an identifier, path, title, description, and at
least one document type in `applies_to`. Each template requires the same
descriptive fields and the identifier of its governing guide.

All identifiers must use lowercase letters, numbers, dots, hyphens, or the
`_` character. Each identifier must be unique across guides and templates. Every
path must be normalized, relative, end in `.md`, exist, and remain inside the
repository after symbolic links are resolved. A contract may declare at most
48 combined guides and templates. Muninn rejects invalid contracts; it does
not guess or truncate.

## Precedence

The core governs every response. For a recognized document type, the most
specific matching guide adds structure. Use one guide. A template supplies a
starting form. Learned preferences may refine the result. A guide, template,
or learned preference cannot override the core.

Muninn embeds the complete core in the canonical global agent file and in the
generated style skill. The manifest supplies the route index. Files that are
not listed in the manifest are not routed.

## Adoption and refresh

Adopt a repository once:

```bash
muninn style adopt ~/repos/my-style
```

Refresh after a local edit:

```bash
muninn style refresh
```

To update a Git-backed repository from its configured upstream before
refreshing, require a fast-forward:

```bash
muninn style refresh --pull
```

Muninn records the contract hash, route-block hash, Git commit, branch, and
upstream in the private `.muninn/style.json` sidecar. `muninn doctor --home`
fails if the managed block, generated skill, hashes, revision, or local
upstream status is stale. Doctor does not fetch a remote.

## Learned preferences

Repeated feedback is compiled under `style-learned/` in the Muninn bundle.
This overlay has lower precedence than the adopted core and guide. Muninn never
writes or commits learned preferences into the adopted repository. A user may
review a learned rule and add it to the source repository explicitly.
