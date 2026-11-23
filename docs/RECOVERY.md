# Railway recording recovery and archives

The `paper-worker` on Railway project `bitprit` reported a full-disk SQLite error around 9:34 a.m. Eastern on November 21. The deployed database is `/data/quarter-hour.sqlite3`. The volume and its original files are the primary recovery source. Preserve them in place while arranging a read-only export. Keep the service stopped during a raw file copy, and leave the release pinned during recovery.

## Preserve the original recordings

1. Identify the existing `paper-worker` volume by its service and `/data` mount. Keep the main database and all sidecars present at the time of capture with their original filenames. A `quarter-hour.sqlite3-wal` can contain committed observations that are absent from the main file. Preserve `quarter-hour.sqlite3-shm` and `quarter-hour.sqlite3-journal` if they are present.
2. Copy the stopped file set directly to a new local folder on a filesystem with space for the original set, a working copy, and a standalone archive. Keep the original Railway files in place. If using a downloaded volume export, note the export timestamp and the crash log timestamp.
3. Preserve the downloaded file set unchanged, and make a working copy in a second folder. Run the SQLite archive operation on that working copy only. Save the resulting database and its JSON manifest together.
4. Verify the standalone archive on the destination and check its last observation time, counts and finalized-result coverage against the known runtime logs. If verification fails, retain the original file set and make a fresh working copy when more evidence is available.

### Railway file access

Railway CLI 5.75.0 supports `railway volume files list` and `download` on an existing active deployment. From an authenticated CLI already linked to `bitprit`, identify the existing volume and try its file list. Use the ID shown for the `/data` volume. These commands read existing resources. [Railway CLI volume commands](https://github.com/railwayapp/cli/blob/v5.75.0/src/commands/volume.rs)

```powershell
railway volume list --json
$VolumeId = Read-Host "ID of the existing paper-worker /data volume"
railway volume files --volume $VolumeId list / --json
```

Use raw downloads only if the file route is accessible and the recorder is no longer writing. From the repository folder, create a new local destination. Download the main file plus each sidecar shown by the list.

```powershell
$Rescue = Join-Path $PWD ("exports\railway-rescue-" + (Get-Date -Format yyyyMMdd-HHmmss))
New-Item -ItemType Directory -Path $Rescue
railway volume files --volume $VolumeId download /quarter-hour.sqlite3 $Rescue
```

For a listed sidecar, use the corresponding remote name with the same destination. For example, use `railway volume files --volume $VolumeId download /quarter-hour.sqlite3-wal $Rescue` when the WAL is present. Record the download results and keep a hash of each original file.

**If Railway reports no active deployment or cannot access the files**, use any existing provider export of the stopped volume or ask Railway for a read-only export of the file set. The current CLI requires an active deployment for volume file access. With a crashed service and unavailable file access, an existing export or provider assistance is the recovery prerequisite. Keep the original volume intact. A restart, redeployment, restore over the original, or plan upgrade is outside this recovery. [Railway CLI active deployment check](https://github.com/railwayapp/cli/blob/v5.75.0/src/controllers/project.rs)

### Verify and archive a local recovery copy

Run from the repository checkout after downloading all files. Keep the downloads in `$Rescue` and open only the working copy. The standalone helper uses Python's standard library and also works as a single copied script.

```powershell
$Work = "$Rescue-working"
$Archive = "$Rescue-archive.sqlite3"
$Manifest = "$Rescue-archive.json"
if (Test-Path $Work) { throw "Choose a new working folder for this recovery copy." }
Copy-Item -LiteralPath $Rescue -Destination $Work -Recurse
py -3.12 quarter_hour/archive.py backup "$Work\quarter-hour.sqlite3" $Archive |
    Set-Content -Path $Manifest -Encoding utf8
py -3.12 quarter_hour/archive.py verify $Archive --manifest $Manifest
```

Check that the backup and verification each exit with code 0. Compare the last observation and counts with the logs. A failure mid-cycle may leave a committed observation without its corresponding forecast or decision. If SQLite reports a missing or inaccessible sidecar, check the captured file set before retrying on another working copy. Retain any copy that fails an integrity check for further recovery. The archival operation checks SQLite integrity and foreign keys, includes committed WAL data, and prints the SHA-256, byte size, schema version, table counts, and UTC observation and result ranges. Archive verification compares the received copy with that manifest. The archive opens as a standalone SQLite file.

### Backups during normal operation

When the updated worker is deployed and the service is accessible, use the SQLite backup operation on an existing running database. Place the output on a **separate filesystem** with enough room, and transfer it promptly to persistent local storage through an existing verified transfer route. The container's `/tmp` can serve as temporary staging only if it is on a different filesystem and has sufficient space. Railway's service file commands can transfer files under `/tmp` from an active deployment. The archive helper can also run from an existing shell on the older deployment as a single copied script, with the `backup SOURCE DESTINATION --off-volume` arguments shown by `--help`.

```sh
umask 077
mkdir /tmp/quarter-hour-archive-unique
python -m quarter_hour --db /data/quarter-hour.sqlite3 backup \
  /tmp/quarter-hour-archive-unique/quarter-hour.sqlite3 --off-volume \
  > /tmp/quarter-hour-archive-unique/manifest.json
python -m quarter_hour verify-backup /tmp/quarter-hour-archive-unique/quarter-hour.sqlite3 \
  --manifest /tmp/quarter-hour-archive-unique/manifest.json
```

Download the two files from a local PowerShell window with the existing project linked. [Railway CLI service file commands](https://github.com/railwayapp/cli/blob/v5.75.0/src/commands/service.rs)

```powershell
$LocalArchive = Join-Path $PWD ("exports\railway-archive-" + (Get-Date -Format yyyyMMdd-HHmmss))
New-Item -ItemType Directory -Path $LocalArchive
railway service files --service paper-worker download /tmp/quarter-hour-archive-unique/quarter-hour.sqlite3 $LocalArchive
railway service files --service paper-worker download /tmp/quarter-hour-archive-unique/manifest.json $LocalArchive
py -3.12 quarter_hour/archive.py verify "$LocalArchive\quarter-hour.sqlite3" --manifest "$LocalArchive\manifest.json"
```

Check headroom before backup. A live SQLite reader can temporarily delay WAL truncation. The backup command reads the source and writes the standalone copy on the destination filesystem. Copying data elsewhere leaves the original volume's usage in place. A full original volume will keep the deployment candidate stopped until there is sufficient headroom. Recovery and verification come before any separately authorized reclaim or redeploy decision.

## Reproducible research

Keep each verified SQLite archive with its manifest, recorder commit, and effective settings. Use the complete database for training and evaluation so raw quotes and receipt times, official results, quality gaps, immutable predictions, decisions, and model artifacts remain available. Record the archive SHA-256, UTC split boundaries, model and code revisions, and settings with each experiment. Keep the original archive immutable and work on a local duplicate. The NDJSON export is useful for inspection and interchange. Record export parameters and the source archive hash alongside it.

Use an archive that contains the entire intended evaluation range. Successive full snapshots may overlap, so compare their observation ranges and avoid double-counting. The existing [evaluation protocol](EVALUATION.md) governs market splits and holdout use. The [market compatibility investigation](KALSHI_COMPATIBILITY.md) still needs a recovered Railway market row with its fee metadata; the downloaded archive can supply that evidence.
