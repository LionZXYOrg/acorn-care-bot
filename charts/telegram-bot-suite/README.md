# Telegram bot suite

This chart runs application workers beside one Telegram gateway. A worker is an application process with its own Telegram route and state. The gateway alone receives the bot token. It creates one private Unix socket for each worker and authenticates requests by the caller's Linux user ID.

The chart version is `0.1.0`. The default values contain no production routes, user IDs, worker configuration, or old claim names. The default image is a development tag. Deployment values must pin `image` to an immutable image reference, such as a digest. Each worker can set its own complete `image` reference. The chart passes these references through without alteration.

## Value contract

`values.schema.json` defines the accepted values. The chart also rejects identity, route, claim, port, environment, and mount conflicts during rendering. Unknown fields are errors. There are no raw container, volume, or security overrides.

`replicas` accepts only `0` or `1`. It defaults to `0`. The Deployment uses `Recreate`, which stops the old Pod before it starts a replacement. Stop any old standalone Telegram receiver before you enable this Deployment.

`workers` defaults to an empty list. This permits default rendering and `helm lint` without invented application configuration. An empty list is preparation only. The gateway runtime requires a worker, so the chart rejects `replicas: 1` without one. Up to 16 workers are supported.

`image` is the complete gateway image reference. Workers inherit it unless they set `image`. `imagePullPolicy` applies to all containers. `fullnameOverride` optionally sets a stable resource name, with a maximum length of 40 characters.

`gateway.tokenSecret.name` names an existing Kubernetes Secret. `gateway.tokenSecret.key` selects its token key. Their generic defaults are `telegram-bot-token` and `bot-token`. The chart does not create this Secret. It mounts only that key, read-only, in the gateway at `/var/run/telegram/token`.

`gateway.persistence` and each worker's `persistence` accept `existingClaim`, `size`, and `storageClassName`. A persistent volume claim, or PVC, requests storage from Kubernetes. If `existingClaim` is nonempty, the chart only references that claim. It does not create, annotate, or adopt it. Use `existingClaim` for old Acorn state and any other state that another release owns.

If `existingClaim` is empty, the chart creates a separate `ReadWriteOnce` claim. The default size is `128Mi`. A null or omitted `storageClassName` uses the cluster default. An empty string explicitly selects no storage class. New claims have `helm.sh/resource-policy: keep` and `kustomize.toolkit.fluxcd.io/prune: disabled`. These annotations retain claims during Helm deletion and Flux pruning. They do not replace backups. Do not change claim identities during an upgrade without a storage migration.

Each worker requires `id`, `uid`, `command`, and `route`. The ID starts with a lowercase letter and permits lowercase letters, digits, and internal hyphens. Its maximum length is 32 characters. IDs must be unique, and `gateway` is reserved. UIDs must be distinct positive integers. UID `10003` belongs to the gateway.

The `route` fields are `chat_id`, `topic_id`, `receives_updates`, `allowed_user_ids`, and `methods`. They use the gateway runtime names exactly. Each chat and topic pair must be unique. A receiver must have at least one allowed user. `methods` contains only supported gateway method names. The schema lists those names. Use only the methods that the worker needs.

The chart generates the gateway registry from this list. It sets `id`, `uid`, and the gateway socket path from each worker. The gateway state path is always `/gateway/private/state.sqlite3`.

A worker's `env` is a plain map of string values. `envFrom` and value references are not supported. The chart supplies these variables from the same route that it puts in the registry:

- `TELEGRAM_GATEWAY_SOCKET=/run/telegram/private/api.sock`
- `TELEGRAM_CHAT_ID` and `TELEGRAM_TOPIC_ID`
- `TELEGRAM_ALLOWED_USER_IDS`, as comma-separated decimal IDs
- `PYTHONDONTWRITEBYTECODE=1`
- `PYTHONUNBUFFERED=1`

Do not override these names. Worker variables that contain `TOKEN`, without regard to case, are also rejected. Keep secrets out of `env` and `config`.

A worker's `config` is a map of file names to string contents. The chart creates a ConfigMap and mounts it read-only at `/etc/worker`. Set application path variables to files in that directory. Worker state is mounted at `/data`. The worker has its own writable `/tmp` and a read-only socket mount at `/run/telegram`.

`secretMounts` accepts entries with `secretName`, `mountPath`, and `items`. Each item has `key` and `path`. Item paths are single file names. Mount paths must be absolute and normalized. They cannot overlap another secret mount or a protected application, state, socket, temporary, or system path. Secret volumes use indexed names and are always read-only. Workers cannot reference the gateway token Secret, even through another key.

