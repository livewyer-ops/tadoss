package controller

import (
	"fmt"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	monitoringv1 "github.com/prometheus-operator/prometheus-operator/pkg/apis/monitoring/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/apiutil"
)

var canonicalKindScheme = newCanonicalKindScheme()

func newCanonicalKindScheme() *runtime.Scheme {
	kindScheme := runtime.NewScheme()
	utilruntime.Must(clientgoscheme.AddToScheme(kindScheme))
	utilruntime.Must(tadossv1.AddToScheme(kindScheme))
	utilruntime.Must(cnpgv1.AddToScheme(kindScheme))
	utilruntime.Must(monitoringv1.AddToScheme(kindScheme))
	return kindScheme
}

func canonicalObjectGVK(obj client.Object) schema.GroupVersionKind {
	gvk, err := apiutil.GVKForObject(obj, canonicalKindScheme)
	if err != nil {
		panic(fmt.Errorf("resolve GVK for %T: %w", obj, err))
	}
	return gvk
}
