# Home deployment

This repository owns the deployment manifests, public care configuration, tests, and operating procedures.
The infrastructure repository selects one exact source commit and its matching image digest.
Flux, a controller that applies Git revisions, reads the deployment files from that source.
Infrastructure keeps the namespace, private Secret dependencies, and Flux references.

`deploy/standalone` contains the active single-process deployment and its approved care configuration.
`deploy/suite` contains the suspended shared-suite release.
`charts/telegram-bot-suite/values-home.yaml` contains the public suite profile.
The suite profile must contain the same care configuration as the standalone deployment.
The placeholder image `deployment-must-pin` requires an explicit image override from infrastructure.

The owners approved public storage of the chat, topic, caregiver identifiers, and recurring care plan.
`deploy/standalone/config.json` defines the approved portions, stage dates, care tasks, notices, mentions, and transfer policy.
`deploy/standalone/runtime.properties` defines the destination, authorized caregivers, and fresh-start flag.
Do not replace either file with the example configuration.
Do not change portions, stage dates, or permanent feeding times without owner confirmation.
Numeric caregiver identifiers control access. Displayed usernames do not grant access.

## Publish a deployment change

The repository and image are public. Both supported architectures must remain available without registry credentials.
Tests run here, not in the infrastructure repository.
The CI workflow installs Helm and kubectl, renders deployment files, runs application tests, and publishes the image.

1. Update the bot-owned deployment files and their tests.
2. Run `python3 -m unittest discover -s tests -v` in this repository with Helm and kubectl on `PATH`.
3. Run `helm lint --strict charts/telegram-bot-suite`.
4. Push the tested bot change to `main`.
5. Make sure that CI publishes both `linux/amd64` and `linux/arm64` images.
6. Update the infrastructure PR with the exact source commit, matching image tag, and immutable image digest.
7. Make sure that the standalone Deployment keeps one replica and the `Recreate` strategy.
8. Keep both suite Flux objects suspended and its profile at zero replicas.
9. Compare rendered resources before and after the move.
10. Preserve object names, generated configuration, Secret references, and the existing care claim.

Do not merge the infrastructure PR without an explicit owner request.
Do not run a second care receiver. Duplicate reminders can cause caregivers to issue a portion twice.
Moving files between repositories does not require a care-state import, reset, fresh start, or scheduler change.

## Preserve state and credentials

The standalone service saves its record at `/data/state.json` on claim `acorn-care-bot-state`.
The claim retains its existing name, namespace, storage configuration, and pruning protection.
The same Flux Kustomization continues to manage it when its source changes.
Do not transfer this claim to Helm ownership or remove it during a retry.
A local-path volume stays on its original node. Back up the volume before moving it.

The application mounts Secret `acorn-care-bot`, key `bot-token`, separately from public configuration.
Keep token values and the Greenwich property Secret outside both public repositories.
If the token key exists, preserve it. A configuration move does not require a new token.
If the Secret is absent, follow the infrastructure repository's private Secret procedure.
Do not reuse the assistant token or change the assistant's Telegram listener.

`ACORN_FRESH_START=1` applies only to the first start on an empty volume.
It skips earlier feeding times and does not reset an existing volume.
On restart, the bot preserves confirmations, queued transfers, health holds, pauses, and Telegram receipts.
Do not repeat the original fresh-start cutover for this repository cleanup.
An active health hold requires owner review before any separate fresh-start migration.

After an approved deployment, make sure that Flux applies its pinned revision and `/readyz` succeeds.
Do not scale the standalone bot above one replica.
If startup fails, stop the failed receiver before restoring another receiver.
Keep the saved record and volume unchanged during rollback.

## Activate the shared suite separately

Follow the [suite procedure](shared-suite.md) for activation, draining, rollback, and offline storage migration.
The standalone Deployment remains responsible for care until that separate cutover.
The suite reuses the existing care claim without changing portions, confirmations, holds, or transfers.
Before activation, supply Secret `greenwich-bin-bot-property` through the private Secret procedure.

Stop bin job `d3cf4dc9-a75d-42b2-943c-0e1d6ba7ac36` during the coordinated cutover.
Keep retired duplicate `cb3196b1-dad3-4ea6-82b1-608143c735ad` disabled.
Do not change either job as part of this repository cleanup.
Do not enable both care receivers or both bin senders.
Back up all retained volumes before the separate cutover.
