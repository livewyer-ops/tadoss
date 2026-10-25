package defaults

import (
	"os"
	"path/filepath"
	"testing"

	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestResolvePrecedenceAndValidation(t *testing.T) {
	installation := Installation{Profile: "edge", BaseDomain: "example.com", IngressClassName: "traefik", Revision: "revision"}
	source := &tadossv1.Tadoss{ObjectMeta: metav1.ObjectMeta{Name: "data", Namespace: "team"}, Spec: tadossv1.TadossSpec{Version: "0.1.0"}}
	resolved := source.DeepCopy()
	if err := Resolve(resolved, installation); err != nil {
		t.Fatal(err)
	}
	if source.Spec.Profile != "" || resolved.Spec.Profile != "edge" || resolved.Spec.Ingress.Host != "api.data.team.example.com" || resolved.Status.Resolved.DefaultsRevision != "revision" {
		t.Fatal("defaults must resolve on the working copy")
	}
	resolved.Spec.Profile = "multi-server"
	resolved.Spec.API.Image = "example.com/api@sha256:abcdef"
	if err := Resolve(resolved, installation); err != nil || resolved.Spec.Profile != "multi-server" || resolved.Spec.API.Image != "example.com/api@sha256:abcdef" {
		t.Fatal("explicit values must win", err)
	}
	for _, version := range []string{"", "latest", "0.2.0"} {
		resolved.Spec.Version = version
		if Resolve(resolved, installation) == nil {
			t.Fatal("unsupported version accepted")
		}
	}
	resolved.Spec.Version = "0.1.0"
	resolved.Spec.API.Resources = corev1.ResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("1Gi")}, Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("64Mi")}}
	if Resolve(resolved, installation) == nil {
		t.Fatal("invalid explicit resources accepted")
	}
}

func TestInstallationRejectsUnknownSettings(t *testing.T) {
	path := filepath.Join(t.TempDir(), "defaults.yaml")
	for _, content := range []string{"profile: unknown\n", "baseDomain: bad..domain\n", "profile: edge\nunknown: value\n"} {
		if err := os.WriteFile(path, []byte(content), 0600); err != nil {
			t.Fatal(err)
		}
		if Load(path).Error == "" {
			t.Fatal("invalid installation accepted")
		}
	}
}

func TestResolveRejectsMalformedOAuthAndIngressClass(t *testing.T) {
	for _, oauth := range []tadossv1.OAuth2Spec{
		{Issuer: "https://issuer.example"},
		{Issuer: "https://", Audience: "tadoss", JWKSURI: "https://issuer.example/jwks"},
		{Issuer: "https://user:password@issuer.example", Audience: "tadoss", JWKSURI: "https://issuer.example/jwks"},
		{Issuer: "https://issuer.example", Audience: "tadoss", JWKSURI: "http://issuer.example/jwks"},
		{Algorithms: []string{"HS256"}},
	} {
		cr := &tadossv1.Tadoss{Spec: tadossv1.TadossSpec{Version: "0.1.0", Profile: "edge", Auth: tadossv1.AuthSpec{OAuth2: oauth}}}
		if Resolve(cr, Installation{}) == nil {
			t.Fatal("invalid OAuth accepted")
		}
	}
	cr := &tadossv1.Tadoss{Spec: tadossv1.TadossSpec{Version: "0.1.0", Profile: "edge"}}
	if Resolve(cr, Installation{IngressClassName: "INVALID_CLASS"}) == nil {
		t.Fatal("invalid installation ingress class accepted")
	}
	cr.Spec.Ingress.ClassName = "traefik"
	cr.Spec.Auth.OAuth2 = tadossv1.OAuth2Spec{Issuer: "https://issuer.example", Audience: "tadoss", JWKSURI: "https://issuer.example/jwks", Algorithms: []string{"RS256", "EdDSA"}}
	if err := Resolve(cr, Installation{}); err != nil {
		t.Fatal(err)
	}
}
