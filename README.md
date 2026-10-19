# Acorn care bot

This bot sends shared feeding and cleaning checklists to one Telegram topic.
It stores each task and caregiver confirmation in one persistent record.
It runs without ZeroClaw, an AI model, or third-party Python packages.
The application requires Python 3.12 or later and Linux file locks.
The cat's name is `Acorn` in the configuration and messages.
The source repository is [LionZXYOrg/acorn-care-bot](https://github.com/LionZXYOrg/acorn-care-bot).

The example plan uses `07:00`, `14:00`, and `19:00` in `Europe/London`.
Quiet hours run from `23:00` to `07:00`, including seasonal clock changes.
Wet food uses can fractions. Dry food uses grams.
The example preserves the owners' original gram amounts and stage dates.
It is not a veterinary prescription or a permanent allowance for a growing kitten.

## Shared care records

Each button identifies a saved task, not the words on the button.
The date and time stay in the message, not in the button label.
The bot accepts only configured numeric caregiver IDs in the configured chat and topic.
It rejects future tasks and buttons from another bot or an unregistered message.
It saves a confirmation before it answers the button press.
A second press does not issue another portion or replace the first caregiver.

Reminders include only tasks that remain open.
An old feeding becomes a confirmation question when the next meal arrives.
The bot does not ask caregivers to issue an old portion again or add it to a later portion.
A health hold stops stage changes but keeps care reminders active.
Unknown delivery outcomes require review instead of an automatic resend.
A definite Telegram rate-limit response can retry after its delay.

## Move a task to the next care time

Each task row has its completion button on the left and `К следующему уходу` on the right.
The right button means “move to the next care time.”
Adding or displaying these buttons does not change tasks, portions, or the recurring schedule.
The separate test button does not move or complete care tasks.

When you press `К следующему уходу`, the bot marks that source task as skipped now.
It cancels that task's repeats and snooze.
The source message shows the skip, caregiver, and next care time.
This action does not mark the task as completed.

The destination is the next scheduled care time after the press, not after the source task's original time.
The bot uses confirmed schedule changes and the active diet stage, including a health hold.
It stores a pending transfer in `carryovers`, a separate queue of one-time reminders.
It does not create a future checklist when you press the button.
The checklist still gets its normal first announcement when that care time arrives.

At the destination, the bot merges reminders of the same kind into one task.
It keeps source task identifiers in `carried_from` and records each move in the audit history.
A cleaning task can become a one-time lunch task, even when lunch has no routine cleaning.
The recurring cleaning schedule stays unchanged.

### Food safety

Food transfers use the saved `food_transfer_policy` setting.
The default, `planned_only`, keeps the next portion unchanged and does not add missed food.
If that food is absent from the next slot, the default policy does not create a food task.

With the owners' confirmation, set `food_transfer_policy` to `add_full` in the care configuration.
This policy adds the entire transferred amount to the next care task of the same food type, including the next day.
If that slot has no planned portion, the bot creates a one-time task containing only the carried food.
The recurring portion plan does not change.
This setting records an owner instruction. It is not a veterinary recommendation.

The target stores `planned_grams`, `carried_grams`, and the combined `grams` amount.
Its checklist shows the total and the amount included from transfers.
The queue stores the exact source amounts in `food_grams`.
The target stores each applied source amount in `carried_portions`, so repeated application cannot add it twice.
A chained move carries the source's entire combined amount once, not its ancestors again.
The button response states the carried amount and destination time.

Old transfers without recorded food amounts keep their original reminder-only meaning.
Changing the policy does not reinterpret old skips or old transfer records.
An unconfirmed task does not transfer food automatically.
A plain skip does not transfer food.
Cleaning transfers do not change food amounts.

Only the configured numeric caregivers can use these buttons in the configured chat and topic.
Future tasks and forged message bindings cannot change care records.
A second press cannot move a closed source again or replace its caregiver record.
If the destination task is already closed, the bot rejects the move and leaves the source unchanged.

The queue uses the destination's date and time as its identifier.
If an operator changes that time after a transfer, the operator must review the pending queue before migration or deployment.
This version does not automatically move queued transfers to a replacement schedule time.
The standalone bot does not offer schedule-change commands.

## Configuration

Copy `config.example.json` to a protected location outside the checkout.
Set its caregiver mentions and make sure that its portions match the approved plan.
The application loads that file before it creates care state.
If a state file exists, the bot preserves its care configuration and confirmations.
A different timezone requires an explicit migration.

Set the following environment variables before startup:

| Variable | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN_FILE` | Protected file that contains the new bot token. |
| `TELEGRAM_BOT_TOKEN` | Alternative token input when no token file is configured. |
| `TELEGRAM_CHAT_ID` | Numeric Telegram chat ID. |
| `TELEGRAM_TOPIC_ID` | Numeric topic ID in that chat. |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated numeric caregiver IDs. Usernames do not grant access. |
| `ACORN_CONFIG_PATH` | Protected care JSON file. The default is `config.example.json`. |
| `ACORN_STATE_PATH` | Persistent record. The default is `/data/state.json`. |
| `ACORN_HTTP_HOST` | Health server address. The default is `0.0.0.0`. |
| `ACORN_HTTP_PORT` | Health server port. The default is `8080`. |

A configured token file takes precedence over the token environment variable.
If the file is missing, the bot refuses startup instead of using another token.
Never commit tokens, Telegram identifiers, caregiver identifiers, or care records.
Keep configuration and state mounts readable only by the service account.
The container uses user and group `10001`.

## Start the service

Use a new bot token. Do not reuse the assistant token.
Give the new bot access to the target group and topic.
Configure its numeric caregiver IDs before startup.
The service refuses an existing webhook instead of deleting it.
A competing Telegram poller stops this service.

After setting the environment variables and mounting persistent storage, run:

```bash
python3 service.py
```

The service starts one Telegram polling loop and one reminder worker.
A process lock rejects a second writer to the same state file.
The bot persists the Telegram update offset with each processed change.
`/livez` reports process health. `/readyz` requires a recent poll and a recent reminder check.
The endpoints return no task, identity, configuration, or secret details.
Do not expose them through a public route.

## Caregiver commands

Use inline buttons for ordinary confirmations.
For an open-task summary, send `/status` in the configured topic.
For one exact task, use these commands:

```text
/done 2026-10-20_07:00_wet
/skip 2026-10-20_07:00_bowls
/snooze 2026-10-20_07:00_water 30
/health_hold Appetite or stool problem. Stage changes need review.
```

A task identifier contains its date, meal time, and type.
The types are `wet`, `dry`, `litter`, `bowls`, and `water`.
Snoozes accept `30` or `60` minutes.
The bot does not treat an ambiguous “done” as completion of every task.
It accepts old text-button labels only when they match one stored task exactly.
These commands do not confirm that the kitten ate the issued food.
A health hold requires veterinary advice before the owners resume the transition plan.
This version does not infer care reports from free text or offer a command to change the diet.

## Import an existing care record

Do not start old and new reminder services together. Duplicate reminders can cause a repeated food portion.
The existing service remains responsible for reminders until an explicit cutover.
An existing care-only state file cannot become live state through an implicit startup.
The import is an offline step that requires a separate output file.

1. Stop the old care scheduler before exporting its record.
2. Stop any other process that can update that record during the cutover.
3. Copy the record to a protected location outside all Git checkouts.
4. Make sure that the configuration uses the source record's timezone.
5. Run the import with the numeric ID of the operator:

   ```bash
   python3 migrate.py \
     --source /protected/old-state.json \
     --output /protected/imported-state.json \
     --operator-id NUMERIC_OPERATOR_ID \
     --confirm-old-scheduler-stopped
   ```

6. Review the imported task states, pending schedule changes, health hold, and caregiver provenance.
7. Mount the imported file at `ACORN_STATE_PATH` with owner `10001:10001`.
8. Start the new service and make sure that `/readyz` succeeds.
9. Test one confirmation with the new bot before ending the cutover.

The import preserves care configuration, history, task states, and caregiver audit records.
It also preserves queued transfers, source skips, and destination links. A record without a transfer queue needs no care-schema conversion.
It discards transport state, pending sends, old inline panels, and old bot message bindings.
It does not send a Telegram message or overwrite an existing output file.
Keep the original backup until the new service passes the cutover test.
If the new service is not ready, keep it stopped before resuming the old service.

## Tests and image publication

Run the offline tests from this directory:

```bash
python3 -m unittest discover -s tests -v
```

The tests use generated records, fake Telegram requests, and local health servers.
They cover shared confirmations, stale and future buttons, quiet hours, stage notices, health holds, migration, and uncertain sends.
Transfer tests cover food safety, queue persistence, merged sources, schedule boundaries, button layouts, and saving records before button responses.
The tests also reject retired names in tracked files and require `Acorn` in the example configuration and messages.
No test needs a real bot token, Telegram history, or private runtime state.

GitHub Actions runs the tests on Python 3.12 and 3.13.
On `main`, it publishes `linux/amd64` and `linux/arm64` images to GitHub Container Registry (GHCR).
The image name is `ghcr.io/lionzxyorg/acorn-care-bot`.
Each image has a `sha-COMMIT` tag and a moving `main` tag.
Deployment manifests must use the tested commit tag or image digest, not the moving tag.
The private package requires a registry credential with `read:packages` permission.
The workflow uses its short-lived `GITHUB_TOKEN` to publish images.
A local container build needs Docker:

```bash
docker build --tag acorn-care-bot:local .
```

The [infra repository](https://github.com/LionZXYOrg/infra) contains the Flux deployment.
Its initial deployment stays suspended with zero replicas until token delivery and state migration are complete.
No production deployment or real Telegram end-to-end test can run before the owner supplies the new token.
