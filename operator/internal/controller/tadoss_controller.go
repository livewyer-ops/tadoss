package controller

import (
	"context"
	"errors"

	operatorstatus "github.com/livewyer-ops/tadoss/operator/internal/status"

	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	"github.com/livewyer-ops/tadoss/operator/internal/controller/defaults"
	"github.com/livewyer-ops/tadoss/operator/internal/releases"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/tools/events"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
)

const finalizer = "tadoss.livewyer.io/finalizer"

// TadossReconciler reconciles one Tadoss instance per request.
type TadossReconciler struct {
	client.Client
	Scheme          *runtime.Scheme
	Recorder        events.EventRecorder
	Defaults        defaults.Installation
	WatchNamespaces WatchNamespaceSet
}

// Update transfers managed field ownership before apply. Delete is restricted to
// resources that the controller prunes; garbage collection removes other children.
// +kubebuilder:rbac:groups=tadoss.livewyer.io,resources=tadosses,verbs=get;list;watch;patch
// +kubebuilder:rbac:groups=tadoss.livewyer.io,resources=tadosses/status,verbs=get;patch
// +kubebuilder:rbac:groups=tadoss.livewyer.io,resources=tadosses/finalizers,verbs=update
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=batch,resources=jobs,verbs=get;list;watch;create;patch;delete
// +kubebuilder:rbac:groups="",resources=secrets,verbs=get;list;watch;create;patch
// +kubebuilder:rbac:groups="",resources=services,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups="",resources=pods,verbs=get;list;watch
// +kubebuilder:rbac:groups="",resources=namespaces,verbs=get;list;watch
// +kubebuilder:rbac:groups="",resources=events,verbs=create;patch
// +kubebuilder:rbac:groups=events.k8s.io,resources=events,verbs=create;patch
// +kubebuilder:rbac:groups=networking.k8s.io,resources=ingresses,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=policy,resources=poddisruptionbudgets,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=postgresql.cnpg.io,resources=clusters;scheduledbackups,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=networking.k8s.io,resources=networkpolicies,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=configmaps,verbs=get;list;watch

// Reconcile drives an instance towards its spec and records the outcome in its status.
func (r *TadossReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	if !r.WatchNamespaces.Allows(req.Namespace) {
		return ctrl.Result{}, nil
	}
	t := &tadossv1.Tadoss{}
	if err := r.Get(ctx, req.NamespacedName, t); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !t.DeletionTimestamp.IsZero() {
		return r.handleDeletion(ctx, t)
	}
	if t.Spec.Paused {
		return ctrl.Result{}, r.status(ctx, t, tadossv1.PhaseProgressing, "Paused", "Reconciliation is paused", false)
	}
	if !controllerutil.ContainsFinalizer(t, finalizer) {
		before := t.DeepCopy()
		controllerutil.AddFinalizer(t, finalizer)
		return ctrl.Result{}, r.Patch(ctx, t, client.MergeFromWithOptions(before, client.MergeFromWithOptimisticLock{}))
	}
	if err := defaults.Resolve(t, r.Defaults); err != nil {
		reason := "ConfigurationInvalid"
		var selection *releases.SelectionError
		if errors.As(err, &selection) {
			reason = selection.Reason
		}
		return ctrl.Result{}, r.status(ctx, t, tadossv1.PhaseError, reason, err.Error(), false)
	}
	if t.Status.CurrentVersion != t.Spec.Version && t.Status.PendingVersion == "" {
		t.Status.PendingVersion = t.Spec.Version
		return r.pending(ctx, t, "ReleasePending", "Selected release recorded before changing managed resources")
	}
	result, err := r.reconcile(ctx, t)
	if err != nil {
		// Resource errors contain names and API diagnostics, never credential values.
		if statusErr := r.status(ctx, t, tadossv1.PhaseError, "ReconcileFailed", err.Error(), false); statusErr != nil {
			logf.FromContext(ctx).Error(statusErr, "recording the error status")
		}
	}
	return result, err
}

func (r *TadossReconciler) reconcile(ctx context.Context, t *tadossv1.Tadoss) (ctrl.Result, error) {
	for _, condition := range []string{"DatabaseReady", "SchemaMigrated", "APIReady", "WorkerReady"} {
		if meta.FindStatusCondition(t.Status.Conditions, condition) == nil {
			operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, condition, false, "Pending", "Waiting for reconciliation")
		}
	}
	database, result, err := r.reconcileDatabase(ctx, t)
	if err != nil || database == nil {
		return result, err
	}
	credentials, result, err := r.reconcileCredentials(ctx, t, database)
	if err != nil || credentials == nil {
		return result, err
	}
	job := schemaJob(t, database.Name, referenceFingerprint(credentials.databaseReferences...))
	migrated, result, err := r.reconcileSchema(ctx, t, job)
	if err != nil || !migrated {
		return result, err
	}
	return r.reconcileWorkloads(ctx, t, job.Name, credentials)
}
