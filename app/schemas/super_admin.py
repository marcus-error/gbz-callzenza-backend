from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, EmailStr, Field


# ---------------------------------------------------------------------------
# Super Admin Auth Schemas
# ---------------------------------------------------------------------------
class SuperAdminLoginRequest(BaseModel):
    email: EmailStr
    password: str
    remember_me: bool = False


class SuperAdminUserOut(BaseModel):
    id: str
    email: str
    full_name: str
    role: str
    is_active: bool


class SuperAdminLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: SuperAdminUserOut


# ---------------------------------------------------------------------------
# Super Admin Dashboard Metrics Schemas
# ---------------------------------------------------------------------------
class SuperAdminDashboardMetrics(BaseModel):
    total_tenants: int
    super_admin_created_tenants: int
    self_registered_tenants: int
    active_tenants: int
    inactive_tenants: int
    total_voice_agents: int
    total_calls: int
    total_call_minutes: float
    total_storage_used_bytes: int
    total_storage_used_formatted: str
    active_subscriptions: int
    mrr_formatted: str
    mrr_cents: int
    server_health: Dict[str, Any]


# ---------------------------------------------------------------------------
# Tenant Management Schemas
# ---------------------------------------------------------------------------
class SuperAdminTenantItem(BaseModel):
    id: str
    name: str
    slug: str
    subdomain: str
    owner_name: str
    owner_email: str
    registration_source: str  # SUPER_ADMIN_CREATED, SELF_REGISTERED
    subscription_plan: str
    status: str
    is_active: bool
    created_at: str
    last_login: Optional[str] = None
    user_count: int
    wallet_balance: float
    cluster_id: str
    isolation_strategy: str


class SuperAdminTenantListResponse(BaseModel):
    tenants: List[SuperAdminTenantItem]
    total: int
    page: int
    limit: int
    total_pages: int


class SuperAdminTenantCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    slug: Optional[str] = None
    subdomain: Optional[str] = None
    custom_domain: Optional[str] = None
    email: Optional[EmailStr] = None
    password: Optional[str] = None
    industry: Optional[str] = "General Business"
    current_plan: Optional[str] = "Starter Free"
    cluster_id: Optional[str] = "default"
    server_url: Optional[str] = None
    isolation_strategy: Optional[str] = "SHARED_DATABASE"
    database_url: Optional[str] = None
    schema_name: Optional[str] = None
    permissions: Optional[Dict[str, bool]] = None


class SuperAdminTenantUpdateRequest(BaseModel):
    name: Optional[str] = None
    industry: Optional[str] = None
    subdomain: Optional[str] = None
    custom_domain: Optional[str] = None
    cluster_id: Optional[str] = None
    server_url: Optional[str] = None
    isolation_strategy: Optional[str] = None
    current_plan: Optional[str] = None
    status: Optional[str] = None
    is_active: Optional[bool] = None


class SuperAdminTenantStatusUpdateRequest(BaseModel):
    is_active: bool
    status: Optional[str] = None


# ---------------------------------------------------------------------------
# Tenant Deep Detail & Isolated Analytics
# ---------------------------------------------------------------------------
class SuperAdminTenantDetailResponse(BaseModel):
    tenant: Dict[str, Any]
    owner: Dict[str, Any]
    subscription: Dict[str, Any]
    usage: Dict[str, Any]
    limits: Dict[str, Any]
    infrastructure: Dict[str, Any]
    stats: Optional[Dict[str, Any]] = None
    configuration: Optional[Dict[str, Any]] = None


class SuperAdminTenantAnalyticsResponse(BaseModel):
    tenant_id: str
    tenant_name: str
    current_plan: str
    total_calls: int
    completed_calls: int
    failed_calls: int
    total_duration_minutes: float
    total_call_duration_minutes: Optional[float] = None
    total_leads: int
    qualified_leads: int
    qualification_rate: float
    total_campaigns: int
    active_campaigns: int
    total_agents: int
    active_agents: int
    time_range: Optional[str] = "30d"
    call_trends: List[Dict[str, Any]]
    call_volume_trends: Optional[List[Dict[str, Any]]] = None
    leads_by_source: List[Dict[str, Any]]


