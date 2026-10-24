package controller

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"k8s.io/apimachinery/pkg/api/equality"
	"reflect"
	"slices"
	"time"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	"github.com/livewyer-ops/tadoss/operator/internal/controller/defaults"
	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	policyv1 "k8s.io/api/policy/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/tools/events"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

const (
	finalizer  = "tadoss.livewyer.io/finalizer"
	fieldOwner = "tadoss-operator"
)

// TadossReconciler reconciles one Tadoss instance per request.
type TadossReconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	Recorder events.EventRecorder
	Defaults defaults.Installation
}

// Owned resources are removed by garbage collection, so delete is granted only where the
// controller deletes directly: superseded Jobs, pruned Ingresses and budgets, and the
// one-off Deployment recreation. Leader-election leases are granted by the chart in the
// release namespace.
// +kubebuilder:rbac:groups=tadoss.livewyer.io,resources=tadosses,verbs=get;list;watch;patch
// +kubebuilder:rbac:groups=tadoss.livewyer.io,resources=tadosses/status,verbs=get;patch
// +kubebuilder:rbac:groups=tadoss.livewyer.io,resources=tadosses/finalizers,verbs=update
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;patch;delete
// +kubebuilder:rbac:groups=batch,resources=jobs,verbs=get;list;watch;create;patch;delete
// +kubebuilder:rbac:groups="",resources=secrets;services,verbs=get;list;watch;create;patch
// +kubebuilder:rbac:groups="",resources=namespaces,verbs=get;list;watch
// +kubebuilder:rbac:groups="",resources=events,verbs=create;patch
// +kubebuilder:rbac:groups=events.k8s.io,resources=events,verbs=create;patch
// +kubebuilder:rbac:groups=networking.k8s.io,resources=ingresses,verbs=get;list;watch;create;patch;delete
// +kubebuilder:rbac:groups=policy,resources=poddisruptionbudgets,verbs=get;list;watch;create;patch;delete
// +kubebuilder:rbac:groups=postgresql.cnpg.io,resources=clusters;scheduledbackups,verbs=get;list;watch;create;patch;delete
// +kubebuilder:rbac:groups=networking.k8s.io,resources=networkpolicies,verbs=get;list;watch;create;patch;delete
// +kubebuilder:rbac:groups="",resources=configmaps,verbs=get;list;watch

