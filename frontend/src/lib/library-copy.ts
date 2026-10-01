// Owner-facing Library wording shared by the TV, Movie, and Other libraries. Tests read it from here.
export const libraryCopy = {
	ready: 'Ready for you',
	newestSeason: 'Newest season',
	newestSeasonModes: {
		auto: 'Auto: skip it while the show is airing',
		on: 'Always skip the newest season',
		off: 'Include the newest season'
	},
	airingUnknown: 'Not sure if the show is still airing',
	skipped: 'Skipped for now',
	newestSeasonUnchanged: 'The newest-season setting does not change.'
} as const;
