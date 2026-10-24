// Package controller reconciles Tadoss instances into their Kubernetes resources.
package controller

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"maps"
	"slices"
	"strings"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	policyv1 "k8s.io/api/policy/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	instanceUID = "tadoss.livewyer.io/instance-uid"
	apiPort     = 8000
	suffixAPI   = "-api"
	suffixDB    = "-db"
	suffixDBApp = "-db-app"
	suffixDBCA  = "-db-ca"
	suffixToken = "-api-token"

	// The migration waits up to five minutes for a restarting database, so the Job
	// deadline must comfortably exceed that plus the retries.
	schemaJobBackoffLimit    = 4
	schemaJobDeadlineSeconds = 900
)

// workloadSet holds the API workloads of an instance in apply order.
type workloadSet struct {
	Deployment   *appsv1.Deployment
	Service      *corev1.Service
	Budget       *policyv1.PodDisruptionBudget
	Ingress      *networkingv1.Ingress
	Worker       *appsv1.Deployment
	WorkerBudget *policyv1.PodDisruptionBudget
	Policies     []*networkingv1.NetworkPolicy
}

func (w workloadSet) objects() []client.Object {
	objects := []client.Object{w.Deployment, w.Service, w.Worker}
	if w.WorkerBudget != nil {
		objects = append(objects, w.WorkerBudget)
	}
	for _, policy := range w.Policies {
		objects = append(objects, policy)
	}
	if w.Budget != nil {
		objects = append(objects, w.Budget)
	}
	if w.Ingress != nil {
		objects = append(objects, w.Ingress)
	}
	return objects
}

func replicas(t *tadossv1.Tadoss) int32 {
	if t.Spec.API.ReplicaCount != nil {
		return *t.Spec.API.ReplicaCount
	}
	if t.Spec.Profile == tadossv1.ProfileMultiServer {
		return 2
	}
	return 1
}

func databaseInstances(t *tadossv1.Tadoss) int {
	if t.Spec.Backends.DB.CNPG.Instances != nil {
		return int(*t.Spec.Backends.DB.CNPG.Instances)
	}
	if t.Spec.Profile == tadossv1.ProfileMultiServer {
		return 3
	}
	return 1
}

// selectorLabels identify API pods without the instance UID, so a recreated
// instance keeps matching the workloads that survived it.
func selectorLabels(t *tadossv1.Tadoss) map[string]string {
	return map[string]string{
		"app.kubernetes.io/name":       "tadoss",
		"app.kubernetes.io/instance":   t.Name,
		"app.kubernetes.io/managed-by": "tadoss-operator",
		"app.kubernetes.io/component":  "api",
	}
}

func labels(t *tadossv1.Tadoss) map[string]string {
	return map[string]string{
		"app.kubernetes.io/name":       "tadoss",
		"app.kubernetes.io/instance":   t.Name,
		"app.kubernetes.io/managed-by": "tadoss-operator",
		instanceUID:                    string(t.UID),
	}
}

func metadata(t *tadossv1.Tadoss, suffix string) metav1.ObjectMeta {
	return metav1.ObjectMeta{Name: t.Name + suffix, Namespace: t.Namespace, Labels: labels(t)}
}

func withDefaults(requirements corev1.ResourceRequirements, requests, limits corev1.ResourceList) corev1.ResourceRequirements {
	resources := requirements.DeepCopy()
	if resources.Requests == nil {
		resources.Requests = corev1.ResourceList{}
	}
	if resources.Limits == nil {
		resources.Limits = corev1.ResourceList{}
	}
	for key, value := range requests {
		if _, explicit := resources.Requests[key]; !explicit {
			if limit, ok := resources.Limits[key]; ok && value.Cmp(limit) > 0 {
				value = limit
			}
			resources.Requests[key] = value.DeepCopy()
		}
	}
	for key, value := range limits {
		if _, explicit := resources.Limits[key]; !explicit {
			if request, ok := resources.Requests[key]; ok && value.Cmp(request) < 0 {
				value = request
			}
			resources.Limits[key] = value.DeepCopy()
		}
	}
	return *resources
}

