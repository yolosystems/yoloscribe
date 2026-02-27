{{/*
Expand the name of the chart.
*/}}
{{- define "agentscribe-backend.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "agentscribe-backend.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else if contains .Release.Name (include "agentscribe-backend.name" .) }}
{{- include "agentscribe-backend.name" . | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "agentscribe-backend.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "agentscribe-backend.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{ include "agentscribe-backend.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "agentscribe-backend.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agentscribe-backend.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Name of the service account
*/}}
{{- define "agentscribe-backend.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- include "agentscribe-backend.fullname" . }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Name of the secret containing the Anthropic API key.
Prefers existingSecret; falls back to the release-managed secret.
*/}}
{{- define "agentscribe-backend.secretName" -}}
{{- if .Values.existingSecret }}
{{- .Values.existingSecret }}
{{- else }}
{{- include "agentscribe-backend.fullname" . }}
{{- end }}
{{- end }}
