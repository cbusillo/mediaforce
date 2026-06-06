export function load({ params }: { params: { prefix?: string } }) {
	const prefix = params.prefix ?? '';
	return { mode: prefix ? ('studio' as const) : ('directory' as const), prefix };
}