func databaseCluster(t *tadossv1.Tadoss) *cnpgv1.Cluster {
	image := t.Spec.Backends.DB.CNPG.Image
	if image == "" {
		image = tadossv1.DefaultPostgresImage
	}
	cluster := &cnpgv1.Cluster{
		TypeMeta:   metav1.TypeMeta{APIVersion: "postgresql.cnpg.io/v1", Kind: "Cluster"},
		ObjectMeta: metadata(t, suffixDB),
		Spec: cnpgv1.ClusterSpec{
			Instances:            databaseInstances(t),
			ImageName:            image,
			StorageConfiguration: cnpgv1.StorageConfiguration{Size: t.Spec.Backends.DB.CNPG.Storage, StorageClass: t.Spec.Backends.DB.CNPG.StorageClass},
			Bootstrap:            &cnpgv1.BootstrapConfiguration{InitDB: &cnpgv1.BootstrapInitDB{Database: "tadoss", Owner: "tadoss"}},
			Resources: withDefaults(t.Spec.Backends.DB.CNPG.Resources,
				corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("100m"), corev1.ResourceMemory: resource.MustParse("128Mi")},
				corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("1"), corev1.ResourceMemory: resource.MustParse("1Gi")}),
			EnableSuperuserAccess: ptr.To(false),
		},
	}
	config := t.Spec.Backends.DB.CNPG
	if config.Backup.Enabled {
		cluster.Spec.Backup = &cnpgv1.BackupConfiguration{BarmanObjectStore: config.Backup.ObjectStore, RetentionPolicy: config.Backup.RetentionPolicy}
	}
	if config.Restore.Enabled {
		recovery := &cnpgv1.BootstrapRecovery{Source: config.Restore.Source, Database: "tadoss", Owner: "tadoss"}
		if config.Restore.TargetTime != "" {
			recovery.RecoveryTarget = &cnpgv1.RecoveryTarget{TargetTime: config.Restore.TargetTime}
		}
		cluster.Spec.Bootstrap = &cnpgv1.BootstrapConfiguration{Recovery: recovery}
		cluster.Spec.ExternalClusters = []cnpgv1.ExternalCluster{{Name: config.Restore.Source, BarmanObjectStore: config.Restore.ObjectStore}}
	}
	return cluster

}

