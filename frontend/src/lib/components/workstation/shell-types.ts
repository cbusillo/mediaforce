export type ShellRouteId = 'queue' | 'ops' | 'completed' | 'settings' | 'studio';
export type ShellTone = 'active' | 'ready' | 'wait' | 'fail' | 'idle';

export type StatusTile = {
	label: string;
	value: string;
	detail?: string;
	tone?: ShellTone;
	mono?: boolean;
};

export type FooterSignal = {
	label: string;
	value: string;
	tone?: ShellTone;
};
