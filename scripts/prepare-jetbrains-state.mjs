import crypto from 'node:crypto';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

export const EXTERNAL_SYSTEM_ATTRIBUTE = 'external.system.id="pyproject.toml"';
export const FRONTEND_EXCLUSION = '      <excludeFolder url="file://$MODULE_DIR$/frontend" />';

/** @param {string} original */
export function normalizePythonModule(original) {
	if (!original.includes('<module type="PYTHON_MODULE"') || !original.includes('    </content>')) {
		throw new Error('Unexpected Python preparation module format; no normalization applied');
	}
	let normalized = original.replace(
		'<module type="PYTHON_MODULE"',
		`<module ${EXTERNAL_SYSTEM_ATTRIBUTE} type="PYTHON_MODULE"`
	);
	if (!normalized.includes(FRONTEND_EXCLUSION)) {
		normalized = normalized.replace('    </content>', `${FRONTEND_EXCLUSION}\n    </content>`);
	}
	return normalized;
}

/** @param {string} moduleFile */
export function normalizePythonModuleFile(moduleFile) {
	const original = fs.readFileSync(moduleFile, 'utf8');
	const normalized = normalizePythonModule(original);
	if (normalized !== original) fs.writeFileSync(moduleFile, normalized);
}

/** @param {string[]} manifests */
export function dependencyDigest(manifests) {
	const digest = crypto.createHash('sha256');
	for (const manifest of manifests) digest.update(fs.readFileSync(manifest));
	return digest.digest('hex');
}

const entryFile = process.argv[1];
if (
	entryFile &&
	fs.existsSync(entryFile) &&
	fs.realpathSync(entryFile) === fs.realpathSync(fileURLToPath(import.meta.url))
) {
	switch (process.argv[2]) {
		case 'normalize':
			normalizePythonModuleFile(process.argv[3]);
			break;
		case 'digest':
			process.stdout.write(dependencyDigest(process.argv.slice(3)));
			break;
		default:
			throw new Error('Expected normalize or digest preparation action');
	}
}
