{{/*
Expand the name of the chart.
*/}}
{{- define "yoloscribe-indexer.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "yoloscribe-indexer.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else if contains .Release.Name (include "yoloscribe-indexer.name" .) }}
{{- include "yoloscribe-indexer.name" . | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "yoloscribe-indexer.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "yoloscribe-indexer.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{ include "yoloscribe-indexer.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "yoloscribe-indexer.selectorLabels" -}}
app.kubernetes.io/name: {{ include "yoloscribe-indexer.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Name of the service account
*/}}
{{- define "yoloscribe-indexer.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- include "yoloscribe-indexer.fullname" . }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Name of the secret containing the Supabase service role key.
*/}}
{{- define "yoloscribe-indexer.secretName" -}}
{{- include "yoloscribe-indexer.fullname" . }}
{{- end }}

{{/*
Effective indexer image (config.indexerImage or image.repository:tag).
*/}}
{{- define "yoloscribe-indexer.indexerImage" -}}
{{- if .Values.config.indexerImage }}
{{- .Values.config.indexerImage }}
{{- else }}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag }}
{{- end }}
{{- end }}
