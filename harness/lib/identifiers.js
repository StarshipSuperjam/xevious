// Reads the generator-emitted identifier manifest (src/xevious/runtime_identifiers.json)
// so the harness never keeps its own copy of the project's variable names. Every read
// resolves through here; an unknown id is a hard error, never a silent `undefined`.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = resolve(here, '..', '..');
export const MANIFEST_PATH = resolve(REPO_ROOT, 'src', 'xevious', 'runtime_identifiers.json');

const manifest = JSON.parse(readFileSync(MANIFEST_PATH, 'utf8'));

/** Generator constants the scenarios assert against (shot-cap ceiling, reload cadence…). */
export const constants = manifest.constants;

/**
 * Resolve a Scratch variable/list id to its current { name, scope, kind }. Throws if the
 * id is absent — the whole point of the manifest: a rename in the generator turns into a
 * red test here rather than a vacuous read elsewhere.
 */
export function variable(id) {
  const info = manifest.variables[id];
  if (!info) {
    throw new Error(
      `harness: unknown variable id '${id}' — not in runtime_identifiers.json. ` +
        `Either the generator renamed/removed it (update the harness reference) or the ` +
        `manifest is stale (run tools/game_director.py generate).`,
    );
  }
  return info;
}