// Reconcile drives an instance towards its spec and records the outcome in its status.
func (r *TadossReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
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
		return ctrl.Result{}, r.status(ctx, t, tadossv1.PhaseError, "ConfigurationInvalid", err.Error(), false)
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
			setCondition(t, condition, false, "Pending", "Waiting for reconciliation")
		}
	}
	databaseSecret := t.Spec.Backends.DB.External.ExistingSecret
	if t.Spec.Backends.DB.ProvidedBy == tadossv1.ProviderManaged {
		cluster := databaseCluster(t)
		live := &cnpgv1.Cluster{}
		if err := r.Get(ctx, client.ObjectKeyFromObject(cluster), live); err == nil && !equality.Semantic.DeepDerivative(cluster.Spec.Bootstrap, live.Spec.Bootstrap) {
			return ctrl.Result{}, r.status(ctx, t, tadossv1.PhaseError, "RestoreRequiresNewInstance", "Database bootstrap configuration requires a new instance", false)
		}
		if err := r.apply(ctx, t, cluster); err != nil {
			return ctrl.Result{}, err
		}
		if t.Spec.Backends.DB.CNPG.Backup.Enabled {
			backup := &cnpgv1.ScheduledBackup{TypeMeta: metav1.TypeMeta{APIVersion: "postgresql.cnpg.io/v1", Kind: "ScheduledBackup"}, ObjectMeta: metadata(t, "-backup"), Spec: cnpgv1.ScheduledBackupSpec{Schedule: t.Spec.Backends.DB.CNPG.Backup.Schedule, Cluster: cnpgv1.LocalObjectReference{Name: cluster.Name}, BackupOwnerReference: "none"}}
			if err := r.apply(ctx, t, backup); err != nil {
				return ctrl.Result{}, err
			}
			setCondition(t, "BackupPolicyReady", cluster.Status.LastSuccessfulBackup != "", "BackupState", "CNPG must report a successful backup; restore requires separate verification") //nolint:staticcheck // Pinned native Barman integration populates this field.
		} else {
			setCondition(t, "BackupPolicyReady", false, "NotConfigured", "Managed backups are not configured")
		}
		databaseSecret = t.Name + suffixDBApp
		if cluster.Status.ReadyInstances < databaseInstances(t) {
			setCondition(t, "DatabaseReady", false, "DatabasePending", "Waiting for PostgreSQL")
			return r.pending(ctx, t, "DatabasePending", "Waiting for PostgreSQL")
		}
	}
	database := &corev1.Secret{}
	if err := r.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: databaseSecret}, database); err != nil {
		if apierrors.IsNotFound(err) {
			setCondition(t, "DatabaseReady", false, "DatabaseSecretMissing", "Waiting for the database Secret")
			return r.pending(ctx, t, "DatabaseSecretMissing", "Waiting for the database Secret")
		}
		return ctrl.Result{}, err
	}
	if len(database.Data["uri"]) == 0 {
		setCondition(t, "DatabaseReady", false, "DatabaseSecretInvalid", "Database Secret must contain uri")
		return r.pending(ctx, t, "DatabaseSecretInvalid", "Database Secret must contain uri")
	}
	setCondition(t, "DatabaseReady", true, "Available", "Database credentials are available")
	token, err := r.token(ctx, t)
	if err != nil {
		if apierrors.IsNotFound(err) {
			return r.pending(ctx, t, "TokenSecretMissing", "Waiting for the token Secret")
		}
		return ctrl.Result{}, err
	}
	if len(token.Data["token"]) == 0 {
		return r.pending(ctx, t, "TokenSecretInvalid", "Token Secret must contain token")
	}
	t.Status.DatabaseSecret, t.Status.TokenSecret = database.Name, token.Name
	databaseReferences := []client.Object{database}
	credentialReferences := []client.Object{database, token}
	for _, name := range referencedSecrets(t) {
		if name == "" || name == database.Name || name == token.Name {
			continue
		}
		secret := &corev1.Secret{}
		if err := r.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: name}, secret); err != nil {
			return r.pending(ctx, t, "ReferencedSecretMissing", "A referenced Secret is unavailable")
		}
		if name == t.Spec.Auth.BasicSecret && (len(secret.Data["username"]) == 0 || len(secret.Data["password"]) == 0) {
			return r.pending(ctx, t, "BasicSecretInvalid", "Basic Secret requires username and password")
		}
		credentialReferences = append(credentialReferences, secret)
	}
	if t.Spec.Backends.DB.ProvidedBy == tadossv1.ProviderManaged {
		ca := &corev1.Secret{}
		if err := r.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: t.Name + suffixDBCA}, ca); err != nil {
			setCondition(t, "DatabaseReady", false, "DatabaseCAMissing", "Waiting for the managed database CA Secret")
			return r.pending(ctx, t, "DatabaseCAMissing", "Waiting for the managed database CA Secret")
		}
		if len(ca.Data["ca.crt"]) == 0 {
			setCondition(t, "DatabaseReady", false, "DatabaseCAInvalid", "Database CA Secret must contain ca.crt")
			return r.pending(ctx, t, "DatabaseCAInvalid", "Database CA Secret must contain ca.crt")
		}
		databaseReferences = append(databaseReferences, ca)
		credentialReferences = append(credentialReferences, ca)
	} else if ca := t.Spec.Backends.DB.External.CAConfigMap; ca != nil {
		config := &corev1.ConfigMap{}
		if err := r.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: ca.Name}, config); err != nil {
			setCondition(t, "DatabaseReady", false, "DatabaseCAMissing", "Database CA ConfigMap is unavailable")
			return r.pending(ctx, t, "DatabaseCAMissing", "Database CA ConfigMap is unavailable")
		}
		if config.Data[ca.Key] == "" {
			setCondition(t, "DatabaseReady", false, "DatabaseCAInvalid", "Database CA key is empty")
			return r.pending(ctx, t, "DatabaseCAInvalid", "Database CA key is empty")
		}
		databaseReferences = append(databaseReferences, config)
		credentialReferences = append(credentialReferences, config)
	}
	job := schemaJob(t, database.Name, referenceFingerprint(databaseReferences...))
	if t.Status.SchemaJob != job.Name {
		setCondition(t, "SchemaMigrated", false, "SchemaPending", "Waiting for selected schema Job")
	}
	t.Status.SchemaJob = job.Name
	liveJob := &batchv1.Job{}
	err = r.Get(ctx, client.ObjectKeyFromObject(job), liveJob)
	if apierrors.IsNotFound(err) {
		if err = controllerutil.SetControllerReference(t, job, r.Scheme); err != nil {
			return ctrl.Result{}, err
		}
		if err = r.Create(ctx, job); err != nil {
			return ctrl.Result{}, err
		}
		return r.pending(ctx, t, "SchemaPending", "Initialising the database schema")
	}
	if err != nil {
		return ctrl.Result{}, err
	}
	if !metav1.IsControlledBy(liveJob, t) {
		return ctrl.Result{}, fmt.Errorf("schema Job %s is not owned by this instance", job.Name)
	}
	complete := false
	for _, condition := range liveJob.Status.Conditions {
		if condition.Type == batchv1.JobFailed && condition.Status == corev1.ConditionTrue {
			return ctrl.Result{}, r.status(ctx, t, tadossv1.PhaseError, "SchemaFailed", "Schema Job failed; fix the cause and change spec.schemaRetryNonce to retry", false)
		}
		if condition.Type == batchv1.JobComplete && condition.Status == corev1.ConditionTrue {
			complete = true
		}
	}
	if !complete {
		return r.pending(ctx, t, "SchemaPending", "Waiting for the schema Job")
	}
	setCondition(t, "SchemaMigrated", true, "Complete", "Database schema matches the selected release")
	set := workloads(t, database.Name, token.Name, referenceFingerprint(credentialReferences...))
	for _, object := range set.objects() {
		if err := r.apply(ctx, t, object); err != nil {
			return ctrl.Result{}, err
		}
	}
	if err := r.prune(ctx, t, job.Name); err != nil {
		return ctrl.Result{}, err
	}
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
	apiReady := deploymentReady(deployment, replicas(t))
	setCondition(t, "APIReady", apiReady, "Rollout", "API rollout must finish")
	workerReady := deploymentReady(set.Worker, *set.Worker.Spec.Replicas)
	t.Status.WorkerReadyReplicas = set.Worker.Status.ReadyReplicas
	setCondition(t, "WorkerReady", workerReady, "Rollout", "Worker rollout must finish")
	if !apiReady || !workerReady {
		return r.pending(ctx, t, "RolloutPending", "Waiting for API and worker rollouts")
	}
	t.Status.CurrentVersion = t.Spec.Version
	t.Status.AppliedDefaultsRevision = t.Status.Resolved.DefaultsRevision
	if err := r.status(ctx, t, tadossv1.PhaseReady, "Available", "Database schema and API are ready", true); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
}

