package controller

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"maps"

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
	suffixToken = "-api-token"

	// The migration waits up to five minutes for a restarting database, so the Job
	// deadline must comfortably exceed that plus the retries.
	schemaJobBackoffLimit    = 4
	schemaJobDeadlineSeconds = 900
)

// workloadSet holds the API workloads of an instance in apply order.
type workloadSet struct {
	Deployment *appsv1.Deployment
	Service    *corev1.Service
	Budget     *policyv1.PodDisruptionBudget
	Ingress    *networkingv1.Ingress
}

func (w workloadSet) objects() []client.Object {
	objects := []client.Object{w.Deployment, w.Service}
	if w.Budget != nil {
		objects = append(objects, w.Budget)
	}
	if w.Ingress != nil {
		objects = append(objects, w.Ingress)
	}
	return objects
}

func replicas(t *tadossv1.Tadoss) int32 {
	if t.Spec.Replicas != nil {
		return *t.Spec.Replicas
	}
	if t.Spec.Profile == tadossv1.ProfileMultiServer {
		return 2
	}
	return 1
}

func databaseInstances(t *tadossv1.Tadoss) int {
	if t.Spec.Database.Instances != nil {
		return int(*t.Spec.Database.Instances)
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
	if len(resources.Requests) == 0 {
		resources.Requests = requests
	}
	if len(resources.Limits) == 0 {
		resources.Limits = limits
	}
	return *resources
}

func databaseCluster(t *tadossv1.Tadoss) *cnpgv1.Cluster {
	image := t.Spec.Database.Image
	if image == "" {
		image = tadossv1.DefaultPostgresImage
	}
	return &cnpgv1.Cluster{
		TypeMeta:   metav1.TypeMeta{APIVersion: "postgresql.cnpg.io/v1", Kind: "Cluster"},
		ObjectMeta: metadata(t, suffixDB),
		Spec: cnpgv1.ClusterSpec{
			Instances:            databaseInstances(t),
			ImageName:            image,
			StorageConfiguration: cnpgv1.StorageConfiguration{Size: t.Spec.Database.Storage, StorageClass: t.Spec.Database.StorageClass},
			Bootstrap:            &cnpgv1.BootstrapConfiguration{InitDB: &cnpgv1.BootstrapInitDB{Database: "tadoss", Owner: "tadoss"}},
			Resources: withDefaults(t.Spec.Database.Resources,
				corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("100m"), corev1.ResourceMemory: resource.MustParse("128Mi")},
				corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("1"), corev1.ResourceMemory: resource.MustParse("1Gi")}),
			EnableSuperuserAccess: ptr.To(false),
		},
	}
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
	resources := withDefaults(t.Spec.Resources,
		corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("100m"), corev1.ResourceMemory: resource.MustParse(memory)},
		corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("1"), corev1.ResourceMemory: resource.MustParse("512Mi")})
	return corev1.PodTemplateSpec{
		ObjectMeta: metav1.ObjectMeta{Labels: podLabels},
		Spec: corev1.PodSpec{
			AutomountServiceAccountToken: ptr.To(false),
			SecurityContext:              &corev1.PodSecurityContext{RunAsNonRoot: ptr.To(true), RunAsUser: ptr.To(int64(10001)), RunAsGroup: ptr.To(int64(10001)), SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault}},
			Containers: []corev1.Container{{
				Name: "api", Image: t.Spec.Image, ImagePullPolicy: t.Spec.ImagePullPolicy,
				Env:             []corev1.EnvVar{secretEnv("TADOSS_DATABASE", databaseSecret, "uri"), secretEnv("TADOSS_TOKEN", tokenSecret, "token"), {Name: "TADOSS_AUTO_MIGRATE", Value: "false"}},
				Ports:           []corev1.ContainerPort{{Name: "http", ContainerPort: apiPort}},
				Resources:       resources,
				SecurityContext: &corev1.SecurityContext{AllowPrivilegeEscalation: ptr.To(false), ReadOnlyRootFilesystem: ptr.To(true), Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}}},
				StartupProbe:    probe("/readyz", 30, 2), ReadinessProbe: probe("/readyz", 3, 5), LivenessProbe: probe("/healthz", 3, 10),
				VolumeMounts: []corev1.VolumeMount{{Name: "tmp", MountPath: "/tmp"}},
			}},
			Volumes: []corev1.Volume{{Name: "tmp", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}}},
		},
	}
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
	container.Command = []string{"python", "-m", "tadoss", "migrate"}
	container.Env = []corev1.EnvVar{secretEnv("TADOSS_DATABASE", databaseSecret, "uri")}
	container.StartupProbe, container.ReadinessProbe, container.LivenessProbe, container.Ports = nil, nil, nil, nil
	resources, _ := json.Marshal(container.Resources)
	sum := sha256.Sum256([]byte(t.Spec.Image + string(t.Spec.ImagePullPolicy) + checksum + string(t.UID) + string(resources)))
	return &batchv1.Job{
		TypeMeta:   metav1.TypeMeta{APIVersion: "batch/v1", Kind: "Job"},
		ObjectMeta: metadata(t, fmt.Sprintf("-schema-%x", sum[:5])),
		Spec:       batchv1.JobSpec{Template: template, BackoffLimit: ptr.To(int32(schemaJobBackoffLimit)), ActiveDeadlineSeconds: ptr.To(int64(schemaJobDeadlineSeconds))},
	}
}

func workloads(t *tadossv1.Tadoss, databaseSecret, tokenSecret, checksum string) workloadSet {
	template := pod(t, databaseSecret, tokenSecret)
	template.Annotations = map[string]string{"checksum/credentials": checksum}
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
			IngressClassName: ptr.To(t.Spec.Ingress.ClassName),
			Rules:            []networkingv1.IngressRule{{Host: t.Spec.Ingress.Host, IngressRuleValue: networkingv1.IngressRuleValue{HTTP: &networkingv1.HTTPIngressRuleValue{Paths: []networkingv1.HTTPIngressPath{{Path: "/", PathType: ptr.To(networkingv1.PathTypePrefix), Backend: networkingv1.IngressBackend{Service: &networkingv1.IngressServiceBackend{Name: t.Name + suffixAPI, Port: networkingv1.ServiceBackendPort{Number: apiPort}}}}}}}}},
		}}
		if t.Spec.Ingress.TLSSecretName != "" {
			ingress.Spec.TLS = []networkingv1.IngressTLS{{Hosts: []string{t.Spec.Ingress.Host}, SecretName: t.Spec.Ingress.TLSSecretName}}
		}
		ingress.Annotations = maps.Clone(t.Spec.Ingress.Annotations)
		if t.Spec.Ingress.ClusterIssuer != "" {
			if ingress.Annotations == nil {
				ingress.Annotations = map[string]string{}
			}
			ingress.Annotations["cert-manager.io/cluster-issuer"] = t.Spec.Ingress.ClusterIssuer
		}
		set.Ingress = ingress
	}
	return set
}
