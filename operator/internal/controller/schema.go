package controller

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"slices"

	operatorstatus "github.com/livewyer-ops/tadoss/operator/internal/status"

	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

// reconcileSchema permits workloads only after the selected Job completes.
func (r *TadossReconciler) reconcileSchema(ctx context.Context, t *tadossv1.Tadoss, job *batchv1.Job) (bool, ctrl.Result, error) {
	if t.Status.SchemaJob != job.Name {
		operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "SchemaMigrated", false, "SchemaPending", "Waiting for selected schema Job")
	}
	t.Status.SchemaJob = job.Name
	liveJob := &batchv1.Job{}
	err := r.Get(ctx, client.ObjectKeyFromObject(job), liveJob)
	if apierrors.IsNotFound(err) {
		if err = controllerutil.SetControllerReference(t, job, r.Scheme); err != nil {
			return false, ctrl.Result{}, err
		}
		if err = r.Create(ctx, job); err != nil {
			return false, ctrl.Result{}, err
		}
		result, err := r.pending(ctx, t, "SchemaPending", "Initialising the database schema")
		return false, result, err
	}
	if err != nil {
		return false, ctrl.Result{}, err
	}
	if !metav1.IsControlledBy(liveJob, t) {
		return false, ctrl.Result{}, fmt.Errorf("schema Job %s is not owned by this instance", job.Name)
	}
	complete := false
	for _, condition := range liveJob.Status.Conditions {
		if condition.Type == batchv1.JobFailed && condition.Status == corev1.ConditionTrue {
			return false, ctrl.Result{}, r.status(ctx, t, tadossv1.PhaseError, "SchemaFailed", "Schema Job failed; fix the cause and change spec.schemaRetryNonce to retry", false)
		}
		if condition.Type == batchv1.JobComplete && condition.Status == corev1.ConditionTrue {
			complete = true
		}
	}
	if !complete {
		result, err := r.pending(ctx, t, "SchemaPending", "Waiting for the schema Job")
		return false, result, err
	}
	operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "SchemaMigrated", true, "Complete", "Database schema matches the selected release")
	return true, ctrl.Result{}, nil
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
