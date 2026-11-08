# Bundled audio asset library

All tracks and effects used by the project are stored inside this workspace:

- `music/` — Mixkit Free License background tracks. The assembler selects a track using mood tags in the filename/registry, loops it as needed, and ducks it under narration.
- `sound_effects/` — Mixkit Free License WAV effects. Screenplays can refer to these exact stems in `sound_effect`:
  - `fast-rocket-whoosh`
  - `fast-sweep-transition`
  - `cinematic-swoosh-heartbeat`
  - `cinematic-tunnel-whoosh`

Source and license notes are stored beside the assets in each folder's `ATTRIBUTION.txt`. These assets are downloaded copies for use inside generated videos, not for redistribution as standalone audio. Review the current Mixkit license terms before publishing or monetizing.