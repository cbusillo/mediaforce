# Staged encode hosts

An encode host reaches media in one of three ways.

- **Mounted**: the host mounts the media share and reads and writes the same
  paths as the controller.
- **Stream**: the host has no mount. The controller pipes the source to the
  remote encoder and reads the result back from it. A pipe cannot seek, so the
  quality search runs on the controller, and MP4-family sources whose index
  follows the media data cannot be read at all.
- **Staged**: a stream host with a `scratch_root`. The controller copies the
  source into a per-job scratch directory on the host, the quality search and
  the encode both run on the host against that file, and the controller pulls
  the finished output back to its staging path.

A staged host is still a `stream` host everywhere the controller reasons about
it. Outputs are controller-local, staged integrity and storage recovery treat it
as before, and removing `scratch_root` returns it to piping.

## Job lifecycle

Before assigning a file, the scheduler checks the computer's scratch filesystem
using a bounded read-only probe in a background task. It keeps at most one check
in flight per computer and briefly reuses its result (up to three scheduler
poll intervals). Probes skip full or off-schedule computers; schedule-bypassed
work still gets a capacity check. Its budget is the source size plus an output
no larger than the source, quality-search working space (25% of source size),
and a 2 GiB margin, matching the worker's check. A shard needs room for its
largest file because it stages files one at a time.

A computer with too little space, an unknown file size, or a failed capacity
probe is skipped for that file. Another eligible computer can take it; otherwise
the file stays queued with a plain waiting reason and no encode attempt is
charged. The next scheduler pass checks again. Schedule bypass does not bypass
the scratch check. Each computer is measured at most once per pass.

Already admitted work reserves its full scratch budget on that computer,
including work that has not started copying. This is conservative: bytes already
on disk are also reflected in the free-space measurement, so parallel work may
wait until an active file finishes. Unknown active budgets hold new scratch work
on that computer. Admission does not sweep or create directories; a missing
scratch root is measured on its nearest existing ancestor.

A staged computer with an automatic start command is prepared before admission,
in a background task outside the database transaction. Startup, capacity checks
and cleanup do not hold the scheduler while other computers finish or Stop is
requested. The file stays queued without an attempt while it starts. A later
pass rechecks the schedule, host eligibility and scratch
capacity. Capacity probes never wake a computer themselves. If a computer was
started for this check but cannot take work, its configured stop command runs;
if it takes work, the worker retains that cleanup responsibility.
The scheduler remembers an unsuccessful startup or the last measured capacity
of an idle computer it stopped, for the existing host cooldown interval. Readings
taken while it has reserved scratch work do not become startup backoff evidence,
so cleanup can immediately make room for the next file. The scheduler does not
power-cycle that computer on each poll for the same oversized file; another
startable computer or a smaller file can proceed. Capacity is refreshed after
the cooldown, or on the next pass while the computer is already available.
Measurements happen outside the database write transaction and are shared
across all claims and preparation in one scheduler pass.

`mediaforce/encoding/staged_host.py` owns the scratch directory.

1. Sweep the scratch root for directories whose keeper is gone.
2. Check that the scratch filesystem has room for the source, an output no
   larger than the source, quality-search samples and a fixed margin. This
   worker check remains necessary if space changes after scheduler admission.
3. Open a keeper connection that creates `.mediaforce-staged-<id>` and records
   its process id.
4. Copy the source over SSH while hashing it, then compare the host's SHA-256.
5. Run the quality search on the host with its temp directory inside the scratch
   directory.
6. Encode on the host from the staged source to a scratch output.
7. Pull the output to the controller's partial staging path and compare sizes.
   The existing probe, header-only guard, finalize and validation steps follow.
8. Release the keeper and remove the directory.

The staged paths travel in the host payload under `staged_job` for the length of
one encode. That key is never persisted with the job.

## Recovering scratch space

Three layers cover every exit.

- The controller removes the directory when the job ends, whether it succeeded,
  failed or was cancelled.
- The keeper removes the directory when its connection ends. It blocks on the
  connection's input, so a controller crash, a restart or a dropped link makes
  it exit, stop anything still using the directory, and delete it.
- The sweep before each job removes directories whose recorded keeper process no
  longer exists, and directories that never recorded one after a grace period.
  This covers a host power loss or a killed keeper.

A scratch failure is classified `host_scratch` and retried like other host
problems.

## Readiness

A host that runs its own quality search must be able to measure, not merely list
a filter. The host probe runs a one-second `libvmaf` self-test on a generated
pattern and reports `ffmpeg_libvmaf_usable`. A mounted host, a stream host with
mapped source roots, and a staged host are all held back with a plain issue when
that self-test fails. The usual cause on a hand-built Linux ffmpeg is a libvmaf
compiled without `xxd` installed, which silently omits its built-in models.

If a VMAF model still fails to load during a job, the failure is classified
`host_configuration`: it says nothing about the item, and targeted recovery can
requeue it.

## Choosing the scratch root

The path is on the encode host and must be absolute. Prefer a disk. A RAM-backed
filesystem also works, but a large movie plus its samples and output is held in
memory for the whole job.
