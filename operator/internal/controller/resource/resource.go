// Package resource defines the metadata shared by managed resources.
package resource

// Standard labels and field ownership identify resources managed by this operator.
const (
	FieldOwner     = "tadoss-operator"
	AppName        = "tadoss"
	ManagedBy      = "tadoss-operator"
	LabelName      = "app.kubernetes.io/name"
	LabelInstance  = "app.kubernetes.io/instance"
	LabelComponent = "app.kubernetes.io/component"
	LabelManagedBy = "app.kubernetes.io/managed-by"
)

// Labels returns the standard application, instance and ownership labels.
func Labels(appName, instance, component string) map[string]string {
	labels := map[string]string{
		LabelName:      appName,
		LabelInstance:  instance,
		LabelManagedBy: ManagedBy,
	}
	if component != "" {
		labels[LabelComponent] = component
	}
	return labels
}
