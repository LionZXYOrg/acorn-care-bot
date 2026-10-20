## Shared Telegram suite

`charts/telegram-bot-suite` deploys isolated workers under one Telegram identity.
A Helm chart generates Kubernetes resources from deployment values.
The gateway mounts an existing token Secret and receives Telegram updates.
Each module runs in its own container with a distinct Linux user ID.
Modules use separate Unix sockets, local connections with kernel-checked caller identities.
Neither module can read the token, another module's socket, or another module's saved records.
The gateway rejects methods, destinations, callback responses, and message changes outside each module's permissions.

Each worker has one configured chat and topic.
Acorn receives authorized messages and button presses.
The Greenwich worker sends notices and cannot receive updates.
The gateway saves routed updates before it acknowledges them to Telegram.
It retains each module's queue until that module acknowledges its saved offset.
A full queue stops polling rather than discarding addressed updates.
The gateway permits edits, pins, and deletion only for messages that belong to the requesting module.
It exposes no method for webhook changes, bot commands, topic changes, or bulk unpinning.

Each container has a read-only root filesystem, resource limits, separate temporary storage, and no cluster API credential.
A network policy blocks incoming application connections and outgoing connections to private networks.
The pod needs public HTTPS for Telegram and council sources, plus cluster DNS.
Containers share the pod's network and the host kernel. This is not virtual-machine isolation.
A node administrator remains trusted. A gateway compromise can affect every module because the gateway holds the token.

The suite uses three separate retained volumes for care, bin publications, and the gateway's update queue and message ownership.
The care container uses its configured existing claim without a reset or care-state import.
A one-time initialization copies only existing Acorn message receipts and its saved update offset to the gateway.
The initialization mounts care state read-only and refuses to run while the old care process holds its file lease.
Later restarts do not import worker-authored ownership records again.
Care confirmations, portions, health holds, transfers, and existing buttons remain in their current record.

During preparation, keep the Flux objects suspended and the chart at zero replicas.
The existing Acorn Deployment remains unchanged and responsible for live care.
This preparation does not stop either current reminder service or publish a Telegram message.
There is no separate bin CronJob in this implementation.

### Greenwich notices

The bin module sends the following Monday-to-Sunday schedule each Monday at noon in `Europe/London`.
Its scheduler accepts only the noon-to-13:00 window, including daylight-saving changes.
It checks gateway access and pin permissions before saving an attempt.
An unavailable gateway creates no attempt and can recover within that window.
After the saved attempt starts the sender, a restart does not retry it.
An unknown send outcome blocks further publications until an operator reviews the saved record.
The rich notice keeps native Telegram headings, date groups, bin lists, and council links.
A compatible Telegram client and the `sendRichMessage` API are required.

Each publication refreshes all five council sources and matches the exact configured property.
The parser uses the published Week A/B calendar and applies holiday revisions once.
It warns about unconfirmed holiday dates instead of guessing.
A failed fetch never uses cached data as a fallback.
A council failure leaves the previous pin and sends at most one fixed alert per week.
After a confirmed send, the module pins the new notice before cleaning up its own previous notice.
Cleanup failure does not block later weeks. Unknown delivery still requires review.
Telegram normally blocks deletion after 48 hours, so older notices can remain after unpinning.

The private deployment supplies a property Secret with key `property.json`.
The worker mounts this Secret at `/etc/bins`.
The JSON object contains `address_label` and `property`.
The `property` object needs string fields `sao`, `pao`, `streetName`, `postTown`, and `Postcode`.
For a property without a subproperty label, use an empty `sao`.
`address_label` joins the house label, street, town, and postcode with ` - `.
The house label joins nonempty `sao` and `pao` with a space.
Use the exact label and fields from the council lookup.
Keep real addresses, postcodes, source responses, tokens, and runtime records outside this public repository.
The chart does not create the token Secret or property Secret.

### Coordinated activation and rollback

Do not enable both care receivers. Duplicate reminders can cause caregivers to issue the same portion twice.
Do not remove any retained volume to retry an uncertain send.
Acorn keeps its approved care plan during this transport change.