func secretEnv(name, secret, key string) corev1.EnvVar {
	return corev1.EnvVar{Name: name, ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: secret}, Key: key}}}
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
			SecurityContext:              &corev1.PodSecurityContext{RunAsNonRoot: ptr.To(true), RunAsUser: ptr.To(int64(10001)), RunAsGroup: ptr.To(int64(10001)), SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault}},
			Containers: []corev1.Container{{
				Name: "api", Image: t.Spec.API.Image, ImagePullPolicy: t.Spec.API.ImagePullPolicy,
				Env:             []corev1.EnvVar{secretEnv("TADOSS_DATABASE", databaseSecret, "uri"), secretEnv("TADOSS_API_TOKEN", tokenSecret, "token")},
				Ports:           []corev1.ContainerPort{{Name: "http", ContainerPort: apiPort}},
				Resources:       resources,
				SecurityContext: &corev1.SecurityContext{AllowPrivilegeEscalation: ptr.To(false), ReadOnlyRootFilesystem: ptr.To(true), Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}}},
				StartupProbe:    probe("/healthz", 30, 2), ReadinessProbe: probe("/readyz", 3, 5), LivenessProbe: probe("/healthz", 3, 10),
				VolumeMounts: []corev1.VolumeMount{{Name: "tmp", MountPath: "/tmp"}},
			}},
			Volumes: []corev1.Volume{{Name: "tmp", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}}},
		},
	}
	template.Spec.ImagePullSecrets = t.Spec.ImagePullSecrets
	container := &template.Spec.Containers[0]
	container.Env = append(container.Env, corev1.EnvVar{Name: "TADOSS_HOST", Value: "0.0.0.0"})
	if t.Spec.Auth.BasicSecret != "" {
		container.Env = append(container.Env, secretEnv("TADOSS_BASIC_AUTH_USERNAME", t.Spec.Auth.BasicSecret, "username"), secretEnv("TADOSS_BASIC_AUTH_PASSWORD", t.Spec.Auth.BasicSecret, "password"))
	}
	oauth := t.Spec.Auth.OAuth2
	values := map[string]string{"TADOSS_OAUTH2_ISSUER": oauth.Issuer, "TADOSS_OAUTH2_AUDIENCE": oauth.Audience, "TADOSS_OAUTH2_JWKS_URI": oauth.JWKSURI, "TADOSS_OAUTH2_ALGORITHMS": strings.Join(oauth.Algorithms, ","), "TADOSS_OAUTH2_READ_SCOPE": oauth.ReadScope, "TADOSS_OAUTH2_WRITE_SCOPE": oauth.WriteScope, "TADOSS_OAUTH2_DELETE_SCOPE": oauth.DeleteScope, "TADOSS_OAUTH2_ADMIN_SCOPE": oauth.AdminScope}
	for _, key := range slices.Sorted(maps.Keys(values)) {
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

// schemaJob runs the migration once per combination of image, database and pod
// resources; Job templates are immutable, so any of those changes produces a new Job.
func schemaJob(t *tadossv1.Tadoss, databaseSecret, checksum string) *batchv1.Job {
	template := pod(t, databaseSecret, "")
	template.Labels["app.kubernetes.io/component"] = "schema"
	template.Spec.RestartPolicy = corev1.RestartPolicyOnFailure
	container := &template.Spec.Containers[0]
	container.Name = "schema"
	container.Command = []string{"tadoss", "migrate", "--target", t.Status.Resolved.SchemaTarget}
	container.Env = slices.DeleteFunc(container.Env, func(env corev1.EnvVar) bool {
		return env.Name == "TADOSS_HOST" || apiAuthEnv(env)
	})
	container.StartupProbe, container.ReadinessProbe, container.LivenessProbe, container.Ports = nil, nil, nil, nil
	resources, _ := json.Marshal(template.Spec)
	sum := sha256.Sum256([]byte(t.Spec.API.Image + string(t.Spec.API.ImagePullPolicy) + checksum + string(t.UID) + string(resources) + t.Spec.SchemaRetryNonce + t.Status.Resolved.SchemaTarget))
	return &batchv1.Job{
		TypeMeta:   metav1.TypeMeta{APIVersion: "batch/v1", Kind: "Job"},
		ObjectMeta: metadata(t, fmt.Sprintf("-schema-%x", sum[:5])),
		Spec:       batchv1.JobSpec{Template: template, BackoffLimit: ptr.To(int32(schemaJobBackoffLimit)), ActiveDeadlineSeconds: ptr.To(int64(schemaJobDeadlineSeconds))},
	}
}

func workloads(t *tadossv1.Tadoss, databaseSecret, tokenSecret, checksum string) workloadSet {
	template := pod(t, databaseSecret, tokenSecret)
	if template.Annotations == nil {
		template.Annotations = map[string]string{}
	}
	template.Annotations["checksum/credentials"] = checksum
	selector := &metav1.LabelSelector{MatchLabels: selectorLabels(t)}
	if replicas(t) > 1 {
		template.Spec.TopologySpreadConstraints = []corev1.TopologySpreadConstraint{{MaxSkew: 1, TopologyKey: "kubernetes.io/hostname", WhenUnsatisfiable: corev1.ScheduleAnyway, LabelSelector: selector}}
	}
	set := workloadSet{
		Deployment: &appsv1.Deployment{TypeMeta: metav1.TypeMeta{APIVersion: "apps/v1", Kind: "Deployment"}, ObjectMeta: metadata(t, suffixAPI), Spec: appsv1.DeploymentSpec{Replicas: ptr.To(replicas(t)), Selector: selector, Template: template}},
		Service:    &corev1.Service{TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "Service"}, ObjectMeta: metadata(t, suffixAPI), Spec: corev1.ServiceSpec{Selector: selectorLabels(t), Ports: []corev1.ServicePort{{Name: "http", Port: apiPort, TargetPort: intstr.FromString("http")}}}},
	}
	if replicas(t) > 1 {
		set.Budget = &policyv1.PodDisruptionBudget{TypeMeta: metav1.TypeMeta{APIVersion: "policy/v1", Kind: "PodDisruptionBudget"}, ObjectMeta: metadata(t, suffixAPI), Spec: policyv1.PodDisruptionBudgetSpec{MinAvailable: ptr.To(intstr.FromInt32(1)), Selector: selector}}
	}
	if t.Spec.Ingress.Host != "" {
		ingress := &networkingv1.Ingress{TypeMeta: metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "Ingress"}, ObjectMeta: metadata(t, suffixAPI), Spec: networkingv1.IngressSpec{
			Rules: []networkingv1.IngressRule{{Host: t.Spec.Ingress.Host, IngressRuleValue: networkingv1.IngressRuleValue{HTTP: &networkingv1.HTTPIngressRuleValue{Paths: []networkingv1.HTTPIngressPath{{Path: "/", PathType: ptr.To(networkingv1.PathTypePrefix), Backend: networkingv1.IngressBackend{Service: &networkingv1.IngressServiceBackend{Name: t.Name + suffixAPI, Port: networkingv1.ServiceBackendPort{Number: apiPort}}}}}}}}},
		}}
		if t.Spec.Ingress.ClassName != "" {
			ingress.Spec.IngressClassName = ptr.To(t.Spec.Ingress.ClassName)
		}
		if t.Spec.Ingress.TLSSecretName != "" {
			ingress.Spec.TLS = []networkingv1.IngressTLS{{Hosts: []string{t.Spec.Ingress.Host}, SecretName: t.Spec.Ingress.TLSSecretName}}
		}
		ingress.Annotations = maps.Clone(t.Spec.Ingress.Annotations)
		if t.Spec.Ingress.ClusterIssuer != "" {
			if ingress.Annotations == nil {
				ingress.Annotations = map[string]string{}
			}
			ingress.Annotations["cert-manager.io/cluster-issuer"] = t.Spec.Ingress.ClusterIssuer
			// SelfSigned issuers copy the subject into the issuer DN, which must not be empty.
			if _, supplied := ingress.Annotations["cert-manager.io/subject-organizations"]; !supplied {
				ingress.Annotations["cert-manager.io/subject-organizations"] = "TADOSS"
			}
			// cert-manager 1.20 checks labels, but omits subject-only changes when
			// deciding whether to update an existing Certificate from annotations.
			settings, _ := json.Marshal(ingress.Annotations)
			sum := sha256.Sum256(settings)
			ingress.Labels["tadoss.livewyer.io/certificate-settings"] = fmt.Sprintf("%x", sum[:8])
		}
		set.Ingress = ingress
	}
	workerInstance := t.DeepCopy()
	workerInstance.Spec.API = t.Spec.Worker
	workerTemplate := pod(workerInstance, databaseSecret, tokenSecret)
	workerTemplate.Labels["app.kubernetes.io/component"] = "worker"
	if workerTemplate.Annotations == nil {
		workerTemplate.Annotations = map[string]string{}
	}
	workerTemplate.Annotations["checksum/credentials"] = checksum
	workerContainer := &workerTemplate.Spec.Containers[0]
	workerContainer.Name = "worker"
	workerContainer.Command = []string{"tadoss", "worker"}
	workerContainer.Env = slices.DeleteFunc(workerContainer.Env, apiAuthEnv)
	workerContainer.Ports = []corev1.ContainerPort{{Name: "http", ContainerPort: 8001}}
	workerSelector := &metav1.LabelSelector{MatchLabels: maps.Clone(selector.MatchLabels)}
	workerSelector.MatchLabels["app.kubernetes.io/component"] = "worker"
	if replicas(workerInstance) > 1 {
		workerTemplate.Spec.TopologySpreadConstraints = []corev1.TopologySpreadConstraint{{MaxSkew: 1, TopologyKey: "kubernetes.io/hostname", WhenUnsatisfiable: corev1.ScheduleAnyway, LabelSelector: workerSelector}}
	}
	set.Worker = &appsv1.Deployment{TypeMeta: metav1.TypeMeta{APIVersion: "apps/v1", Kind: "Deployment"}, ObjectMeta: metadata(t, "-worker"), Spec: appsv1.DeploymentSpec{Replicas: ptr.To(replicas(workerInstance)), Selector: workerSelector, Template: workerTemplate}}
	if replicas(workerInstance) > 1 || t.Spec.Worker.MinAvailable != nil {
		minimum := ptr.To(intstr.FromInt32(1))
		if t.Spec.Worker.MinAvailable != nil {
			minimum = t.Spec.Worker.MinAvailable
		}
		set.WorkerBudget = &policyv1.PodDisruptionBudget{TypeMeta: metav1.TypeMeta{APIVersion: "policy/v1", Kind: "PodDisruptionBudget"}, ObjectMeta: metadata(t, "-worker"), Spec: policyv1.PodDisruptionBudgetSpec{MinAvailable: minimum, Selector: workerSelector}}
	}
	if t.Spec.API.MinAvailable != nil {
		set.Budget = &policyv1.PodDisruptionBudget{TypeMeta: metav1.TypeMeta{APIVersion: "policy/v1", Kind: "PodDisruptionBudget"}, ObjectMeta: metadata(t, suffixAPI), Spec: policyv1.PodDisruptionBudgetSpec{MinAvailable: t.Spec.API.MinAvailable, Selector: selector}}
	}
	if ptr.Deref(t.Spec.NetworkPolicy.Enabled, t.Spec.Profile == tadossv1.ProfileMultiServer) {
		set.Policies = []*networkingv1.NetworkPolicy{networkPolicy(t, "api", selector.MatchLabels, t.Spec.NetworkPolicy.API), networkPolicy(t, "worker", workerSelector.MatchLabels, t.Spec.NetworkPolicy.Worker)}
	}
	return set
}

