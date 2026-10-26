package controller

import (
	"maps"
	"slices"
	"strings"

	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
)

func secretKeyEnv(name, secretName, key string) corev1.EnvVar {
	return corev1.EnvVar{
		Name: name,
		ValueFrom: &corev1.EnvVarSource{
			SecretKeyRef: &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: secretName},
				Key:                  key,
			},
		},
	}
}

func pod(t *tadossv1.Tadoss, databaseSecret, tokenSecret string) corev1.PodTemplateSpec {
	podLabels := labels(t)
	podLabels["app.kubernetes.io/component"] = "api"
	memory := "128Mi"
	if t.Spec.Profile == tadossv1.ProfileEdge {
		memory = "64Mi"
	}
	resources := withDefaults(t.Spec.API.Resources,
		corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("100m"), corev1.ResourceMemory: resource.MustParse(memory)},
		corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("1"), corev1.ResourceMemory: resource.MustParse("512Mi")})
	template := corev1.PodTemplateSpec{
		ObjectMeta: metav1.ObjectMeta{Labels: podLabels},
		Spec: corev1.PodSpec{
			AutomountServiceAccountToken: ptr.To(false),
			SecurityContext: &corev1.PodSecurityContext{
				RunAsNonRoot:   ptr.To(true),
				RunAsUser:      ptr.To(int64(10001)),
				RunAsGroup:     ptr.To(int64(10001)),
				SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
			},
			Containers: []corev1.Container{{
				Name: "api", Image: t.Spec.API.Image, ImagePullPolicy: t.Spec.API.ImagePullPolicy,
				Env:       []corev1.EnvVar{secretKeyEnv("TADOSS_DATABASE", databaseSecret, "uri")},
				Ports:     []corev1.ContainerPort{{Name: "http", ContainerPort: apiPort}},
				Resources: resources,
				SecurityContext: &corev1.SecurityContext{
					AllowPrivilegeEscalation: ptr.To(false),
					ReadOnlyRootFilesystem:   ptr.To(true),
					Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
				},
				StartupProbe:   probe("/healthz", 30, 2),
				ReadinessProbe: probe("/readyz", 3, 5),
				LivenessProbe:  probe("/healthz", 3, 10),
				VolumeMounts:   []corev1.VolumeMount{{Name: "tmp", MountPath: "/tmp"}},
			}},
			Volumes: []corev1.Volume{{Name: "tmp", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}}},
		},
	}
	template.Spec.ImagePullSecrets = t.Spec.ImagePullSecrets
	container := &template.Spec.Containers[0]
	if tokenSecret != "" && ptr.Deref(t.Spec.Auth.TokenEnabled, true) {
		container.Env = append(container.Env, secretKeyEnv("TADOSS_API_TOKEN", tokenSecret, "token"))
	} else if !ptr.Deref(t.Spec.Auth.TokenEnabled, true) {
		// Explicit blanks also override credentials inherited through envFrom.
		container.Env = append(container.Env, corev1.EnvVar{Name: "TADOSS_API_TOKEN"}, corev1.EnvVar{Name: "TADOSS_API_TOKEN_FILE"})
	}
	container.Env = append(container.Env, corev1.EnvVar{Name: "TADOSS_HOST", Value: "0.0.0.0"})
	if t.Spec.Auth.BasicSecret != "" {
		container.Env = append(container.Env, secretKeyEnv("TADOSS_BASIC_AUTH_USERNAME", t.Spec.Auth.BasicSecret, "username"), secretKeyEnv("TADOSS_BASIC_AUTH_PASSWORD", t.Spec.Auth.BasicSecret, "password"))
	}
	oauth := t.Spec.Auth.OAuth2
	values := map[string]string{"TADOSS_OAUTH2_ISSUER": oauth.Issuer, "TADOSS_OAUTH2_AUDIENCE": oauth.Audience, "TADOSS_OAUTH2_JWKS_URI": oauth.JWKSURI, "TADOSS_OAUTH2_ALGORITHMS": strings.Join(oauth.Algorithms, ","), "TADOSS_OAUTH2_READ_SCOPE": oauth.ReadScope, "TADOSS_OAUTH2_WRITE_SCOPE": oauth.WriteScope, "TADOSS_OAUTH2_DELETE_SCOPE": oauth.DeleteScope, "TADOSS_OAUTH2_ADMIN_SCOPE": oauth.AdminScope}
	for _, key := range sortedEnv(values) {
		if values[key] != "" {
			container.Env = append(container.Env, corev1.EnvVar{Name: key, Value: values[key]})
		}
	}
	var caVolume *corev1.VolumeSource
	if t.Spec.Backends.DB.ProvidedBy == tadossv1.ProviderManaged {
		caVolume = &corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{SecretName: t.Name + suffixDBCA, Items: []corev1.KeyToPath{{Key: "ca.crt", Path: "ca.pem"}}}}
	} else if ca := t.Spec.Backends.DB.External.CAConfigMap; ca != nil {
		caVolume = &corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{LocalObjectReference: ca.LocalObjectReference, Items: []corev1.KeyToPath{{Key: ca.Key, Path: "ca.pem"}}}}
	}
	if caVolume != nil {
		template.Spec.Volumes = append(template.Spec.Volumes, corev1.Volume{Name: "database-ca", VolumeSource: *caVolume})
		container.VolumeMounts = append(container.VolumeMounts, corev1.VolumeMount{Name: "database-ca", MountPath: "/etc/tadoss/database-ca", ReadOnly: true})
		container.Env = append(container.Env, corev1.EnvVar{Name: "TADOSS_DATABASE_SSL_ROOT_CERT", Value: "/etc/tadoss/database-ca/ca.pem"})
	}
	if t.Status.Resolved.SchemaTarget != "" {
		container.Env = append(container.Env, corev1.EnvVar{Name: "TADOSS_SCHEMA_TARGET", Value: t.Status.Resolved.SchemaTarget})
	}
	applyWorkload(&template, t.Spec.API)
	return template

}

