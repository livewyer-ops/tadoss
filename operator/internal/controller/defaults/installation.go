// Package defaults resolves installation and profile settings without changing the CR.
package defaults

import (
	"crypto/sha256"
	"fmt"
	"net/url"
	"os"
	"slices"
	"strings"

	"k8s.io/apimachinery/pkg/util/validation"

	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	"github.com/livewyer-ops/tadoss/operator/internal/releases"
	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/yaml"
)

// Installation contains defaults shared by instances managed by this operator.
type Installation struct {
	Profile          string `json:"profile"`
	BaseDomain       string `json:"baseDomain"`
	IngressClassName string `json:"ingressClassName"`
	ClusterIssuer    string `json:"clusterIssuer"`
	Revision         string `json:"-"`
	Error            string `json:"-"`
}

// Load validates a defaults file and records its content revision.
func Load(path string) Installation {
	if path == "" {
		return Installation{}
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return Installation{Error: "cannot read installation defaults"}
	}
	var result Installation
	if err = yaml.UnmarshalStrict(data, &result); err != nil {
		return Installation{Error: "invalid installation defaults YAML"}
	}
	result.Revision = fmt.Sprintf("%x", sha256.Sum256(data))
	if result.Profile != "" && !validProfile(result.Profile) {
		result.Error = "invalid installation profile"
	}
	if result.BaseDomain != "" && len(validation.IsDNS1123Subdomain(result.BaseDomain)) > 0 {
		result.Error = "invalid installation baseDomain"
	}
	return result
}

func validProfile(value string) bool {
	switch value {
	case "local-kind", "edge", "single-server", "multi-server":
		return true
	}
	return false
}

