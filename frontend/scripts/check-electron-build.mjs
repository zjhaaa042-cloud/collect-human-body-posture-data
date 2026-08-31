import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';


const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const indexPath = path.join(scriptDirectory, '..', 'build', 'index.html');
const html = readFileSync(indexPath, 'utf8');

const absoluteLocalAssets = [...html.matchAll(/\b(?:src|href)="\/(?!\/)/g)];
if (absoluteLocalAssets.length > 0) {
  throw new Error(
    'Electron build contains root-absolute assets. Set Vite base to ./ before packaging.'
  );
}
if (!html.includes('id="root"')) {
  throw new Error('Electron build is missing the React root element.');
}

console.log('Electron file:// asset paths verified');