func probe(path string, failures, period int32) *corev1.Probe {
	return &corev1.Probe{ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{Path: path, Port: intstr.FromString("http")}}, FailureThreshold: failures, PeriodSeconds: period, TimeoutSeconds: 3}
}

func applyWorkload(template *corev1.PodTemplateSpec, config tadossv1.WorkloadSpec) {
	// Instance/component ownership labels cannot be overridden.
	template.Labels = mergeStringMaps(config.PodLabels, template.Labels)
	template.Annotations = maps.Clone(config.PodAnnotations)
	template.Spec.NodeSelector = config.NodeSelector
	template.Spec.Tolerations = config.Tolerations
	template.Spec.Affinity = config.Affinity
	if config.PodSecurityContext != nil {
		template.Spec.SecurityContext = config.PodSecurityContext
	}
	template.Spec.Volumes = append(template.Spec.Volumes, config.Volumes...)
	container := &template.Spec.Containers[0]
	container.EnvFrom = config.EnvFrom
	reserved := make([]string, 0, len(container.Env))
	for _, env := range container.Env {
		reserved = append(reserved, env.Name)
	}
	container.Env = append(container.Env, literalEnv(config.Env, reserved...)...)
	if config.SecurityContext != nil {
		container.SecurityContext = config.SecurityContext
	}
	container.VolumeMounts = append(container.VolumeMounts, config.VolumeMounts...)
	if config.StartupProbe != nil {
		container.StartupProbe = config.StartupProbe
	}
	if config.ReadinessProbe != nil {
		container.ReadinessProbe = config.ReadinessProbe
	}
	if config.LivenessProbe != nil {
		container.LivenessProbe = config.LivenessProbe
	}
}

func apiAuthEnv(env corev1.EnvVar) bool {
	return env.Name == "TADOSS_API_TOKEN" || env.Name == "TADOSS_API_TOKEN_FILE" || strings.HasPrefix(env.Name, "TADOSS_BASIC_") || strings.HasPrefix(env.Name, "TADOSS_OAUTH2_")
}

func literalEnv(env map[string]string, excluded ...string) []corev1.EnvVar {
	excludedNames := make(map[string]struct{}, len(excluded))
	for _, name := range excluded {
		excludedNames[name] = struct{}{}
	}
	vars := make([]corev1.EnvVar, 0, len(env))
	for _, key := range sortedEnv(env) {
		if _, excluded := excludedNames[key]; excluded {
			continue
		}
		vars = append(vars, corev1.EnvVar{Name: key, Value: env[key]})
	}
	return vars
}

func sortedEnv(env map[string]string) []string {
	return slices.Sorted(maps.Keys(env))
}

func mergeStringMaps(base map[string]string, overlays ...map[string]string) map[string]string {
	merged := map[string]string{}
	for key, value := range base {
		merged[key] = value
	}
	for _, overlay := range overlays {
		for key, value := range overlay {
			merged[key] = value
		}
	}
	return merged
}
