# Organizations, plans and sharing

How who-can-use-what works on Command Labs. If the code and this page ever
disagree, one of them is a bug.

## Three rules decide access

1. **An org's plan is its owner's plan.** An org is on premium when its owner
   has an active subscription (or super admins pinned a plan on it), else
   free. Everyone working *inside* an org gets that org's plan — their own
   subscription only matters in orgs they own.
   `services/modules.org_entitlement`

2. **A role caps the plan.**

   | Role | Gets |
   |---|---|
   | Owner | everything the plan includes |
   | Manager | everything the plan includes |
   | Community member | only `home`, `courses`, `agent_chat`, `credentials` |

   `config/roles_registry.py`

3. **An agent is reachable by its owner plus the people it is shared with**, and
   only inside its org.
   `services/access.py`

   **Agent Chat vs Agents.** Agent Chat is *using* agents: listing and opening
   the ones you may use, chatting, approving their emails, attaching files.
   Agents adds *authoring*: create, edit, delete, share. Both open `/api/agents`,
   `/api/tool-approvals` and `/api/files`; the authoring routes also require
   Agents. `config/modules_registry.py`, `routers/agents/router.py`

## Sharing an agent

- Only the agent's **owner** can share it.
- Sharing is a **premium** feature: the org must be on premium. (On free, the
  person could not open agent chat anyway.)
- The person must already be a **member of the org** — invite them first.
  Community member is the usual role for "someone I share agents with".

`routers/agents/grants/create_grant.py`

## Who can do what with an agent

You **see** an agent exactly when you can **use** it: you own it, or its owner
shared it with you — in the org you are acting in. Managing it is the owner's
alone.

| Action | Who |
|---|---|
| See it in the list, open it, chat with it | owner, and the people it is shared with |
| Create an agent | anyone with the **Agents** module (managers, owners — premium) |
| Edit, delete, share, see who it is shared with | its **owner** only |

A community member therefore sees the Agents page as a read-only list of the
agents shared with them. The UI shows Create only with the Agents module, and
edit/delete/share only where `is_owner` is true.

`routers/agents/list.py`, `routers/agents/_shared.py`, `services/access.can_access`

## Whose credentials a shared agent uses

> A shared agent always works on its **owner's data with the owner's access**.
> One switch decides only **who pays for the AI model**.

| | Uses |
|---|---|
| Knowledge bases, datasets, forecasting, Python, email, database read/write | always the **owner's** access |
| Which tools a shared agent has | knowledge-base tools: the **owner's** plan; CRM and email tools: also the **chatter's** role (they reach other people — customer records, mail sent as the owner) |
| The AI model (OpenAI / Anthropic / Google / Kimi) | the switch `shareOwnerCredentials` on the agent: **on** → owner's key, **off** → the key of the person chatting |

With the switch off, the person chatting adds their own key under
**Credentials**, which every plan and every role can open — a person's API keys
belong to their account, not to an org, and storing them is free.

`agent_runtime/context.py` (the switch), `agent_runtime/tools/*` (tools).

## Example: sharing with someone on the free plan

1. They sign up (free). They get their own personal org.
2. You (owner of a premium org) invite them as a community member; they accept.
3. You share your agent with them.
4. They switch to your org and chat with the agent. Your plan applies there,
   and their role opens agent chat.
5. Switch on: it runs on your model key. Switch off: they add their own key
   under Credentials first. Either way the agent's tools use your data.
