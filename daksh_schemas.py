"""Bounded public Daksh request contracts. Internal filesystem paths are never accepted."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

class StrictModel(BaseModel):
    model_config=ConfigDict(extra='forbid')

class Preflight(StrictModel):
    source: str=Field(default='',max_length=160)
    initial_model: str=Field(default='best.pt',max_length=160)
    device: str=Field(default='cpu:1',pattern=r'^(mps|cpu:[1-9][0-9]*|gpu:([0-9]|all))$')
    epochs: int=Field(default=20,ge=10,le=50,strict=True)
    batch_size: int | None=Field(default=None,ge=1,le=10,strict=True)
    image_size: Literal[320,416,512,640,768]=640
    workers: int=Field(default=2,ge=0,le=4,strict=True)
    precision: Literal['fp32','amp']='fp32'
    resource_mode: bool=Field(default=True,strict=True)
    checkpoint_id: str | None=Field(default=None,max_length=160)

class JobRequest(StrictModel):
    kind: Literal['calibration','retraining']
    configuration: dict[str,Any]=Field(default_factory=dict)
    idempotency_key: str=Field(min_length=8,max_length=128)

class ScheduleRequest(StrictModel):
    kind: Literal['calibration','retraining']
    enabled: bool=Field(default=False,strict=True)
    frequency: Literal['daily','weekly']='daily'
    hour: int=Field(default=8,ge=0,le=23,strict=True)
    minute: int=Field(default=0,ge=0,le=59,strict=True)
    weekday: int=Field(default=0,ge=0,le=6,strict=True)
    timezone: str=Field(default='UTC',max_length=100)
    defer_seconds: int=Field(default=7200,ge=0,le=7200,strict=True)
    payload: dict[str,Any]=Field(default_factory=dict)
