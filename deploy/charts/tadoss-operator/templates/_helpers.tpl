{{- define "tadoss-operator.fullname" -}}
{{- printf "%s-%s" .Release.Namespace .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "tadoss-operator.selectorLabels" -}}
app.kubernetes.io/name: tadoss-operator
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "tadoss-operator.labels" -}}
{{ include "tadoss-operator.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}
