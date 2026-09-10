package v1alpha1

import (
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type DatabaseSpec struct {
	// +kubebuilder:validation:Enum=managed;external
	// +kubebuilder:default=managed
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="database provider is immutable"
	Provider string `json:"provider,omitempty"`
	// ExistingSecret contains a PostgreSQL URI in its uri key.
	// +kubebuilder:validation:MaxLength=253
	// +optional
	ExistingSecret string `json:"existingSecret,omitempty"`
	// +kubebuilder:default="5Gi"
	// +kubebuilder:validation:Pattern=`^[1-9][0-9]*(Gi|Ti)$`
	Storage string `json:"storage,omitempty"`
	// +optional
	StorageClass *string `json:"storageClass,omitempty"`
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=5
	// +optional
	Instances *int32 `json:"instances,omitempty"`
}

type IngressSpec struct {
	// +kubebuilder:validation:MaxLength=253
	// +kubebuilder:validation:Pattern=`^$|^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]*[a-z0-9])?$`
	// +optional
	Host string `json:"host,omitempty"`
	// +kubebuilder:default=traefik
	ClassName string `json:"className,omitempty"`
	// +optional
	TLSSecretName string `json:"tlsSecretName,omitempty"`
	// ClusterIssuer enables certificate creation through cert-manager's ingress shim.
	// +optional
	ClusterIssuer string `json:"clusterIssuer,omitempty"`
}

// +kubebuilder:validation:XValidation:rule="!has(self.database) || !has(self.database.provider) || self.database.provider != 'external' || (has(self.database.existingSecret) && size(self.database.existingSecret) > 0)",message="external PostgreSQL requires database.existingSecret"
type TadossSpec struct {
	// +kubebuilder:validation:Enum=local-kind;edge;single-server;multi-server
	// +kubebuilder:default=local-kind
	Profile string `json:"profile,omitempty"`
	// Image is the API image used by both the schema Job and API Deployment.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:default="livewyer/tadoss-api:0.1.0"
	Image string `json:"image,omitempty"`
	// +kubebuilder:validation:Enum=Always;IfNotPresent;Never
	// +kubebuilder:default=IfNotPresent
	ImagePullPolicy corev1.PullPolicy `json:"imagePullPolicy,omitempty"`
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=20
	// +optional
	Replicas *int32 `json:"replicas,omitempty"`
	// +kubebuilder:default={provider: managed, storage: "5Gi"}
	Database DatabaseSpec `json:"database,omitempty"`
	// TokenSecret names an existing Secret with a token key; empty generates one.
	// +optional
	TokenSecret string `json:"tokenSecret,omitempty"`
	// +optional
	Ingress IngressSpec `json:"ingress,omitempty"`
	// +optional
	Resources corev1.ResourceRequirements `json:"resources,omitempty"`
	// +kubebuilder:validation:Enum=Retain;Delete
	// +kubebuilder:default=Retain
	DeletionPolicy string `json:"deletionPolicy,omitempty"`
	// DeletionProtection holds the finalizer until explicitly disabled.
	// +kubebuilder:default=true
	DeletionProtection bool `json:"deletionProtection,omitempty"`
}

type TadossStatus struct {
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
	// +optional
	Phase string `json:"phase,omitempty"`
	// +optional
	Endpoint string `json:"endpoint,omitempty"`
	// +optional
	TokenSecret string `json:"tokenSecret,omitempty"`
	// +optional
	DatabaseSecret string `json:"databaseSecret,omitempty"`
	// +optional
	SchemaJob string `json:"schemaJob,omitempty"`
	// +optional
	ReadyReplicas int32 `json:"readyReplicas,omitempty"`
	// +listType=map
	// +listMapKey=type
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:validation:XValidation:rule="size(self.metadata.name) <= 40",message="instance name must be at most 40 characters"
// +kubebuilder:resource:shortName=tads
// +kubebuilder:printcolumn:name="Profile",type=string,JSONPath=`.spec.profile`
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Ready",type=integer,JSONPath=`.status.readyReplicas`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`
type Tadoss struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	// +kubebuilder:default={}
	Spec   TadossSpec   `json:"spec,omitempty"`
	Status TadossStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type TadossList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Tadoss `json:"items"`
}

func init() { SchemeBuilder.Register(&Tadoss{}, &TadossList{}) }
