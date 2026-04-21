import { writable } from 'svelte/store';

export type ToastKind = 'success' | 'error' | 'info' | 'warning';

export type Toast = {
	id: string;
	kind: ToastKind;
	eyebrow?: string;
	heading: string;
	lede: string;
	detail?: string;
	dismissLabel?: string;
	onDismiss?: () => void;
	autoCloseMs?: number | null;
};

const toastsStore = writable<Toast[]>([]);
const toastTimers = new Map<string, ReturnType<typeof setTimeout>>();

function clearTimer(id: string) {
	const timer = toastTimers.get(id);
	if (!timer) return;
	clearTimeout(timer);
	toastTimers.delete(id);
}

function scheduleRemoval(id: string, autoCloseMs: number | null | undefined) {
	clearTimer(id);
	if (autoCloseMs == null || autoCloseMs <= 0) return;
	const timer = setTimeout(() => {
		toastTimers.delete(id);
		toastsStore.update((items) => items.filter((item) => item.id !== id));
	}, autoCloseMs);
	toastTimers.set(id, timer);
}

function upsert(toast: Toast) {
	let inserted = false;
	toastsStore.update((items) => {
		const index = items.findIndex((item) => item.id === toast.id);
		if (index === -1) {
			inserted = true;
			return [toast, ...items];
		}
		const next = [...items];
		next[index] = {
			...next[index],
			...toast,
			id: next[index].id
		};
		return next;
	});
	scheduleRemoval(toast.id, toast.autoCloseMs);
	return inserted;
}

function push(kind: ToastKind, heading: string, lede: string) {
	const toast: Toast = {
		id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
		kind,
		heading,
		lede,
		autoCloseMs: 4500
	};
	upsert(toast);
}

export const toasts = {
	subscribe: toastsStore.subscribe,
	upsert,
	remove(id: string) {
		clearTimer(id);
		toastsStore.update((items) => items.filter((item) => item.id !== id));
	},
	dismiss(id: string) {
		let dismissed: Toast | undefined;
		clearTimer(id);
		toastsStore.update((items) => {
			dismissed = items.find((item) => item.id === id);
			return items.filter((item) => item.id !== id);
		});
		const onDismiss = dismissed?.onDismiss;
		if (onDismiss) {
			onDismiss();
		}
	},
	success(title: string, body: string) {
		push('success', title, body);
	},
	error(title: string, body: string) {
		push('error', title, body);
	},
	warning(title: string, body: string) {
		push('warning', title, body);
	},
	info(title: string, body: string) {
		push('info', title, body);
	}
};
