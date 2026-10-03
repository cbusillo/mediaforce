# CI media tools

The Linux acceptance lane downloads a pinned static GPL FFmpeg build from
[BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds), rather than installing
FFmpeg and its dependencies from Ubuntu's package mirror. It installs both
`ffmpeg` and `ffprobe` into the runner's temporary directory and adds them to
the job's path. Local and deployed tool selection is unchanged.

The workflow owns the download URL and SHA-256 digest. Actions caches only the
archive, with an exact key containing the operating system, architecture, and
digest. Both downloaded and restored archives must pass the checksum before
extraction or execution. A missing cache downloads the same pinned artifact;
a checksum mismatch fails the lane. There is no fallback to a moving build or
an unverified binary.

Downloads have a 15-second connection timeout, a 60-second attempt limit, and
at most two retries within a 120-second retry window. The entire installation
step has a five-minute cap. Its log and job summary report installation time
and whether the archive cache matched; the existing acceptance budget report
still measures the entire lane.

When refreshing the pin, choose the last build of a completed month from a
release branch, with the `linux64-gpl` static variant. Upstream retains monthly
builds for two years; refresh before the pinned release expires. Read the
asset's SHA-256 digest from its GitHub release metadata, download it, and
verify agreement before changing both workflow values. A new digest creates
a cold cache automatically. Validate with `actionlint` and a complete cold
acceptance run, then rerun that commit to measure a warm cache. Check the
restore step's hit/miss and the full lane result, rather than inferring cache
coverage from the installation time alone.
