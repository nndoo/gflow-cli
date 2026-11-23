# Spike: @Name mention resolution stalls because the labs character route answers 401

**Date:** 2026-11-23 · **Host served:** flow.google.com · **Locale:** en · **Cost:** $0
(character reads are free; the probe below spent nothing)

## Question

`gflow video t2v` with an `@Name` mention stalls on two factory accounts
(azoolabit, adminmandala): within ~5 s the CLI logs
`mention_character_source_unavailable` + `AuthExpiredError`, then sits idle
until the SSH transport breaks. Plain t2v on the same accounts works, and
`gflow character list` lists the projects' characters. Is the mention path
reading the character catalog from a different source than `character list`,
and does the other source work?

## Probe

[`scripts/dev/spike_mention_migrated_read.py`](../../../scripts/dev/spike_mention_migrated_read.py)
calls `client.list_characters(project_id)` both ways for both factory
projects, in the worker container where the profiles live:
`migrated_read=False` (the call `AssetIndex.build_for_project` makes) and
the default `migrated_read=True` (the call `gflow character list` makes).

## Observation

| Project | `migrated_read=False` (TRPC only) | default (TRPC, migrated fallback) |
|---|---|---|
| azoolabit `69644734-...` | `AuthExpiredError: Authentication expired: UNAUTHORIZED [status=401]` | 6 chars: mbak_velfit, sereal_coklat_mangkok, sereal_velfit, sereal_pandan + 2 untitled |
| adminmandala `5471bc69-...` | `AuthExpiredError: ... [status=401]` | 7 chars: mbak_velfit, roticelup_strawberry, roticelup_coklat, roticelup_vanilla, roticelup_taro + 2 others |

The default call logs `character.labs_route_refused issue_ref=#864
status=401` and falls back to the migrated project read, which returns the
entities. The TRPC-only call raises instead, because
`_labs_project_route_refused` is only consulted when `migrated_read` is
true (`_flow_characters.py: list_characters`).

## Conclusion

The labs TRPC `projectInitialData` route is retired for these accounts
(it answers 401), and mention resolution was the only production caller
still forcing the TRPC-only read (`mentions.py: build_for_project`). With
no catalog, every `@Name` degraded to a by-name mention and the run stalled
in the browser attach. Fix: resolve mentions with the same read
`gflow character list` uses (drop `migrated_read=False`), so the 401 falls
back to the migrated project read and mentions resolve to entity ids.
