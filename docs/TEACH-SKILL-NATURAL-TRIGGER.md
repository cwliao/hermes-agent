# Teach skill natural-language trigger

Operational note, 2026-08-09 (Asia/Taipei).

The live gateway's `tool_search` tier is not the cause of the `teach` skill
miss. `teach` is a user-local skill under `~/.hermes/skills/`, while the
release slots contain the Hermes runtime only. The runtime's skill prompt
lists `teach` as a visible skill and keeps `skill_view` in the core-visible
tool set; it does not rank `teach` through the deferred-tool BM25 catalog.

The durable live fix is the backed-up frontmatter description in:

`~/.hermes/skills/teach/SKILL.md`

It begins with an explicit natural-language trigger and instructs the model to
call `skill_view(name="teach")` for requests beginning with `teach me`. The
backup made before the live edit is:

`~/.hermes/skills/teach/SKILL.md.bak.pre-teach-natural-trigger-20260809212127`

Do not replace this user-local skill metadata during a future skills sync or
release operation. If the skill is reinstalled from its upstream source, keep
the explicit trigger description and rerun the same-release CLI smoke test.

Evidence from the live release `v2026.8.3-t0140-058e9da17b`: after the gateway
restart, `hermes chat -q 'teach me trigger verification' -m ornith:35b
--provider openai-api` completed `skill_view` and ended with `tool_turns=2`.
