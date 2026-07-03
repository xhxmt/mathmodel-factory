try:
    from pydantic import BaseModel, field_validator
except ModuleNotFoundError:  # pragma: no cover - lightweight unit tests may run without pydantic installed
    class BaseModel:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

        def dict(self):
            return self.__dict__.copy()

    def field_validator(*args, **kwargs):
        def _wrap(fn):
            return fn

        return _wrap


class UserInfo(BaseModel):
    username: str
    role: str
    status: str = "active"
    display_name: str = ""


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str
    status: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""


class UserResponse(BaseModel):
    username: str
    role: str
    status: str
    display_name: str = ""
    created_at: int = 0
    approved_at: int | None = None
    approved_by: str | None = None
    rejected_at: int | None = None
    rejected_by: str | None = None
    rejection_reason: str | None = None


class UserDecisionRequest(BaseModel):
    reason: str = ""


class SecretBindingStatus(BaseModel):
    env: str
    secret: str
    loaded: bool = False
    accessible: bool = False
    error: str | None = None


class LocalEnvFileStatus(BaseModel):
    path: str
    exists: bool = False
    mode: str | None = None
    secure_mode: bool = False
    sensitive_keys: list[str] = []


class OpsSecretsStatus(BaseModel):
    project_id: str = ""
    gcloud_path: str = ""
    loader: str = ""
    secrets: list[SecretBindingStatus] = []
    local_config: list[LocalEnvFileStatus] = []


class AuditLogResponse(BaseModel):
    id: int
    actor: str
    action: str
    target_type: str
    target_id: str
    created_at: int
    metadata: dict = {}


class ProjectAction(BaseModel):
    action: str


class WsTicketResponse(BaseModel):
    ticket: str


class NewProjectRequest(BaseModel):
    base_name: str
    problem_path: str
    no_start: bool = False
    consult: bool = False

    @field_validator("base_name")
    @classmethod
    def _check_base_name(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("base_name 仅允许字母、数字、下划线、连字符")
        return value


class ProjectRequestCreate(BaseModel):
    base_name: str
    problem_path: str
    no_start: bool = False
    consult: bool = False

    @field_validator("base_name")
    @classmethod
    def _check_base_name(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("base_name 仅允许字母、数字、下划线、连字符")
        return value


class ProjectRequestDecision(BaseModel):
    note: str = ""


class ProjectRequestResponse(BaseModel):
    id: int
    requester: str
    base_name: str
    problem_path: str
    no_start: bool = False
    consult: bool = False
    status: str
    created_at: int
    decided_at: int | None = None
    decided_by: str | None = None
    decision_note: str | None = None
    launched_at: int | None = None
    launched_base_name: str | None = None
    launch_output: str | None = None
    failure_reason: str | None = None


class ProjectStatus(BaseModel):
    base_name: str
    status: str
    display_status: str = ""
    current_step: int
    total_steps: int = 16
    progress_percent: float
    last_updated: str
    is_running: bool
    pid: int | None = None
    consultation_pending: bool = False
    consultation_gate: str | None = None
    selection_pending: bool = False
    selection_gate: str | None = None
    selection_deadline: int | None = None
    reason_code: str = ""
    reason_summary: str = ""
    suggested_actions: list[str] = []
    evidence: list[dict] = []
    diagnostic_reason_code: str | None = None
    diagnostic_badge: str | None = None
    diagnostic_priority: int = 999


class ConsultationRequest(BaseModel):
    gate: str
    step: int
    title: str
    content: str
    project: str
    created: str
    background: str | None = None
    impact: str | None = None
    key_files: list[str] | None = None
    suggestions: str | None = None


class ConsultationAnswer(BaseModel):
    answer: str


class ModelingDirectionSelection(BaseModel):
    direction_id: str


class SelectionDecisionRequest(BaseModel):
    gate: str = "step3"
    selected_option_id: str
    selected_aux_id: str = ""
    reason: str = ""


class ModelEntry(BaseModel):
    id: str
    label: str
    backend: str
    model: str = ""
    effort: str = ""
    base_url: str = ""
    key_env: str = ""
    enabled: bool = True
    builtin: bool = False


class ModelRegistryPayload(BaseModel):
    models: list[ModelEntry]


class StepAssignment(BaseModel):
    primary: str = ""
    fallback: str = ""


class ModelConfigPayload(BaseModel):
    scope: str
    steps: dict[str, StepAssignment]
