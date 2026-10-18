"""Small response models for the health, readiness, and model-list routes.

The analysis route returns the existing incident document and does not redefine it.
"""

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    error: str
    detail: str


class HealthBody(BaseModel):
    status: str


class ReadyBody(BaseModel):
    status: str
    model: str
    reason: str


class ModelsBody(BaseModel):
    models: list[str] = Field(default_factory=list)
