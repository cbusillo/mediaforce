import { writable } from 'svelte/store';

export type ToastKind = 'success' | 'error' | 'info';

export type Toast = {
	id: string;
	kind: ToastKind;
	title: string;
	body: string;
};

const toastsStore = writable<Toast[]>([]);

function push(kind: ToastKind, title: string, body: string) {
	const toast: Toast = {
		id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
		kind,
		title,
		body
	};
	toastsStore.update((items) => [...items, toast]);
	setTimeout(() => {
		toastsStore.update((items) => items.filter((item) => item.id !== toast.id));
	}, 4500);
}

export const toasts = {
	subscribe: toastsStore.subscribe,
	remove(id: string) {
		toastsStore.update((items) => items.filter((item) => item.id !== id));
	},
	success(title: string, body: string) {
		push('success', title, body);
	},
	error(title: string, body: string) {
		push('error', title, body);
	},
	info(title: string, body: string) {
		push('info', title, body);
	}
};
