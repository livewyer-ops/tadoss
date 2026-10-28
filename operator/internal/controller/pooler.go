package controller

import (
	"context"
	"strconv"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	operatorstatus "github.com/livewyer-ops/tadoss/operator/internal/status"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func databasePooler(t *tadossv1.Tadoss) *cnpgv1.Pooler {
	config := t.Spec.Backends.DB.CNPG.Pooler
	servers := strconv.Itoa(int(ptr.Deref(config.ServerConnections, 16)))
	name := t.Name + "-pooler"
	return &cnpgv1.Pooler{
		TypeMeta: metav1.TypeMeta{APIVersion: "postgresql.cnpg.io/v1", Kind: "Pooler"}, ObjectMeta: metadata(t, "-pooler"),
		Spec: cnpgv1.PoolerSpec{
			Cluster: cnpgv1.LocalObjectReference{Name: t.Name + suffixDB}, Type: cnpgv1.PoolerTypeRW,
			Instances:          ptr.To(ptr.Deref(config.Instances, 2)),
			DeploymentStrategy: &appsv1.DeploymentStrategy{Type: appsv1.RollingUpdateDeploymentStrategyType, RollingUpdate: &appsv1.RollingUpdateDeployment{MaxSurge: ptr.To(intstr.FromInt32(1)), MaxUnavailable: ptr.To(intstr.FromInt32(0))}},
			PgBouncer: &cnpgv1.PgBouncerSpec{PoolMode: cnpgv1.PgBouncerPoolModeTransaction, Parameters: map[string]string{
				"default_pool_size": servers, "max_db_connections": servers, "max_user_connections": servers,
				"reserve_pool_size": "0", "max_prepared_statements": "100",
				"max_client_conn": strconv.Itoa(int(ptr.Deref(config.MaxClientConnections, 100))), "query_wait_timeout": "30",
			}},
			Template: &cnpgv1.PodTemplateSpec{
				ObjectMeta: cnpgv1.Metadata{Labels: map[string]string{"tadoss.livewyer.io/pooler": name}},
				Spec: corev1.PodSpec{
					Containers: []corev1.Container{{Name: "pgbouncer", Resources: withDefaults(config.Resources,
						corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("100m"), corev1.ResourceMemory: resource.MustParse("128Mi")},
						corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("1"), corev1.ResourceMemory: resource.MustParse("256Mi")})}},
					Affinity: &corev1.Affinity{PodAntiAffinity: &corev1.PodAntiAffinity{RequiredDuringSchedulingIgnoredDuringExecution: []corev1.PodAffinityTerm{{TopologyKey: "kubernetes.io/hostname", LabelSelector: &metav1.LabelSelector{MatchLabels: map[string]string{"tadoss.livewyer.io/pooler": name}}}}}},
				},
			},
		},
	}
}

func (r *TadossReconciler) reconcilePooler(ctx context.Context, t *tadossv1.Tadoss) error {
	if !t.Spec.Backends.DB.CNPG.Pooler.Enabled {
		operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "PoolerReady", true, "Disabled", "API connects directly to PostgreSQL")
		return nil
	}
	p := databasePooler(t)
	if err := r.apply(ctx, t, p); err != nil {
		return err
	}
	deployment := &appsv1.Deployment{}
	if err := r.Get(ctx, client.ObjectKeyFromObject(p), deployment); client.IgnoreNotFound(err) != nil {
		return err
	}
	ready := deploymentReady(deployment, *p.Spec.Instances)
	operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "PoolerReady", ready, "Rollout", "CNPG transaction pool must be available")
	return nil
}
