# Bundled server and Sparkle delivery proposal

Draft for the review requested on [the packaging issue](https://github.com/cbusillo/mediaforce/issues/1044).
Landing this document records a proposal; it does not approve its unsettled
choices, authorize a release, or change the installed controller.

## Settled direction and present implementation

The [Director's packaging decision](https://github.com/cbusillo/mediaforce/issues/1044#issuecomment-6401490426)
settles notarized GitHub releases, Sparkle updates and the server inside the
Mac app. One update replaces both. The Mac App Store is not pursued.
The [interim trim](https://github.com/cbusillo/mediaforce/pull/1301) is landed
and stays until bundled delivery replaces clone-based deployment.

At the start of this draft, `scripts/build-mac-app.sh` builds and Developer ID
signs the SwiftUI app; `macos/project.yml` has no Sparkle dependency. The app's
`BackgroundService` controls an existing `com.mediaforce.web` LaunchAgent.
`controller_deploy.py` builds an independent clone and environment, saves a
checked database backup, switches that LaunchAgent and installs the app.
There is no notarized Mediaforce release workflow or bundled Python engine.
[Release flow](release-flow.md) remains the operating procedure until cutover.

## Recommendation for review

Keep the existing per-user background service and API, but launch its server
from the installed app. Extend the current deploy transaction into one local
update transaction around Sparkle's standard installer. Keep a complete old
app together with the matching database backup, saved launch configuration
and settings evidence. Do not introduce a privileged daemon, second server
installer, release-clone manager, channel hierarchy or custom Sparkle installer.

The proposed service lifecycle, Python packaging, update consent, feed hosting
and rollback retention below need the Director's review before implementation.
They are recommendations, not additional decisions inferred from Sparkle.

## Bundle and mutable data

Proposed logical layout, resolved relative to the installed bundle:

```text
Mediaforce.app/Contents/
  MacOS/Mediaforce                    native UI
  MacOS/mediaforce-server             launcher for the embedded interpreter
  Frameworks/Python.framework/        relocatable CPython and native libraries
  Frameworks/Sparkle.framework/       Sparkle, including its signed helpers
  Resources/server/                   wheel, dependencies, defaults, migrations
  Resources/                          assets, licenses and build identity
```

Use the existing BD_to_AVP embedded-Python staging approach as the starting
recipe, adapted to Mediaforce's wheel and locked dependencies. Build for the
current Mac target without silently reducing its supported CPU/OS range;
check all native dependencies and set the feed's minimum-system metadata from
the built app. Prove relocation and signing before choosing final subpaths.
Python must not depend on a developer checkout, venv symlink, `uv`, Homebrew
Python, Xcode or a package download at launch. Disable bytecode writes inside
the bundle. Include migrations and package defaults explicitly; installed
startup must not find `alembic.ini` or resources through the build checkout.

Preserve the current config, database, settings, credential permissions, logs,
review clips and original-media rollback locations outside the app. Carry
forward custom `--config` and its database identity, rather than silently
opening the default database. No library roots, private config, credentials,
databases or review media enter the archive. Extend the existing package
contents verifier to the app artifact instead of creating another privacy gate.

Keep ffmpeg/ffprobe, remote hosts, SSH and shared-storage setup as currently
configured external capabilities. This proposal bundles the server, not those
systems. Preflight them and show the existing actionable failures; do not
install packages or change system settings during an update. Redistributing
encoder binaries, if later wanted, is a separate choice.

The per-user LaunchAgent keeps the existing label and log paths, with an
absolute executable inside the installed app, a persistent working directory
and the saved config option. Window closure must leave background work and
notifications working. A second app copy must not seize the same listener or
database. App moves, missing bundles and stale launch paths need a visible
repair through the same service path, with no second concurrent server.

## One update transaction

Sparkle owns download, archive verification, replacement and relaunch. The app
and bundled service own safe admission and database recovery. Start with
standard Sparkle update UI and automatic checks; recommend installation only
after the user accepts it, then defer until all work is safely idle. Silent
automatic installation is an unsettled product choice and is not enabled here.

1. Download and verify the exact candidate without stopping current work.
   Serialize installation with the existing deploy/cleanup locks. Fence new
   encodes, samples, scans, measurements and publishing admissions; let active
   files finish, including remote workers, validation and publication.
   Preserve the previous queue pause state and the M4 night window.
2. Recheck authoritative job state, remote-process custody and publishing locks.
   `/api/release/work` counts alone are insufficient, as release-flow explains.
   Unknown custody leaves the old app running and the update waiting. Surface
   every reason in the app. Canceling preparation restores prior admissions.
3. Save the complete old signed app and launch configuration in a fresh stage
   beside the configured database's existing `deployments/`. Protect that stage
   through `retain_deploy_stage`; copy and verify before replacement. Do not
   rely on Sparkle's temporary files or Trash as the rollback copy.
4. Stop the server and verify the listener and exclusive database custody.
   Reuse the SQLite online backup and `quick_check` helpers to save
   `before.sqlite` before candidate startup can migrate. Save settings and
   configuration evidence without logging secrets. Failure before replacement
   restores the unchanged old service and its previous pause state.
5. Permit Sparkle installation only after those proofs. Cover its quit,
   install-on-quit, background-install and relaunch paths; postponing relaunch
   alone is not a replacement gate. Prevent launchd KeepAlive from restarting
   the old executable during replacement. A crash/reboot must encounter the
   saved stage before either server can open the database or resume work.
6. Start the candidate through the saved service path. Verify the exact bundled
   UI/server build identity, database migration, API health, queued work and
   external capability availability. Resume only a queue that was running
   before the update. A lost response is reconciled from durable state, never
   retried as a new install.

Qualification must demonstrate the Sparkle delegates can enforce step 5 on
all installation paths. If standard integration cannot, bring that evidence
back to the Director before substituting an installer or weakening the gate.
The app can quit normally without installing while work is active.

## Rollback and retention

Before migration, a failed update may restart the unchanged old app only when
its database is proven untouched. After migrations or uncertain startup,
leave the service stopped and use the existing stopped rollback procedure:
verify custody, retain the failed database for diagnosis, restore the stage's
checked database copy and its matching old app and launch configuration,
then verify identity, health and work before resuming. Never run old code on a
possibly migrated database. No originals or encoded outputs are deleted.

Recommend rollback only before releasing new work admissions. Once the new
version has encoded or published files, restoring its pre-update database can
lose those records; stop and reconcile those effects before choosing recovery.
A forward fix may be safer. Sparkle is not the database rollback engine, and
removing an appcast item does not downgrade installed apps.

Reuse the existing deployment-stage retention count and protected-stage
pointer for bundled backups. Keep the prior healthy app/database pair until
a later verified update creates another usable pair. Never overwrite or delete
the last rollback copy. During first cutover, keep the legacy runtime, its
recorded rollback and protection registry; retire old clones only after their
service/job/rollback dependencies are disproved through verified retirement.
Do not delete a last copy without the Director's approval. Delete the interim
clone trim with the clone-creation path when neither is used, not beforehand.

## Notarized GitHub delivery and agent publishing

Adapt the proven BD_to_AVP release sequence, not its channel system, PyPI
publication, product identity or private credentials. Proposed stable delivery:
GitHub Release assets and one HTTPS appcast on this repository's GitHub Pages.
Use Sparkle's standard framework and appcast tools. Keep one committed source
for app version/build identity; derive app, server, tag and appcast metadata
from it. Build numbers increase across attempts; never overwrite published
bytes under an existing version. Source builds do not contact the live feed.

1. A Director release request starts preparation. An agent lands version and
   release-note changes through the merge train and selects the exact green,
   reviewed main commit. A merge by itself never publishes a release.
2. A main-only workflow, dispatched by the existing automation identity,
   builds from that exact commit and lockfile on macOS. It runs the required
   repo gates and installed-artifact smokes in isolated support directories.
   Build UI and server together, stage their dependencies and license notices,
   and verify artifact contents and relocation.
3. Sign nested native libraries, interpreter, Sparkle helpers and executables
   inside out, then the outer app, with Developer ID and hardened runtime.
   Verify the whole app; do not use blanket deep signing as the signing recipe.
   Submit with `notarytool`, require acceptance, staple and validate the ticket,
   then create the final distribution archive. Verify Gatekeeper assessment.
4. Sign the final archive with Mediaforce's Sparkle EdDSA key. Create a draft
   GitHub Release containing that archive, checksums and release notes; build
   its appcast item against those immutable asset URLs and exact build/OS data.
   Download and verify the uploaded bytes and signature before publication.
5. Publish the release, then publish the validated feed last, serializing feed
   updates so a stale workflow cannot replace newer history. Verify served
   feed and download. Resume an interrupted draft only after reading its exact
   tag, asset digests and publication state. Never replay an uncertain publish.
   A feed failure after release publication leaves a downloadable release;
   reconcile the feed without rebuilding or replacing the published archive.

Keep signing/notarization credentials and the EdDSA private key in their
purpose-specific existing protected CI custody. Only the public Sparkle key
ships in the app. Never copy keys from BD_to_AVP. Mediaforce currently has no
release workflow; availability of its CI signing inputs, EdDSA key, Pages
setup and automation permissions has not been established by this source
review. Before any release request, walk the full publishing path read-only
and report missing setup together. Any new access grant goes to the Director;
no secrets or policy changes are authorized by approval of this document.

## Delivery order after approval

Open the implementation issue required by the packaging decision only after
the review is resolved, linked to the source decision and this plan. Deliver
in this order, without a second lasting deployment system:

1. Embedded-server artifact and installed-bundle lifecycle, proved offline in
   an isolated user/support directory, including custom config and migrations.
2. Safe update/rollback integration and Sparkle; prove two signed versions
   through the real installer, busy/deferred installs, quit/reboot interruption,
   backup failure, migration failure, rollback and retained work. Use existing
   deployment, custody and package fixtures; do not assert workflow text.
3. Notarized release workflow, feed and agent publishing procedure. Prove the
   downloaded artifact and installed older-to-newer update in isolation before
   requesting the first real release. The Director's macOS permission prompts
   remain theirs. No production-library test is implied by a packaging smoke.
4. First requested release performs one safe transition from the existing
   LaunchAgent/runtime. Verify app/server identity and saved rollback; remove
   the replaced clone-deploy code, interim trim and obsolete docs in the same
   cutover change once its replacement is usable. Retain legacy copies until
   verified retirement is safe. Update release-flow.md to the delivered path.

Removed when delivered: per-deploy Git clone and venv, local build on the
controller, separate server/app deployment and its interim clone trim.
Added upkeep: embedded runtime assembly, signing/notarization and Sparkle
integration. No new service, channel matrix, retention setting or registry.
The implementation's net lines are unknown until it exists; report measured
adds/deletes per change. This proposal adds documentation only.

## Reference behavior checked for this draft

- [Sparkle standard setup](https://sparkle-project.org/documentation/) and
  [updater delegate hooks](https://sparkle-project.org/documentation/api-reference/Protocols/SPUUpdaterDelegate.html).
  Installation deferral is distinct from relaunch deferral; it needs the
  installed two-version proof above.
- [Apple distribution signing](https://developer.apple.com/documentation/xcode/creating-distribution-signed-code-for-the-mac/)
  and [notarization](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution).
- [BD_to_AVP release procedure](https://github.com/cbusillo/BD_to_AVP/blob/main/docs/release-process.md)
  and [embedded Python staging](https://github.com/cbusillo/BD_to_AVP/blob/main/scripts/embedded_python.py).
  These are reference implementations, not Mediaforce release authority.
