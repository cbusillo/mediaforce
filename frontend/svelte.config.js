import adapter from '@sveltejs/adapter-static';
import { relative, sep } from 'node:path';

function useProjectRunes(filename, compileOptions) {
	const relativePath = relative(import.meta.dirname, filename);
	const pathSegments = relativePath.toLowerCase().split(sep);
	const isExternalLibrary = pathSegments.includes('node_modules');

	if (isExternalLibrary || compileOptions.runes) {
		return undefined;
	}

	return { runes: true };
}

/** @type {import('@sveltejs/kit').Config} */
const config = {
	vitePlugin: {
		// defaults to rune mode for the project, except for `node_modules`. Can be removed in Svelte 6.
		dynamicCompileOptions: ({ filename, compileOptions }) =>
			useProjectRunes(filename, compileOptions)
	},
	kit: {
		adapter: adapter({
			fallback: 'index.html'
		})
	}
};

export default config;