func applyWorkload(template *corev1.PodTemplateSpec, config tadossv1.WorkloadSpec) {
	// Instance/component ownership labels cannot be overridden.
	labels := maps.Clone(config.PodLabels)
	if labels == nil {
		labels = map[string]string{}
	}
	maps.Copy(labels, template.Labels)
	template.Labels = labels
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
	reserved := map[string]bool{}
	for _, env := range container.Env {
		reserved[env.Name] = true
	}
	for _, key := range slices.Sorted(maps.Keys(config.Env)) {
		if !reserved[key] {
			container.Env = append(container.Env, corev1.EnvVar{Name: key, Value: config.Env[key]})
		}
	}
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

func networkPolicy(t *tadossv1.Tadoss, component string, selector map[string]string, rules tadossv1.NetworkRules) *networkingv1.NetworkPolicy {
	ingress, egress := rules.Ingress, rules.Egress
	if ingress == nil && component == "api" {
		ingress = []networkingv1.NetworkPolicyIngressRule{{Ports: []networkingv1.NetworkPolicyPort{{Port: ptr.To(intstr.FromInt32(8000))}}}}
	}
	if egress == nil {
		egress = []networkingv1.NetworkPolicyEgressRule{{Ports: []networkingv1.NetworkPolicyPort{{Protocol: ptr.To(corev1.ProtocolUDP), Port: ptr.To(intstr.FromInt32(53))}, {Protocol: ptr.To(corev1.ProtocolTCP), Port: ptr.To(intstr.FromInt32(53))}, {Port: ptr.To(intstr.FromInt32(5432))}, {Port: ptr.To(intstr.FromInt32(443))}, {Port: ptr.To(intstr.FromInt32(80))}}}}
	}
	return &networkingv1.NetworkPolicy{TypeMeta: metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"}, ObjectMeta: metadata(t, "-"+component), Spec: networkingv1.NetworkPolicySpec{PodSelector: metav1.LabelSelector{MatchLabels: selector}, PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress, networkingv1.PolicyTypeEgress}, Ingress: ingress, Egress: egress}}
}

func apiAuthEnv(env corev1.EnvVar) bool {
	return env.Name == "TADOSS_API_TOKEN" || env.Name == "TADOSS_API_TOKEN_FILE" || strings.HasPrefix(env.Name, "TADOSS_BASIC_") || strings.HasPrefix(env.Name, "TADOSS_OAUTH2_")
}
