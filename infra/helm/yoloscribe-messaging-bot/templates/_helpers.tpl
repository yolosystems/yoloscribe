{{- define "yoloscribe-messaging-bot.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "yoloscribe-messaging-bot.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else if contains .Release.Name (include "yoloscribe-messaging-bot.name" .) }}
{{- include "yoloscribe-messaging-bot.name" . | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "yoloscribe-messaging-bot.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "yoloscribe-messaging-bot.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{ include "yoloscribe-messaging-bot.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "yoloscribe-messaging-bot.selectorLabels" -}}
app.kubernetes.io/name: {{ include "yoloscribe-messaging-bot.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
