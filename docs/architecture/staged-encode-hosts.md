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

`mediaforce/encoding/staged_host.py` owns the scratch directory.

1. Sweep the scratch root for directories whose keeper is gone.
2. Check that the scratch filesystem has room for the source, an output no
   larger than the source, quality-search samples and a fixed margin.
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

## Choosing the scratch root

The path is on the encode host and must be absolute. Prefer a disk. A RAM-backed
filesystem also works, but a large movie plus its samples and output is held in
memory for the whole job.
