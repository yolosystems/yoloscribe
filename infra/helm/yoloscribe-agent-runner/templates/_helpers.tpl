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
