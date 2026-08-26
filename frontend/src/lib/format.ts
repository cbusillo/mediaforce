const FILE_SIZE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'] as const;
const BINARY_FILE_SIZE_FACTORS: Readonly<Record<string, number>> = {
	KiB: 1024,
	MiB: 1024 ** 2,
	GiB: 1024 ** 3,
	TiB: 1024 ** 4
};

export function formatFileSize(value: unknown, unavailable = '—'): string {
	const bytes = Number(value);
	if (!Number.isFinite(bytes) || bytes <= 0) return unavailable;

	let scaled = bytes;
	let unitIndex = 0;
	while (scaled >= 1000 && unitIndex < FILE_SIZE_UNITS.length - 1) {
		scaled /= 1000;
		unitIndex += 1;
	}

	const maximumFractionDigits = Number.isInteger(scaled)
		? 0
		: scaled >= 100
			? 0
			: scaled >= 10
				? 1
				: 2;
	return `${scaled.toLocaleString('en-US', { maximumFractionDigits })} ${FILE_SIZE_UNITS[unitIndex]}`;
}

export function normalizeFileSizeCopy(value: string): string {
	return value.replace(
		/\b(\d+(?:\.\d+)?)\s*(KiB|MiB|GiB|TiB)\b/g,
		(_match, amount: string, unit: string) =>
			formatFileSize(Number(amount) * BINARY_FILE_SIZE_FACTORS[unit])
	);
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

export function toAnchorFragment(value: string): string {
	const normalized = value
		.toLowerCase()
		.replaceAll(/[^a-z0-9]+/g, '-')
		.replaceAll(/^-+|-+$/g, '');

	return normalized || 'item';
}

export function hostSettingsAnchor(hostKey: string): string {
	return `remote-worker-${toAnchorFragment(hostKey)}`;
}

type CalendarParts = {
	year: number;
	month: number;
	day: number;
};

function scheduleTimeZone(timeZone: string | null | undefined): string | undefined {
	const normalized = String(timeZone ?? '').trim();
	if (!normalized) return undefined;
	try {
		new Intl.DateTimeFormat('en-US', { timeZone: normalized }).format(new Date(0));
		return normalized;
	} catch {
		return undefined;
	}
}

function calendarParts(value: Date, timeZone: string | undefined): CalendarParts {
	const parts = new Intl.DateTimeFormat('en-US', {
		timeZone,
		year: 'numeric',
		month: 'numeric',
		day: 'numeric'
	}).formatToParts(value);
	const partValue = (type: Intl.DateTimeFormatPartTypes): number =>
		Number(parts.find((part) => part.type === type)?.value ?? 0);
	return {
		year: partValue('year'),
		month: partValue('month'),
		day: partValue('day')
	};
}

function calendarDayOffset(value: Date, now: Date, timeZone: string | undefined): number {
	const target = calendarParts(value, timeZone);
	const current = calendarParts(now, timeZone);
	return Math.round(
		(Date.UTC(target.year, target.month - 1, target.day) -
			Date.UTC(current.year, current.month - 1, current.day)) /
			86_400_000
	);
}

export function formatScheduleMoment(
	value: string | null | undefined,
	timeZone?: string | null,
	now = new Date()
): string | null {
	const timestamp = Date.parse(String(value ?? ''));
	if (!Number.isFinite(timestamp)) return null;
	const date = new Date(timestamp);
	const resolvedTimeZone = scheduleTimeZone(timeZone);
	const dayOffset = calendarDayOffset(date, now, resolvedTimeZone);
	const timeCopy = new Intl.DateTimeFormat('en-US', {
		timeZone: resolvedTimeZone,
		hour: 'numeric',
		minute: '2-digit',
		timeZoneName: 'short'
	}).format(date);
	if (dayOffset === 0) return `today at ${timeCopy}`;
	if (dayOffset === 1) return `tomorrow at ${timeCopy}`;
	const dateCopy = new Intl.DateTimeFormat('en-US', {
		timeZone: resolvedTimeZone,
		weekday: 'short',
		month: 'short',
		day: 'numeric'
	}).format(date);
	return `${dateCopy} at ${timeCopy}`;
}

export function formatScheduleCountdown(
	value: string | null | undefined,
	now = new Date()
): string | null {
	const timestamp = Date.parse(String(value ?? ''));
	if (!Number.isFinite(timestamp)) return null;
	const remainingMinutes = Math.ceil((timestamp - now.getTime()) / 60_000);
	if (remainingMinutes <= 0) return null;
	const days = Math.floor(remainingMinutes / (24 * 60));
	const hours = Math.floor((remainingMinutes % (24 * 60)) / 60);
	const minutes = remainingMinutes % 60;
	if (days > 0) return hours > 0 ? `${days}d ${hours}h` : `${days}d`;
	if (hours > 0) return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`;
	return `${remainingMinutes}m`;
}
