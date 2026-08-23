# Agent Skills

Org-owned, SKILL.md-compatible instruction packages that agents load on
demand. v1 shipped 2026-08-15; the implementation plan (with the full
rationale) lives at the Agent Skills v1 artifact; the decisions that shaped
the code are summarized here so they survive next to it.

## What a skill is

A `skills` row: kebab-case `name` (unique per org — it is the handle the
model passes to `load_skill`), a required `description` (what the model reads
when deciding to load), a markdown `content` body ≤64 KB, and optional parsed
YAML `frontmatter`. Tenancy matches agents/vector_stores: `org_id` NOT NULL,
`visibility` 'private' | 'org', `account_id` as created-by.

Create/update accepts either explicit fields or a full Anthropic-format
SKILL.md — front matter (parsed with `yaml.safe_load`, see
`src/services/skill_markdown.py`) fills whichever fields the request leaves
blank, and the stored body has the front matter stripped.

## How an agent uses one

1. The agent config carries references: `data.skills: [{"skillId": N}]`
   (schema `src/schemas/agent_config.v4.json`, still version 4 — the key is
   additive). Write-time validation (`routers/agents/skill_refs.py`) rejects
   ids the caller cannot see.
2. At runtime `prepare_agent_context` (`src/agent_runtime/context.py`) calls
   `src/agent_runtime/skills.py`:
   - an `<available_skills>` index (name + description per skill) is appended
     to the system prompt, brace-escaped like everything else that feeds the
     LangChain template;
   - a `load_skill` tool is added, closing over the already-loaded bodies
     (no DB at invoke time — tools outlive the request session).
3. The model calls `load_skill("name")` and receives the markdown body as a
   tool result. Persisted tool calls store a 500-char preview
   (`toolType: "loadSkill"`, see `helpers/tool_calls.py`) — the full body
   already fed the model in-turn and does not belong on every chat row.

Verify the injection with `DUMP_AGENT_PROMPT=1` — the dumped system message
shows the skills block with zero extra code.

## Slash commands (explicit invocation)

Everything above is model-discretionary: the model reads the index and
decides. A chat message that starts with `/skill-name` is the user deciding,
so it must not depend on that election. `expand_slash_command`
(`src/agent_runtime/skills.py`) matches the first token against the attached
(already entitlement- and visibility-filtered) skills and, on a hit,
`prepare_agent_context` swaps the TURN INPUT for the skill body plus the
rest of the message as arguments. Three properties to preserve:

- **The transcript keeps the raw command.** `ctx.prompt` (persisted) and
  `ctx.agent_input` (sent to the model) are separate on purpose — the same
  split attachments use. Only the model sees the expansion.
- **No brace escaping** on the expanded text: it rides in the `{input}`
  template *variable*, not template text like the system-prompt index.
- **Non-matches pass through untouched** — exact name match only. A near-miss
  falls back to the model-side index (`load_skill`'s unknown-name reply lets
  the model self-correct); an ordinary message starting with "/" is just text.

The composer's autocomplete (cmdlabs-ui `agent-chat/prompt-form.tsx`) offers
ATTACHED skills only — the menu mirrors what the runtime will actually
expand. It reads names via `/api/skills`, so a member running a shared agent
may not see the owner's private skills in the menu; typing such a command
still expands, because runtime visibility follows the owner (above).

## skill-creator and `save_skill` (the one write path)

Skills are instruction-only, so a "skill that writes skills" needs somewhere
to put its output. That is `save_skill` (`agent_runtime/skills.py`), the
only write in the skills runtime:

- **Built only when an attached skill is named `skill-creator`**
  (`has_skill_creator`). The capability travels with the skill that knows
  how to use it; every other skilled agent sees `load_skill` alone.
- **Writes as the CALLER** (`org_scope`), not the agent owner: a colleague
  running a shared skill-creator agent authors into their own space.
  Created rows are `private`; widening to org is a deliberate act in the UI.
- **Never silently overwrites.** Name clash → tool result asks the model to
  confirm with the user and call again with `overwrite=true`; a clash with a
  skill the caller does not own is refused outright.
- **Fails as a tool result, never an exception** — the route validators are
  reused so limits match the UI, but a bad name comes back as text the model
  can fix within the turn.
- Opens its own short-lived session (`tools/sessions.py` pattern); tools run
  after the request session closes.
- Persisted as `toolType: "saveSkill"` with the skill id (not the body, which
  now lives on the row). The drawer card links to the skill's edit page.

The skill itself ships as a **built-in template**
(`src/skill_templates/skill-creator.md`, loaded by
`services/skill_templates.py`). `GET /api/skills/templates/` lists templates
with an `installed` flag; `POST /api/skills/templates/{name}/install` copies
one into the org as a normal private row the org then owns — later edits to
the file never touch an installed copy. The skills page shows an install CTA
until the org has a skill by that name. Templates are mounted before
`/{skill_id}` so "templates" is never parsed as an id.

Demo path: Skills → Install skill-creator → attach to an agent → in chat,
`/skill-creator <what the agent should learn>` → approve the draft → the
agent calls `save_skill` → attach the new skill → `/new-skill-name`.

## Failure directions (the part worth re-reading before changing anything)

- **Stale reference → fail-soft.** A deleted/inaccessible skill is logged
  and skipped at runtime, mirroring the tool factory. Deleting a skill does
  NOT rewrite referencing agent configs.
- **Entitlement → fail-closed.** `skills` is a module
  (`config/modules_registry.py`), Premium-only (`config/plans_registry.py`).
  `require_module` gates `/api/skills`; `load_agent_skills` checks the same
  key for the CALLER so the runtime is not the way around the front door —
  no module means no index and no tool, silently.
- **Visibility follows the AGENT OWNER at runtime**, not the caller: a
  colleague running a shared agent gets the agent the owner built, exactly
  like the agent's tools.
- `load_skill` is deliberately absent from `TOOL_MODULES` — skills are not
  `data.tools` entries; their gate lives in `agent_runtime/skills.py`.

## Why it is shaped this way

- **DB text column, not GCS** for bodies: uploads require a per-account GCS
  credential, and a skill must not fail to save for an account that never
  configured one. Multi-file bundles later = `skill_files` table + GCS
  prefix; this row stays the identity.
- **Progressive disclosure in v1**: inlining bodies into the prompt would
  duplicate the Prompts feature and cap how many skills an agent can carry.
- **By-reference attachment**: what makes versioning/pinning an additive
  change later.
- **Resource-shaped tenancy** (`SKILL` constant in `services/org_scope.py`):
  per-person AccessGrant sharing becomes one resource type + a UI wrapper.

## Limits

64 KB body · 1 KB description · 20 skills per agent · kebab-case names
≤64 chars. All enforced in `routers/skills/models.py` and the config schema.

## Tests

`tests/test_skills.py` (CRUD + attachment + template install),
`tests/test_skill_markdown.py` (front matter),
`tests/test_org_isolation_skills.py` (tenancy + the cross-org attachment
boundary), `tests/agent_runtime/test_skills_runtime.py` (index escaping,
tool behavior, loader failure directions, `save_skill` ownership/overwrite
rules).
