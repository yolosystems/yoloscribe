{{/*
Expand the name of the chart.
*/}}
{{- define "agentscribe-discord-bot.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "agentscribe-discord-bot.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else if contains .Release.Name (include "agentscribe-discord-bot.name" .) }}
{{- include "agentscribe-discord-bot.name" . | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "agentscribe-discord-bot.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "agentscribe-discord-bot.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{ include "agentscribe-discord-bot.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "agentscribe-discord-bot.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agentscribe-discord-bot.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