func (r *TadossReconciler) token(ctx context.Context, t *tadossv1.Tadoss) (*corev1.Secret, error) {
	name := t.Spec.Auth.TokenSecret
	generated := name == ""
	if generated {
		name = t.Name + suffixToken
	}
	secret := &corev1.Secret{}
	err := r.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: name}, secret)
	if err == nil {
		if generated && !metav1.IsControlledBy(secret, t) {
			return nil, fmt.Errorf("token Secret %s is not owned by this instance", name)
		}
		if generated && len(secret.Data["token"]) == 0 {
			// An emptied generated Secret is refilled; a user-supplied one is left alone.
			before := secret.DeepCopy()
			value, err := randomToken()
			if err != nil {
				return nil, err
			}
			secret.Data = map[string][]byte{"token": value}
			return secret, r.Patch(ctx, secret, client.MergeFrom(before))
		}
		return secret, nil
	}
	if !apierrors.IsNotFound(err) || !generated {
		return nil, err
	}
	value, err := randomToken()
	if err != nil {
		return nil, err
	}
	secret = &corev1.Secret{ObjectMeta: metadata(t, suffixToken), Data: map[string][]byte{"token": value}}
	if err := controllerutil.SetControllerReference(t, secret, r.Scheme); err != nil {
		return nil, err
	}
	return secret, r.Create(ctx, secret)
}

func randomToken() ([]byte, error) {
	random := make([]byte, 32)
	if _, err := rand.Read(random); err != nil {
		return nil, err
	}
	return []byte(base64.RawURLEncoding.EncodeToString(random)), nil
}

