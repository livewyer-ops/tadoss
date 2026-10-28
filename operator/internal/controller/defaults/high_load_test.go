package defaults

import (
	"testing"

	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	"k8s.io/utils/ptr"
)

func TestHighLoadRejectsUnsafeBudgetsAndDurability(t *testing.T) {
	for _, change := range []func(*tadossv1.Tadoss){
		func(t *tadossv1.Tadoss) { t.Spec.Backends.DB.CNPG.Pooler.ServerConnections = ptr.To(int32(30)) },
		func(t *tadossv1.Tadoss) { t.Spec.Worker.ReplicaCount = ptr.To(int32(8)) },
		func(t *tadossv1.Tadoss) {
			t.Spec.Backends.DB.CNPG.Postgresql.Parameters = map[string]string{"synchronous_commit": "off"}
		},
		func(t *tadossv1.Tadoss) {
			t.Spec.Backends.DB.CNPG.Postgresql.Parameters = map[string]string{"fsync": "off"}
		},
		func(t *tadossv1.Tadoss) { t.Spec.Backends.DB.CNPG.Instances = ptr.To(int32(1)) },
		func(t *tadossv1.Tadoss) {
			t.Spec.API.Autoscaling.Enabled = true
			t.Spec.API.ReplicaCount = ptr.To(int32(2))
		},
	} {
		cr := &tadossv1.Tadoss{Spec: tadossv1.TadossSpec{Version: "0.1.0-rc1", Profile: tadossv1.ProfileMultiServer}}
		cr.Spec.Backends.DB.CNPG.Pooler.Enabled = true
		if err := Resolve(cr, Installation{}); err != nil {
			t.Fatal(err)
		}
		change(cr)
		if Resolve(cr, Installation{}) == nil {
			t.Fatal("unsafe configuration accepted")
		}
	}
}