`resources` accepts Kubernetes resource requests and limits. The default requests are `10m` CPU and `32Mi` memory. The default limits are `250m` CPU and `128Mi` memory. `gateway.resources` controls the gateway independently.

`ports` accepts a list of `name` and `containerPort` pairs. Ports are TCP. Names and numbers must be unique across the Pod. Gateway health uses port `8081` and name `gateway-health`. Workers cannot reuse them. HTTP and TCP probes must reference a port declared by that worker.

`startupProbe`, `livenessProbe`, and `readinessProbe` accept `exec`, `httpGet`, or `tcpSocket`, plus standard timing fields. A probe must use exactly one action. No probe host override is accepted. Acorn can use HTTP probes on port `8080`. Greenwich can use `exec.command: [python3, /app/greenwich/runner.py, health]`. These application details belong in deployment values, not templates.

## Add a worker

Supply an immutable suite image, the gateway Secret reference, and one list entry per worker. The example uses synthetic IDs and claim names. Replace the image placeholder before deployment.

```yaml
replicas: 0
image: registry.example/suite@sha256:<digest>
gateway:
  tokenSecret:
    name: example-telegram-token
    key: bot-token
  persistence:
    existingClaim: example-gateway-state
workers:
  - id: example
    uid: 11001
    command: [python3, /app/example.py]
    route:
      chat_id: -1000000000001
      topic_id: 1
      receives_updates: false
      allowed_user_ids: []
      methods: [getMe, sendMessage]
    env:
      EXAMPLE_CONFIG_PATH: /etc/worker/example.json
      EXAMPLE_STATE_PATH: /data/state.json
    config:
      example.json: '{"example":true}'
    persistence:
      existingClaim: example-worker-state
    secretMounts:
      - secretName: example-property
        mountPath: /etc/property
        items:
          - key: property.json
            path: property.json
    resources:
      requests: {cpu: 10m, memory: 32Mi}
      limits: {cpu: 250m, memory: 128Mi}
```

To add another worker, append another entry. Choose a new ID, UID, route, claim, and any health ports. No template edits are needed. A third worker receives its own registry entry and separate mounts automatically.

Before you deploy, run `helm lint` and `helm template` with your values. Inspect the generated registry and claims. Keep `replicas: 0` until the old receivers stop and the storage is ready.

## Legacy Acorn receipts

`legacyAcorn.workerId` defaults to an empty string, which disables migration. To export old Acorn receipts, set it to `acorn`.
That worker must use ID `acorn`. That worker's claim must contain the old state. The selected route supplies `TELEGRAM_CHAT_ID` and `TELEGRAM_TOPIC_ID`.

The chart adds an init container, which runs before the main containers. It uses the gateway image and command `python3 /app/gateway_seed.py`. It runs as the selected worker's UID with group `10001`. It mounts that worker's state read-only at `/legacy` and gateway state at `/gateway`. It has no token, socket, worker configuration, or worker secret mounts.

The init container and gateway both use `GATEWAY_LEGACY_PATH=/gateway/legacy.json`. The seed program controls its one-time export and ownership rules. Do not enable migration while an old receiver still owns the source state.

## Isolation limits

The gateway uses UID `10003` and group `10001`. Every container uses group `10001`, and the Pod uses `fsGroup: 10001`. Workers have distinct UIDs. Each worker has separate state, configuration, socket, and temporary volumes. The gateway sees the sockets but not worker state, except during the optional init step.

All containers use nonroot users, read-only root filesystems, dropped Linux capabilities, and the default seccomp filter. Privilege escalation is disabled. The Pod does not mount a service account token or share a process namespace.

These containers still share a Pod network and kernel. This is not isolation against a hostile kernel exploit. Workers can reach each other's listening TCP ports through localhost. Keep sensitive worker APIs off TCP. The gateway must enforce its UID and route rules for every socket request.

The NetworkPolicy denies application ingress. It permits DNS through `kube-system` Pods labeled `k8s-app: kube-dns`. It permits only TCP `443` to public IPv4 and IPv6 ranges, with the original private and reserved range exclusions. The policy applies to the whole Pod, not individual workers. It does not restrict public HTTPS by domain. Enforcement depends on the cluster network plugin.

Pod annotations contain checksums of the generated registry and configuration. A Helm change to either causes a rollout. Changes to external Secret contents or existing claim contents do not change these checksums. Restart the Deployment after a required external Secret change.

Telegram route identifiers must fit 52 significant bits to prevent numeric rounding.
