package controller

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"slices"

	operatorstatus "github.com/livewyer-ops/tadoss/operator/internal/status"

	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

type credentials struct {
	database           *corev1.Secret
	token              *corev1.Secret
	databaseReferences []client.Object
	workloadReferences []client.Object
}

// reconcileCredentials returns no credentials until all required references are valid.
func (r *TadossReconciler) reconcileCredentials(ctx context.Context, t *tadossv1.Tadoss, database *corev1.Secret) (*credentials, ctrl.Result, error) {
	token, err := r.token(ctx, t)
	if err != nil {
		if apierrors.IsNotFound(err) {
			result, err := r.pending(ctx, t, "TokenSecretMissing", "Waiting for the token Secret")
			return nil, result, err
		}
		return nil, ctrl.Result{}, err
	}
	if len(token.Data["token"]) == 0 {
		result, err := r.pending(ctx, t, "TokenSecretInvalid", "Token Secret must contain token")
		return nil, result, err
	}
	t.Status.DatabaseSecret, t.Status.TokenSecret = database.Name, token.Name
	databaseReferences := []client.Object{database}
	credentialReferences := []client.Object{database, token}
	for name, required := range referencedSecrets(t) {
		if name == "" || name == database.Name || name == token.Name {
			continue
		}
		secret := &corev1.Secret{}
		if err := r.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: name}, secret); err != nil {
			if !required && apierrors.IsNotFound(err) {
				continue
			}
			result, err := r.pending(ctx, t, "ReferencedSecretMissing", "A referenced Secret is unavailable")
			return nil, result, err
		}
		if name == t.Spec.Auth.BasicSecret && (len(secret.Data["username"]) == 0 || len(secret.Data["password"]) == 0) {
			result, err := r.pending(ctx, t, "BasicSecretInvalid", "Basic Secret requires username and password")
			return nil, result, err
		}
		credentialReferences = append(credentialReferences, secret)
	}
	if t.Spec.Backends.DB.ProvidedBy == tadossv1.ProviderManaged {
		ca := &corev1.Secret{}
		if err := r.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: t.Name + suffixDBCA}, ca); err != nil {
			operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "DatabaseReady", false, "DatabaseCAMissing", "Waiting for the managed database CA Secret")
			result, err := r.pending(ctx, t, "DatabaseCAMissing", "Waiting for the managed database CA Secret")
			return nil, result, err
		}
		if len(ca.Data["ca.crt"]) == 0 {
			operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "DatabaseReady", false, "DatabaseCAInvalid", "Database CA Secret must contain ca.crt")
			result, err := r.pending(ctx, t, "DatabaseCAInvalid", "Database CA Secret must contain ca.crt")
			return nil, result, err
		}
		databaseReferences = append(databaseReferences, ca)
		credentialReferences = append(credentialReferences, ca)
	} else if ca := t.Spec.Backends.DB.External.CAConfigMap; ca != nil {
		config := &corev1.ConfigMap{}
		if err := r.Get(ctx, client.ObjectKey{Namespace: t.Namespace, Name: ca.Name}, config); err != nil {
			operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "DatabaseReady", false, "DatabaseCAMissing", "Database CA ConfigMap is unavailable")
			result, err := r.pending(ctx, t, "DatabaseCAMissing", "Database CA ConfigMap is unavailable")
			return nil, result, err
		}
		if config.Data[ca.Key] == "" {
			operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "DatabaseReady", false, "DatabaseCAInvalid", "Database CA key is empty")
			result, err := r.pending(ctx, t, "DatabaseCAInvalid", "Database CA key is empty")
			return nil, result, err
		}
		databaseReferences = append(databaseReferences, config)
		credentialReferences = append(credentialReferences, config)
	}
	return &credentials{
		database:           database,
		token:              token,
		databaseReferences: databaseReferences,
		workloadReferences: credentialReferences,
	}, ctrl.Result{}, nil
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

// referencedSecrets records whether each Secret is required by any reference.
func referencedSecrets(t *tadossv1.Tadoss) map[string]bool {
	required := map[string]bool{t.Spec.Auth.TokenSecret: true, t.Spec.Auth.BasicSecret: true, t.Spec.Backends.DB.External.ExistingSecret: true}
	for _, workload := range []tadossv1.WorkloadSpec{t.Spec.API, t.Spec.Worker} {
		for _, source := range workload.EnvFrom {
			if source.SecretRef != nil {
				name := source.SecretRef.Name
				required[name] = required[name] || !ptr.Deref(source.SecretRef.Optional, false)
			}
		}
		for _, volume := range workload.Volumes {
			if volume.Secret != nil {
				name := volume.Secret.SecretName
				required[name] = required[name] || !ptr.Deref(volume.Secret.Optional, false)
			}
		}
	}
	delete(required, "")
	return required
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