func (r *TadossReconciler) apply(ctx context.Context, t *tadossv1.Tadoss, desired client.Object) error {
	empty, err := r.Scheme.New(desired.GetObjectKind().GroupVersionKind())
	if err != nil {
		return err
	}
	live := empty.(client.Object)
	if err := r.Get(ctx, client.ObjectKeyFromObject(desired), live); err == nil {
		if !metav1.IsControlledBy(live, t) {
			return fmt.Errorf("%s %s is not owned by this instance", desired.GetObjectKind().GroupVersionKind().Kind, desired.GetName())
		}
	} else if !apierrors.IsNotFound(err) {
		return err
	}
	if err := controllerutil.SetControllerReference(t, desired, r.Scheme); err != nil {
		return err
	}
	// The typed apply patch remains supported; apply configurations need an
	// unstructured round trip that would lose the status returned into desired.
	return r.Patch(ctx, desired, client.Apply, client.FieldOwner(fieldOwner), client.ForceOwnership) //nolint:staticcheck
}

func (r *TadossReconciler) prune(ctx context.Context, t *tadossv1.Tadoss, currentJob string) error {
	var stale []client.Object
	key := client.ObjectKey{Namespace: t.Namespace, Name: t.Name + suffixAPI}
	if t.Spec.Ingress.Host == "" {
		ingress, err := r.owned(ctx, t, key, &networkingv1.Ingress{})
		if err != nil {
			return err
		}
		stale = append(stale, ingress)
	}
	if replicas(t) <= 1 && t.Spec.API.MinAvailable == nil {
		budget, err := r.owned(ctx, t, key, &policyv1.PodDisruptionBudget{})
		if err != nil {
			return err
		}
		stale = append(stale, budget)
	}
	workerReplicas := replicas(t)
	if t.Spec.Worker.ReplicaCount != nil {
		workerReplicas = *t.Spec.Worker.ReplicaCount
	}
	if workerReplicas <= 1 && t.Spec.Worker.MinAvailable == nil {
		budget, err := r.owned(ctx, t, client.ObjectKey{Namespace: t.Namespace, Name: t.Name + "-worker"}, &policyv1.PodDisruptionBudget{})
		if err != nil {
			return err
		}
		stale = append(stale, budget)
	}
	if !ptr.Deref(t.Spec.NetworkPolicy.Enabled, t.Spec.Profile == tadossv1.ProfileMultiServer) {
		for _, component := range []string{"api", "worker"} {
			policy, err := r.owned(ctx, t, client.ObjectKey{Namespace: t.Namespace, Name: t.Name + "-" + component}, &networkingv1.NetworkPolicy{})
			if err != nil {
				return err
			}
			stale = append(stale, policy)
		}
	}
	if t.Spec.Backends.DB.ProvidedBy == tadossv1.ProviderManaged && !t.Spec.Backends.DB.CNPG.Backup.Enabled {
		backup, err := r.owned(ctx, t, client.ObjectKey{Namespace: t.Namespace, Name: t.Name + "-backup"}, &cnpgv1.ScheduledBackup{})
		if err != nil {
			return err
		}
		stale = append(stale, backup)
	}
	jobs := &batchv1.JobList{}
	if err := r.List(ctx, jobs, client.InNamespace(t.Namespace), client.MatchingLabels{instanceUID: string(t.UID)}); err != nil {
		return err
	}
	for i := range jobs.Items {
		if jobs.Items[i].Name != currentJob && metav1.IsControlledBy(&jobs.Items[i], t) {
			stale = append(stale, &jobs.Items[i])
		}
	}
	for _, object := range stale {
		if object == nil {
			continue
		}
		if err := r.Delete(ctx, object, client.PropagationPolicy(metav1.DeletePropagationBackground)); client.IgnoreNotFound(err) != nil {
			return err
		}
	}
	return nil
}

// owned reads an object into empty and returns it only when this instance controls it.
func (r *TadossReconciler) owned(ctx context.Context, t *tadossv1.Tadoss, key client.ObjectKey, empty client.Object) (client.Object, error) {
	if err := r.Get(ctx, key, empty); err != nil {
		return nil, client.IgnoreNotFound(err)
	}
	if !metav1.IsControlledBy(empty, t) {
		return nil, nil
	}
	return empty, nil
}

