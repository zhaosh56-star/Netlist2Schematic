// Wrap a public source-preparation export, leaving the original editor bundle
// and its compiler/importer untouched. The host supplies the verified alias.
export * from './native-convert-import-sources.js';
import { __N2S_PREPARE_EXPORT__ as nativePrepare } from './native-convert-import-sources.js';
import { prepareCompatibleSources } from './spice-compat.mjs';

export function __N2S_PREPARE_EXPORT__(sources) {
  const prepared = prepareCompatibleSources(nativePrepare, sources);
  globalThis.__netlist2schematicImportReport = prepared.compatibility;
  if (prepared.compatibility.changes.length)
    console.info('Netlist2Schematic import compatibility', prepared.compatibility);
  return prepared;
}
