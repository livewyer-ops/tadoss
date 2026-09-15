package controller

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"fmt"
	"reflect"
	"time"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
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

type TadossReconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	Recorder events.EventRecorder
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
// +kubebuilder:rbac:groups=postgresql.cnpg.io,resources=clusters,verbs=get;list;watch;create;patch

func (r *TadossReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	t := &tadossv1.Tadoss{}
	if err := r.Get(ctx, req.NamespacedName, t); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !t.DeletionTimestamp.IsZero() {
		return r.finalize(ctx, t)
	}
	if !controllerutil.ContainsFinalizer(t, finalizer) {
		before := t.DeepCopy()
		controllerutil.AddFinalizer(t, finalizer)
		return ctrl.Result{}, r.Patch(ctx, t, client.MergeFromWithOptions(before, client.MergeFromWithOptimisticLock{}))
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
	databaseSecret := t.Spec.Database.ExistingSecret
	if t.Spec.Database.Provider == tadossv1.ProviderManaged {
		cluster := databaseCluster(t)
		if err := r.apply(ctx, t, cluster); err != nil {
			return ctrl.Result{}, err
		}
		databaseSecret = t.Name + suffixDBApp
		if cluster.Status.ReadyInstances < databaseInstances(t) {
			return r.pending(ctx, t, "DatabasePending", "Waiting for PostgreSQL")
		}
	}
	database := &corev1.Secret{}
	if err := r.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: databaseSecret}, database); err != nil {
		if apierrors.IsNotFound(err) {
			return r.pending(ctx, t, "DatabaseSecretMissing", "Waiting for the database Secret")
		}
		return ctrl.Result{}, err
	}
	if len(database.Data["uri"]) == 0 {
		return r.pending(ctx, t, "DatabaseSecretInvalid", "Database Secret must contain uri")
	}
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
	databaseHash := fmt.Sprintf("%x", sha256.Sum256(database.Data["uri"]))
	checksum := fmt.Sprintf("%x", sha256.Sum256(append(append([]byte(databaseHash), 0), token.Data["token"]...)))
	job := schemaJob(t, database.Name, databaseHash)
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
			return ctrl.Result{}, r.status(ctx, t, tadossv1.PhaseError, "SchemaFailed", "Schema Job failed; fix the cause and delete the failed Job to retry", false)
		}
		if condition.Type == batchv1.JobComplete && condition.Status == corev1.ConditionTrue {
			complete = true
		}
	}
	if !complete {
		return r.pending(ctx, t, "SchemaPending", "Waiting for the schema Job")
	}
	set := workloads(t, database.Name, token.Name, checksum)
	if waiting, err := r.migrateDeployment(ctx, t, set.Deployment); err != nil {
		return ctrl.Result{}, err
	} else if waiting {
		return r.pending(ctx, t, "SelectorMigration", "Recreating the API Deployment with a UID-free selector")
	}
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
	ready := deployment.Status.ObservedGeneration >= deployment.Generation && deployment.Status.UpdatedReplicas == replicas(t) && deployment.Status.AvailableReplicas == replicas(t)
	if !ready {
		return r.pending(ctx, t, "RolloutPending", "Waiting for the API rollout")
	}
	if err := r.status(ctx, t, tadossv1.PhaseReady, "Available", "Database schema and API are ready", true); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
}

