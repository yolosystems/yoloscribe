{{- define "yoloscribe-external-secrets.labels" -}}
helm.sh/chart: "{{ .Chart.Name }}-{{ .Chart.Version }}"
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
The ServiceAccount ESO assumes to read this product's secrets. Overridable, but
the default is what the IAM role's trust policy is written against.
*/}}
{{- define "yoloscribe-external-secrets.serviceAccountName" -}}
{{- .Values.serviceAccount.name | default "yoloscribe-eso" -}}
{{- end -}}

{{/*
Where the deployment secrets live in Secrets Manager.

NOT `yoloscribe/` — that prefix is the runtime per-user namespace, holding every
user's OAuth tokens and LiteLLM keys under yoloscribe/{user-uuid}/. Deployment
secrets sit one level in so the operator's IAM role can be granted exactly this
and nothing else; the CLI refuses to attach an ESO policy that reaches wider.

Stage-scoped, so dev's secrets operator cannot read prod's credentials.
*/}}
{{- define "yoloscribe-external-secrets.secretsPrefix" -}}
{{- if .Values.secretsPrefix -}}
{{- .Values.secretsPrefix -}}
{{- else -}}
{{- printf "yoloscribe/%s/deploy" (required "stage is required: the Secrets Manager prefix is derived from it, and a wrong prefix means every ExternalSecret silently fails to sync" .Values.stage) -}}
{{- end -}}
{{- end -}}
