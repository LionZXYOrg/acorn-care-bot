{{- define "suite.name" -}}
{{- default (printf "%s-%s" .Release.Name .Chart.Name | trunc 40 | trimSuffix "-") .Values.fullnameOverride -}}
{{- end -}}

{{- define "suite.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
{{- end -}}

{{- define "suite.claim" -}}
{{- $p := .persistence | default dict -}}
{{- default (printf "%s-%s-state" (include "suite.name" .root) .id) $p.existingClaim -}}
{{- end -}}

{{- define "suite.security" -}}
runAsNonRoot: true
runAsUser: {{ printf "%.0f" (float64 .) }}
runAsGroup: 10001
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: [ALL]
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "suite.registry" -}}
{{- $workers := list -}}
{{- range .Values.workers -}}
{{- $workers = append $workers (merge (dict "id" .id "uid" .uid "socket_path" (printf "/run/workers/%s/private/api.sock" .id)) (deepCopy .route)) -}}
{{- end -}}
{{- dict "workers" $workers "state_path" "/gateway/private/state.sqlite3" | toPrettyJson -}}
{{- end -}}

{{- define "suite.validate" -}}
{{- if or (lt (int .Values.replicas) 0) (gt (int .Values.replicas) 1) -}}{{ fail "replicas must be 0 or 1" }}{{- end -}}
{{- if and (gt (int .Values.replicas) 0) (empty .Values.workers) -}}{{ fail "at least one worker is required before replicas can be 1" }}{{- end -}}
{{- $root := . -}}
{{- $ids := dict "gateway" true -}}
{{- $uids := dict "10003" true -}}
{{- $routes := dict -}}
{{- $claims := dict (include "suite.claim" (dict "root" . "id" "gateway" "persistence" .Values.gateway.persistence)) true -}}
{{- $ports := dict "8081" true -}}
{{- $portNames := dict "gateway-health" true -}}
{{- if and (not (empty .Values.legacyAcorn.workerId)) (ne .Values.legacyAcorn.workerId "acorn") -}}{{ fail "legacyAcorn.workerId supports only the acorn worker" }}{{- end -}}
{{- $legacyFound := empty .Values.legacyAcorn.workerId -}}
{{- range .Values.workers -}}
  {{- $w := . -}}
  {{- if or (not (regexMatch "^[a-z][a-z0-9]*(-[a-z0-9]+)*$" .id)) (gt (len .id) 32) -}}{{ fail "invalid worker id" }}{{- end -}}
  {{- if hasKey $ids .id -}}{{ fail (printf "duplicate or reserved worker id: %s" .id) }}{{- end -}}
  {{- $_ := set $ids .id true -}}
  {{- $uid := printf "%.0f" (float64 .uid) -}}
  {{- if or (le (float64 .uid) 0.0) (gt (float64 .uid) 4294967294.0) (ne (float64 .uid) (float64 (int64 .uid))) -}}{{ fail "worker uid must be a positive nonroot integer" }}{{- end -}}
  {{- if hasKey $uids $uid -}}{{ fail "duplicate worker uid or gateway UID collision" }}{{- end -}}
  {{- $_ := set $uids $uid true -}}
  {{- $route := printf "%.0f/%.0f" (float64 .route.chat_id) (float64 .route.topic_id) -}}
  {{- if or (eq (float64 .route.chat_id) 0.0) (le (float64 .route.topic_id) 0.0) -}}{{ fail "invalid worker route" }}{{- end -}}
  {{- if hasKey $routes $route -}}{{ fail "duplicate chat_id and topic_id route" }}{{- end -}}
  {{- $_ := set $routes $route true -}}
  {{- if and .route.receives_updates (empty .route.allowed_user_ids) -}}{{ fail "receives_updates requires allowed_user_ids" }}{{- end -}}
  {{- $claim := include "suite.claim" (dict "root" $root "id" .id "persistence" .persistence) -}}
  {{- if hasKey $claims $claim -}}{{ fail "duplicate persistence claim: workers and gateway require separate state" }}{{- end -}}
  {{- $_ := set $claims $claim true -}}
  {{- range $name, $value := .env -}}
    {{- if not (kindIs "string" $value) -}}{{ fail "worker env values must be plain strings" }}{{- end -}}
    {{- if or (contains "TOKEN" (upper $name)) (has (upper $name) (list "TELEGRAM_GATEWAY_SOCKET" "TELEGRAM_CHAT_ID" "TELEGRAM_TOPIC_ID" "TELEGRAM_ALLOWED_USER_IDS" "PYTHONDONTWRITEBYTECODE" "PYTHONUNBUFFERED")) -}}{{ fail (printf "unsafe worker env override: %s" $name) }}{{- end -}}
  {{- end -}}
  {{- $mounts := list "/data" "/etc/worker" "/etc/gateway" "/run" "/var/run" "/tmp" "/gateway" "/legacy" "/app" "/proc" "/sys" "/dev" "/bin" "/sbin" "/usr" "/lib" "/lib64" -}}
  {{- range .secretMounts -}}
    {{- if eq .secretName $root.Values.gateway.tokenSecret.name -}}{{ fail "workers must not mount the gateway token secret" }}{{- end -}}
    {{- $path := .mountPath -}}
    {{- if or (not (hasPrefix "/" $path)) (ne (clean $path) $path) (eq $path "/") (contains "\\" $path) -}}{{ fail "secret mountPath must be an absolute normalized directory" }}{{- end -}}
    {{- range $mounts -}}
      {{- if or (eq . $path) (hasPrefix (printf "%s/" .) $path) (hasPrefix (printf "%s/" $path) .) -}}{{ fail (printf "protected or overlapping secret mountPath: %s" $path) }}{{- end -}}
    {{- end -}}
    {{- $mounts = append $mounts $path -}}
    {{- $itemPaths := dict -}}
    {{- range .items -}}
      {{- if or (not (regexMatch "^[A-Za-z0-9_-][A-Za-z0-9_.-]*$" .path)) (hasKey $itemPaths .path) -}}{{ fail "invalid or duplicate secret item path" }}{{- end -}}
      {{- $_ := set $itemPaths .path true -}}
    {{- end -}}
  {{- end -}}
  {{- $ownPorts := dict -}}
  {{- range .ports -}}
    {{- $number := printf "%.0f" (float64 .containerPort) -}}
    {{- if or (hasKey $ports $number) (hasKey $portNames .name) -}}{{ fail "duplicate health port number or name (including gateway port 8081)" }}{{- end -}}
    {{- $_ := set $ports $number true -}}
    {{- $_ := set $portNames .name true -}}
    {{- $_ := set $ownPorts $number true -}}
    {{- $_ := set $ownPorts .name true -}}
  {{- end -}}
  {{- range $key := list "startupProbe" "livenessProbe" "readinessProbe" -}}
    {{- $probe := get $w $key | default dict -}}
    {{- range $type := list "httpGet" "tcpSocket" -}}
      {{- if hasKey $probe $type -}}
        {{- $port := get (get $probe $type) "port" -}}
        {{- $keyPort := toString $port -}}
        {{- if not (hasKey $ownPorts $keyPort) -}}{{ fail "worker probe port must reference its own declared port" }}{{- end -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
  {{- if eq .id $root.Values.legacyAcorn.workerId -}}{{- $legacyFound = true -}}{{- end -}}
{{- end -}}
{{- if not $legacyFound -}}{{ fail "legacyAcorn.workerId must name a worker" }}{{- end -}}
{{- end -}}

{{- define "suite.allowedUsers" -}}
{{- $ids := list -}}
{{- range . -}}
{{- $ids = append $ids (printf "%.0f" (float64 .)) -}}
{{- end -}}
{{- join "," $ids -}}
{{- end -}}