func (r *TadossReconciler) handleDeletion(ctx context.Context, t *tadossv1.Tadoss) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(t, finalizer) {
		return ctrl.Result{}, nil
	}
	terminating, err := r.namespaceTerminating(ctx, t.Namespace)
	if err != nil {
		return ctrl.Result{}, err
	}
	switch {
	case terminating:
		logf.FromContext(ctx).Info("namespace is terminating; releasing the finalizer regardless of deletionProtection and deletionPolicy")
	case ptr.Deref(t.Spec.DeletionProtection, true):
		if err := r.status(ctx, t, tadossv1.PhaseProgressing, "DeletionBlocked", "Set spec.deletionProtection=false to permit deletion", false); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{RequeueAfter: time.Minute}, nil
	case t.Spec.Backends.DB.ProvidedBy == tadossv1.ProviderManaged && t.Spec.DeletionPolicy == tadossv1.DeletionPolicyRetain:
		retained := []client.Object{
			&cnpgv1.Cluster{ObjectMeta: metadata(t, suffixDB)},
			&cnpgv1.ScheduledBackup{ObjectMeta: metadata(t, "-backup")},
		}
		for _, object := range retained {
			if err := r.Get(ctx, client.ObjectKeyFromObject(object), object); err != nil {
				if apierrors.IsNotFound(err) {
					continue
				}
				return ctrl.Result{}, err
			}
			before := object.DeepCopyObject().(client.Object)
			owners := slices.DeleteFunc(slices.Clone(object.GetOwnerReferences()), func(owner metav1.OwnerReference) bool { return owner.UID == t.UID })
			if len(owners) == len(object.GetOwnerReferences()) {
				continue
			}
			object.SetOwnerReferences(owners)
			if err := r.Patch(ctx, object, client.MergeFromWithOptions(before, client.MergeFromWithOptimisticLock{})); err != nil {
				return ctrl.Result{}, err
			}
		}
	}
	before := t.DeepCopy()
	controllerutil.RemoveFinalizer(t, finalizer)
	return ctrl.Result{}, r.Patch(ctx, t, client.MergeFromWithOptions(before, client.MergeFromWithOptimisticLock{}))
}

// namespaceTerminating reports whether the instance namespace carries a deletion timestamp.
func (r *TadossReconciler) namespaceTerminating(ctx context.Context, name string) (bool, error) {
	ns := &metav1.PartialObjectMetadata{}
	ns.SetGroupVersionKind(corev1.SchemeGroupVersion.WithKind("Namespace"))
	if err := r.Get(ctx, client.ObjectKey{Name: name}, ns); err != nil {
		return false, client.IgnoreNotFound(err)
	}
	return !ns.DeletionTimestamp.IsZero(), nil
}

func (r *TadossReconciler) pending(ctx context.Context, t *tadossv1.Tadoss, reason, message string) (ctrl.Result, error) {
	if err := r.status(ctx, t, tadossv1.PhaseProgressing, reason, message, false); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
}

func (r *TadossReconciler) status(ctx context.Context, t *tadossv1.Tadoss, phase, reason, message string, ready bool) error {
	live := &tadossv1.Tadoss{}
	if err := r.Get(ctx, client.ObjectKeyFromObject(t), live); err != nil {
		return err
	}
	before := live.DeepCopy()
	live.Status = t.Status
	live.Status.Phase = phase
	// observedGeneration advances only once a generation has been rolled out or has
	// failed, so waiting on it is meaningful.
	live.Status.ObservedGeneration = before.Status.ObservedGeneration
	if ready || phase == tadossv1.PhaseError {
		live.Status.ObservedGeneration = t.Generation
	}
	state := metav1.ConditionFalse
	if ready {
		state = metav1.ConditionTrue
	}
	meta.SetStatusCondition(&live.Status.Conditions, metav1.Condition{Type: tadossv1.ConditionReady, Status: state, Reason: reason, Message: message, ObservedGeneration: t.Generation})
	if reflect.DeepEqual(before.Status, live.Status) {
		return nil
	}
	if err := r.Status().Patch(ctx, live, client.MergeFrom(before)); err != nil {
		return err
	}
	old := meta.FindStatusCondition(before.Status.Conditions, tadossv1.ConditionReady)
	if old == nil || old.Reason != reason {
		logf.FromContext(ctx).Info("status changed", "phase", phase, "reason", reason, "message", message)
	}
	if r.Recorder != nil && (old == nil || old.Reason != reason) {
		eventType := corev1.EventTypeNormal
		if phase == tadossv1.PhaseError || reason == "DeletionBlocked" {
			eventType = corev1.EventTypeWarning
		}
		r.Recorder.Eventf(t, nil, eventType, reason, "Reconcile", "%s", message)
	}
	return nil
}

