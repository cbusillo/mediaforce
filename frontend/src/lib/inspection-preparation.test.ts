import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

import {
	dependencyDigest,
	EXTERNAL_SYSTEM_ATTRIBUTE,
	FRONTEND_EXCLUSION,
	normalizePythonModule,
	normalizePythonModuleFile
} from '../../../scripts/prepare-jetbrains-state.mjs';

const helperFile = fileURLToPath(
	new URL('../../../scripts/prepare-jetbrains-state.mjs', import.meta.url)
);

const moduleXml = `<module type="PYTHON_MODULE" version="4">
    <content url="file://$MODULE_DIR$">
      <excludeFolder url="file://$MODULE_DIR$/operator-folder" />
    </content>
</module>
`;

describe('Python module preparation', () => {
	it('sets the external model and excludes frontend while preserving operator exclusions', () => {
		const normalized = normalizePythonModule(moduleXml);
		expect(normalized).toContain(EXTERNAL_SYSTEM_ATTRIBUTE);
		expect(normalized.split(FRONTEND_EXCLUSION)).toHaveLength(2);
		expect(normalized).toContain('file://$MODULE_DIR$/operator-folder');
	});

	it('keeps one frontend exclusion when the input already contains it', () => {
		const original = moduleXml.replace('    </content>', `${FRONTEND_EXCLUSION}\n    </content>`);
		expect(normalizePythonModule(original).split(FRONTEND_EXCLUSION)).toHaveLength(2);
	});

	it.each(['unrecognized input', '<module type="JAVA_MODULE">\n    </content>'])(
		'rejects unsupported input without overwriting the file',
		(original) => {
			const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'mediaforce-module-'));
			const moduleFile = path.join(directory, 'module.iml');
			try {
				fs.writeFileSync(moduleFile, original);
				expect(() => normalizePythonModuleFile(moduleFile)).toThrow();
				expect(fs.readFileSync(moduleFile, 'utf8')).toBe(original);
			} finally {
				fs.rmSync(directory, { recursive: true });
			}
		}
	);

	it('writes the normalized module to the requested fixture file', () => {
		const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'mediaforce-module-'));
		const moduleFile = path.join(directory, 'module.iml');
		try {
			fs.writeFileSync(moduleFile, moduleXml);
			execFileSync(process.execPath, [helperFile, 'normalize', moduleFile]);
			const normalized = fs.readFileSync(moduleFile, 'utf8');
			expect(normalized).toContain(EXTERNAL_SYSTEM_ATTRIBUTE);
			expect(normalized).toContain(FRONTEND_EXCLUSION);
		} finally {
			fs.rmSync(directory, { recursive: true });
		}
	});
});

describe('frontend preparation digest', () => {
	it.each(['package.json', 'package-lock.json'])('changes when %s changes', (changedFile) => {
		const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'mediaforce-dependencies-'));
		const manifests = ['package.json', 'package-lock.json'].map((name) =>
			path.join(directory, name)
		);
		try {
			fs.writeFileSync(manifests[0], '{"name":"fixture"}');
			fs.writeFileSync(manifests[1], '{"lockfileVersion":3}');
			const before = dependencyDigest(manifests);
			expect(
				execFileSync(process.execPath, [helperFile, 'digest', ...manifests], { encoding: 'utf8' })
			).toBe(before);
			expect(dependencyDigest(manifests)).toBe(before);
			fs.appendFileSync(path.join(directory, changedFile), '\n');
			expect(dependencyDigest(manifests)).not.toBe(before);
		} finally {
			fs.rmSync(directory, { recursive: true });
		}
	});
});
