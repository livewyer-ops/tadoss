package controller

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"maps"
	"slices"
	"time"

	operatorstatus "github.com/livewyer-ops/tadoss/operator/internal/status"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	policyv1 "k8s.io/api/policy/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func (r *TadossReconciler) reconcileWorkloads(ctx context.Context, t *tadossv1.Tadoss, currentJob string, credentials *credentials) (ctrl.Result, error) {
	if err := r.reconcilePooler(ctx, t); err != nil {
		return ctrl.Result{}, err
	}
	if err := r.reconcileAutoscaling(ctx, t); err != nil {
		return ctrl.Result{}, err
	}
	set := workloads(t, credentials.database.Name, t.Status.TokenSecret, referenceFingerprint(credentials.workloadReferences...))
	for _, object := range set.objects() {
		if err := r.apply(ctx, t, object); err != nil {
			return ctrl.Result{}, err
		}
	}
	if err := r.prune(ctx, t, currentJob, set); err != nil {
		return ctrl.Result{}, err
	}
	r.reconcileMonitoring(ctx, t)
	deployment := set.Deployment
	t.Status.ReadyReplicas = deployment.Status.ReadyReplicas
	t.Status.Endpoint = fmt.Sprintf("http://%s%s.%s.svc:%d", t.Name, suffixAPI, t.Namespace, apiPort)
	if t.Spec.Ingress.Host != "" {
		scheme := "http"
		if t.Spec.Ingress.TLSSecretName != "" {
			scheme = "https"
		}
		t.Status.Endpoint = scheme + "://" + t.Spec.Ingress.Host
	}
	desiredReplicas := replicas(t)
	if t.Spec.API.Autoscaling.Enabled {
		desiredReplicas = ptr.Deref(deployment.Spec.Replicas, 1)
	}
	apiReady := deploymentReady(deployment, desiredReplicas)
	operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "APIReady", apiReady, "Rollout", "API rollout must finish")
	workerReady := deploymentReady(set.Worker, *set.Worker.Spec.Replicas)
	t.Status.WorkerReadyReplicas = set.Worker.Status.ReadyReplicas
	operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "WorkerReady", workerReady, "Rollout", "Worker rollout must finish")
	if !apiReady || !workerReady || !meta.IsStatusConditionTrue(t.Status.Conditions, "PoolerReady") {
		return r.pending(ctx, t, "RolloutPending", "Waiting for API, worker and Pooler rollouts")
	}
	if !t.Spec.Backends.DB.CNPG.Pooler.Enabled {
		object, err := r.owned(ctx, t, client.ObjectKey{Namespace: t.Namespace, Name: t.Name + "-pooler"}, &cnpgv1.Pooler{})
		if err != nil {
			return ctrl.Result{}, err
		}
		if object != nil {
			if err := r.Delete(ctx, object); client.IgnoreNotFound(err) != nil {
				return ctrl.Result{}, err
			}
		}
	}
	t.Status.CurrentVersion = t.Spec.Version
	t.Status.PendingVersion = ""
	t.Status.AppliedDefaultsRevision = t.Status.Resolved.DefaultsRevision
	if err := r.status(ctx, t, tadossv1.PhaseReady, "Available", "Database schema and API are ready", true); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
}

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
	if t.Spec.API.Autoscaling.Enabled {
		set.Deployment.Spec.Replicas = nil
	}
	if t.Spec.Backends.DB.CNPG.Pooler.Enabled {
		setContainerEnv(&set.Deployment.Spec.Template.Spec.Containers[0], corev1.EnvVar{Name: "TADOSS_DATABASE_HOST", Value: t.Name + "-pooler." + t.Namespace + ".svc"})
	}
	if ptr.Deref(t.Spec.Monitoring.Enabled, false) {
		setContainerEnv(&set.Deployment.Spec.Template.Spec.Containers[0], secretKeyEnv("TADOSS_METRICS_TOKEN", t.Name+"-metrics", "token"))
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
	workerInstance.Spec.API = tadossv1.APIWorkloadSpec{WorkloadSpec: t.Spec.Worker}
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
	if t.Spec.Backends.DB.CNPG.Pooler.Enabled {
		setContainerEnv(workerContainer, corev1.EnvVar{Name: "TADOSS_DATABASE_HOST", Value: ""})
	}
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

func networkPolicy(t *tadossv1.Tadoss, component string, selector map[string]string, rules tadossv1.NetworkRules) *networkingv1.NetworkPolicy {
	ingress, egress := rules.Ingress, rules.Egress
	if ingress == nil && component == "api" {
		ingress = []networkingv1.NetworkPolicyIngressRule{{Ports: []networkingv1.NetworkPolicyPort{{Port: ptr.To(intstr.FromInt32(8000))}}}}
	}
	if ptr.Deref(t.Spec.Monitoring.Enabled, false) && component == "worker" {
		ingress = append(slices.Clone(ingress), networkingv1.NetworkPolicyIngressRule{
			From:  []networkingv1.NetworkPolicyPeer{{NamespaceSelector: t.Spec.Monitoring.NamespaceSelector}},
			Ports: []networkingv1.NetworkPolicyPort{{Port: ptr.To(intstr.FromInt32(8001))}},
		})
	}
	if egress == nil {
		egress = []networkingv1.NetworkPolicyEgressRule{{Ports: []networkingv1.NetworkPolicyPort{{Protocol: ptr.To(corev1.ProtocolUDP), Port: ptr.To(intstr.FromInt32(53))}, {Protocol: ptr.To(corev1.ProtocolTCP), Port: ptr.To(intstr.FromInt32(53))}, {Port: ptr.To(intstr.FromInt32(5432))}, {Port: ptr.To(intstr.FromInt32(443))}, {Port: ptr.To(intstr.FromInt32(80))}}}}
	}
	return &networkingv1.NetworkPolicy{TypeMeta: metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"}, ObjectMeta: metadata(t, "-"+component), Spec: networkingv1.NetworkPolicySpec{PodSelector: metav1.LabelSelector{MatchLabels: selector}, PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress, networkingv1.PolicyTypeEgress}, Ingress: ingress, Egress: egress}}
}