1. Add the property Secret through the deployment repository's private secret procedure.
2. Merge the private change before updating its public reference.
3. Make sure that the tested image is public and supports both cluster architectures.
4. Back up all three volumes outside Git before the cutover.
5. If bin storage already contains old sender records, complete the offline ownership migration below.
6. Stop the old Acorn Deployment through a separate reviewed Git change with `replicas: 0`.
7. Wait until its pod terminates and releases the care volume and polling lease.
8. Disable the existing bin sender before the next unpublished target week.
9. Keep all retired duplicate senders disabled.
10. Make sure that no old care receiver, bin sender, or unfinished publication remains active.
11. Unsuspend the Flux Kustomization and HelmRelease, and set `replicas: 1` through a reviewed Git change.
12. Make sure that gateway and care readiness succeed before testing an existing care button.
13. Before Monday noon, run a bin `preview` and a read-only `verify` through its scoped socket.
14. After the first scheduled notice, make sure that exactly one correct notice is pinned.

If the old bin sender already published the target week, wait for the next unpublished target week before activation.
Before rollback, pause upstream polling without stopping Acorn.
Create `/gateway/private/pause-polling` inside the gateway container as its configured user.
Keep Acorn running until the gateway endpoint `/drainz` returns HTTP 200.
This endpoint requires paused polling, no active upstream poll, and an empty saved update queue.
Acorn must save and acknowledge every queued update before the endpoint succeeds.
If the queue cannot drain, keep both receivers stopped and recover its saved updates offline.
Do not restore the old receiver while acknowledged updates remain only in the gateway queue.

After the queue drains, stop the suite completely before restoring the old care Deployment to one replica.
The old care service reuses the same saved care record and ignores already-processed update IDs.
If a bin delivery is uncertain, review the topic and records before reactivating either bin sender.
Keep the gateway volume and ownership snapshot during rollback. Never reset the saved polling offset.
Before returning to the suite, stop the old receiver and reconcile any updates it processed during rollback.
Do not reuse the old gateway database without that review.
Remove the pause file only after that review, before restarting gateway polling.

### Existing bin storage

The former standalone bin sender used Linux user ID `10001`.
The isolated bin container uses user ID `10002`.
Existing files with mode `0600` need an offline ownership migration before this change.
The volume group alone does not grant access to those files.

Stop all bin senders before migration. A concurrent sender can cause repeated or lost publications.
Back up the volume before changing ownership.
Mount the detached bin volume at `/offline/bin-state` on a trusted maintenance host.
As root on that host, run the repository script with the explicit stopped-worker confirmation:

```bash
python3 greenwich/migrate_storage.py \
  /offline/bin-state --confirm-workers-stopped
```

The script preserves records and permissions and changes ownership to `10002:10002`.
It rejects unsafe entries and held file locks before changing any ownership.
Do not run this command on the care volume or gateway volume.
The suite does not receive root privileges or an automatic ownership-changing init container.

### Add another module

The chart accepts a `workers` list. Each entry defines an image, command, route, identity, and storage.
The chart creates the worker container, its private socket, temporary storage, and gateway registry entry.
A worker can use its own image and dependencies.
The chart rejects duplicate identities, overlapping destinations, and unsafe token access.
See the [chart configuration](../charts/telegram-bot-suite/README.md) for its value structure.

For a receiving worker, use `gateway_client.UnixClient` and acknowledge updates only after saving their effects.
For a sending worker, set `receives_updates: false` and omit `getUpdates`.
Before activation, add offline tests for permissions, restart recovery, and uncertain delivery.
Do not mount the token or another worker's state into a worker.

### Chart delivery

Flux reads the chart from a public GitRepository at an exact commit.
The HelmRelease uses `./charts/telegram-bot-suite` and `reconcileStrategy: Revision`.
A commit reference prevents an unrelated change on `main` from changing the release.
The image uses its own tested commit tag and immutable digest.
No extra registry credential or chart package permission is required.

The chart retains its created volumes during Helm removal and Flux pruning.
An `existingClaim` remains outside Helm ownership.
Back up each volume before an upgrade. Retention is not a backup.
A retained claim can require explicit ownership recovery before a later reinstall.
Do not delete a retained claim to resolve an install failure.
