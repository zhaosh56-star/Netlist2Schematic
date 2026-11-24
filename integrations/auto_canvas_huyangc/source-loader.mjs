// Development-only resolution against a read-only auto-canvas checkout.
// Production builds may import the project's dist packages instead.
import { registerHooks } from 'node:module';
import { existsSync, readFileSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';

export function registerProjectSources(projectRoot, runtimeRoot) {
  const root = resolve(projectRoot);
  const runtimeParent = pathToFileURL(join(resolve(runtimeRoot), 'entry.mjs')).href;
  registerHooks({
    resolve(specifier, context, nextResolve) {
      if (specifier.startsWith('@icm/')) {
        const [name, ...parts] = specifier.slice(5).split('/');
        const folder = join(root, 'packages', name);
        const manifest = JSON.parse(readFileSync(join(folder, 'package.json'), 'utf8'));
        const key = parts.length ? './' + parts.join('/') : '.';
        const entry = manifest.exports?.[key];
        const source = (typeof entry === 'object' && entry.development) ||
          (parts.length ? './src/' + parts.join('/') : './src/index.ts');
        return { url: pathToFileURL(resolve(folder, source)).href, shortCircuit: true };
      }
      if ((specifier.startsWith('.') || specifier.startsWith('file:')) && context.parentURL) {
        const url = new URL(specifier, context.parentURL);
        if (url.protocol === 'file:' && url.pathname.endsWith('.js')) {
          const path = fileURLToPath(url);
          const ts = path.slice(0, -3) + '.ts';
          if (!existsSync(path) && existsSync(ts))
            return { url: pathToFileURL(ts).href, shortCircuit: true };
        }
      }
      if (!specifier.startsWith('.') && !specifier.startsWith('node:') &&
          !specifier.startsWith('file:') && !specifier.startsWith('/')) {
        try {
          return nextResolve(specifier, { ...context, parentURL: runtimeParent });
        } catch (error) {
          if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error;
        }
      }
      return nextResolve(specifier, context);
    },
  });
}