class SuperAdminTenantBillingResponse(BaseModel):
    tenant_id: str
    tenant_name: str
    current_plan: str
    subscription_status: str
    billing_interval: str
    billing_cycle: Optional[str] = None
    start_date: Optional[str]
    renewal_date: Optional[str]
    wallet_balance: float
    plan_limits: Dict[str, Any]
    actual_usage: Dict[str, Any]
    limits: Optional[Dict[str, Any]] = None
    usage: Optional[Dict[str, Any]] = None
    billing_profile: Dict[str, Any]
    invoices: List[Dict[str, Any]]
    available_plans: List[Dict[str, Any]]


# ---------------------------------------------------------------------------
# Server Status Schemas
# ---------------------------------------------------------------------------
class SuperAdminServerStatusResponse(BaseModel):
    backend_api_status: str  # Healthy, Warning, Critical
    api_status: Optional[str] = None
    database_status: str     # Connected, Error
    db_status: Optional[str] = None
    database_latency_ms: float
    server_uptime_seconds: float
    uptime_seconds: Optional[float] = None
    server_uptime_formatted: str
    uptime_formatted: Optional[str] = None
    cpu_usage_percent: float
    memory_usage_percent: float
    memory_used_mb: float
    memory_total_mb: float
    memory_used_gb: float
    memory_total_gb: float
    disk_usage_percent: float
    disk_used_gb: float
    disk_available_gb: float
    disk_free_gb: float
    disk_total_gb: float
    application_health: str  # Healthy, Warning, Critical
    last_health_check_time: str
    last_health_check: Optional[str] = None
    process_id: int
    threads_count: int
    python_version: str
    platform_system: str


# ---------------------------------------------------------------------------
# Storage Usage Schemas
# ---------------------------------------------------------------------------
class SuperAdminStorageCategoryItem(BaseModel):
    category_name: str
    bytes: int
    formatted: str
    file_count: int


class SuperAdminTenantStorageItem(BaseModel):
    tenant_id: str
    tenant_name: str
    storage_used_bytes: int
    storage_used_formatted: str
    storage_limit_mb: int
    usage_percentage: float
    status: str  # Normal, Warning, Critical


class SuperAdminStorageResponse(BaseModel):
    total_storage_used_bytes: int
    total_storage_used_formatted: str
    remaining_capacity_gb: float
    disk_total_gb: float
    disk_usage_percent: float
    categories: List[SuperAdminStorageCategoryItem]
    tenants_storage: List[SuperAdminTenantStorageItem]


# ---------------------------------------------------------------------------
# Subscription Plan Schemas
# ---------------------------------------------------------------------------
class SuperAdminPlanItem(BaseModel):
    id: str
    name: str
    slug: str
    description: str
    price_cents: int
    annual_price_cents: int
    billing_interval: str
    max_agents: int
    max_concurrent_calls: int
    max_leads: int
    max_minutes_monthly: int
    max_storage_mb: int
    features: Dict[str, Any]
    is_active: bool
    created_at: str
    tenant_count: int


class SuperAdminPlanCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    slug: Optional[str] = None
    description: Optional[str] = ""
    price_cents: int = Field(ge=0)
    annual_price_cents: Optional[int] = 0
    billing_interval: str = "monthly"
    max_agents: int = Field(ge=1, default=5)
    max_concurrent_calls: int = Field(ge=1, default=2)
    max_leads: int = Field(ge=10, default=1000)
    max_minutes_monthly: int = Field(ge=10, default=300)
    max_storage_mb: int = Field(ge=100, default=5000)
    features: Optional[Dict[str, Any]] = None
    is_active: bool = True


class SuperAdminPlanUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    price_cents: Optional[int] = None
    annual_price_cents: Optional[int] = None
    billing_interval: Optional[str] = None
    max_agents: Optional[int] = None
    max_concurrent_calls: Optional[int] = None
    max_leads: Optional[int] = None
    max_minutes_monthly: Optional[int] = None
    max_storage_mb: Optional[int] = None
    features: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None


# ---------------------------------------------------------------------------
# Platform-Wide Analytics Schemas
# ---------------------------------------------------------------------------
class SuperAdminAnalyticsResponse(BaseModel):
    time_range: str
    total_calls: int
    completed_calls: int
    failed_calls: int
    total_call_duration_seconds: int
    total_call_duration_minutes: float
    total_leads: int
    qualified_leads: int
    active_voice_agents: int
    total_tenants: int
    tenant_growth: List[Dict[str, Any]]
    subscription_distribution: List[Dict[str, Any]]
    storage_distribution: List[Dict[str, Any]]
    call_volume_trends: List[Dict[str, Any]]
