{{/*
Expand the name of the chart.
*/}}
{{- define "agentscribe-indexer.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "agentscribe-indexer.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else if contains .Release.Name (include "agentscribe-indexer.name" .) }}
{{- include "agentscribe-indexer.name" . | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "agentscribe-indexer.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "agentscribe-indexer.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{ include "agentscribe-indexer.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "agentscribe-indexer.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agentscribe-indexer.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Name of the service account
*/}}
{{- define "agentscribe-indexer.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- include "agentscribe-indexer.fullname" . }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Name of the secret containing the Supabase service role key.
*/}}
{{- define "agentscribe-indexer.secretName" -}}
{{- include "agentscribe-indexer.fullname" . }}
{{- end }}

{{/*
Effective indexer image (config.indexerImage or image.repository:tag).
*/}}
{{- define "agentscribe-indexer.indexerImage" -}}
{{- if .Values.config.indexerImage }}
{{- .Values.config.indexerImage }}
{{- else }}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag }}
{{- end }}
{{- end }}
