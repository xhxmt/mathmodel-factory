from pydantic import BaseModel, field_validator


class UserInfo(BaseModel):
    username: str
    role: str


class ProjectAction(BaseModel):
    action: str


class WsTicketResponse(BaseModel):
    ticket: str
