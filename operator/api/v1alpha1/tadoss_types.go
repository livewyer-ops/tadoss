package v1alpha1

import (
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	ProviderManaged  = "managed"
	ProviderExternal = "external"

	DeletionPolicyRetain = "Retain"
	DeletionPolicyDelete = "Delete"

	ProfileLocalKind    = "local-kind"
	ProfileEdge         = "edge"
	ProfileSingleServer = "single-server"
	ProfileMultiServer  = "multi-server"

	PhaseProgressing = "Progressing"
	PhaseReady       = "Ready"
	PhaseError       = "Error"

	ConditionReady = "Ready"

	// DefaultPostgresImage is the CloudNativePG operand used for managed databases.
	DefaultPostgresImage = "ghcr.io/cloudnative-pg/postgresql:18.4-standard-trixie"
)

// +kubebuilder:validation:XValidation:rule="!has(self.provider) || self.provider != 'external' || (has(self.existingSecret) && size(self.existingSecret) > 0)",message="external PostgreSQL requires database.existingSecret"
// +kubebuilder:validation:XValidation:rule="!has(self.provider) || self.provider != 'managed' || !has(self.existingSecret) || size(self.existingSecret) == 0",message="database.existingSecret applies only to provider external"
type DatabaseSpec struct {
	// +kubebuilder:validation:Enum=managed;external
	// +kubebuilder:default=managed
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="database provider is immutable"
	Provider string `json:"provider,omitempty"`
	// ExistingSecret contains a PostgreSQL URI in its uri key.
	// +kubebuilder:validation:MaxLength=253
	// +kubebuilder:validation:Pattern=`^$|^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$`
	// +optional
	ExistingSecret string `json:"existingSecret,omitempty"`
	// Image is the PostgreSQL operand for managed databases.
	// +kubebuilder:default="ghcr.io/cloudnative-pg/postgresql:18.4-standard-trixie"
	// +kubebuilder:validation:MinLength=1
	Image string `json:"image,omitempty"`
	// +kubebuilder:default="5Gi"
	// +kubebuilder:validation:Pattern=`^[1-9][0-9]*(Gi|Ti)$`
	Storage string `json:"storage,omitempty"`
	// +optional
	StorageClass *string `json:"storageClass,omitempty"`
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=5
	// +optional
	Instances *int32 `json:"instances,omitempty"`
	// Resources of each managed PostgreSQL instance; defaults apply when unset.
	// +optional
	Resources corev1.ResourceRequirements `json:"resources,omitempty"`
}

// +kubebuilder:validation:XValidation:rule="!has(self.clusterIssuer) || size(self.clusterIssuer) == 0 || (has(self.tlsSecretName) && size(self.tlsSecretName) > 0)",message="ingress.clusterIssuer requires ingress.tlsSecretName"
type IngressSpec struct {
	// +kubebuilder:validation:MaxLength=253
	// +kubebuilder:validation:Pattern=`^$|^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]*[a-z0-9])?$`
	// +optional
	Host string `json:"host,omitempty"`
	// +kubebuilder:default=traefik
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=253
	// +kubebuilder:validation:Pattern=`^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$`
	ClassName string `json:"className,omitempty"`
	// +kubebuilder:validation:MaxLength=253
	// +kubebuilder:validation:Pattern=`^$|^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$`
	// +optional
	TLSSecretName string `json:"tlsSecretName,omitempty"`
	// ClusterIssuer enables certificate creation through cert-manager's ingress shim.
	// +kubebuilder:validation:MaxLength=253
	// +kubebuilder:validation:Pattern=`^$|^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$`
	// +optional
	ClusterIssuer string `json:"clusterIssuer,omitempty"`
	// Annotations are added to the Ingress, for class-specific behaviour such as
	// redirects. The cert-manager issuer annotation always wins.
	// +kubebuilder:validation:MaxProperties=32
	// +optional
	Annotations map[string]string `json:"annotations,omitempty"`
}

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
	// Replicas of the API Deployment. The operator owns the Deployment's replica
	// count, so external autoscalers are reverted on every reconcile.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=20
	// +optional
	Replicas *int32 `json:"replicas,omitempty"`
	// +kubebuilder:default={provider: managed, storage: "5Gi"}
	Database DatabaseSpec `json:"database,omitempty"`
	// TokenSecret names an existing Secret with a token key; empty generates one.
	// +kubebuilder:validation:MaxLength=253
	// +kubebuilder:validation:Pattern=`^$|^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$`
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
	// +optional
	DeletionProtection *bool `json:"deletionProtection,omitempty"`
}

type TadossStatus struct {
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
	// +kubebuilder:validation:Enum=Progressing;Ready;Error
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
