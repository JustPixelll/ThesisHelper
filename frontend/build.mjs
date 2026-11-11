import { build } from 'esbuild';
import { copyFile, mkdir } from 'node:fs/promises';

await mkdir('../thesishelper/static', { recursive: true });
await build({
  entryPoints: ['src/app.js'],
  outfile: '../thesishelper/static/app.js',
  bundle: true,
  minify: true,
  sourcemap: false,
  target: ['chrome110', 'edge110', 'firefox115'],
  legalComments: 'linked',
  format: 'esm',
});
for (const name of ['index.html', 'app.css']) {
  await copyFile(`src/${name}`, `../thesishelper/static/${name}`);
}
console.log('Built the self-contained browser interface. No Node.js is required to run ThesisHelper.');
