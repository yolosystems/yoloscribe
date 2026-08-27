{{/*
Expand the name of the chart.
*/}}
{{- define "yoloscribe-backend.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "yoloscribe-backend.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else if contains .Release.Name (include "yoloscribe-backend.name" .) }}
{{- include "yoloscribe-backend.name" . | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "yoloscribe-backend.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "yoloscribe-backend.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{ include "yoloscribe-backend.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "yoloscribe-backend.selectorLabels" -}}
app.kubernetes.io/name: {{ include "yoloscribe-backend.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Name of the service account
*/}}
{{- define "yoloscribe-backend.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- include "yoloscribe-backend.fullname" . }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Name of the secret containing the Anthropic API key.
Prefers existingSecret; falls back to the release-managed secret.
*/}}
{{- define "yoloscribe-backend.secretName" -}}
{{- if .Values.existingSecret }}
{{- .Values.existingSecret }}
{{- else }}
{{- include "yoloscribe-backend.fullname" . }}
{{- end }}
{{- end }}

{{/*
Secret holding the OTLP headers. External Secrets manages it in a real
deployment; the chart only renders one when the value is passed inline.
*/}}
{{- define "yoloscribe-backend.otelSecretName" -}}
{{- .Values.otel.existingSecret | default (printf "%s-otel" (include "yoloscribe-backend.fullname" .)) -}}
{{- end }}

{{/*
Registry pull secret. Same split: ESO-managed by name, or rendered from an
inline PAT.
*/}}
{{- define "yoloscribe-backend.ghcrSecretName" -}}
{{- .Values.ghcr.existingSecret | default (printf "%s-ghcr" (include "yoloscribe-backend.fullname" .)) -}}
{{- end }}

{{/*
The IngressClass this release references. Defaults to the release fullname, so
a chart installed on its own owns a class named after itself.
*/}}
{{- define "yoloscribe-backend.ingressClassName" -}}
{{- .Values.ingressClass.name | default (include "yoloscribe-backend.fullname" .) -}}
{{- end }}

{{/*
The DynamoDB table this release should use for a given role.

Every AWS resource YoloScribe owns is named yoloscribe-{stage}-..., so that two
environments in one AWS account never share one. That matters most here: the
tables hold users, API tokens and messaging bindings, and two stages sharing
them means two stages sharing their users.

Derived from .Values.stage rather than written out, because this name is a
contract between things that never see each other -- the installer creates the
table, this chart points the backend at it, an IAM policy grants its ARN, and
the agent-runner has to arrive at the identical string for locking to work. Four
places, one name; computing it four times is how one of them ends up different.

stage is required rather than defaulted. A default would let a missing value
produce a service that starts cleanly and reads the wrong table, which is the
failure mode this is meant to remove.

Usage:
  {{ include "yoloscribe-backend.tableName" (dict "root" . "suffix" "user-site" "override" .Values.dynamodb.userSiteTable) }}
*/}}
{{- define "yoloscribe-backend.tableName" -}}
{{- if .override -}}
{{- .override -}}
{{- else -}}
{{- printf "yoloscribe-%s-%s" (required "stage is required: DynamoDB table names are derived from it, and an unset stage would silently point this release at another environment's tables" .root.Values.stage) .suffix -}}
{{- end -}}
{{- end -}}
