export function formatGiB(bytes: number | null | undefined, digits = 1): string {
	if (!bytes) return '0 GiB';
	return `${(bytes / 1024 ** 3).toFixed(digits)} GiB`;
}

export function formatCounts(mapping: Record<string, number> | null | undefined): string {
	if (!mapping) return 'None';
	const entries = Object.entries(mapping);
	if (entries.length === 0) return 'None';
	return entries.map(([key, value]) => `${value} ${key}`).join(' · ');
}

export function titleCase(value: string): string {
	return value
		.replaceAll('_', ' ')
		.split(' ')
		.filter(Boolean)
		.map((part) => part.charAt(0).toUpperCase() + part.slice(1))
		.join(' ');
}

export function formatTimestamp(seconds: number): string {
	const roundedSeconds = Math.round(seconds);
	const minutes = Math.floor(roundedSeconds / 60);
	const remaining = roundedSeconds % 60;
	return `${minutes}m ${remaining}s`;
}
