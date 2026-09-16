"""Instruction-provenance and task-contract governance for this project.

Scope note (read before extending): this package governs how CODE IN THIS
PROJECT dispatches and accepts results from subagents/forks/child tasks —
it is a policy layer other scripts in this repo must call through. It does
not and cannot modify the Claude Code platform's own Agent/fork tool
internals, which live outside this repository. What it *does* guarantee,
for any code path in this project that routes through it, is the property
that the real a1a55326e796526bd incident violated: a child task's own
generated text (messages, markdown, logs, artifacts) can never itself
become an instruction that changes scope, advances a phase, or authorizes
production.
"""