// Resolve fills a working copy without writing defaults back to the CR.
func Resolve(t *tadossv1.Tadoss, installation Installation) error {
	if installation.Error != "" {
		return fmt.Errorf("InvalidInstallationDefaults: %s", installation.Error)
	}
	release, err := releases.Resolve(t.Spec.Version, t.Status.CurrentVersion, t.Status.PendingVersion)
	if err != nil {
		return err
	}
	if t.Spec.Profile == "" {
		t.Spec.Profile = installation.Profile
	}
	if !validProfile(t.Spec.Profile) {
		return fmt.Errorf("InstallationDefaultsRequired: configure a profile on the instance or installation")
	}
	if t.Spec.PublicEndpoint.BaseDomain == "" {
		t.Spec.PublicEndpoint.BaseDomain = installation.BaseDomain
	}
	if t.Spec.Ingress.ClassName == "" {
		t.Spec.Ingress.ClassName = installation.IngressClassName
	}
	if class := t.Spec.Ingress.ClassName; class != "" && len(validation.IsDNS1123Subdomain(class)) > 0 {
		return fmt.Errorf("invalid ingress className")
	}
	oauth := t.Spec.Auth.OAuth2
	if t.Spec.Auth.TokenEnabled != nil && !*t.Spec.Auth.TokenEnabled && oauth.Issuer == "" && t.Spec.Auth.BasicSecret == "" {
		return fmt.Errorf("disabling query tokens requires Basic or OAuth2 authentication")
	}
	if oauth.Issuer != "" || oauth.Audience != "" || oauth.JWKSURI != "" {
		if oauth.Issuer == "" || oauth.Audience == "" || oauth.JWKSURI == "" {
			return fmt.Errorf("OAuth2 requires issuer, audience and jwksURI")
		}
		for _, value := range []string{oauth.Issuer, oauth.JWKSURI} {
			parsed, err := url.Parse(value)
			if err != nil || parsed.Scheme != "https" || parsed.Hostname() == "" || parsed.User != nil || parsed.Fragment != "" {
				return fmt.Errorf("OAuth2 issuer and jwksURI must be HTTPS URLs with a host and no credentials or fragment")
			}
		}
	}
	for _, algorithm := range oauth.Algorithms {
		if !slices.Contains([]string{"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512", "EdDSA"}, algorithm) {
			return fmt.Errorf("OAuth2 algorithms must be supported asymmetric signing algorithms")
		}
	}
	if t.Spec.Ingress.ClusterIssuer == "" {
		t.Spec.Ingress.ClusterIssuer = installation.ClusterIssuer
	}
	if base := t.Spec.PublicEndpoint.BaseDomain; base != "" && len(validation.IsDNS1123Subdomain(base)) > 0 {
		return fmt.Errorf("invalid baseDomain")
	}
	if base := t.Spec.PublicEndpoint.BaseDomain; base != "" && t.Spec.Ingress.Host == "" {
		t.Spec.Ingress.Host = "api." + t.Name + "." + t.Namespace + "." + base
	}
	if t.Spec.Ingress.ClusterIssuer != "" && t.Spec.Ingress.TLSSecretName == "" {
		t.Spec.Ingress.TLSSecretName = t.Name + "-api-tls"
	}
	if t.Spec.API.Image == "" {
		t.Spec.API.Image = release.APIImage
	}
	if t.Spec.Worker.Image == "" {
		t.Spec.Worker.Image = t.Spec.API.Image
	}
	if t.Spec.API.ImagePullPolicy == "" {
		t.Spec.API.ImagePullPolicy = corev1.PullIfNotPresent
	}
	if t.Spec.Worker.ImagePullPolicy == "" {
		t.Spec.Worker.ImagePullPolicy = corev1.PullIfNotPresent
	}
	if t.Spec.Backends.DB.ProvidedBy == "" {
		t.Spec.Backends.DB.ProvidedBy = tadossv1.ProviderManaged
	}
	if t.Spec.Backends.DB.CNPG.Storage == "" {
		t.Spec.Backends.DB.CNPG.Storage = "5Gi"
	}
	if t.Spec.Backends.DB.CNPG.Image == "" {
		t.Spec.Backends.DB.CNPG.Image = tadossv1.DefaultPostgresImage
		if t.Spec.Backends.DB.CNPG.Backup.Enabled || t.Spec.Backends.DB.CNPG.Restore.Enabled {
			t.Spec.Backends.DB.CNPG.Image = strings.Replace(tadossv1.DefaultPostgresImage, "-standard-", "-system-", 1)
		}
	}
	if t.Spec.DeletionPolicy == "" {
		t.Spec.DeletionPolicy = tadossv1.DeletionPolicyRetain
	}
	for _, workload := range []tadossv1.WorkloadSpec{t.Spec.API, t.Spec.Worker} {
		if strings.Contains(workload.Image, ":sha256:") {
			return fmt.Errorf("image digests must use repository@sha256:digest")
		}
		for key, request := range workload.Resources.Requests {
			if limit, ok := workload.Resources.Limits[key]; ok && request.Cmp(limit) > 0 {
				return fmt.Errorf("resource request exceeds explicit limit for %s", key)
			}
		}
	}
	for key, request := range t.Spec.Backends.DB.CNPG.Resources.Requests {
		if limit, ok := t.Spec.Backends.DB.CNPG.Resources.Limits[key]; ok && request.Cmp(limit) > 0 {
			return fmt.Errorf("database resource request exceeds explicit limit for %s", key)
		}
	}
	backup := t.Spec.Backends.DB.CNPG.Backup
	if backup.Enabled && (backup.ObjectStore == nil || len(strings.Fields(backup.Schedule)) != 6 || backup.RetentionPolicy == "") {
		return fmt.Errorf("backup requires objectStore, a six-field schedule and retentionPolicy")
	}
	restore := t.Spec.Backends.DB.CNPG.Restore
	if restore.Enabled && (restore.ObjectStore == nil || restore.Source == "") {
		return fmt.Errorf("restore requires source and objectStore")
	}
	if t.Spec.Backends.DB.ProvidedBy == tadossv1.ProviderExternal && t.Spec.Backends.DB.External.ExistingSecret == "" {
		return fmt.Errorf("external database requires existingSecret")
	}
	t.Status.Resolved = tadossv1.ResolvedStatus{Profile: t.Spec.Profile, APIImage: t.Spec.API.Image, WorkerImage: t.Spec.Worker.Image, SchemaTarget: release.SchemaTarget, DefaultsRevision: installation.Revision}
	return nil
}