func (r *TadossReconciler) token(ctx context.Context, t *tadossv1.Tadoss) (*corev1.Secret, error) {
	name := t.Spec.TokenSecret
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

// migrateDeployment replaces a Deployment whose immutable selector still carries the
// instance UID. Orphaning the ReplicaSet keeps the pods running; the recreated
// Deployment adopts it because the pod template is unchanged.
func (r *TadossReconciler) migrateDeployment(ctx context.Context, t *tadossv1.Tadoss, desired *appsv1.Deployment) (bool, error) {
	live := &appsv1.Deployment{}
	if err := r.Get(ctx, client.ObjectKeyFromObject(desired), live); err != nil {
		return false, client.IgnoreNotFound(err)
	}
	if !metav1.IsControlledBy(live, t) {
		return false, nil
	}
	if !live.DeletionTimestamp.IsZero() {
		return true, nil
	}
	if live.Spec.Selector == nil {
		return false, nil
	}
	if _, legacy := live.Spec.Selector.MatchLabels[instanceUID]; !legacy {
		return false, nil
	}
	logf.FromContext(ctx).Info("recreating the API Deployment with a UID-free selector", "deployment", live.Name)
	if r.Recorder != nil {
		r.Recorder.Eventf(t, nil, corev1.EventTypeNormal, "SelectorMigration", "Reconcile", "Recreating Deployment %s with a UID-free selector; pods keep running", live.Name)
	}
	uid := live.UID
	return true, r.Delete(ctx, live, client.PropagationPolicy(metav1.DeletePropagationOrphan), client.Preconditions{UID: &uid})
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
	return r.Patch(ctx, desired, client.Apply, client.FieldOwner(fieldOwner), client.ForceOwnership)
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
	if replicas(t) == 1 {
		budget, err := r.owned(ctx, t, key, &policyv1.PodDisruptionBudget{})
		if err != nil {
			return err
		}
		stale = append(stale, budget)
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

func (r *TadossReconciler) finalize(ctx context.Context, t *tadossv1.Tadoss) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(t, finalizer) {
		return ctrl.Result{}, nil
	}
	terminating, err := r.namespaceTerminating(ctx, t.Namespace)
	if err != nil {
		return ctrl.Result{}, err
	}
	if terminating {
		logf.FromContext(ctx).Info("namespace is terminating; releasing the finalizer regardless of deletionProtection and deletionPolicy")
	} else if ptr.Deref(t.Spec.DeletionProtection, true) {
		if err := r.status(ctx, t, tadossv1.PhaseProgressing, "DeletionBlocked", "Set spec.deletionProtection=false to permit deletion", false); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{RequeueAfter: time.Minute}, nil
	} else if t.Spec.Database.Provider == tadossv1.ProviderManaged && t.Spec.DeletionPolicy == tadossv1.DeletionPolicyRetain {
		cluster := &cnpgv1.Cluster{}
		err := r.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: t.Name + suffixDB}, cluster)
		if err != nil && !apierrors.IsNotFound(err) {
			return ctrl.Result{}, err
		}
		if err == nil && metav1.IsControlledBy(cluster, t) {
			before := cluster.DeepCopy()
			owners := []metav1.OwnerReference{}
			for _, owner := range cluster.OwnerReferences {
				if owner.UID != t.UID {
					owners = append(owners, owner)
				}
			}
			cluster.OwnerReferences = owners
			if err := r.Patch(ctx, cluster, client.MergeFromWithOptions(before, client.MergeFromWithOptimisticLock{})); err != nil {
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

func (r *TadossReconciler) SetupWithManager(mgr ctrl.Manager) error {
	terminating := builder.WithPredicates(predicate.NewPredicateFuncs(func(o client.Object) bool { return !o.GetDeletionTimestamp().IsZero() }))
	b := ctrl.NewControllerManagedBy(mgr).For(&tadossv1.Tadoss{}).
		Owns(&appsv1.Deployment{}).Owns(&batchv1.Job{}).Owns(&corev1.Service{}).
		Owns(&networkingv1.Ingress{}).Owns(&policyv1.PodDisruptionBudget{}).
		WatchesMetadata(&corev1.Secret{}, handler.EnqueueRequestsFromMapFunc(r.secretRequests)).
		WatchesMetadata(&corev1.Namespace{}, handler.EnqueueRequestsFromMapFunc(r.namespaceRequests), terminating)
	if _, err := mgr.GetRESTMapper().RESTMapping(cnpgv1.SchemeGroupVersion.WithKind("Cluster").GroupKind(), "v1"); err == nil {
		b = b.Owns(&cnpgv1.Cluster{})
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
		if object.GetName() == t.Spec.TokenSecret || object.GetName() == t.Name+suffixToken || object.GetName() == t.Spec.Database.ExistingSecret || object.GetName() == t.Name+suffixDBApp {
			requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&t)})
		}
	}
	return requests
}
