{{/*
Expand the name of the chart.
*/}}
{{- define "yoloscribe-agent-runner.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "yoloscribe-agent-runner.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else if contains .Release.Name (include "yoloscribe-agent-runner.name" .) }}
{{- include "yoloscribe-agent-runner.name" . | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "yoloscribe-agent-runner.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "yoloscribe-agent-runner.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{ include "yoloscribe-agent-runner.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "yoloscribe-agent-runner.selectorLabels" -}}
app.kubernetes.io/name: {{ include "yoloscribe-agent-runner.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Name of the service account
*/}}
{{- define "yoloscribe-agent-runner.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- include "yoloscribe-agent-runner.fullname" . }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Name of the secret containing the Anthropic API key.
Prefers existingSecret; falls back to the release-managed secret.
*/}}
{{- define "yoloscribe-agent-runner.secretName" -}}
{{- if .Values.existingSecret }}
{{- .Values.existingSecret }}
{{- else }}
{{- include "yoloscribe-agent-runner.fullname" . }}
{{- end }}
{{- end }}

{{/*
Effective agent-runner image (config.agentRunnerImage or image.repository:tag).
*/}}
{{- define "yoloscribe-agent-runner.runnerImage" -}}
{{- if .Values.config.agentRunnerImage }}
{{- .Values.config.agentRunnerImage }}
{{- else }}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag }}
{{- end }}
{{- end }}

{{/*
Secret holding the OTLP headers. External Secrets manages it in a real
deployment; the chart only renders one when the value is passed inline.
*/}}
{{- define "yoloscribe-agent-runner.otelSecretName" -}}
{{- .Values.otel.existingSecret | default (printf "%s-otel" (include "yoloscribe-agent-runner.fullname" .)) -}}
{{- end }}

{{/*
Registry pull secret. Same split: ESO-managed by name, or rendered from an
inline PAT.
*/}}
{{- define "yoloscribe-agent-runner.ghcrSecretName" -}}
{{- .Values.ghcr.existingSecret | default (printf "%s-ghcr" (include "yoloscribe-agent-runner.fullname" .)) -}}
{{- end }}

{{/*
The agent-locks DynamoDB table.

This must resolve to the same string the backend produces. The runner takes the
lock; the backend writes this table's ARN into each user's IAM policy. If the
two disagree, provisioning succeeds, the policy grants a table nobody uses, and
locking fails as AccessDenied at run time -- far from the cause.

They agree today only because both defaulted to the same hardcoded name. That is
not a guarantee, which is why both charts now derive it from `stage` the same
way.
*/}}
{{- define "yoloscribe-agent-runner.agentLocksTable" -}}
{{- if .Values.dynamodb.agentLocksTable -}}
{{- .Values.dynamodb.agentLocksTable -}}
{{- else -}}
{{- printf "yoloscribe-%s-agent-locks" (required "stage is required: the agent-locks table name is derived from it, and it must match the backend's or locking fails as AccessDenied" .Values.stage) -}}
{{- end -}}
{{- end -}}
