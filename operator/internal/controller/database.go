package controller

import (
	"context"

	operatorstatus "github.com/livewyer-ops/tadoss/operator/internal/status"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// reconcileDatabase returns no Secret while database readiness is unresolved.
func (r *TadossReconciler) reconcileDatabase(ctx context.Context, t *tadossv1.Tadoss) (*corev1.Secret, ctrl.Result, error) {
	databaseSecret := t.Spec.Backends.DB.External.ExistingSecret
	if t.Spec.Backends.DB.ProvidedBy == tadossv1.ProviderManaged {
		cluster := databaseCluster(t)
		live := &cnpgv1.Cluster{}
		if err := r.Get(ctx, client.ObjectKeyFromObject(cluster), live); err == nil && !equality.Semantic.DeepDerivative(cluster.Spec.Bootstrap, live.Spec.Bootstrap) {
			return nil, ctrl.Result{}, r.status(ctx, t, tadossv1.PhaseError, "RestoreRequiresNewInstance", "Database bootstrap configuration requires a new instance", false)
		}
		if err := r.apply(ctx, t, cluster); err != nil {
			return nil, ctrl.Result{}, err
		}
		if t.Spec.Backends.DB.CNPG.Backup.Enabled {
			backup := &cnpgv1.ScheduledBackup{
				TypeMeta:   metav1.TypeMeta{APIVersion: "postgresql.cnpg.io/v1", Kind: "ScheduledBackup"},
				ObjectMeta: metadata(t, "-backup"),
				Spec: cnpgv1.ScheduledBackupSpec{
					Schedule:             t.Spec.Backends.DB.CNPG.Backup.Schedule,
					Cluster:              cnpgv1.LocalObjectReference{Name: cluster.Name},
					BackupOwnerReference: "none",
				},
			}
			if err := r.apply(ctx, t, backup); err != nil {
				return nil, ctrl.Result{}, err
			}
			operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "BackupPolicyReady", cluster.Status.LastSuccessfulBackup != "", "BackupState", "CNPG must report a successful backup; restore requires separate verification") //nolint:staticcheck // Pinned native Barman integration populates this field.
		} else {
			operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "BackupPolicyReady", false, "NotConfigured", "Managed backups are not configured")
		}
		databaseSecret = t.Name + suffixDBApp
		rolloutReady, err := r.cnpgRolloutReady(ctx, cluster)
		if err != nil {
			return nil, ctrl.Result{}, err
		}
		if cluster.Status.ReadyInstances < databaseInstances(t) || !rolloutReady {
			operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "DatabaseReady", false, "DatabasePending", "Waiting for PostgreSQL")
			result, err := r.pending(ctx, t, "DatabasePending", "Waiting for PostgreSQL")
			return nil, result, err
		}
	}
	database := &corev1.Secret{}
	if err := r.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: databaseSecret}, database); err != nil {
		if apierrors.IsNotFound(err) {
			operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "DatabaseReady", false, "DatabaseSecretMissing", "Waiting for the database Secret")
			result, err := r.pending(ctx, t, "DatabaseSecretMissing", "Waiting for the database Secret")
			return nil, result, err
		}
		return nil, ctrl.Result{}, err
	}
	if len(database.Data["uri"]) == 0 {
		operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "DatabaseReady", false, "DatabaseSecretInvalid", "Database Secret must contain uri")
		result, err := r.pending(ctx, t, "DatabaseSecretInvalid", "Database Secret must contain uri")
		return nil, result, err
	}
	operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "DatabaseReady", true, "Available", "Database credentials are available")
	return database, ctrl.Result{}, nil
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
