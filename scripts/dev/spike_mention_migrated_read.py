"""Spike: does the migrated project read return the same character entities
for the mention path that `gflow character list` returns?

Question (from the 2026-11-23 factory outage): `gflow video t2v` with @mentions
stalls on both factory accounts with `mention_character_source_unavailable` +
`AuthExpiredError`, while plain t2v works and `gflow character list` works.

Hypothesis: `AssetIndex.build_for_project` calls
`client.list_characters(project_id, migrated_read=False)` — the labs TRPC
`projectInitialData` route — which now answers 401 (AuthExpiredError). With
`migrated_read=False` the 401 is re-raised instead of falling back to the
migrated project read, so mentions resolve by name and the browser attach
hangs. `gflow character list` calls `list_characters(project_id)` (default
`migrated_read=True`), which falls back to the migrated read on 401/404 and
works.

This spike calls both variants for both factory projects and prints what each
returns/raises. Read-only, zero credits. Run inside the worker container
(where the profiles live):

    docker cp scripts/dev/spike_mention_migrated_read.py <container>:/tmp/spike.py
    docker exec -e DISPLAY=:99 -e GFLOW_CLI_HOME=/data/gflow-cli <container> \
        /opt/gflow-venv/bin/python /tmp/spike.py

Expected if the hypothesis holds: TRPC-only raises AuthExpiredError(401) for
both projects; the default call returns the same entity titles `gflow
character list` shows (azoolabit: mbak_velfit, sereal_pandan,
sereal_coklat_mangkok, sereal_velfit; adminmandala: mbak_velfit,
roticelup_taro, ...).
"""

import asyncio
import sys
from pathlib import Path

PROJECTS = {
    "azoolabit": "69644734-4247-43c4-8d35-90c3ecea5656",
    "adminmandala": "5471bc69-cb82-48c2-bfe8-306081348bda",
}


async def main() -> int:
    from gflow_cli.api.client import FlowApiClient

    ok = True
    for profile, project_id in PROJECTS.items():
        profile_dir = Path(f"/data/gflow-cli/profile_{profile}")
        async with FlowApiClient(profile_dir=profile_dir, headless=True) as client:
            print(f"== {profile} project {project_id} ==", flush=True)
            try:
                chars_trpc = await client.list_characters(
                    project_id, migrated_read=False
                )
                print(
                    "TRPC-only (migrated_read=False): "
                    f"{len(chars_trpc)} chars: "
                    f"{[c.display_name for c in chars_trpc]}",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001 - spike prints whatever it is
                print(
                    f"TRPC-only (migrated_read=False): RAISED "
                    f"{type(exc).__name__}: {exc} "
                    f"[status={getattr(exc, 'status', None)}]",
                    flush=True,
                )
            try:
                chars_default = await client.list_characters(project_id)
                print(
                    "default (migrated_read=True): "
                    f"{len(chars_default)} chars: "
                    f"{[c.display_name for c in chars_default]}",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001 - spike prints whatever it is
                print(
                    f"default (migrated_read=True): RAISED "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
                ok = False
    print("SPIKE-RESULT:", "OK" if ok else "FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
