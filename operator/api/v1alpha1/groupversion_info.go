// Package v1alpha1 defines the Tadoss API: one resource that provisions a TADOSS
// instance with its database, schema migration, API deployment and ingress.
// +kubebuilder:object:generate=true
// +groupName=tadoss.livewyer.io
package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
)

var (
	// GroupVersion identifies the API served by this package.
	GroupVersion = schema.GroupVersion{Group: "tadoss.livewyer.io", Version: "v1alpha1"}
	// SchemeBuilder registers the package types with a runtime scheme.
	SchemeBuilder = runtime.NewSchemeBuilder(addKnownTypes)
	// AddToScheme registers the package types with the given scheme.
	AddToScheme = SchemeBuilder.AddToScheme
)

func addKnownTypes(s *runtime.Scheme) error {
	s.AddKnownTypes(GroupVersion, &Tadoss{}, &TadossList{})
	metav1.AddToGroupVersion(s, GroupVersion)
	return nil
}
