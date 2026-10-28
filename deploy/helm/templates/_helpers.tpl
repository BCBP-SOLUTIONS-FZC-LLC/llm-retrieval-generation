{{/*
Expand the name of the chart.
*/}}
{{- define "llm-retrieval-generation.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "llm-retrieval-generation.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Common labels applied to every resource.
*/}}
{{- define "llm-retrieval-generation.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "llm-retrieval-generation.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels: used by Deployment, Service, HPA, PDB and NetworkPolicy.
*/}}
{{- define "llm-retrieval-generation.selectorLabels" -}}
app.kubernetes.io/name: {{ include "llm-retrieval-generation.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Image tag: prefer .Values.image.tag; fall back to chart appVersion.
*/}}
{{- define "llm-retrieval-generation.imageTag" -}}
{{- .Values.image.tag | default .Chart.AppVersion }}
{{- end }}

{{/*
Name of the Secret the pods read: the pre-provisioned one, else the one this chart creates.
*/}}
{{- define "llm-retrieval-generation.secretName" -}}
{{- default (printf "%s-secrets" (include "llm-retrieval-generation.fullname" .)) .Values.existingSecret }}
{{- end }}

{{/*
The container environment shared by the API, the migration Job and the sweep CronJobs: plain
values, then required secret keys, then optional secret keys. Call with the root context.
*/}}
{{- define "llm-retrieval-generation.env" -}}
- name: BUILD_VERSION
  value: {{ include "llm-retrieval-generation.imageTag" . | quote }}
{{- range $key, $val := .Values.env }}
- name: {{ $key }}
  value: {{ $val | quote }}
{{- end }}
{{- $secretName := include "llm-retrieval-generation.secretName" . }}
{{- range .Values.envFromSecret }}
- name: {{ . }}
  valueFrom:
    secretKeyRef:
      name: {{ $secretName }}
      key: {{ . }}
{{- end }}
{{- range .Values.envFromSecretOptional }}
- name: {{ . }}
  valueFrom:
    secretKeyRef:
      name: {{ $secretName }}
      key: {{ . }}
      optional: true
{{- end }}
{{- end }}