// SetupWithManager registers the controller, its owned resources and its watches.
func (r *TadossReconciler) SetupWithManager(mgr ctrl.Manager) error {
	terminating := builder.WithPredicates(predicate.NewPredicateFuncs(func(o client.Object) bool { return !o.GetDeletionTimestamp().IsZero() }))
	b := ctrl.NewControllerManagedBy(mgr).For(&tadossv1.Tadoss{}).
		Owns(&appsv1.Deployment{}).Owns(&batchv1.Job{}).Owns(&corev1.Service{}).
		Owns(&networkingv1.Ingress{}).Owns(&networkingv1.NetworkPolicy{}).Owns(&policyv1.PodDisruptionBudget{}).
		WatchesMetadata(&corev1.ConfigMap{}, handler.EnqueueRequestsFromMapFunc(r.secretRequests)).
		WatchesMetadata(&corev1.Secret{}, handler.EnqueueRequestsFromMapFunc(r.secretRequests)).
		WatchesMetadata(&corev1.Namespace{}, handler.EnqueueRequestsFromMapFunc(r.namespaceRequests), terminating)
	if _, err := mgr.GetRESTMapper().RESTMapping(cnpgv1.SchemeGroupVersion.WithKind("Cluster").GroupKind(), "v1"); err == nil {
		b = b.Owns(&cnpgv1.Cluster{}).Owns(&cnpgv1.ScheduledBackup{})
	}
	return b.Complete(r)
}

// namespaceRequests enqueues every instance in a namespace that started terminating.
func (r *TadossReconciler) namespaceRequests(ctx context.Context, object client.Object) []reconcile.Request {
	instances := &tadossv1.TadossList{}
	if err := r.List(ctx, instances, client.InNamespace(object.GetName())); err != nil {
		return nil
	}
	requests := make([]reconcile.Request, 0, len(instances.Items))
	for i := range instances.Items {
		requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&instances.Items[i])})
	}
	return requests
}

func (r *TadossReconciler) secretRequests(ctx context.Context, object client.Object) []reconcile.Request {
	instances := &tadossv1.TadossList{}
	if err := r.List(ctx, instances, client.InNamespace(object.GetNamespace())); err != nil {
		return nil
	}
	requests := []reconcile.Request{}
	for _, t := range instances.Items {
		names := append(referencedSecrets(&t), t.Name+suffixToken, t.Name+suffixDBApp, t.Name+suffixDBCA)
		if ca := t.Spec.Backends.DB.External.CAConfigMap; ca != nil {
			names = append(names, ca.Name)
		}
		if slices.Contains(names, object.GetName()) {
			requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&t)})
		}

	}
	return requests
}

func deploymentReady(deployment *appsv1.Deployment, desired int32) bool {
	status := deployment.Status
	return status.ObservedGeneration >= deployment.Generation && status.UpdatedReplicas == desired && status.AvailableReplicas == desired && status.Replicas == desired
}

func setCondition(t *tadossv1.Tadoss, name string, ready bool, reason, message string) {
	status := metav1.ConditionFalse
	if ready {
		status = metav1.ConditionTrue
	}
	meta.SetStatusCondition(&t.Status.Conditions, metav1.Condition{Type: name, Status: status, Reason: reason, Message: message, ObservedGeneration: t.Generation})
}

func referencedSecrets(t *tadossv1.Tadoss) []string {
	names := []string{t.Spec.Auth.TokenSecret, t.Spec.Auth.BasicSecret, t.Spec.Backends.DB.External.ExistingSecret}
	for _, workload := range []tadossv1.WorkloadSpec{t.Spec.API, t.Spec.Worker} {
		for _, source := range workload.EnvFrom {
			if source.SecretRef != nil && !ptr.Deref(source.SecretRef.Optional, false) {
				names = append(names, source.SecretRef.Name)
			}
		}
		for _, volume := range workload.Volumes {
			if volume.Secret != nil && !ptr.Deref(volume.Secret.Optional, false) {
				names = append(names, volume.Secret.SecretName)
			}
		}
	}
	slices.Sort(names)
	return slices.Compact(names)
}

// referenceFingerprint tracks API object revisions without hashing credential bytes.
func referenceFingerprint(objects ...client.Object) string {
	entries := make([]string, 0, len(objects))
	for _, object := range objects {
		kind := "Secret"
		if _, ok := object.(*corev1.ConfigMap); ok {
			kind = "ConfigMap"
		}
		encoded, _ := json.Marshal([]string{kind, object.GetNamespace(), object.GetName(), string(object.GetUID()), object.GetResourceVersion()})
		entries = append(entries, string(encoded))
	}
	slices.Sort(entries)
	encoded, _ := json.Marshal(slices.Compact(entries))
	return fmt.Sprintf("%x", sha256.Sum256(encoded))
}
