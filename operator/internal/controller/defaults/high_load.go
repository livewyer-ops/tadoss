package defaults

import (
	"fmt"
	"maps"
	"strconv"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	autoscalingv2 "k8s.io/api/autoscaling/v2"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
)

func highLoad(t *tadossv1.Tadoss, installation Installation) error {
	m := &t.Spec.Monitoring
	if m.Enabled == nil {
		m.Enabled = ptr.To(ptr.Deref(installation.Monitoring.Enabled, false))
	}
	labels := maps.Clone(installation.Monitoring.Labels)
	if labels == nil {
		labels = map[string]string{}
	}
	maps.Copy(labels, m.Labels)
	m.Labels = labels
	if m.NamespaceSelector == nil {
		m.NamespaceSelector = installation.Monitoring.NamespaceSelector.DeepCopy()
	}
	if m.NamespaceSelector == nil {
		m.NamespaceSelector = &metav1.LabelSelector{MatchLabels: map[string]string{"kubernetes.io/metadata.name": "monitoring"}}
	}
	a := &t.Spec.API.Autoscaling
	if a.Enabled {
		if t.Spec.API.ReplicaCount != nil {
			return fmt.Errorf("api.replicaCount and enabled autoscaling are mutually exclusive")
		}
		a.MinReplicas = ptr.To(ptr.Deref(a.MinReplicas, 2))
		a.MaxReplicas = ptr.To(ptr.Deref(a.MaxReplicas, 8))
		if *a.MinReplicas < 1 || *a.MaxReplicas < *a.MinReplicas {
			return fmt.Errorf("autoscaling requires 1 <= minReplicas <= maxReplicas")
		}
		if len(a.Metrics) == 0 {
			a.Metrics = []autoscalingv2.MetricSpec{{Type: autoscalingv2.ContainerResourceMetricSourceType, ContainerResource: &autoscalingv2.ContainerResourceMetricSource{Name: corev1.ResourceCPU, Container: "api", Target: autoscalingv2.MetricTarget{Type: autoscalingv2.UtilizationMetricType, AverageUtilization: ptr.To(int32(70))}}}}
		}
		if a.Behavior == nil {
			a.Behavior = &autoscalingv2.HorizontalPodAutoscalerBehavior{
				ScaleDown: &autoscalingv2.HPAScalingRules{StabilizationWindowSeconds: ptr.To(int32(300)), Policies: []autoscalingv2.HPAScalingPolicy{{Type: autoscalingv2.PodsScalingPolicy, Value: 1, PeriodSeconds: 60}}},
				ScaleUp:   &autoscalingv2.HPAScalingRules{StabilizationWindowSeconds: ptr.To(int32(0)), Policies: []autoscalingv2.HPAScalingPolicy{{Type: autoscalingv2.PodsScalingPolicy, Value: 2, PeriodSeconds: 60}}},
			}
		}
	}
	if t.Spec.Backends.DB.ProvidedBy != tadossv1.ProviderManaged {
		if t.Spec.Backends.DB.CNPG.Pooler.Enabled {
			return fmt.Errorf("operator-managed pooling requires a CNPG database")
		}
		return nil
	}
	db := &t.Spec.Backends.DB.CNPG
	if t.Spec.Profile == tadossv1.ProfileMultiServer && db.Postgresql.Synchronous == nil {
		db.Postgresql.Synchronous = &cnpgv1.SynchronousReplicaConfiguration{Method: cnpgv1.SynchronousReplicaConfigurationMethodAny, Number: 1, DataDurability: cnpgv1.DataDurabilityLevelRequired, FailoverQuorum: true}
	}
	if sync := db.Postgresql.Synchronous; sync != nil {
		instances := int32(1)
		if t.Spec.Profile == tadossv1.ProfileMultiServer {
			instances = 3
		}
		if sync.Number < 1 || sync.Number >= int(ptr.Deref(db.Instances, instances)) {
			return fmt.Errorf("synchronous standby count must be positive and below database instances")
		}
		if sync.DataDurability == cnpgv1.DataDurabilityLevelRequired {
			if db.Postgresql.Parameters == nil {
				db.Postgresql.Parameters = map[string]string{}
			}
			if value := db.Postgresql.Parameters["synchronous_commit"]; value != "" && value != "on" && value != "remote_apply" {
				return fmt.Errorf("required durability needs synchronous_commit=on or remote_apply")
			}
			for _, setting := range []string{"fsync", "full_page_writes"} {
				if v := db.Postgresql.Parameters[setting]; v != "" && v != "on" {
					return fmt.Errorf("required durability needs %s=on", setting)
				}
			}
			if db.Postgresql.Parameters["synchronous_commit"] == "" {
				db.Postgresql.Parameters["synchronous_commit"] = "on"
			}
		}
	}
	p := &db.Pooler
	if !p.Enabled {
		return nil
	}
	p.Instances = ptr.To(ptr.Deref(p.Instances, 2))
	p.ServerConnections = ptr.To(ptr.Deref(p.ServerConnections, 16))
	p.MaxClientConnections = ptr.To(ptr.Deref(p.MaxClientConnections, 100))
	if *p.Instances < 1 || *p.ServerConnections < 1 || *p.MaxClientConnections < *p.ServerConnections {
		return fmt.Errorf("pooler instances and connections must be positive; clients must cover server connections")
	}
	if t.Spec.Worker.Env == nil {
		t.Spec.Worker.Env = map[string]string{}
	}
	if t.Spec.Worker.Env["TADOSS_POOL_MAX_SIZE"] == "" {
		t.Spec.Worker.Env["TADOSS_POOL_MAX_SIZE"] = "10"
	}
	workers := int32(1)
	if t.Spec.Profile == tadossv1.ProfileMultiServer {
		workers = 2
	}
	workers = ptr.Deref(t.Spec.Worker.ReplicaCount, workers)
	workerPool, err := strconv.Atoi(t.Spec.Worker.Env["TADOSS_POOL_MAX_SIZE"])
	if err != nil || workerPool < 1 {
		return fmt.Errorf("pooling requires a positive worker TADOSS_POOL_MAX_SIZE")
	}
	maxConnections := 100
	if value := db.Postgresql.Parameters["max_connections"]; value != "" {
		maxConnections, err = strconv.Atoi(value)
		if err != nil || maxConnections < 1 {
			return fmt.Errorf("max_connections must be a positive integer")
		}
	}
	// Poolers roll with one extra pod; worker Deployments use 25% surge.
	workerSurge := (workers + 3) / 4
	budget := int64(*p.Instances+1)*int64(*p.ServerConnections) + int64(workers+workerSurge)*int64(workerPool) + 20
	if budget > int64(maxConnections) {
		return fmt.Errorf("connection budget %d exceeds max_connections %d (includes rollout surge and 20 reserved connections)", budget, maxConnections)
	}
	return nil
}
