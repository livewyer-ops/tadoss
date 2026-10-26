package v1alpha1

import (
	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
)

// Values of the enumerated spec and status fields.
const (
	ProviderManaged  = "cnpg"
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

// DatabaseSpec configures a CNPG-managed PostgreSQL cluster.
type DatabaseSpec struct {
	Image string `json:"image,omitempty"`
	// +kubebuilder:validation:Pattern=`^[1-9][0-9]*(Gi|Ti)$`
	Storage      string  `json:"storage,omitempty"`
	StorageClass *string `json:"storageClass,omitempty"`
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=5
	Instances *int32                      `json:"instances,omitempty"`
	Resources corev1.ResourceRequirements `json:"resources,omitempty"`
	Backup    BackupSpec                  `json:"backup,omitempty"`
	Restore   RestoreSpec                 `json:"restore,omitempty"`
}

// BackupSpec configures scheduled native CNPG object-store backups.
type BackupSpec struct {
	Enabled         bool                                   `json:"enabled,omitempty"`
	Schedule        string                                 `json:"schedule,omitempty"`
	RetentionPolicy string                                 `json:"retentionPolicy,omitempty"`
	ObjectStore     *cnpgv1.BarmanObjectStoreConfiguration `json:"objectStore,omitempty"`
}

// RestoreSpec bootstraps a new database from an object-store archive.
type RestoreSpec struct {
	Enabled     bool                                   `json:"enabled,omitempty"`
	Source      string                                 `json:"source,omitempty"`
	TargetTime  string                                 `json:"targetTime,omitempty"`
	ObjectStore *cnpgv1.BarmanObjectStoreConfiguration `json:"objectStore,omitempty"`
}

// ExternalDatabaseSpec references application credentials and a trusted database CA.
type ExternalDatabaseSpec struct {
	ExistingSecret string                       `json:"existingSecret,omitempty"`
	CAConfigMap    *corev1.ConfigMapKeySelector `json:"caConfigMap,omitempty"`
}

// DatabaseBackendSpec selects a managed or external database.
// +kubebuilder:validation:XValidation:rule="self.providedBy != 'external' || (has(self.external) && has(self.external.existingSecret) && size(self.external.existingSecret)>0)",message="external PostgreSQL requires existingSecret"
type DatabaseBackendSpec struct {
	// +kubebuilder:validation:Enum=cnpg;external
	// +kubebuilder:default=cnpg
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="database provider is immutable"
	ProvidedBy string               `json:"providedBy,omitempty"`
	CNPG       DatabaseSpec         `json:"cnpg,omitempty"`
	External   ExternalDatabaseSpec `json:"external,omitempty"`
}

// BackendsSpec selects the database provider for this instance.
type BackendsSpec struct {
	// +kubebuilder:default={providedBy:cnpg}
	DB DatabaseBackendSpec `json:"db,omitempty"`
}

// IngressSpec exposes the API through an Ingress; an empty host disables it.
type IngressSpec struct {
	// +kubebuilder:validation:MaxLength=253
	// +kubebuilder:validation:Pattern=`^$|^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]*[a-z0-9])?$`
	// +optional
	Host string `json:"host,omitempty"`
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

// WorkloadSpec exposes native Kubernetes workload controls.
type WorkloadSpec struct {
	Image string `json:"image,omitempty"`
	// +kubebuilder:validation:Enum=Always;IfNotPresent;Never
	ImagePullPolicy corev1.PullPolicy `json:"imagePullPolicy,omitempty"`
	// +kubebuilder:validation:Minimum=0
	ReplicaCount       *int32                      `json:"replicaCount,omitempty"`
	Env                map[string]string           `json:"env,omitempty"`
	EnvFrom            []corev1.EnvFromSource      `json:"envFrom,omitempty"`
	Resources          corev1.ResourceRequirements `json:"resources,omitempty"`
	PodLabels          map[string]string           `json:"podLabels,omitempty"`
	PodAnnotations     map[string]string           `json:"podAnnotations,omitempty"`
	NodeSelector       map[string]string           `json:"nodeSelector,omitempty"`
	Tolerations        []corev1.Toleration         `json:"tolerations,omitempty"`
	Affinity           *corev1.Affinity            `json:"affinity,omitempty"`
	PodSecurityContext *corev1.PodSecurityContext  `json:"podSecurityContext,omitempty"`
	SecurityContext    *corev1.SecurityContext     `json:"securityContext,omitempty"`
	Volumes            []corev1.Volume             `json:"volumes,omitempty"`
	VolumeMounts       []corev1.VolumeMount        `json:"volumeMounts,omitempty"`
	StartupProbe       *corev1.Probe               `json:"startupProbe,omitempty"`
	ReadinessProbe     *corev1.Probe               `json:"readinessProbe,omitempty"`
	LivenessProbe      *corev1.Probe               `json:"livenessProbe,omitempty"`
	MinAvailable       *intstr.IntOrString         `json:"minAvailable,omitempty"`
}

// OAuth2Spec configures JWT verification and operation scopes.
// +kubebuilder:validation:XValidation:rule="(has(self.issuer) && has(self.audience) && has(self.jwksURI)) || (!has(self.issuer) && !has(self.audience) && !has(self.jwksURI))",message="issuer, audience and jwksURI must be supplied together"
type OAuth2Spec struct {
	// ClientCredentialsSecret is read by access tooling, never mounted into API or worker pods.
	// +kubebuilder:validation:Pattern=`^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$`
	ClientCredentialsSecret string `json:"clientCredentialsSecret,omitempty"`
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:Pattern=`^https://`
	Issuer string `json:"issuer,omitempty"`
	// +kubebuilder:validation:MinLength=1
	Audience string `json:"audience,omitempty"`
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:Pattern=`^https://`
	JWKSURI string `json:"jwksURI,omitempty"`
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:items:Enum=RS256;RS384;RS512;ES256;ES384;ES512;PS256;PS384;PS512;EdDSA
	Algorithms  []string `json:"algorithms,omitempty"`
	ReadScope   string   `json:"readScope,omitempty"`
	WriteScope  string   `json:"writeScope,omitempty"`
	DeleteScope string   `json:"deleteScope,omitempty"`
	AdminScope  string   `json:"adminScope,omitempty"`
}

// AuthSpec references static credentials and an external JWT issuer.
// +kubebuilder:validation:XValidation:rule="!has(self.tokenEnabled) || self.tokenEnabled || has(self.basicSecret) || (has(self.oauth2) && has(self.oauth2.issuer))",message="disabling query tokens requires Basic or OAuth2 authentication"
type AuthSpec struct {
	// TokenEnabled defaults to true for compatibility with existing installations.
	TokenEnabled *bool `json:"tokenEnabled,omitempty"`
	// +kubebuilder:validation:Pattern=`^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$`
	TokenSecret string     `json:"tokenSecret,omitempty"`
	BasicSecret string     `json:"basicSecret,omitempty"`
	OAuth2      OAuth2Spec `json:"oauth2,omitempty"`
}

// PublicEndpointSpec supplies the domain used to derive the instance hostname.
type PublicEndpointSpec struct {
	BaseDomain string `json:"baseDomain,omitempty"`
}

// NetworkRules contains native Kubernetes ingress and egress rules.
type NetworkRules struct {
	Ingress []networkingv1.NetworkPolicyIngressRule `json:"ingress,omitempty"`
	Egress  []networkingv1.NetworkPolicyEgressRule  `json:"egress,omitempty"`
}

// NetworkPolicySpec controls network policies for API and worker pods.
type NetworkPolicySpec struct {
	Enabled *bool        `json:"enabled,omitempty"`
	API     NetworkRules `json:"api,omitempty"`
	Worker  NetworkRules `json:"worker,omitempty"`
}

// TadossSpec describes one release-pinned instance.
type TadossSpec struct {
	// +kubebuilder:validation:MinLength=1
	Version string `json:"version"`
	// +kubebuilder:validation:Enum=local-kind;edge;single-server;multi-server
	Profile        string             `json:"profile,omitempty"`
	Paused         bool               `json:"paused,omitempty"`
	PublicEndpoint PublicEndpointSpec `json:"publicEndpoint,omitempty"`
	API            WorkloadSpec       `json:"api,omitempty"`
	Worker         WorkloadSpec       `json:"worker,omitempty"`
	// +kubebuilder:default={db:{providedBy:cnpg}}
	Backends         BackendsSpec                  `json:"backends,omitempty"`
	Auth             AuthSpec                      `json:"auth,omitempty"`
	Ingress          IngressSpec                   `json:"ingress,omitempty"`
	NetworkPolicy    NetworkPolicySpec             `json:"networkPolicy,omitempty"`
	ImagePullSecrets []corev1.LocalObjectReference `json:"imagePullSecrets,omitempty"`
	SchemaRetryNonce string                        `json:"schemaRetryNonce,omitempty"`
	// +kubebuilder:validation:Enum=Retain;Delete
	// +kubebuilder:default=Retain
	DeletionPolicy string `json:"deletionPolicy,omitempty"`
	// +kubebuilder:default=true
	DeletionProtection *bool `json:"deletionProtection,omitempty"`
}

// ResolvedStatus records the effective release and installation defaults.
type ResolvedStatus struct {
	Profile          string `json:"profile,omitempty"`
	APIImage         string `json:"apiImage,omitempty"`
	WorkerImage      string `json:"workerImage,omitempty"`
	SchemaTarget     string `json:"schemaTarget,omitempty"`
	DefaultsRevision string `json:"defaultsRevision,omitempty"`
}

// TadossStatus reports the observed instance; it never contains credential values.
type TadossStatus struct {
	CurrentVersion string `json:"currentVersion,omitempty"`
	// PendingVersion is persisted before release changes start and cleared after readiness.
	PendingVersion          string         `json:"pendingVersion,omitempty"`
	AppliedDefaultsRevision string         `json:"appliedDefaultsRevision,omitempty"`
	Resolved                ResolvedStatus `json:"resolved,omitempty"`
	WorkerReadyReplicas     int32          `json:"workerReadyReplicas,omitempty"`
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

// Tadoss is a TADOSS instance: database, schema migration, API and ingress.
// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:validation:XValidation:rule="size(self.metadata.name) <= 40 && self.metadata.name.matches('^[a-z]([-a-z0-9]*[a-z0-9])?$')",message="instance name must be a DNS label starting with a letter, at most 40 characters"
// +kubebuilder:resource:shortName=tads
// +kubebuilder:printcolumn:name="Profile",type=string,JSONPath=`.spec.profile`
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Ready",type=integer,JSONPath=`.status.readyReplicas`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`
type Tadoss struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              TadossSpec   `json:"spec,omitempty"`
	Status            TadossStatus `json:"status,omitempty"`
}

// TadossList is a list of Tadoss instances.
// +kubebuilder:object:root=true
type TadossList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Tadoss `json:"items"`
}
