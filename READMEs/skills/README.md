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

`tests/test_skills.py` (CRUD + attachment), `tests/test_skill_markdown.py`
(front matter), `tests/test_org_isolation_skills.py` (tenancy + the
cross-org attachment boundary), `tests/agent_runtime/test_skills_runtime.py`
(index escaping, tool behavior, loader failure directions).
