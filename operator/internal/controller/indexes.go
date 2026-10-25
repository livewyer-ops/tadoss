package controller

import (
	"maps"
	"slices"

	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const tadossReferenceIndex = ".spec.references"

func tadossReferenceIndexValue(obj client.Object) []string {
	t, ok := obj.(*tadossv1.Tadoss)
	if !ok {
		return nil
	}
	names := append(slices.Collect(maps.Keys(referencedSecrets(t))), t.Name+suffixToken, t.Name+suffixDBApp, t.Name+suffixDBCA)
	if ca := t.Spec.Backends.DB.External.CAConfigMap; ca != nil {
		names = append(names, ca.Name)
	}
	names = slices.DeleteFunc(names, func(name string) bool { return name == "" })
	slices.Sort(names)
	return slices.Compact(names)
}
